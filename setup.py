from src import mine
from src.io.reader import Setting, Reader, Series
from src.io.printer import Printer
from src.time_series import timeseries
from src.optimization import OptimizationModel
from src.optimization.decomposition.driver import NestedBendersSolver
import argparse, pprint
import json
import os
import pandas as pd
from os.path import join
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

    model = Reader(args.data_folder+args.model, start_in=1)
    series = Series(args.data_folder+args.series)
    #time_series = timeseries.Timeseries(series, [15, 105, 196, 288, 380, 470, 561, 653, 745, 835, 926, 1018, 1110, 1200, 1291, 1383, 1475, 1565, 1656, 1748], 8/60)  # 5 años, 4 día representativo/año
    #time_series = timeseries.Timeseries(series, [15, 380, 745, 1110, 1475], 8/60)  # 5 años, 1 día representativo/año
    #time_series = timeseries.Timeseries(series, [1,366], 8/60)
    #time_series = timeseries.Timeseries(series, [1,32,60,91,121,152,182,213,244,274,305,335], 8/60)
    time_series = timeseries.Timeseries(series, [196, 561, 926, 1291, 1656], 8/60)
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
        '--mode', choices=['monolithic', 'decomposed'], default='monolithic',
        help='monolithic (default): resuelve el modelo completo de una vez. '
             'decomposed: descomposicion Nested Benders por año (ver '
             'implementacion_descomposicion_carga_ob.md).'
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

        solver = NestedBendersSolver(
            mine_system, time_series, exogenous_stations_by_year,
            gap_tol=args.gap_tol, max_iter=args.max_iter,
            autonomous_mode=args.autonomous_mode,
            solver_kwargs={"solvername": solver_name, "gap": gap, "timelimit": 900},
        )
        result = solver.solve()
        print(
            f"NestedBenders: UB={result['ub']:.4f}  LB={result['lb']:.4f}  "
            f"gap={result['gap']:.4%}  iteraciones={result['iterations']}"
        )

        om_report = solver.build_report_model(output_folder)
        printer = Printer(om_report, output_folder, time_series, mine_system)
        printer.create_all_plots()


if __name__ == '__main__':
    main()
