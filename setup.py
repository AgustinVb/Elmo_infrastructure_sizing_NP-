from src import mine
from src.io.reader import Setting, Reader, Series
from src.io.printer import Printer
from src.time_series import timeseries
from src.optimization import OptimizationModel
from src.optimization.decomposition.driver import NestedBendersSolver
import argparse, pprint
import json
import os
import sys
import time
import pandas as pd
from os.path import join
import xlrd
xlrd.xlsx.ensure_elementtree_imported(False, None)
xlrd.xlsx.Element_has_iter = True

# En Windows la consola suele quedar en cp1252, que no puede codificar los
# emojis usados en los prints de progreso (✅/⚠️/etc.) a lo largo del
# pipeline (opt_model.py, printer.py, ...) -- eso lanzaba UnicodeEncodeError
# DESPUES de que Gurobi ya hubiera resuelto el modelo, abortando antes de
# escribir cualquier resultado a disco.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def resolve_wp2_json_path(args):
    json_path = args.wp2_consumption_json or 'electric_routes_within_time.json'
    if not os.path.isabs(json_path):
        json_path = os.path.join(args.data_folder, json_path)
    return json_path


# --- Dias representativos -------------------------------------------------
# La eleccion NO es libre: get_alpha_g busca el perfil renovable con la clave
# ((day - 1) % 365) + 1 contra la columna 'day' de GenProfiles, asi que solo
# son utilizables los dias-del-anio que esa hoja trae con day <= 365. Hoy son
# exactamente {15, 105, 196, 288} (las filas con day > 365 existen pero son
# inalcanzables para el lookup, y ademas son copias exactas de las del anio 1:
# el perfil es ciclico).
#
# OJO: get_alpha_g devuelve 0.0 cuando no encuentra el dia, SIN AVISAR. Una
# lista anterior usaba los dias-del-anio 1 y 91, que no tienen perfil: la
# generacion y el almacenamiento quedaban desactivados en silencio -- el modelo
# invertia en solar/eolica, pagaba inversion y operacion, y recibia cero
# energia. Si se agrega un dia hay que poblarlo antes en GenProfiles.
#
# Cobro de potencia: power_cost_peak_limit solo ata P_pot entre los dias 91 y
# 244 (abril-septiembre, meses de punta) sobre el dia-del-anio, que es lo que
# guarda model.days. Con 2 dias cae 1 de 2 (el 196); con 4 caen 2 de 4 (105 y
# 196). La proporcion se mantiene, asi que las dos configuraciones siguen
# siendo comparables en ese termino.
DIAS_POR_ANIO = {
    2: [15, 196],            # verano sin cobro + invierno con cobro
    4: [15, 105, 196, 288],  # los cuatro que GenProfiles tiene poblados
}
ANIOS_HORIZONTE = 14  # plan minero completo (ExtractionGoal/FleetByYear traen 14)

def build_mine(args):
    """ building power system base function

    :param args: a argument based list, you can access to any
    attribute using args.property.

    :return:
    """

    # os.path.join y no `+`: concatenando a mano, un --data_folder sin barra
    # final daba "…/960kW_2diaselmo_data.xlsx" y fallaba con un Not Found que
    # no apunta a la causa. Asi acepta las dos formas, igual que la rama de
    # battery swapping.
    model = Reader(os.path.join(args.data_folder, args.model), start_in=1)
    series = Series(os.path.join(args.data_folder, args.series))
    doys = DIAS_POR_ANIO[getattr(args, 'days_per_year', 4)]
    days = [(y - 1) * 365 + doy
            for y in range(1, ANIOS_HORIZONTE + 1)
            for doy in doys]
    n_years = getattr(args, 'n_years', None)
    if n_years is not None:
        days = days[:n_years * len(doys)]
    time_series = timeseries.Timeseries(series, days, 8/60)
    mine_system = mine.Mine(model)
    if getattr(args, 'consumption_model', 'wp1') == 'wp2':
        wp2_json_path = resolve_wp2_json_path(args)
        time_series.mapper['Trips'] = time_series.get_trips(
            mine_system, consumption_model='wp2', wp2_consumption_json=wp2_json_path
        )
    else:
        time_series.mapper['Trips'] = time_series.get_trips(mine_system)
    return series, mine_system, time_series


def main():
    """ Main function building argument collection from setting
    default values.

    :return:
    """
    system_title = 'ELMOMINE'
    parser = argparse.ArgumentParser(description=system_title)
    parser.add_argument('--data_folder', default='data/')
    parser.add_argument('--model', default='elmo_data.xlsx')
    parser.add_argument('--series', default='time_series.xlsx')
    parser.add_argument('--output_folder', default='output/')
    parser.add_argument('--solver', default='glpk')
    parser.add_argument('--y_init_path', default=None, help='Ruta opcional a Y.json para warm start puntual')
    parser.add_argument(
        '--n_years', type=int, default=None,
        help='Trunca el horizonte a los primeros N años (tomando los primeros '
             'N grupos de dias representativos, ver DIAS_POR_ANIO en setup.py) '
             'en vez de los 14 años completos. Util para reproducir/depurar un '
             'problema con una corrida chica antes de escalar al horizonte '
             'completo.'
    )
    parser.add_argument(
        '--days_per_year', type=int, choices=[2, 4], default=4,
        help='Dias representativos por anio. 4 (default): dias-del-anio 15, 105, '
             '196 y 288. 2: solo 15 y 196 (config historica). Solo esos cuatro '
             'tienen perfil renovable cargado en GenProfiles; cualquier otro deja '
             'alpha_g = 0 en silencio. Pasar a 4 duplica el tamanio del modelo y '
             'baja el factor de escalado operacional de 182,5 a 91,25.'
    )
    parser.add_argument(
        '--init_solution_folder', '--warm_start_folder',
        dest='init_solution_folder',
        default=None,
        help='Carpeta opcional con JSONs de variables (<VarName>.json) para warm start completo'
    )
    parser.add_argument(
        '--relax_integrality',
        action='store_true',
        help='Resuelve la relajación lineal del modelo sin cambiar las variables originales'
    )
    parser.add_argument(
        '--mccormick_degradation',
        action='store_true',
        help='[monolithic, solo si hay hoja BatteryDegradation] linealiza el bilineal '
             'N_ciclos*b_bar con la envolvente de McCormick (Camino A, ver '
             'degradacion_descomposicion_mccormick.md) en vez de resolverlo como '
             'restriccion cuadratica no convexa (Gurobi NonConvex=2). El modelo queda '
             'MILP puro -- suele ser mucho mas rapido, a costa de una aproximacion.'
    )
    parser.add_argument('--consumption_model', choices=['wp1', 'wp2'], default='wp1',
                         help='wp1: calculo fisico interno. wp2: consumos y tiempos precalculados desde un JSON por nodo.')
    parser.add_argument('--wp2_consumption_json', default=None,
                         help='Ruta al JSON de consumos WP2 (relativa a data_folder salvo que sea absoluta). '
                              "Si se omite y --consumption_model wp2, se usa 'electric_routes_within_time.json'.")
    parser.add_argument(
        '--autonomous_mode',
        action='store_true',
        help='Escenario DET de vehiculos autonomos: durante la colacion el LHD '
             'puede ademas operar (viajar/extraer), no solo cargar o estar '
             'detenido. El cambio de turno (between_shifts) sigue restringido '
             'a cargar-o-detenido en ambos modos. Solo tiene efecto si el '
             'esquema DET esta activo en build_all_constraints (ver functions.py).'
    )
    parser.add_argument(
        '--mode', choices=['monolithic', 'decomposed', 'hybrid'], default='monolithic',
        help='monolithic (default): resuelve el modelo completo de una vez. '
             'decomposed: descomposicion Nested Benders por año (ver '
             'implementacion_descomposicion_carga_ob.md). hybrid: corre la '
             'descomposicion y le entrega su solucion al monolitico como MIP '
             'start. Los dos metodos tienen perfiles opuestos -- la '
             'descomposicion consigue un incumbente enseguida pero su cota '
             'inferior se estanca, y el branch and bound sube la cota rapido '
             'pero le cuesta el incumbente --, asi que el hibrido le da a cada '
             'uno lo que al otro le falta. Renuncia a la ventaja de memoria: '
             'construye el monolitico completo.'
    )
    parser.add_argument(
        '--no_monolithic_lp_bound', action='store_true',
        help='[--mode decomposed|hybrid] no calcular la relajacion lineal del '
             'monolitico como cota inferior inicial. Por defecto SI se calcula: '
             'suele ser una cota mucho mejor que la del backward y cuesta un solo '
             'LP, pero obliga a construir el monolitico completo, que es el gasto '
             'de memoria que la descomposicion evita.'
    )
    parser.add_argument(
        '--mono_timelimit', type=int, default=None,
        help='Timelimit en segundos del solve monolitico: en --mode monolithic '
             '(por defecto 172800 s, 48 h) y en la fase monolitica de --mode hybrid '
             '(por defecto el valor de --solve_timelimit). Sirve para el benchmark '
             'a presupuesto igual entre monolitico solo e hibrido.'
    )
    parser.add_argument(
        '--mip_focus', type=int, choices=[0, 1, 2, 3], default=3,
        help='Gurobi MIPFocus del solve monolitico (monolithic e hybrid). 3 (default, '
             'el historico) prioriza la cota; 1 prioriza encontrar incumbentes, que '
             'es lo que le falta al hibrido cuando llega con un MIP start bueno y no '
             'lo mejora.'
    )
    parser.add_argument(
        '--capacity_presolve', choices=['peak', 'all', 'off'], default='peak',
        help='[decomposed|hybrid] presolve de capacidad de subestacion (ver '
             'NestedBendersSolver._capacity_presolve): antes de iterar resuelve, '
             'por nave, el minimo n_ssee_k que hace factible a un anio con todo el '
             'estado heredado libre, y lo impone como cota inferior en el anio 1 y '
             'en el LP monolitico. Es una desigualdad valida (no invalida UB ni LB) '
             'y evita que la capacidad del pico de produccion llegue al anio 1 via '
             'cortes de factibilidad, cada uno de los cuales reinicia el forward. '
             'peak (default): solo el anio de mayor meta de produccion. all: todos '
             'los anios, cota mas fuerte pero |naves|*|anios| MILP. off: sin presolve.'
    )
    parser.add_argument(
        '--max_iter', type=int, default=20,
        help='[decomposed] maximo de iteraciones forward/backward.'
    )
    parser.add_argument(
        '--gap_tol', type=float, default=0.01,
        help='[decomposed] tolerancia de gap (UB-LB)/UB para detener.'
    )
    parser.add_argument(
        '--solve_timelimit', type=int, default=600,
        help='[decomposed] timelimit en segundos (Gurobi TimeLimit) para CADA '
             'resolucion individual (forward MILP o backward LP de un año). '
             'Default 600 (10 min); antes estaba fijo en 900 (15 min).'
    )
    parser.add_argument(
        '--threads', type=int, default=None,
        help='[decomposed, solo con --solver gurobi] cota de threads (Gurobi '
             'Threads) para CADA resolucion individual. Por defecto (sin '
             'pasar el flag), Gurobi decide solo (tipicamente usa todos los '
             'cores fisicos) -- en una maquina compartida con otros '
             'usuarios activos puede convenir acotarlo.'
    )
    parser.add_argument(
        '--degradation_cut_mode', choices=['mccormick', 'lagrangean'], default='mccormick',
        help='[decomposed, solo si hay hoja BatteryDegradation] camino usado para el '
             'corte del año con degradacion de bateria (ver '
             'degradacion_descomposicion_mccormick.md): mccormick (Camino A, default, '
             'barato) o lagrangean (Camino B, fisica bilineal exacta via subgradiente, '
             'mas caro).'
    )
    parser.add_argument(
        '--block_build_jobs', type=int, default=None,
        help='[decomposed] procesos en paralelo para construir los bloques '
             'anuales antes de arrancar el loop forward/backward (ver '
             'NestedBendersSolver._build_blocks). Por defecto: min(anios, '
             'cpus disponibles). Usar 1 para forzar construccion secuencial '
             '(comportamiento anterior, util para debug).'
    )
    parser.add_argument(
        '--macroblock_forward', action='store_true',
        help='[decomposed] resuelve cada año POSTERIOR al primero como varios '
             'MILP de macrobloque (una nave de carga con sus equipos y sus '
             'puntos de extraccion cada uno) en vez de un solo MILP del año, '
             'repartiendo entre macrobloques la meta de produccion diaria, la '
             'potencia de red (p_peak/P_pot) y el aporte de generacion y '
             'almacenamiento (ver src/optimization/decomposition/macroblocks.py). '
             'Solo afecta la fase forward: el reparto restringe el problema, '
             'asi que la cota superior sigue siendo valida pero los cortes y la '
             'cota inferior se siguen generando sobre el año completo. Sin el '
             'flag, comportamiento identico al de siempre.'
    )
    parser.add_argument(
        '--fixed_stations_json', default=None,
        help='[decomposed, opcional] ruta a JSON {"<year>": {"<station>": 0/1, ...}, ...} '
             'con las estaciones X fijas por año -- en modo descompuesto X es exogeno '
             '(documento sec. 2.1), no se optimiza. Si se omite, se infiere de la '
             'asignacion LHD-estacion de los datos de entrada (StationAssignment): se '
             'construye toda estacion con al menos un LHD asignado, en todos los años.'
    )

    args = parser.parse_args()
    series, mine_system, time_series = build_mine(args)
    gap= 1/100;
    solver_name=args.solver
    output_folder=args.output_folder

    if args.mode == 'monolithic':
        opt = OptimizationModel(
            mine_system,
            time_series,
            gap,
            solver_name,
            output_folder,
            y_init_path=args.y_init_path,
            init_solution_folder=args.init_solution_folder,
            relax_integrality=args.relax_integrality,
            autonomous_mode=args.autonomous_mode,
            mccormick_degradation=args.mccormick_degradation,
            mip_focus=args.mip_focus,
            **({'timelimit': args.mono_timelimit} if args.mono_timelimit else {}),
        )
    else:
        if args.fixed_stations_json:
            with open(args.fixed_stations_json, 'r', encoding='utf-8') as f:
                raw_stations = json.load(f)
            exogenous_stations_by_year = {
                int(y): {k: int(v) for k, v in stations.items()}
                for y, stations in raw_stations.items()
            }
        else:
            # Por defecto, X se infiere de la asignacion LHD-estacion de los
            # datos de entrada (toda estacion con al menos un LHD asignado se
            # construye en todos los años) -- ver NestedBendersSolver /
            # infer_exogenous_stations. --fixed_stations_json queda para
            # cuando se quiera forzar un layout distinto.
            exogenous_stations_by_year = None

        solver_kwargs = {"solvername": solver_name, "gap": gap, "timelimit": args.solve_timelimit}
        if args.threads is not None:
            solver_kwargs["extra_options"] = {"Threads": args.threads}

        solver = NestedBendersSolver(
            mine_system, time_series, exogenous_stations_by_year,
            gap_tol=args.gap_tol, max_iter=args.max_iter,
            autonomous_mode=args.autonomous_mode,
            solver_kwargs=solver_kwargs,
            degradation_cut_mode=args.degradation_cut_mode,
            block_build_jobs=args.block_build_jobs,
            macroblock_forward=args.macroblock_forward,
            monolithic_lp_bound=not args.no_monolithic_lp_bound,
            capacity_presolve=args.capacity_presolve,
        )
        result = solver.solve()
        interrupted_tag = " (interrumpido con Ctrl+C)" if result.get("interrupted") else ""
        print(
            f"NestedBenders{interrupted_tag}: UB={result['ub']:.4f}  LB={result['lb']:.4f}  "
            f"gap={result['gap']:.4%}  iteraciones={result['iterations']}  "
            f"tiempo_total={result['total_time_sec']:.1f}s"
        )

        if result.get("best_solution") is None:
            print(
                "NestedBenders: no hay ninguna solucion factible que reportar "
                "(interrumpido u horizonte roto antes de completar la primera "
                "iteracion) -- se omite build_report_model/Printer."
            )
        else:
            om_report = solver.build_report_model(output_folder)

            if args.mode == 'hybrid':
                # La solucion descompuesta entra como MIP start
                # (build_report_model deja has_warm_start puesto). Gurobi
                # arranca con un incumbente que le habria costado encontrar
                # -- medido en battery_swapping_multianio a 6 anios, el
                # monolitico solo no encontro NINGUNO en 600 s -- y se dedica
                # a cerrar la cota, que es lo que hace bien.
                t_hib = time.time()
                print("[Hibrido] resolviendo el monolitico con la solucion "
                      "descompuesta como MIP start...")
                om_report.mip_focus = args.mip_focus
                om_report.solve_model(gap, solver_name,
                                      timelimit=args.mono_timelimit or args.solve_timelimit)
                mejora = result['ub'] - om_report.opt_cost_result
                print(f"[Hibrido] monolitico resuelto en "
                      f"{time.time() - t_hib:.0f}s: "
                      f"costo = {om_report.opt_cost_result:,.2f} "
                      f"(la descomposicion habia llegado a {result['ub']:,.2f}; "
                      f"mejora de {mejora:,.2f} = {mejora / result['ub']:.2%})")

            printer = Printer(om_report, output_folder, time_series, mine_system)
            printer.create_all_plots()


if __name__ == '__main__':
    main()
