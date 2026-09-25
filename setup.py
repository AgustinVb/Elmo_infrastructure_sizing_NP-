from src import mine
from src.io.reader import Setting, Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel
import argparse, pprint
import pandas as pd
from os.path import join
import os
import sys
import xlrd

# En Windows la consola suele quedar en cp1252, que no puede codificar los
# emojis usados en los prints de progreso (✅/⚠️/etc.) a lo largo del
# pipeline (opt_model.py, printer.py, ...) -- eso lanzaba UnicodeEncodeError
# DESPUES de que Gurobi ya hubiera resuelto el modelo (visto en
# limited_infeasible_log tras un monolitico sin incumbente). Mismo arreglo
# que carga_ob_multiaño.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
xlrd.xlsx.ensure_elementtree_imported(False, None)
xlrd.xlsx.Element_has_iter = True




def resolve_wp2_json_path(args):
    json_path = args.wp2_consumption_json or 'electric_routes_within_time.json'
    if not os.path.isabs(json_path):
        json_path = os.path.join(args.data_folder, json_path)
    return json_path


def build_mine(args):
    """ building power system base function

    :param args: a argument based list, you can access to any
    attribute using args.property.

    :return:
    """

    model = Reader(join(args.data_folder, args.model), start_in=1)
    series = Series(join(args.data_folder, args.series))
    # --- Dias representativos ---------------------------------------------
    # La eleccion NO es libre: get_alpha_g busca el perfil renovable con la
    # clave ((day - 1) % 365) + 1 contra la columna 'day' de GenProfiles, asi
    # que solo son utilizables los dias-del-anio que esa hoja trae con
    # day <= 365. Hoy son exactamente {15, 105, 196, 288} (las filas con
    # day > 365 existen pero son inalcanzables para el lookup).
    #
    # OJO: get_alpha_g devuelve 0.0 cuando no encuentra el dia, SIN AVISAR. Una
    # lista anterior usaba los dias-del-anio 1 y 91, que no tienen perfil: la
    # generacion y el almacenamiento quedaban desactivados en silencio -- el
    # modelo invertia en solar/eolica, pagaba inversion y operacion, y recibia
    # cero energia. Si se agrega un dia hay que poblarlo antes en GenProfiles.
    #
    # Cobro de potencia: power_peak_limit solo ata P_pot entre los dias 91 y
    # 244 (abril-septiembre, meses de punta). Con 2 dias cae 1 de 2 (el 196);
    # con 4 caen 2 de 4 (105 y 196). La proporcion se mantiene, asi que las dos
    # configuraciones siguen siendo comparables en ese termino.
    DIAS_POR_ANIO = {
        2: [15, 196],            # verano sin cobro + invierno con cobro
        4: [15, 105, 196, 288],  # los cuatro que GenProfiles tiene poblados
    }
    ANIOS_HORIZONTE = 14  # plan minero completo (ExtractionGoal/FleetByYear traen 14)

    doys = DIAS_POR_ANIO[getattr(args, 'days_per_year', 4)]
    days = [(y - 1) * 365 + doy
            for y in range(1, ANIOS_HORIZONTE + 1)
            for doy in doys]
    # --n_years recorta el horizonte a los primeros N anios, para validaciones
    # cortas sin editar nada a mano.
    n_years = getattr(args, 'n_years', None)
    if n_years is not None:
        days = days[:n_years * len(doys)]
    time_series = timeseries.Timeseries(series, days, 8/60)
    #time_series = timeseries.Timeseries(series, [1, 91], 8/60)
    mine_system = mine.Mine(model)
    if args.consumption_model == 'wp2':
        wp2_json_path = resolve_wp2_json_path(args)
        time_series.mapper['Trips'] = time_series.get_trips(
            mine_system, consumption_model='wp2', wp2_consumption_json=wp2_json_path
        )
    else:
        time_series.mapper['Trips'] = time_series.get_trips(mine_system)
    return series, mine_system, time_series


def run_decomposed(args, mine_system, time_series, hybrid=False):
    """Resuelve con Nested Benders y genera la misma salida de siempre.

    Printer consume un OptModel monolitico, asi que despues de iterar se arma
    uno de solo lectura con la mejor solucion cargada (build_report_model): el
    reporte no se entera de que el problema se resolvio por bloques.
    """
    import json
    import time

    from src.io.printer import Printer
    from src.optimization.decomposition import passes as passes_module
    from src.optimization.decomposition.driver import NestedBendersSolver
    from src.optimization.opt_model import stop_file_path

    # La carpeta de salida se creaba recien en build_report_model, o sea al
    # terminar TODA la descomposicion. Crearla aca sirve para dos cosas: el
    # archivo STOP tiene donde vivir desde el minuto cero, y un
    # `| Tee-Object -FilePath <carpeta>/run.log` deja de fallar con
    # DirectoryNotFoundException al arrancar.
    os.makedirs(args.output_folder, exist_ok=True)
    stop_path = stop_file_path(args.output_folder)
    if os.path.exists(stop_path):
        os.remove(stop_path)
        print(f"[STOP] habia un archivo de parada de una corrida anterior: se borro.")
    passes_module.set_stop_file(stop_path)
    print(f"[STOP] para cortar a mano conservando la solucion, cree: {stop_path}")

    exogenous = None
    if args.fixed_stations_json:
        with open(args.fixed_stations_json, encoding='utf-8') as fh:
            crudo = json.load(fh)
        exogenous = {int(y): {k: int(v) for k, v in por_nave.items()}
                     for y, por_nave in crudo.items()}

    # En --mode hybrid la cota del LP monolitico no se calcula. No es que aporte
    # poco: no aporta nada. La fase monolitica posterior construye el MISMO
    # modelo y su propio root bound la pasa por arriba enseguida -- medido en
    # DET/Gen_Bat/241kW: 1,807,131 en 2,251 s aca, contra 2,149,198 a los 245 s
    # del solve de Gurobi que igual se iba a correr. Encima obliga a construir
    # el monolitico dos veces, que es justo el gasto que la descomposicion evita.
    #
    # Y ni siquiera son cotas del mismo problema: este LP va con X exogeno (mas
    # las cotas del presolve de capacidad), mientras que build_report_model arma
    # el OptModel sin exogenous_stations, con X libre. El conjunto factible del
    # monolitico es mas grande, asi que su optimo puede ser menor y este numero
    # NO es cota inferior valida para el. En el mismo log: 1,807,131 con X fijo
    # contra 1,623,012 de root relaxation con X libre, y la diferencia es casi
    # exactamente la apertura de naves que el X fraccionario esquiva.
    #
    # En --mode decomposed, en cambio, es la unica cota que hay: el backward no
    # la supero en ninguna corrida.
    monolithic_lp_bound = not args.no_monolithic_lp_bound and not hybrid
    if hybrid and not args.no_monolithic_lp_bound:
        print("[Hibrido] se omite la cota del LP monolitico: la fase monolitica "
              "la supera con su propio root bound en una fraccion del tiempo.")

    solver = NestedBendersSolver(
        mine_system, time_series,
        exogenous_stations_by_year=exogenous,
        gap_tol=args.gap_tol,
        max_iter=args.max_iter,
        autonomous_mode=args.autonomous_mode,
        solver_kwargs={'solvername': args.solver, 'gap': args.gap_tol,
                       'timelimit': args.solve_timelimit},
        block_build_jobs=args.block_build_jobs,
        monolithic_lp_bound=monolithic_lp_bound,
        capacity_presolve=args.capacity_presolve,
        cut_type=args.cut_type,
        strengthened_timelimit=args.strengthened_timelimit,
        warm_start_cuts=args.warm_start_cuts,
    )
    resultado = solver.solve(verbose=True)

    print(f"[NestedBenders] termino en {resultado['iterations']} iteraciones "
          f"({resultado['total_time_sec']:.0f}s): UB={resultado['ub']:,.2f}  "
          f"LB={resultado['lb']:,.2f}  gap={resultado['gap']:.4%}"
          + ("  [INTERRUMPIDO]" if resultado['interrupted'] else ""))

    if resultado['best_full_solution'] is None:
        # Parada (STOP o Ctrl+C) antes de completar la primera pasada forward:
        # no existe ninguna trayectoria factible del horizonte, asi que no hay
        # nada que reportar. Se avisa y se sale en vez de reventar en
        # build_report_model.
        print("[STOP] Se corto antes de completar la primera iteracion: no hay "
              "solucion factible del horizonte que guardar. No se escribe salida.")
        return

    report, _informe = solver.build_report_model(args.output_folder)

    if hybrid:
        # La solucion descompuesta entra como MIP start (build_report_model deja
        # has_warm_start puesto). Gurobi arranca con un incumbente que le habria
        # costado encontrar y se dedica a cerrar la cota, que es lo que hace bien.
        t0 = time.time()
        print("[Hibrido] resolviendo el monolitico con la solucion descompuesta "
              "como MIP start...")
        report.mip_focus = args.mip_focus
        report.solve_model(args.gap_tol, args.solver,
                           timelimit=args.mono_timelimit or args.solve_timelimit)
        mejora = resultado['ub'] - report.opt_cost_result
        print(f"[Hibrido] monolitico resuelto en {time.time() - t0:.0f}s: "
              f"costo = {report.opt_cost_result:,.2f} "
              f"(la descomposicion habia llegado a {resultado['ub']:,.2f}; "
              f"mejora de {mejora:,.2f} = {mejora / resultado['ub']:.2%})")

    printer = Printer(report, args.output_folder, time_series, mine_system)
    printer.create_all_plots()


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
    parser.add_argument(
        '--days_per_year', type=int, choices=[2, 4], default=4,
        help='Dias representativos por anio. 4 (default): dias-del-anio 15, 105, '
             '196 y 288. 2: solo 15 y 196 (config historica de los escenarios '
             'P_red). Solo esos cuatro tienen perfil renovable '
             'cargado en GenProfiles; cualquier otro deja alpha_g = 0 en silencio. '
             'Pasar a 4 duplica el tamanio del modelo y baja el factor de escalado '
             'operacional de 182,5 a 91,25 dias por dia modelado.'
    )
    parser.add_argument(
        '--n_years', type=int, default=None,
        help='Recorta el horizonte a los primeros N años (2 dias representativos '
             'por año). Sin el flag se usa el horizonte completo de 11 años.'
    )
    parser.add_argument('--solver', default='glpk')
    parser.add_argument('--y_init_path', default=None,
                        help='Ruta opcional a Y.json para usar warm start en variable Y')
    parser.add_argument(
        '--init_solution_folder',
        default=None,
        help='Carpeta opcional con JSONs de variables (<VarName>.json) para warm start completo',
    )
    parser.add_argument(
        '--relax_operational', action='store_true',
        help='[--mode monolithic] relaja SOLO las variables de operacion (Y, Sv, Z, '
             'Z_swap, StartAssign, EndAssign, S, X_dch, X_ini, W) y deja enteras las '
             'de inversion (X, N_bays, N_chargers, N_batteries, n_ssee_k, R). El '
             'optimo es una cota inferior del MILP completo, mas apretada que la '
             'relajacion lineal total, que tambien afloja la inversion. NO es un plan '
             'reportable: la operacion queda fraccionaria.'
    )
    parser.add_argument(
        '--relax_integrality',
        action='store_true',
        help='Resuelve la relajación lineal del modelo sin cambiar las variables originales'
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
             'puede ademas operar (viajar/extraer), no solo hacer swap o estar '
             'detenido. El cambio de turno (between_shifts) sigue restringido '
             'a swap-o-detenido en ambos modos.'
    )
    parser.add_argument(
        '--mccormick_degradation',
        action='store_true',
        help='[solo si hay hoja BatteryDegradation] linealiza los dos '
             'bilineales encadenados de la degradacion del pool de swap '
             '(N_ciclos*n_battery_fleet, N_total*b_bar) con la envolvente de '
             'McCormick (Camino A, ver degradacion_descomposicion_mccormick.md '
             'en la rama carga_ob_multiaño) en vez de resolverlos como '
             'restricciones cuadraticas no convexas (Gurobi NonConvex=2). El '
             'modelo queda MILP puro -- suele ser mas rapido, a costa de una '
             'aproximacion.'
    )

    parser.add_argument(
        '--mode', choices=['monolithic', 'decomposed', 'hybrid'], default='monolithic',
        help='monolithic (default): arma y resuelve el modelo completo de una vez, '
             'igual que siempre. decomposed: descomposicion temporal Nested Benders, '
             'un subproblema por año acoplado por cortes de Benders. hybrid: corre la '
             'descomposicion y le entrega su solucion al monolitico como MIP start. '
             'Los dos metodos tienen perfiles opuestos -- la descomposicion consigue '
             'un incumbente muy bueno enseguida pero su cota inferior se estanca, y el '
             'branch and bound sube la cota rapido pero le cuesta el incumbente --, '
             'asi que el hibrido le da a cada uno lo que al otro le falta. Renuncia a '
             'la ventaja de memoria: construye el monolitico completo. decomposed e '
             'hybrid requieren un solver con duales (gurobi).'
    )
    parser.add_argument(
        '--max_iter', type=int, default=20,
        help='[--mode decomposed] tope de iteraciones forward/backward.'
    )
    parser.add_argument(
        '--gap_tol', type=float, default=0.01,
        help='[--mode decomposed] gap relativo (UB-LB)/|UB| con el que se corta.'
    )
    parser.add_argument(
        '--solve_timelimit', type=int, default=900,
        help='[--mode decomposed] limite de tiempo, en segundos, de CADA resolucion '
             'de bloque anual (no del total).'
    )
    parser.add_argument(
        '--block_build_jobs', type=int, default=None,
        help='[--mode decomposed] procesos para construir los bloques anuales en '
             'paralelo. Sin el flag, min(años, cpus); 1 fuerza secuencial (util para '
             'depurar).'
    )
    parser.add_argument(
        '--fixed_stations_json', default=None,
        help='[--mode decomposed] JSON {"<año>": {"<nave>": 0/1}} con la apertura de '
             'naves, que en modo descompuesto es exogena. Si se omite se infiere de '
             'la hoja StationAssignment: se construye toda nave con al menos un equipo '
             'asignado.'
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
        help='[--mode decomposed] presolve de capacidad de subestacion (ver '
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
        '--cut_type', choices=['benders', 'strengthened'], default='benders',
        help='[--mode decomposed/hybrid] familia de cortes del backward pass '
             '(Lara et al. 2018, EJOR 271:1037-1054). benders (default, ec. 59): '
             'la constante sale de la relajacion LINEAL del bloque hijo; barato, '
             'pero el LB converge como mucho a la relajacion lineal del '
             'monolitico. strengthened (ec. 63): reusa el mismo mu del dual del '
             'LP y recalcula la constante con la relajacion LAGRANGEANA, que '
             'conserva la integralidad; un MILP extra por bloque, sin el bucle de '
             'subgradiente del corte lagrangeano completo. Es el corte que rompe '
             'ese techo cuando la relajacion lineal es floja, que es el caso de '
             'este modelo (LP 1,807,131 contra 2,212,229 que Gurobi prueba).'
    )
    parser.add_argument(
        '--strengthened_timelimit', type=int, default=300,
        help='[--cut_type strengthened] segundos por MILP lagrangeano. Cortarlo '
             'NO invalida el corte: se usa la cota dual del solver, que subestima '
             'siempre, y si no llega a superar Phi^LP el corte queda igual al de '
             'Benders puro.'
    )
    parser.add_argument(
        '--warm_start_cuts', action='store_true',
        help='[--mode decomposed/hybrid] Accelerated Nested Decomposition (Lara '
             'et al. 2018, sec. 5.3): antes de la primera pasada forward corre un '
             'backward completo sobre la trayectoria de la relajacion lineal del '
             'monolitico, para que alpha no arranque sin cotas. En el paper baja '
             'de 7 a 4 iteraciones para el mismo gap, y la ganancia es mayor en la '
             'instancia grande. Reusa el LP que --no_monolithic_lp_bound ya '
             'resolvia, asi que no cuesta un solve extra.'
    )
    parser.add_argument(
        '--no_monolithic_lp_bound', action='store_true',
        help='[--mode decomposed] no calcular la relajacion lineal del monolitico '
             'como cota inferior inicial. Por defecto SI se calcula: suele ser una '
             'cota mucho mejor que la del backward y cuesta un solo LP, pero obliga '
             'a construir el monolitico completo, que es el gasto de memoria que la '
             'descomposicion evita. En --mode hybrid NO se calcula nunca (la fase '
             'monolitica la supera con su propio root bound), asi que el flag no '
             'tiene efecto ahi.'
    )

    args = parser.parse_args()
    series, mine_system, time_series = build_mine(args)

    if args.mode in ('decomposed', 'hybrid'):
        run_decomposed(args, mine_system, time_series, hybrid=(args.mode == 'hybrid'))
        return

    gap= 1/100;
    solver_name=args.solver
    output_folder=args.output_folder
    y_init_path=args.y_init_path
    init_solution_folder=args.init_solution_folder
    opt = OptimizationModel(
        mine_system,
        time_series,
        gap,
        solver_name,
        output_folder,
        y_init_path=y_init_path,
        init_solution_folder=init_solution_folder,
        relax_integrality=args.relax_integrality,
        relax_operational=args.relax_operational,
        autonomous_mode=args.autonomous_mode,
        mccormick_degradation=args.mccormick_degradation,
        mip_focus=args.mip_focus,
        **({'timelimit': args.mono_timelimit} if args.mono_timelimit else {}),
    )


if __name__ == '__main__':
    main()
