"""
Descompone el problema completo (carga on-board, N estaciones) en subproblemas
independientes, uno por macrobloque/estacion de carga, leyendo UNA SOLA VEZ los
datos completos (elmo_data.xlsx + time_series.xlsx) y filtrando en memoria por
estacion.

No crea ni modifica archivos Excel: la particion se hace sobre los DataFrames ya
cargados, usando la asignacion estacion<->LHD de la hoja 'StationAssignment'.

Modo normal (--days con 1+ dias, sin --parallel_days):
    Cada macrobloque se resuelve en UN solo MIP que incluye todos los dias dados,
    usando solo su propio p_max_ssee como cota electrica.

Modo --parallel_days (descomposicion adicional por dia, en paralelo):
    Cada dia significativo se modela como un ciclo cerrado de bateria
    (B[i,d,0] == B[i,d,tf]), sin continuidad de SOC entre dias, asi que los
    dias son independientes entre si salvo por:
      - infraestructura (N_chargers, X): se decide UNA vez para todo el
        horizonte, no por dia. (No hay N_bays/N_batteries: en carga on-board
        no hay intercambio de baterias, cada LHD carga la suya propia.)
      - P_pot/p_peak: acoples ya discutidos para el caso por estacion.
    Por eso se resuelve en dos sub-fases:
      Fase 1: cada par (estacion, dia) se resuelve por separado y en paralelo
              con infraestructura libre.
      Agregacion: para cada estacion, se toma el maximo de N_chargers/X entre
              sus dias (la infraestructura debe servir al dia mas exigente).
      Fase 2: cada par (estacion, dia) se vuelve a resolver en paralelo, esta
              vez con la infraestructura fija al resultado de la agregacion.

    Cada (estacion, dia) final queda en una carpeta plana
    <output_folder>/<estacion>_d<dia>/, para que consumer.py / batch_plotter
    los detecten automaticamente como "macrobloques" a agregar/graficar.
    OJO: sumar P_red entre carpetas de DIAS DISTINTOS no tiene sentido fisico
    (no son simultaneos); el costo total escalado SI es correcto de sumar.

LIMITACION CONOCIDA: si el escenario tiene generacion renovable/BESS propios
(hojas 'Generators'/'Storage'), este script NO las propaga a los macrobloques
(build_macrobloque_mine no incluye esas hojas en el container filtrado), asi
que mine.Mine las desactiva silenciosamente (self.generators/self.storage =
None) en cada subproblema. Generacion/BESS son recursos compartidos por todo
el sitio (P_gen, P_bat, G_g, H_h no estan indexados por estacion en el modelo
completo), asi que decomponerlos por estacion requeriria un diseno aparte (no
es un simple fixed_infra). Usar este script solo con escenarios sin
Generators/Storage hasta resolver eso explicitamente.

Modelo de consumo (--consumption_model wp1|wp2): igual que setup.py, wp2 usa
consumos/tiempos precalculados por nodo desde un JSON (electric_ o
diesel_routes_within_time.json) en vez del calculo fisico interno. El JSON se
resuelve una sola vez en main() (misma logica que resolve_wp2_json_path de
setup.py) y se propaga a la Fase 0 (targets maestro), a cada macrobloque y a
cada worker (estacion,dia); el lookup es por nodo, asi que funciona igual de
bien sobre el subconjunto de nodos de cada macrobloque.
"""
import argparse
import json
import math
import os
import random
import time
from os import makedirs
from os.path import join
from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd

from src import mine
from src.io.reader import Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel
from setup import resolve_wp2_json_path
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


def compute_master_daily_targets(full_model: Reader, series: Series, lhds_per_station, nodes_per_station, days, delta_t,
                                  consumption_model='wp1', wp2_consumption_json=None):
    """'Fase 0' (problema maestro): reparte el target diario de produccion
    total entre macrobloques, en vez de exigirle a cada uno (como hace hoy
    daily_production al decomponer) la suma de los m_j de sus PROPIOS nodos
    nada mas.

    Para cada estacion/dia calcula el rango [Cap_min, Cap_max] que esa
    estacion puede producir sola (mismo floor/ceil por nodo que ya impone
    production() en functions.py, asi que es una cota exacta, no estimada) y
    reparte el target global D_d con un water-filling de un solo paso:
    arranca cada estacion en su Cap_min y le agrega, proporcional a su
    holgura (Cap_max-Cap_min), lo que falte para llegar a D_d.
    """
    full_mine = mine.Mine(full_model)
    ts = timeseries.Timeseries(series, days, delta_t)
    ts.mapper['Trips'] = ts.get_trips(full_mine, consumption_model=consumption_model,
                                       wp2_consumption_json=wp2_consumption_json)

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
                     daily_target_override=None, consumption_model='wp1', wp2_consumption_json=None,
                     autonomous_mode=False):
    mine_system = build_macrobloque_mine(full_model, lhd_names, station_name, node_names)

    time_series = timeseries.Timeseries(series, days, delta_t)
    time_series.mapper['Trips'] = time_series.get_trips(mine_system, consumption_model=consumption_model,
                                                          wp2_consumption_json=wp2_consumption_json)

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
        autonomous_mode=autonomous_mode,
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


def _build_series_with_retry(series_path, attempts=8, base_delay=0.5):
    """Construye Series(series_path) con reintentos.

    Series cachea una version "simple" de time_series.xlsx en disco
    (simple_time_series.xlsx + *.npy + series.ini, ver src/io/reader.py).
    `_all_npy_exist` ahi chequea TODOS los tipos de hoja conocidos
    (incluye Emissions/GenProfiles) en vez de solo los presentes en este
    Excel, asi que para escenarios sin esas hojas la cache se considera
    "desactualizada" en CADA llamada y siempre se reescribe. Con varios
    workers en paralelo (--parallel_days) eso provoca una carrera real
    escribiendo el mismo simple_time_series.xlsx a la vez (OSError en
    Windows). No es seguro arreglar ese chequeo aqui (es codigo
    compartido por todo el proyecto, fuera del alcance de este script),
    asi que en su lugar reintentamos con backoff: el worker que pierde la
    carrera simplemente vuelve a intentar.
    """
    last_exc = None
    for attempt in range(attempts):
        try:
            return Series(series_path)
        except OSError as exc:
            last_exc = exc
            time.sleep(base_delay * (attempt + 1) + random.uniform(0, 0.3))
    raise last_exc


def solve_macrobloque_day(job):
    """ Worker function (top-level para ser picklable en multiprocessing).

    job: tupla con todos los parametros primitivos necesarios; cada worker
    vuelve a leer los datos desde disco (Reader/Series no son picklables y
    el costo de releer el Excel es despreciable frente al tiempo de Gurobi).
    """
    (data_folder, model_name, series_name, station_name, lhd_names, node_names,
     day, total_n_days, delta_t, gap, solver_name, timelimit, output_folder,
     fixed_infra, threads, daily_target_value, consumption_model, wp2_consumption_json,
     autonomous_mode) = job

    full_model = Reader(join(data_folder, model_name), start_in=1)
    series = _build_series_with_retry(join(data_folder, series_name))

    mine_system = build_macrobloque_mine(full_model, lhd_names, station_name, node_names)

    time_series = timeseries.Timeseries(series, [day], delta_t)
    # El dia se resuelve solo, pero representa 1/total_n_days del año:
    # sobreescribimos el factor de escala que Timeseries calcula como
    # 365/len(days) (que asumiria que este unico dia es todo el año).
    time_series.scaling_factor_op_cost = 365.0 / total_n_days
    time_series.mapper['Trips'] = time_series.get_trips(mine_system, consumption_model=consumption_model,
                                                          wp2_consumption_json=wp2_consumption_json)

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
        autonomous_mode=autonomous_mode,
    )

    return {
        'station': station_name,
        'day': day,
        'N_chargers': _read_station_scalar(output_folder, 'N_chargers', station_name),
        'X': _read_station_scalar(output_folder, 'X', station_name),
    }


def run_parallel_by_station_and_day(args, lhds_per_station, nodes_per_station, days, master_targets,
                                     consumption_model='wp1', wp2_consumption_json=None):
    delta_t = 8 / 60
    gap = 1 / 100
    n_jobs = len(lhds_per_station) * len(days)
    n_workers = args.n_workers or min(os.cpu_count() or 1, n_jobs)
    # Reparte los hilos de Gurobi entre los workers concurrentes para no
    # sobre-suscribir la maquina.
    threads_per_worker = max(1, (os.cpu_count() or n_workers) // n_workers)
    print(f"Hilos de Gurobi por worker: {threads_per_worker} (workers={n_workers}, cpus={os.cpu_count()})")

    def make_job(station_name, day, output_folder, fixed_infra):
        return (
            args.data_folder, args.model, args.series, station_name,
            lhds_per_station[station_name], nodes_per_station[station_name],
            day, len(days), delta_t, gap, args.solver, args.timelimit,
            output_folder, fixed_infra, threads_per_worker,
            master_targets[station_name][day],
            consumption_model, wp2_consumption_json,
            args.autonomous_mode,
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
        for key in ('N_chargers', 'X'):
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
    parser = argparse.ArgumentParser(description='Descomposicion por macrobloque/estacion (y opcionalmente por dia, en paralelo) para carga on-board')
    parser.add_argument('--data_folder', default='data/Escenarios_DCH_costos_nuevos/Costo_fijo/Carga_on_board_fixed_3estaciones_P320kW/')
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
    parser.add_argument(
        '--consumption_model',
        choices=['wp1', 'wp2'],
        default='wp1',
        help='wp1: calculo fisico interno. wp2: consumos y tiempos precalculados desde un JSON por nodo.'
    )
    parser.add_argument(
        '--wp2_consumption_json',
        default=None,
        help='Ruta al JSON de consumos WP2 (relativa a data_folder salvo que sea absoluta). '
             'Si se omite y --consumption_model wp2, se infiere segun la tecnologia de la flota.'
    )
    parser.add_argument(
        '--autonomous_mode',
        action='store_true',
        help='Escenario DET de vehiculos autonomos: durante la colacion el LHD '
             'puede ademas operar (viajar/extraer), no solo cargar o estar '
             'detenido. El cambio de turno (between_shifts) sigue restringido '
             'a cargar-o-detenido en ambos modos.'
    )
    args = parser.parse_args()

    days = [int(d) for d in args.days.split(',')]

    full_model = Reader(join(args.data_folder, args.model), start_in=1)
    series = Series(join(args.data_folder, args.series))
    lhds_per_station, nodes_per_station = get_lhds_and_nodes_per_station(series)

    delta_t = 8 / 60
    gap = 1 / 100

    wp2_consumption_json = None
    if args.consumption_model == 'wp2':
        wp2_consumption_json = resolve_wp2_json_path(args, mine.Mine(full_model))
        print(f"[wp2] usando consumos precalculados desde: {wp2_consumption_json}")

    print("\n=== Fase 0 (maestro): repartiendo target de produccion diario entre macrobloques ===")
    master_targets = compute_master_daily_targets(
        full_model, series, lhds_per_station, nodes_per_station, days, delta_t,
        consumption_model=args.consumption_model, wp2_consumption_json=wp2_consumption_json,
    )

    if args.parallel_days and len(days) > 1:
        run_parallel_by_station_and_day(args, lhds_per_station, nodes_per_station, days, master_targets,
                                         consumption_model=args.consumption_model,
                                         wp2_consumption_json=wp2_consumption_json)
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
            consumption_model=args.consumption_model,
            wp2_consumption_json=wp2_consumption_json,
            autonomous_mode=args.autonomous_mode,
        )


if __name__ == '__main__':
    main()
