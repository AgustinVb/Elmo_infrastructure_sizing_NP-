"""Test: 320kW variable, station_2 con 2 cargadores, station_1/3 con 1."""
import os
from os.path import join
from concurrent.futures import ProcessPoolExecutor, as_completed

from run_macrobloques_decomposicion import (
    Reader, Series, get_lhds_and_nodes_per_station,
    compute_master_daily_targets, solve_macrobloque_day,
)

DATA_FOLDER = "data/Escenarios_DCH_costos_nuevos/Costo_variable/Carga_on_board_variable_3estaciones_P320kW/"
OUTPUT_FOLDER = "output/DCH_taller_julio/Costo_variable/Carga_on_board_320kW_st2_2ch/"
DAYS = [1,32,60,91,121,152,182,213,244,274,305,335]
DELTA_T = 8/60
GAP = 1/100
N_WORKERS = 6

INFRA = {
    'station_1': {'N_chargers': 1, 'X': 1},
    'station_2': {'N_chargers': 2, 'X': 1},
    'station_3': {'N_chargers': 1, 'X': 1},
}


def main():
    threads_per_worker = max(1, (os.cpu_count() or N_WORKERS) // N_WORKERS)

    full_model = Reader(join(DATA_FOLDER, "elmo_data.xlsx"), start_in=1)
    series = Series(join(DATA_FOLDER, "time_series.xlsx"))
    lhds_per_station, nodes_per_station = get_lhds_and_nodes_per_station(series)

    print("=== Fase 0: repartiendo targets ===")
    master_targets = compute_master_daily_targets(
        full_model, series, lhds_per_station, nodes_per_station, DAYS, DELTA_T,
    )

    jobs = [
        (DATA_FOLDER, "elmo_data.xlsx", "time_series.xlsx",
         st, lhds_per_station[st], nodes_per_station[st],
         d, len(DAYS), DELTA_T, GAP, "gurobi", 172800,
         join(OUTPUT_FOLDER, f"{st}_d{d}"),
         INFRA[st], threads_per_worker,
         master_targets[st][d])
        for st in sorted(lhds_per_station)
        for d in DAYS
    ]

    print(f"\n=== Resolviendo 36 subproblemas ({N_WORKERS} workers) ===")
    print("Infra: station_1=1ch, station_2=2ch, station_3=1ch")
    with ProcessPoolExecutor(max_workers=N_WORKERS) as ex:
        futures = [ex.submit(solve_macrobloque_day, job) for job in jobs]
        for fut in as_completed(futures):
            fut.result()

    print(f"\nListo -> {OUTPUT_FOLDER}")


if __name__ == '__main__':
    main()
