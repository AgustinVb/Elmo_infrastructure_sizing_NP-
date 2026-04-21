from __future__ import annotations

import argparse
import os
import time
from typing import List

from src import mine
from src.io.reader import Reader, Series
from src.optimization import OptimizationModel
from src.time_series import timeseries

DEFAULT_DAYS = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]


def parse_days(days_raw: str) -> List[int]:
    if not days_raw.strip():
        return DEFAULT_DAYS
    return [int(x.strip()) for x in days_raw.split(",") if x.strip()]


def build_case(data_folder: str, model_name: str, series_name: str, day: int, delta_t: float):
    model_path = os.path.join(data_folder, model_name)
    series_path = os.path.join(data_folder, series_name)

    model = Reader(model_path, start_in=1)
    series = Series(series_path)
    time_series = timeseries.Timeseries(series, [day], delta_t)

    mine_system = mine.Mine(model)
    time_series.mapper["Trips"] = time_series.get_trips(mine_system)

    return mine_system, time_series


def run_one_day(args, day: int) -> bool:
    day_output = os.path.join(args.output_root, f"day_{day:03d}")
    os.makedirs(day_output, exist_ok=True)

    print("=" * 90)
    print(f"Running day {day} -> {day_output}")
    print("=" * 90)

    t0 = time.time()
    try:
        mine_system, time_series = build_case(
            data_folder=args.data_folder,
            model_name=args.model,
            series_name=args.series,
            day=day,
            delta_t=args.delta_t,
        )

        OptimizationModel(
            mine_system=mine_system,
            time_series=time_series,
            gap=args.gap,
            solver_name=args.solver,
            output_folder=day_output,
            timelimit=args.timelimit,
            y_init_path=args.y_init_path,
            relax_integrality=args.relax_integrality,
        )

        elapsed = time.time() - t0
        print(f"[OK] Day {day} finished in {elapsed:.2f}s")
        return True
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"[ERROR] Day {day} failed after {elapsed:.2f}s: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Run 12 representative days as independent optimization runs."
    )
    parser.add_argument(
        "--data_folder",
        default=r"data\Escenarios_DCH\Costo_variable\Swap_variable_3estaciones_2C",
        help="Input folder containing elmo_data.xlsx and time_series.xlsx",
    )
    parser.add_argument("--model", default="elmo_data.xlsx")
    parser.add_argument("--series", default="time_series.xlsx")

    parser.add_argument(
        "--output_root",
        default=r"output\Escenarios_DCH\DCH_3estaciones_Swap_variable_2C\12_diasxseparado",
        help="Root output folder where each day will be saved in a separate subfolder",
    )

    parser.add_argument("--solver", default="gurobi")
    parser.add_argument("--gap", type=float, default=0.05, help="MIP gap (relative)")
    parser.add_argument(
        "--timelimit",
        type=int,
        default=3600,
        help="Max solve time per day in seconds",
    )
    parser.add_argument(
        "--delta_t",
        type=float,
        default=8.0 / 60.0,
        help="Time-step size in hours",
    )
    parser.add_argument(
        "--days",
        default=",".join(str(d) for d in DEFAULT_DAYS),
        help="Comma-separated day list, e.g., 1,32,60",
    )
    parser.add_argument(
        "--y_init_path",
        default=None,
        help="Optional path to Y.json for warm start",
    )
    parser.add_argument(
        "--relax_integrality",
        action="store_true",
        help="Solve the linear relaxation instead of the MIP",
    )

    args = parser.parse_args()
    args.days = parse_days(args.days)

    print("Configuration:")
    print(f"  data_folder: {args.data_folder}")
    print(f"  output_root: {args.output_root}")
    print(f"  solver:      {args.solver}")
    print(f"  gap:         {args.gap}")
    print(f"  timelimit:   {args.timelimit}")
    print(f"  delta_t:     {args.delta_t}")
    print(f"  days:        {args.days}")

    ok_days = []
    fail_days = []

    for day in args.days:
        success = run_one_day(args, day)
        if success:
            ok_days.append(day)
        else:
            fail_days.append(day)

    print("\nSummary")
    print(f"  Successful days: {ok_days}")
    print(f"  Failed days:     {fail_days}")


if __name__ == "__main__":
    main()
