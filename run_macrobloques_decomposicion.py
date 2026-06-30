"""
Descompone el problema completo (3 estaciones) en subproblemas independientes,
uno por macrobloque/estacion de carga, leyendo UNA SOLA VEZ los datos completos
(elmo_data.xlsx + time_series.xlsx) y filtrando en memoria por estacion.

No crea ni modifica archivos Excel: la particion se hace sobre los DataFrames ya
cargados, usando la asignacion estacion<->LHD de la hoja 'StationAssignment'.

Modo normal (--days con 1+ dias, sin --parallel_days):
    Cada macrobloque se resuelve en UN solo MIP que incluye todos los dias dados,
    usando solo su propio p_max_ssee como cota electrica (Fase 1: fijar
    infraestructura, ver discusion en el chat).

Modo --parallel_days (descomposicion adicional por dia, en paralelo):
    Cada dia significativo se modela como un ciclo cerrado de bateria
    (B[i,d,0] == B[i,d,tf]), sin continuidad de SOC entre dias, asi que los
    dias son independientes entre si salvo por:
      - infraestructura (N_chargers, N_bays, N_batteries, X): se decide UNA
        vez para todo el horizonte, no por dia.
      - P_pot/p_peak: acoples ya discutidos para el caso por estacion.
    Por eso se resuelve en dos sub-fases:
      Fase 1: cada par (estacion, dia) se resuelve por separado y en paralelo
              con infraestructura libre.
      Agregacion: para cada estacion, se toma el maximo de N_chargers/N_bays/
              N_batteries/X entre sus dias (la infraestructura debe servir al
              dia mas exigente).
      Fase 2: cada par (estacion, dia) se vuelve a resolver en paralelo, esta
              vez con la infraestructura fija al resultado de la agregacion.

    Cada (estacion, dia) final queda en una carpeta plana
    <output_folder>/<estacion>_d<dia>/, para que consumer.py / batch_plotter
    los detecten automaticamente como "macrobloques" a agregar/graficar.
    OJO: sumar P_red entre carpetas de DIAS DISTINTOS no tiene sentido fisico
    (no son simultaneos); el costo total escalado SI es correcto de sumar.
"""
import argparse
import json
import math
import os
from os import makedirs
from os.path import join
from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd

from src import mine
from src.io.reader import Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel
import xlrd
xlrd.xlsx.ensure_elementtree_imported(False, None)
xlrd.xlsx.Element_has_iter = True


class FilteredReader:
    """ Mimica la interfaz de Reader (__getitem__ + .container) sobre un dict
    de DataFrames ya filtrados, aplicando el mismo recorte de filas de cabecera
    (start_in) que usa Reader.
    """

    def __init__(self, container, start_in):
        self.container = container
        self.start_in = start_in

    def __getitem__(self, sheet):
        data = self.container.get(sheet)
        if data is not None:
            return data.iloc[self.start_in:, ]
        raise KeyError(f'{sheet} no esta en el modelo filtrado')


def filter_sheet(raw_df, name_col, keep_names, start_in):
    """ Conserva las filas de cabecera (0..start_in-1) + las filas cuyo
    name_col esta en keep_names. """
    header = raw_df.iloc[:start_in]
    body = raw_df.iloc[start_in:]
    body = body[body[name_col].isin(keep_names)]
    return pd.concat([header, body])


def build_macrobloque_mine(full_model: Reader, lhd_names, station_name, node_names):
    start_in = full_model.start_in
    filtered = {
        'LHD': filter_sheet(full_model.container['LHD'], 'name', set(lhd_names), start_in),
        'stations': filter_sheet(full_model.container['stations'], 'station_name', {station_name}, start_in),
        'extraction_nodes': filter_sheet(full_model.container['extraction_nodes'], 'node_name', set(node_names), start_in),
        'chargers': full_model.container['chargers'],
        'discharge_nodes': full_model.container['discharge_nodes'],
    }
    filtered_reader = FilteredReader(filtered, start_in)
    return mine.Mine(filtered_reader)


def get_lhds_and_nodes_per_station(series: Series):
    station_assignment = series['StationAssignment']  # cols: id, elh_name, station_name
    node_assignment = series['NodeAssignment']         # cols: id, shift_name_start, shift_name_end, elhd_name, ex_node_name

    lhds_per_station = {}
    nodes_per_station = {}
    for station_name, group in station_assignment.groupby('station_name'):
        lhd_names = list(group['elh_name'])
        lhds_per_station[station_name] = lhd_names
        nodes_per_station[station_name] = sorted(set(
            node_assignment.loc[node_assignment['elhd_name'].isin(lhd_names), 'ex_node_name']
        ))
    return lhds_per_station, nodes_per_station


def _node_representative_lhd_map(series: Series, lhds_per_station):
    """Para cada estacion, mapea node_name -> primer LHD de esa estacion
    asignado a ese nodo segun la hoja NodeAssignment (la misma fuente que usa
    el modelo real para decidir que LHD puede visitar que nodo)."""
    node_assignment = series['NodeAssignment']
    rep_by_station = {}
    for station_name, lhd_names in lhds_per_station.items():
        sub = node_assignment[node_assignment['elhd_name'].isin(lhd_names)]
        rep_by_station[station_name] = (
            sub.drop_duplicates('ex_node_name').set_index('ex_node_name')['elhd_name'].to_dict()
        )
    return rep_by_station


def compute_node_capacity_bounds(mine_system, time_series, lhd_name, node_name, day):
    """Cota [Cap_min, Cap_max] de produccion alcanzable en node_name el dia
    `day` con el LHD representante `lhd_name`, replicando la misma formula
    floor/ceil que usa ConstraintRules.production() en functions.py."""
    n_trips = time_series.get_n_trips(node_name, lhd_name)
    prod_per_assign = (mine_system.elhd.get_load_capacity(lhd_name)
                        * n_trips
                        * mine_system.elhd.get_filling_factor(lhd_name))
    if prod_per_assign <= 0:
        return 0.0, 0.0
    target = time_series.get_extraction_goal(node_name, day)
    cap_min = math.floor(target / prod_per_assign) * prod_per_assign
    cap_max = math.ceil(target / prod_per_assign) * prod_per_assign
    return cap_min, cap_max


def compute_master_daily_targets(full_model: Reader, series: Series, lhds_per_station, nodes_per_station, days, delta_t):
    """'Fase 0' (problema maestro): reparte el target diario de produccion
    total entre macrobloques, en vez de exigirle a cada uno (como hace hoy
    daily_production al decomponer, ver discusion en el chat) la suma de los
    m_j de sus PROPIOS nodos nada mas.

    Para cada estacion/dia calcula el rango [Cap_min, Cap_max] que esa
    estacion puede producir sola (mismo floor/ceil por nodo que ya impone
    production() en functions.py, asi que es una cota exacta, no estimada) y
    reparte el target global D_d con un water-filling de un solo paso:
    arranca cada estacion en su Cap_min y le agrega, proporcional a su
    holgura (Cap_max-Cap_min), lo que falte para llegar a D_d. Esto reproduce
    la compensacion entre macrobloques que existe en el modelo completo (una
    estacion produce de mas, otra de menos) sin iterar ni resolver ningun
    MIP: las cotas salen directo de los datos.
    """
    full_mine = mine.Mine(full_model)
    ts = timeseries.Timeseries(series, days, delta_t)
    ts.mapper['Trips'] = ts.get_trips(full_mine)

    rep_lhd_by_station = _node_representative_lhd_map(series, lhds_per_station)
    all_nodes = sorted({n for nodes in nodes_per_station.values() for n in nodes})

    targets = {st: {} for st in lhds_per_station}
    for d in days:
        cap_min, cap_max = {}, {}
        for st, node_names in nodes_per_station.items():
            cmin = cmax = 0.0
            for node_name in node_names:
                lhd_name = rep_lhd_by_station[st].get(node_name)
                if lhd_name is None:
                    continue
                lo, hi = compute_node_capacity_bounds(full_mine, ts, lhd_name, node_name, d)
                cmin += lo
                cmax += hi
            cap_min[st], cap_max[st] = cmin, cmax

        D_d = sum(ts.get_extraction_goal(n, d) for n in all_nodes)
        total_headroom = sum(cap_max[st] - cap_min[st] for st in lhds_per_station)
        remaining = D_d - sum(cap_min.values())

        if remaining > total_headroom + 1e-6:
            print(f"  [ADVERTENCIA] dia {d}: ni el maximo combinado entre estaciones "
                  f"({sum(cap_max.values()):.1f}) alcanza el target global ({D_d:.1f}) "
                  f"-> infactible tambien en el modelo completo, no es un artefacto de la decomposicion.")
            remaining = total_headroom

        T = {}
        for st in lhds_per_station:
            headroom = cap_max[st] - cap_min[st]
            share = (headroom / total_headroom) if total_headroom > 0 else 0.0
            T[st] = cap_min[st] + max(0.0, remaining) * share
            targets[st][d] = T[st]

        print(f"  [maestro] dia {d}: D_d={D_d:.1f}  " + "  ".join(
            f"{st}: T={T[st]:.1f} (Cap=[{cap_min[st]:.1f},{cap_max[st]:.1f}])"
            for st in sorted(lhds_per_station)
        ))

    return targets


def run_macrobloque(full_model, series, station_name, lhd_names, node_names,
                     days, delta_t, gap, solver_name, output_folder, timelimit,
                     daily_target_override=None):
    mine_system = build_macrobloque_mine(full_model, lhd_names, station_name, node_names)

    time_series = timeseries.Timeseries(series, days, delta_t)
    time_series.mapper['Trips'] = time_series.get_trips(mine_system)

    makedirs(output_folder, exist_ok=True)
    print(f"\n=== Resolviendo macrobloque '{station_name}' "
          f"({len(lhd_names)} LHD, {len(node_names)} nodos, dias={days}) -> {output_folder} ===")
    OptimizationModel(
        mine_system,
        time_series,
        gap,
        solver_name,
        output_folder,
        timelimit=timelimit,
        daily_target_override=daily_target_override,
    )


def _read_station_scalar(output_folder, var_name, station_name):
    """Lee un valor escalar indexado solo por estacion (ej. N_chargers.json
    con una sola estacion) desde el output de un subproblema ya resuelto."""
    path = join(output_folder, f"{var_name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    node = data
    while isinstance(node, dict) and len(node) == 1:
        node = next(iter(node.values()))
    if isinstance(node, dict):
        return node.get(station_name)
    return node


def solve_macrobloque_day(job):
    """ Worker function (top-level para ser picklable en multiprocessing).

    job: tupla con todos los parametros primitivos necesarios; cada worker
    vuelve a leer los datos desde disco (Reader/Series no son picklables y
    el costo de releer el Excel es despreciable frente al tiempo de Gurobi).
    """
    (data_folder, model_name, series_name, station_name, lhd_names, node_names,
     day, total_n_days, delta_t, gap, solver_name, timelimit, output_folder,
     fixed_infra, threads, daily_target_value) = job

    full_model = Reader(join(data_folder, model_name), start_in=1)
    series = Series(join(data_folder, series_name))

    mine_system = build_macrobloque_mine(full_model, lhd_names, station_name, node_names)

    time_series = timeseries.Timeseries(series, [day], delta_t)
    # El dia se resuelve solo, pero representa 1/total_n_days del año:
    # sobreescribimos el factor de escala que Timeseries calcula como
    # 365/len(days) (que asumiria que este unico dia es todo el año).
    time_series.scaling_factor_op_cost = 365.0 / total_n_days
    time_series.mapper['Trips'] = time_series.get_trips(mine_system)

    daily_target_override = {day: daily_target_value} if daily_target_value is not None else None

    makedirs(output_folder, exist_ok=True)
    fase = "Fase 2 (infra fija)" if fixed_infra else "Fase 1 (infra libre)"
    print(f"  [{fase}] estacion={station_name} dia={day} -> {output_folder}")
    OptimizationModel(
        mine_system,
        time_series,
        gap,
        solver_name,
        output_folder,
        timelimit=timelimit,
        fixed_infra=fixed_infra,
        threads=threads,
        daily_target_override=daily_target_override,
    )

    return {
        'station': station_name,
        'day': day,
        'N_chargers': _read_station_scalar(output_folder, 'N_chargers', station_name),
        'N_bays': _read_station_scalar(output_folder, 'N_bays', station_name),
        'N_batteries': _read_station_scalar(output_folder, 'N_batteries', station_name),
        'X': _read_station_scalar(output_folder, 'X', station_name),
    }


def run_parallel_by_station_and_day(args, lhds_per_station, nodes_per_station, days, master_targets):
    delta_t = 8 / 60
    gap = 1 / 100
    n_jobs = len(lhds_per_station) * len(days)
    n_workers = args.n_workers or min(os.cpu_count() or 1, n_jobs)
    # Reparte los hilos de Gurobi entre los workers concurrentes para no
    # sobre-suscribir la maquina (cada worker pedia 24 hilos por defecto).
    threads_per_worker = max(1, (os.cpu_count() or n_workers) // n_workers)
    print(f"Hilos de Gurobi por worker: {threads_per_worker} (workers={n_workers}, cpus={os.cpu_count()})")

    def make_job(station_name, day, output_folder, fixed_infra):
        return (
            args.data_folder, args.model, args.series, station_name,
            lhds_per_station[station_name], nodes_per_station[station_name],
            day, len(days), delta_t, gap, args.solver, args.timelimit,
            output_folder, fixed_infra, threads_per_worker,
            master_targets[station_name][day],
        )

    # ---- Fase 1: infraestructura libre, todas las combinaciones en paralelo ----
    print(f"\n=== Fase 1: {n_jobs} subproblemas (estacion x dia) en paralelo "
          f"({n_workers} workers), infraestructura libre ===")
    stage1_jobs = [
        make_job(st, d, join(args.output_folder, f"{st}_d{d}_stage1"), None)
        for st in sorted(lhds_per_station) for d in days
    ]

    results = []
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(solve_macrobloque_day, job) for job in stage1_jobs]
        for fut in as_completed(futures):
            results.append(fut.result())

    # ---- Agregacion: infraestructura = maximo necesitado entre dias, por estacion ----
    infra_by_station = {}
    for r in results:
        st = r['station']
        agg = infra_by_station.setdefault(st, {})
        for key in ('N_chargers', 'N_bays', 'N_batteries', 'X'):
            val = r.get(key)
            if val is None:
                continue
            agg[key] = max(agg.get(key, val), val)

    print("\n=== Infraestructura fijada por estacion (maximo entre dias) ===")
    for st, infra in infra_by_station.items():
        print(f"  {st}: {infra}")

    # ---- Fase 2: re-resolver con infraestructura fija, en paralelo ----
    print(f"\n=== Fase 2: {n_jobs} subproblemas (estacion x dia) en paralelo "
          f"({n_workers} workers), infraestructura fija ===")
    stage2_jobs = [
        make_job(st, d, join(args.output_folder, f"{st}_d{d}"), infra_by_station.get(st))
        for st in sorted(lhds_per_station) for d in days
    ]

    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(solve_macrobloque_day, job) for job in stage2_jobs]
        for fut in as_completed(futures):
            fut.result()

    print("\nListo. Resultados finales en:")
    for st in sorted(lhds_per_station):
        for d in days:
            print(f"  {join(args.output_folder, f'{st}_d{d}')}")


def main():
    parser = argparse.ArgumentParser(description='Descomposicion por macrobloque/estacion (y opcionalmente por dia, en paralelo)')
    parser.add_argument('--data_folder', default='data/Escenarios_DCH_costos_nuevos/Costo_fijo/Swap_fixed_3estaciones_320kW/')
    parser.add_argument('--model', default='elmo_data.xlsx')
    parser.add_argument('--series', default='time_series.xlsx')
    parser.add_argument('--output_folder', default='output/MB_test_prueba/')
    parser.add_argument('--solver', default='gurobi')
    parser.add_argument('--days', default='1', help='Dias significativos separados por coma, ej: 1,32,60')
    parser.add_argument('--timelimit', type=int, default=172800)
    parser.add_argument(
        '--parallel_days', action='store_true',
        help='Ademas de por estacion, descompone por dia y resuelve cada (estacion,dia) '
             'en paralelo (Fase 1 infra libre -> agregacion -> Fase 2 infra fija).',
    )
    parser.add_argument(
        '--n_workers', type=int, default=None,
        help='Procesos en paralelo para --parallel_days (default: min(cpus, n_jobs))',
    )
    args = parser.parse_args()

    days = [int(d) for d in args.days.split(',')]

    full_model = Reader(join(args.data_folder, args.model), start_in=1)
    series = Series(join(args.data_folder, args.series))
    lhds_per_station, nodes_per_station = get_lhds_and_nodes_per_station(series)

    delta_t = 8 / 60
    gap = 1 / 100

    print("\n=== Fase 0 (maestro): repartiendo target de produccion diario entre macrobloques ===")
    master_targets = compute_master_daily_targets(
        full_model, series, lhds_per_station, nodes_per_station, days, delta_t,
    )

    if args.parallel_days and len(days) > 1:
        run_parallel_by_station_and_day(args, lhds_per_station, nodes_per_station, days, master_targets)
        return

    for station_name in sorted(lhds_per_station):
        run_macrobloque(
            full_model, series, station_name,
            lhds_per_station[station_name],
            nodes_per_station[station_name],
            days, delta_t, gap, args.solver,
            join(args.output_folder, station_name),
            args.timelimit,
            daily_target_override=master_targets[station_name],
        )


if __name__ == '__main__':
    main()
