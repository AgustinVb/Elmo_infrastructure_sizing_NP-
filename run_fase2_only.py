"""
Re-resuelve solo la Fase 2 (infraestructura fija) de run_descomposicion.py
--parallel_days, reutilizando los resultados de Fase 1 (*_stage1) que ya
estan en --output_folder en vez de volver a resolverlos.

Util cuando se corrige un dato de entrada (ej. max_bays_k en la hoja
'stations' del Excel) DESPUES de que Fase 1 ya termino, y no vale la pena
re-resolver los subproblemas de Fase 1 (pueden tardar horas).

La infraestructura fija se recalcula leyendo N_chargers.json/X.json de cada
carpeta "<estacion>_d<dia>_stage1" existente y tomando el maximo por
estacion, exactamente igual que la agregacion original
(run_descomposicion.run_parallel_by_station_and_day).

Ejemplo:
    python run_fase2_only.py \
        --data_folder "data/Taller_DET_Agosto/Resultados_finales/Carga_ob_8lhds_autonomo/" \
        --output_folder "output/Taller_DET_Agosto/Resultados_finales/Carga_ob_8lhds_autonomo/" \
        --days 1,32,60,91,121,152,182,213,244,274,305,335 \
        --solver gurobi --consumption_model wp2 --autonomous_mode
"""
import argparse
import os
from os.path import join
from concurrent.futures import ProcessPoolExecutor, as_completed

from src import mine
from src.io.reader import Reader, Series
from setup import resolve_wp2_json_path
from run_descomposicion import (
    get_lhds_and_nodes_per_station,
    compute_master_daily_targets,
    solve_macrobloque_day,
    _read_station_scalar,
)


def main():
    parser = argparse.ArgumentParser(
        description='Re-resuelve solo Fase 2 (infra fija) reutilizando Fase 1 (*_stage1) ya calculada.'
    )
    parser.add_argument('--data_folder', required=True)
    parser.add_argument('--model', default='elmo_data.xlsx')
    parser.add_argument('--series', default='time_series.xlsx')
    parser.add_argument('--output_folder', required=True)
    parser.add_argument('--solver', default='gurobi')
    parser.add_argument('--days', default='1,32,60,91,121,152,182,213,244,274,305,335',
                         help='Dias significativos separados por coma (deben coincidir con los ya '
                              'resueltos en Fase 1, para que existan las carpetas *_stage1).')
    parser.add_argument('--timelimit', type=int, default=172800)
    parser.add_argument('--n_workers', type=int, default=None)
    parser.add_argument('--consumption_model', choices=['wp1', 'wp2'], default='wp1')
    parser.add_argument('--wp2_consumption_json', default=None)
    parser.add_argument(
        '--autonomous_mode', action='store_true',
        help='Debe coincidir con el flag usado para generar los *_stage1 existentes.'
    )
    args = parser.parse_args()

    days = [int(d) for d in args.days.split(',')]
    delta_t = 8 / 60
    gap = 1 / 100

    full_model = Reader(join(args.data_folder, args.model), start_in=1)
    series = Series(join(args.data_folder, args.series))
    lhds_per_station, nodes_per_station = get_lhds_and_nodes_per_station(series)

    wp2_consumption_json = None
    if args.consumption_model == 'wp2':
        wp2_consumption_json = resolve_wp2_json_path(args, mine.Mine(full_model))
        print(f"[wp2] usando consumos precalculados desde: {wp2_consumption_json}")

    print("\n=== Fase 0 (maestro): repartiendo target de produccion diario entre macrobloques ===")
    master_targets = compute_master_daily_targets(
        full_model, series, lhds_per_station, nodes_per_station, days, delta_t,
        consumption_model=args.consumption_model, wp2_consumption_json=wp2_consumption_json,
    )

    n_jobs = len(lhds_per_station) * len(days)
    n_workers = args.n_workers or min(os.cpu_count() or 1, n_jobs)
    threads_per_worker = max(1, (os.cpu_count() or n_workers) // n_workers)
    print(f"Hilos de Gurobi por worker: {threads_per_worker} (workers={n_workers}, cpus={os.cpu_count()})")

    def make_job(station_name, day, output_folder, fixed_infra):
        return (
            args.data_folder, args.model, args.series, station_name,
            lhds_per_station[station_name], nodes_per_station[station_name],
            day, len(days), delta_t, gap, args.solver, args.timelimit,
            output_folder, fixed_infra, threads_per_worker,
            master_targets[station_name][day],
            args.consumption_model, wp2_consumption_json,
            args.autonomous_mode,
        )

    # ---- Reusar Fase 1 ya calculada: leer N_chargers/X desde *_stage1 ----
    print("\n=== Leyendo infraestructura de Fase 1 ya calculada (*_stage1) ===")
    infra_by_station = {}
    for st in sorted(lhds_per_station):
        agg = infra_by_station.setdefault(st, {})
        for d in days:
            stage1_folder = join(args.output_folder, f"{st}_d{d}_stage1")
            for key in ('N_chargers', 'X'):
                val = _read_station_scalar(stage1_folder, key, st)
                if val is None:
                    print(f"  ADVERTENCIA: no se pudo leer {key} de {stage1_folder}")
                    continue
                agg[key] = max(agg.get(key, val), val)
        print(f"  {st}: {agg}")

    # ---- Fase 2: re-resolver con infraestructura fija, en paralelo ----
    print(f"\n=== Fase 2: {n_jobs} subproblemas (estacion x dia) en paralelo "
          f"({n_workers} workers), infraestructura fija (desde Fase 1 existente) ===")
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


if __name__ == '__main__':
    main()
