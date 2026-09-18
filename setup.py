from src import mine
from src.io.reader import Setting, Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel
import argparse, pprint
import pandas as pd
from os.path import join
import os
import xlrd
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
    #time_series = timeseries.Timeseries(series, [1,91,181,271], 8/60)
    #time_series = timeseries.Timeseries(series, [1,32,60,91,121,152,182,213,244,274,305,335], 8/60) #12 dias significativos
    #time_series = timeseries.Timeseries(series, [15, 380, 745, 1110, 1475], 8/60)
    #time_series = timeseries.Timeseries(series, [196, 561, 926, 1291, 1656], 8/60)
    #time_series = timeseries.Timeseries(series, [1, 91, 366, 456, 731, 821, 1096, 1186, 1461, 1551, 1826, 1916, 2191, 2281, 2556, 2646, 2921, 3011, 3286, 3376, 3651, 3741, 4016, 4106, 4381, 4471, 4746, 4836], 8/60)  # 14 años, 2 días representativos/año (verano sin cobro potencia + invierno con cobro potencia)
    # 11 anios, 2 dias representativos/anio: dia 15 (verano, sin cobro de
    # potencia) y dia 196 (invierno, con cobro). Se eligen 15 y 196 -- y no
    # cualquier par -- porque son los unicos dias del anio para los que la hoja
    # GenProfiles trae perfiles de solar/eolica que cumplan ese criterio: alli
    # solo existen los dias-del-anio {15, 105, 196, 288}.
    #
    # OJO: get_alpha_g devuelve 0.0 cuando no encuentra el dia, sin avisar. Con
    # la lista anterior (dias-del-anio 1 y 91) NINGUNO tenia perfil, asi que la
    # generacion y el almacenamiento quedaban silenciosamente desactivados: el
    # modelo podia invertir en solar/eolica, pagaba inversion y operacion y
    # recibia cero energia. Si se cambia esta lista hay que verificar que los
    # dias nuevos existan en GenProfiles.
    #   lista anterior (perfiles de generacion en cero):
    #   days = [1, 91, 366, 456, 731, 821, 1096, 1186, 1461, 1551, 1826, 1916, 2191, 2281, 2556, 2646, 2921, 3011, 3286, 3376, 3651, 3741]  # 11 años, 2 días representativos/año (verano sin cobro potencia + invierno con cobro potencia)
    days = [15, 196, 380, 561, 745, 926, 1110, 1291, 1475, 1656, 1840, 2021, 2205, 2386, 2570, 2751, 2935, 3116, 3300, 3481, 3665, 3846]
    # --n_years recorta el horizonte a los primeros N años (2 días por año),
    # para poder correr validaciones cortas sin editar esta lista a mano. Sin
    # el flag, el horizonte es el de siempre.
    n_years = getattr(args, 'n_years', None)
    if n_years is not None:
        days = days[:n_years * 2]
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
    from src.optimization.decomposition.driver import NestedBendersSolver

    exogenous = None
    if args.fixed_stations_json:
        with open(args.fixed_stations_json, encoding='utf-8') as fh:
            crudo = json.load(fh)
        exogenous = {int(y): {k: int(v) for k, v in por_nave.items()}
                     for y, por_nave in crudo.items()}

    solver = NestedBendersSolver(
        mine_system, time_series,
        exogenous_stations_by_year=exogenous,
        gap_tol=args.gap_tol,
        max_iter=args.max_iter,
        autonomous_mode=args.autonomous_mode,
        solver_kwargs={'solvername': args.solver, 'gap': args.gap_tol,
                       'timelimit': args.solve_timelimit},
        block_build_jobs=args.block_build_jobs,
        monolithic_lp_bound=not args.no_monolithic_lp_bound,
        capacity_presolve=args.capacity_presolve,
    )
    resultado = solver.solve(verbose=True)

    print(f"[NestedBenders] termino en {resultado['iterations']} iteraciones "
          f"({resultado['total_time_sec']:.0f}s): UB={resultado['ub']:,.2f}  "
          f"LB={resultado['lb']:,.2f}  gap={resultado['gap']:.4%}"
          + ("  [INTERRUMPIDO]" if resultado['interrupted'] else ""))

    report, _informe = solver.build_report_model(args.output_folder)

    if hybrid:
        # La solucion descompuesta entra como MIP start (build_report_model deja
        # has_warm_start puesto). Gurobi arranca con un incumbente que le habria
        # costado encontrar y se dedica a cerrar la cota, que es lo que hace bien.
        t0 = time.time()
        print("[Hibrido] resolviendo el monolitico con la solucion descompuesta "
              "como MIP start...")
        report.solve_model(args.gap_tol, args.solver, timelimit=args.solve_timelimit)
        print(f"[Hibrido] monolitico resuelto en {time.time() - t0:.0f}s: "
              f"costo = {report.opt_cost_result:,.2f} "
              f"(la descomposicion habia llegado a {resultado['ub']:,.2f} "
              f"mas la inversion en estaciones)")

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
        '--no_monolithic_lp_bound', action='store_true',
        help='[--mode decomposed] no calcular la relajacion lineal del monolitico '
             'como cota inferior inicial. Por defecto SI se calcula: suele ser una '
             'cota mucho mejor que la del backward y cuesta un solo LP, pero obliga '
             'a construir el monolitico completo, que es el gasto de memoria que la '
             'descomposicion evita.'
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
        autonomous_mode=args.autonomous_mode,
        mccormick_degradation=args.mccormick_degradation,
    )


if __name__ == '__main__':
    main()
