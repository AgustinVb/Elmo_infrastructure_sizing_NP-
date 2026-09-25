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
    parser.add_argument(
        '--relax_operational',
        action='store_true',
        help='[monolithic] relaja SOLO las binarias operacionales (Y, Z, Z_charge, '
             'Start*/End*: ~460k en la instancia de 10 años) dejando enteras las de '
             'inversion (X, N_chargers, n_ssee_k, R: unas decenas). El modelo sigue '
             'siendo MILP y su optimo es una COTA INFERIOR valida del monolitico '
             'completo, porque relajar integralidad solo agranda el factible. Mide '
             'cuanto del gap se explica por la integralidad de las INVERSIONES y no por '
             'la operacion. Combinalo con --mccormick_degradation para que sea MILP puro '
             '(McCormick tambien es relajacion, asi que la cota sigue siendo valida). '
             'Distinto de --relax_integrality, que relaja todo y da un LP.'
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
        '--free_charging',
        action='store_true',
        help='[monolithic|decomposed|hybrid] carga liberada: el LHD puede cargar en cualquier '
             'intervalo salvo maintenance DET (donde la maquina esta en '
             'servicio). Por defecto rige el regimen restringido, que solo '
             'permite cargar en meal/road_clearing/between_shifts DET -- 144 '
             'de los 480 min de cada turno (30%%); con el flag pasan a ser 408 '
             '(85%%). NO cambia el esquema de detenciones: det_stop_all sigue '
             'impidiendo OPERAR durante maintenance y road_clearing. Ver '
             'ConstraintRules.no_charge_maintenance_det en functions.py.'
    )
    parser.add_argument(
        '--free_maintenance',
        action='store_true',
        help='[monolithic|decomposed|hybrid] mantenimiento liberado: las ventanas de maintenance '
             'DET (24+48 min por turno) dejan de ser detenciones y el LHD puede '
             'OPERAR y CARGAR en ellas, como en cualquier intervalo libre. Por '
             'defecto maintenance esta en det_stop (impide operar) y fuera de '
             'toda regla de carga, lo que lo deja detenido puro. Sube los '
             'intervalos operables de 97 a 126 por dia. Combinable con '
             '--free_charging: juntos no dejan ninguna ventana con carga '
             'prohibida. Ver OptSets.build_sets en functions.py.'
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
        '--strengthen',
        action='store_true',
        help='[decomposed|hybrid] Strengthened Benders (documento sec. 6.2, opcion 2): '
             'calcula los multiplicadores del corte por ascenso de subgradiente sobre la '
             'relajacion Lagrangeana del MILP del bloque, en vez de tomar los duales del LP '
             'relajado. Por dualidad debil nunca debilita el corte, y puede ajustarlo. '
             'ORTOGONAL a --degradation_cut_mode (aquel trata la fisica bilineal de la '
             'degradacion; este aprieta el corte sobre N_chargers/G/H/D). Por defecto '
             'genera el Strengthened Benders cut de Lara et al. (2018) ec. (63): UNA '
             'resolucion extra del MILP del bloque por año y por iteracion del backward. '
             'Ver --strengthen_max_iter para pasar al corte Lagrangeano ec. (62). Util '
             'cuando la relajacion lineal NO es apretada, que es cuando el corte de '
             'Benders estandar queda flojo.'
    )
    parser.add_argument(
        '--export_sol', default=None,
        help='[decomposed|hybrid] al terminar, escribe la mejor solucion en formato '
             '.sol de SCIP en esta ruta, para usarla como incumbente en GCG '
             '(Fase 5.1 de plan_gcg.md). Requiere --varmap. Los valores salen del '
             'monolitico que arma build_report_model, asi que los nombres coinciden '
             'con el MPS por construccion.'
    )
    parser.add_argument(
        '--varmap', default=None,
        help='[con --export_sol] varmap.json escrito por export_mps.py, que traduce '
             'nombres de Pyomo a etiquetas del MPS. Debe venir de un MPS exportado '
             'con los MISMOS flags de regimen y el mismo horizonte.'
    )
    parser.add_argument(
        '--force_hybrid_mono',
        action='store_true',
        help='[hybrid] corre la fase monolitica AUNQUE la descomposicion ya haya '
             'certificado gap <= --gap_tol. Por defecto se omite: en ese caso el '
             'monolitico no puede aportar cota (la suya es mucho peor que la de '
             'relajacion operacional, que no conoce) y solo podria mejorar el '
             'incumbente marginalmente, gastando el timelimit entero.'
    )
    parser.add_argument(
        '--no_mono_lb_cut',
        action='store_true',
        help='[hybrid] NO agrega al monolitico la fila obj >= LB con la cota externa '
             'de la descomposicion. Por defecto se agrega: toda solucion factible '
             'cumple obj >= optimo >= LB, asi que la fila no corta nada, y sin ella '
             'Gurobi arranca desde SU cota (la del LP completo, mucho peor), reporta '
             'un gap enorme y su MIPGap nunca se cumple: las unicas salidas son '
             'BestObjStop --inalcanzable si el optimo esta por encima del UB objetivo-- '
             'o el timelimit.'
    )
    parser.add_argument(
        '--operational_bound',
        action='store_true',
        help='[decomposed|hybrid] antes de iterar, calcula una cota inferior resolviendo '
             'el monolitico con las binarias OPERACIONALES relajadas y las de INVERSION '
             'enteras, y la toma como LB inicial. Es MUCHO mejor que la del LP: medido en '
             'P_red_gen_bat a 10 anios, 2.146.169 contra 1.593.859, o sea el gap del '
             'incumbente baja de 26,64%% a 1,22%%, en 34,5 min. El backward con cortes de '
             'Benders no puede superar la del LP por construccion, asi que esta es la '
             'unica forma barata de tener una cota decente.'
    )
    parser.add_argument(
        '--op_bound_timelimit', type=int, default=None,
        help='[con --operational_bound] timelimit en segundos de ese solve. Default 3600, '
             'PROPIO: no hereda --solve_timelimit, que es el tope por bloque anual y no '
             'tiene relacion con un solve monolitico. Regla para dimensionarlo: cada nodo '
             'cuesta un LP del modelo completo y el arbol crece con las enteras de '
             'inversion, que son pocas; en la instancia medida el solve tardo ~40x el LP '
             'de raiz. Si corta por tiempo la cota dual igual es valida, solo mas floja.'
    )
    parser.add_argument(
        '--accelerate',
        action='store_true',
        help='[decomposed|hybrid] tecnica de aceleracion de Lara et al. (2018) sec. 5.3: '
             'antes de la primera iteracion resuelve un modelo AGREGADO (el monolitico '
             'con las binarias operacionales Y/Z/Z_charge/Start*/End* relajadas a '
             'continuas, dejando enteras las de inversion) y pre-genera cortes en los '
             'estados que ese modelo elige. Sin esto, el año 1 arranca creyendo que el '
             'futuro es gratis y las primeras iteraciones se gastan corrigiendolo. Los '
             'cortes los produce el backward REAL, asi que son validos por construccion.'
    )
    parser.add_argument(
        '--accel_pool_size', type=int, default=5,
        help='[decomposed|hybrid, solo con --accelerate] cuantas soluciones del modelo '
             'agregado usar como puntos para pre-generar cortes, via el solution pool de '
             'Gurobi (PoolSearchMode=2, las n mejores). 1 = solo la optima. Mas puntos '
             'representan mejor el modelo original pero cuestan un pre-Backward completo '
             '(|años| resoluciones) cada uno.'
    )
    parser.add_argument(
        '--accel_timelimit', type=int, default=None,
        help='[decomposed|hybrid, solo con --accelerate] timelimit en segundos del solve '
             'del modelo agregado. Por defecto usa el de --solve_timelimit.'
    )
    parser.add_argument(
        '--strengthen_max_iter', type=int, default=1,
        help='[decomposed|hybrid, solo con --strengthen] iteraciones del subgradiente, '
             'que deciden QUE corte de Lara et al. (2018) sec. 5.2.2 se genera. '
             '1 (default): Strengthened Benders cut, ec. (63) -- UNA relajacion '
             'Lagrangeana resuelta en los duales del LP, sin mejorar multiplicadores; '
             'cuesta una resolucion extra del MILP del bloque por año y por iteracion '
             'del backward. Mayor que 1: corte Lagrangeano, ec. (62), aproximado por '
             'subgradiente truncado -- mas ajustado y proporcionalmente mas caro.'
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
            free_charging=args.free_charging,
            free_maintenance=args.free_maintenance,
            relax_operational=args.relax_operational,
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
            strengthen=args.strengthen,
            strengthen_max_iter=args.strengthen_max_iter,
            accelerate=args.accelerate,
            accel_pool_size=args.accel_pool_size,
            accel_timelimit=args.accel_timelimit,
            operational_bound=args.operational_bound,
            op_bound_timelimit=args.op_bound_timelimit,
            history_path=os.path.join(output_folder, 'benders_history.json'),
            block_build_jobs=args.block_build_jobs,
            macroblock_forward=args.macroblock_forward,
            monolithic_lp_bound=not args.no_monolithic_lp_bound,
            capacity_presolve=args.capacity_presolve,
            free_charging=args.free_charging,
            free_maintenance=args.free_maintenance,
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
                gap_dec = result.get('gap')
                lb_dec = result.get('lb')

                # Si la descomposicion YA certifico la tolerancia, entrar al
                # monolitico es trabajo perdido: su cota propia es mucho peor
                # que la que ya se tiene (la de relajacion operacional o la del
                # LP), no la puede usar, y va a reportar un gap enorme contra su
                # propia cota mientras gasta el timelimit entero cerrando algo
                # que ya esta cerrado.
                saltear = (
                    gap_dec is not None and gap_dec <= args.gap_tol
                    and not args.force_hybrid_mono
                )
                if saltear:
                    print(f"[Hibrido] la descomposicion ya certifico gap="
                          f"{gap_dec:.4%} <= {args.gap_tol:.4%}: se OMITE la fase "
                          f"monolitica (no aportaria cota y su cota propia es "
                          f"peor). Usa --force_hybrid_mono para correrla igual.")
                if not saltear:
                    print("[Hibrido] resolviendo el monolitico con la solucion "
                          "descompuesta como MIP start...")
                    om_report.mip_focus = args.mip_focus
                    # Corta en cuanto el incumbente alcance el gap objetivo
                    # MEDIDO CONTRA LA COTA EXTERNA. Gurobi mide contra la suya,
                    # que es peor, asi que sin esto no sabe cuando parar.
                    if lb_dec not in (None, float('-inf')) and args.gap_tol < 1:
                        om_report.best_obj_stop = lb_dec / (1.0 - args.gap_tol)
                        print(f"[Hibrido] BestObjStop = "
                              f"{om_report.best_obj_stop:,.2f} (UB que cierra "
                              f"{args.gap_tol:.2%} contra LB={lb_dec:,.2f})")

                    # La cota externa, ADEMAS, como fila del modelo. BestObjStop
                    # solo dice cuando parar: no entra en la cota de Gurobi, que
                    # sigue partiendo de su propio LP de raiz (medido aqui:
                    # 1.693.372 contra LB=2.152.170, o sea 22,6% de gap
                    # reportado sobre un incumbente que en realidad esta a 1,6%).
                    # Con la fila, en cambio:
                    #   - es VALIDA: LB es una cota inferior del optimo y toda
                    #     solucion factible cumple obj >= optimo >= LB, asi que
                    #     no corta ninguna;
                    #   - todo LP de nodo queda con valor >= LB, asi que el
                    #     BestBd que Gurobi reporta y usa para su MIPGap pasa a
                    #     ser la cota REAL y el criterio de parada por gap
                    #     vuelve a tener sentido;
                    #   - la fila entra en presolve y en el fijado por costo
                    #     reducido con un rango objetivo de ~35k en vez de
                    #     ~470k.
                    # El margen 1e-6 relativo protege de que la cota dual venga
                    # al filo de las tolerancias de Gurobi. Si el monolitico
                    # diera INFACTIBLE con la fila puesta, la cota no era
                    # valida: correr con --no_mono_lb_cut para confirmarlo.
                    if (lb_dec not in (None, float('-inf'))
                            and not args.no_mono_lb_cut):
                        import pyomo.environ as _pyo
                        lb_cut = lb_dec - abs(lb_dec) * 1e-6
                        om_report.model.lb_externa = _pyo.Constraint(
                            expr=om_report.model.obj.expr >= lb_cut)
                        print(f"[Hibrido] fila obj >= {lb_cut:,.2f} agregada al "
                              f"monolitico (cota externa; sin ella Gurobi parte "
                              f"de su LP de raiz)")
                    om_report.solve_model(gap, solver_name,
                                          timelimit=args.mono_timelimit or args.solve_timelimit)
                mejora = result['ub'] - om_report.opt_cost_result
                if not saltear:
                    print(f"[Hibrido] monolitico resuelto en "
                      f"{time.time() - t_hib:.0f}s: "
                      f"costo = {om_report.opt_cost_result:,.2f} "
                      f"(la descomposicion habia llegado a {result['ub']:,.2f}; "
                      f"mejora de {mejora:,.2f} = {mejora / result['ub']:.2%})")

                # Gap final contra la MEJOR cota disponible. Sin esto no lo
                # calcula nadie: el UB lo mejora la fase monolitica y el LB vive
                # en el resultado de la descomposicion, que se imprimio antes.
                # result['lb'] ya es el max de la cota del LP monolitico y la de
                # relajacion operacional (--operational_bound), asi que es la
                # mejor cota que tiene la corrida.
                ub_final = om_report.opt_cost_result
                lb_final = result['lb']
                resumen = {
                    'ub_descomposicion': result['ub'],
                    'ub_final': ub_final,
                    'lb_final': lb_final,
                    'lb_monolithic_lp': result.get('lb_monolithic_lp'),
                    'lb_operational': result.get('lb_operational'),
                    'iteraciones': result.get('iterations'),
                    'tiempo_descomposicion_sec': result.get('total_time_sec'),
                    'tiempo_monolitico_sec': time.time() - t_hib,
                }
                if lb_final not in (None, float('-inf')) and ub_final:
                    gap_final = (ub_final - lb_final) / abs(ub_final)
                    resumen['gap_final'] = gap_final
                    cual = 'relajacion operacional' if (
                        result.get('lb_operational') is not None
                        and abs(result['lb_operational'] - lb_final) < 1e-6
                    ) else 'LP monolitico / backward'
                    print(f"[Hibrido] GAP FINAL = {gap_final:.4%}  "
                          f"(UB={ub_final:,.2f}  LB={lb_final:,.2f}, de {cual})")
                else:
                    print("[Hibrido] sin cota inferior utilizable: no se reporta gap final.")
                try:
                    os.makedirs(output_folder, exist_ok=True)
                    with open(os.path.join(output_folder, 'resumen_hibrido.json'),
                              'w', encoding='utf-8') as f:
                        json.dump(resumen, f, indent=2, default=str)
                except Exception as exc:
                    print(f"WARN no se pudo escribir resumen_hibrido.json: {exc}")

            if args.export_sol:
                if not args.varmap:
                    print('WARN --export_sol requiere --varmap: se omite la exportacion.')
                else:
                    from sol_io import write_scip_sol
                    costo = getattr(om_report, 'opt_cost_result', None)
                    if costo is None:
                        costo = result['ub']
                    esc, sin_val, fuera = write_scip_sol(
                        om_report.model, args.varmap, args.export_sol, costo)
                    print(f"[export_sol] {args.export_sol}: {esc:,} variables no nulas "
                          f"(objetivo {costo:,.2f})")
                    if sin_val:
                        # Una variable sin valor se interpreta como cero al leer el
                        # .sol, y eso puede volver infactible la solucion. Conviene
                        # saberlo ANTES de que checkSol lo reporte.
                        print(f"[export_sol] OJO: {sin_val:,} variables sin valor "
                              f"(se omiten, SCIP las tomara como cero)")
                    if fuera:
                        print(f"[export_sol] OJO: {fuera:,} variables no estan en el "
                              f"varmap -- .revisa que venga del mismo horizonte y flags")

            printer = Printer(om_report, output_folder, time_series, mine_system)
            printer.create_all_plots()


if __name__ == '__main__':
    main()
