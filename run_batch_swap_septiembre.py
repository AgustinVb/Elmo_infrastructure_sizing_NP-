"""
Corre, con descomposicion (run_descomposicion.py --parallel_days) y consumo
WP2, los escenarios de swap de data/Escenarios_DCH_septiembre (Bateria_353 +
Bateria_482, cada uno con Costo_fijo + Costo_variable x 3 potencias) en las
dos ventanas de swap DCH: restringida (solo colacion/entre-turnos) y libre
(cualquier momento salvo mantencion). En total 12 escenarios x 2 ventanas =
24 corridas, cada una de 36 subproblemas (3 estaciones x 12 dias x 2 fases).

Adaptado desde run_batch_swap_agosto.py, agregando el eje Bateria_353/482 que
septiembre tiene y agosto no (agosto solo variaba costo x potencia).

gap/timelimit por defecto: 0.05 / 1200s (no 0.01 / 3600 como en agosto). Con
los defaults de agosto, los subproblemas de dias punta en estaciones 2-3 se
estancan en ~7% de gap sin mejorar y agotan el timelimit dos veces (Fase 1 +
Fase 2) sin ganancia real -- verificado en Bateria_353/Costo_fijo/160kW/
restringida: 2h con 3600/0.01 vs. el mismo resultado esperable en ~40min con
1200/0.05. Siguen siendo overridable por CLI si hace falta mas precision en
algun escenario puntual.

Salida: output/DCH_septiembre/{Bateria_353,Bateria_482}/{Costo_fijo,Costo_variable}/{Swap_restringido,Swap_libre}/<escenario>_Descomp/

Uso:
    python run_batch_swap_septiembre.py
    python run_batch_swap_septiembre.py --dry_run
    python run_batch_swap_septiembre.py --only Bateria_482
    python run_batch_swap_septiembre.py --only 320kW,640kW
    python run_batch_swap_septiembre.py --windows restringida
    python run_batch_swap_septiembre.py --skip_existing false

Cada combinacion (escenario x ventana) se corre en un subproceso separado de
run_descomposicion.py, de forma SECUENCIAL (una a la vez) para no
sobre-suscribir CPU/licencia de Gurobi -- cada subproceso ya usa
--parallel_days internamente con varios workers. Si una combinacion falla
(excepcion del proceso completo, no infactibilidad de un subproblema
individual -- eso ya lo maneja run_descomposicion.py sin abortar), se
registra el error y se sigue con la siguiente. Con --skip_existing true
(default), si el batch se corta a mitad de camino se puede volver a lanzar
tal cual y retoma solo lo que falta.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DATA_ROOT = REPO_ROOT / "data" / "Escenarios_DCH_septiembre"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output" / "DCH_septiembre"

DAYS = "1,32,60,91,121,152,182,213,244,274,305,335"
SOLVER = "gurobi"

BATTERY_DIRS = ["Bateria_353", "Bateria_482"]
COST_DIRS = ["Costo_fijo", "Costo_variable"]

WINDOW_OUTPUT_NAME = {
    "restringida": "Swap_restringido",
    "libre": "Swap_libre",
}


def discover_scenarios(only=None):
    """Enumera (battery_dir, cost_dir, scenario_path) para los escenarios de
    swap, opcionalmente filtrados por --only (nombre de bateria, cost_dir o
    substring del nombre de escenario)."""
    scenarios = []
    for battery_dir in BATTERY_DIRS:
        for cost_dir in COST_DIRS:
            base = DATA_ROOT / battery_dir / cost_dir
            if not base.is_dir():
                continue
            for scenario_path in sorted(base.iterdir()):
                if not scenario_path.is_dir():
                    continue
                if only:
                    name_matches = any(
                        tok.lower() in scenario_path.name.lower()
                        or tok.lower() == cost_dir.lower()
                        or tok.lower() == battery_dir.lower()
                        for tok in only
                    )
                    if not name_matches:
                        continue
                scenarios.append((battery_dir, cost_dir, scenario_path))
    return scenarios


def expected_station_count(scenario_path: Path) -> int:
    """Lee la hoja 'stations' de elmo_data.xlsx para saber cuantas estaciones
    tiene el escenario."""
    import openpyxl
    wb = openpyxl.load_workbook(scenario_path / "elmo_data.xlsx", read_only=True)
    ws = wb["stations"]
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    name_idx = header.index("station_name")
    count = sum(1 for r in rows[2:] if r[name_idx] is not None)
    return count


def is_already_complete(output_folder: Path, n_stations: int, n_days: int) -> bool:
    """True si ya existen summary.txt (fase 2, no _stage1) para todas las
    combinaciones estacion x dia esperadas."""
    if not output_folder.is_dir():
        return False
    expected = n_stations * n_days
    found = 0
    for d in output_folder.iterdir():
        if not d.is_dir() or d.name.endswith("_stage1") or d.name == "combined":
            continue
        if (d / "summary.txt").exists():
            found += 1
    return found >= expected


def read_run_summary(output_folder: Path):
    """Suma 'Total Cost' y cuenta infactibles entre las carpetas de fase 2
    (no _stage1) de output_folder. Devuelve (total_cost, n_ok, n_infeasible)."""
    total_cost = 0.0
    n_ok = 0
    n_infeasible = 0
    if not output_folder.is_dir():
        return total_cost, n_ok, n_infeasible
    for d in sorted(output_folder.iterdir()):
        if not d.is_dir() or d.name.endswith("_stage1") or d.name == "combined":
            continue
        f = d / "summary.txt"
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"Total Cost[^:]*:\s*([\d,\.]+)", text)
        if m:
            total_cost += float(m.group(1).replace(",", ""))
            n_ok += 1
        else:
            n_infeasible += 1
    return total_cost, n_ok, n_infeasible


def run_one(scenario_path: Path, output_folder: Path, window: str, log_path: Path, dry_run: bool,
            gap: float, timelimit: int):
    cmd = [
        sys.executable, str(REPO_ROOT / "run_descomposicion.py"),
        "--data_folder", str(scenario_path) + "/",
        "--output_folder", str(output_folder) + "/",
        "--consumption_model", "wp2",
        "--pause_scheme", "dch",
        "--swap_window", window,
        "--parallel_days", "--days", DAYS,
        "--solver", SOLVER,
        "--gap", str(gap),
        "--timelimit", str(timelimit),
    ]
    print(f"\n=== {scenario_path.name} [{window}] ===")
    print("  ", " ".join(cmd))
    if dry_run:
        return True

    output_folder.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), stdout=logf, stderr=subprocess.STDOUT,
            env={"PYTHONIOENCODING": "utf-8", **os.environ},
        )
    ok = proc.returncode == 0
    print(f"  -> {'OK' if ok else 'FALLO (ver ' + str(log_path) + ')'} (exit={proc.returncode})")
    return ok


def main():
    parser = argparse.ArgumentParser(
        description="Corre con descomposicion + WP2, en ambas ventanas de swap DCH, "
                     "todos los escenarios de swap de data/Escenarios_DCH_septiembre."
    )
    parser.add_argument(
        "--only", default=None,
        help="Filtra escenarios: Bateria_353/Bateria_482, Costo_fijo/Costo_variable, o "
             "substring del nombre de escenario (ej. 320kW), separados por coma."
    )
    parser.add_argument(
        "--windows", default="restringida,libre",
        help="Ventanas de swap a correr, separadas por coma (subset de restringida,libre)."
    )
    parser.add_argument(
        "--skip_existing", default="true", choices=["true", "false"],
        help="Si 'true' (default), no re-corre una combinacion escenario x ventana "
             "cuyo output_folder ya tenga todos los summary.txt de fase 2 -- permite "
             "relanzar el batch tal cual si se corto a mitad de camino."
    )
    parser.add_argument(
        "--gap", type=float, default=0.05,
        help="MIPGap objetivo para Gurobi, pasado a run_descomposicion.py --gap (default: 0.05 = 5%%)."
    )
    parser.add_argument(
        "--timelimit", type=int, default=1200,
        help="Timelimit en segundos por subproblema, pasado a run_descomposicion.py --timelimit (default: 1200)."
    )
    parser.add_argument(
        "--output_root", default=str(DEFAULT_OUTPUT_ROOT),
        help="Carpeta raiz de salida (default: output/DCH_septiembre). Usar otra permite "
             "re-correr la campana sin pisar los resultados anteriores; los logs por corrida "
             "van a output/_batch_logs/<nombre de la carpeta raiz>/."
    )
    parser.add_argument(
        "--dry_run", action="store_true",
        help="Solo imprime los 24 (o menos, segun filtros) comandos que se ejecutarian, sin correr nada."
    )
    args = parser.parse_args()

    only = [tok.strip() for tok in args.only.split(",")] if args.only else None
    windows = [w.strip() for w in args.windows.split(",")]
    skip_existing = args.skip_existing == "true"
    days_list = DAYS.split(",")
    output_root = Path(args.output_root).expanduser().resolve()
    log_root = REPO_ROOT / "output" / "_batch_logs" / output_root.name

    scenarios = discover_scenarios(only=only)
    if not scenarios:
        sys.exit(f"No se encontraron escenarios en {DATA_ROOT} con --only={args.only!r}")

    total_runs = len(scenarios) * len(windows)
    print(f"Escenarios a correr ({len(scenarios)}) x ventanas ({len(windows)}) = {total_runs} corridas:")
    for battery_dir, cost_dir, scenario_path in scenarios:
        print(f"  {battery_dir}/{cost_dir}/{scenario_path.name}")
    print(f"Ventanas: {windows}")
    print(f"skip_existing={skip_existing}  dry_run={args.dry_run}  gap={args.gap}  timelimit={args.timelimit}")
    print(f"output_root={output_root}")
    print(f"log_root={log_root}")

    results = []
    t_batch_start = time.time()
    run_idx = 0

    for battery_dir, cost_dir, scenario_path in scenarios:
        n_stations = expected_station_count(scenario_path)
        for window in windows:
            run_idx += 1
            window_out_name = WINDOW_OUTPUT_NAME[window]
            output_folder = output_root / battery_dir / cost_dir / window_out_name / f"{scenario_path.name}_Descomp"
            log_path = log_root / f"{battery_dir}_{cost_dir}_{scenario_path.name}_{window}.log"

            print(f"\n[{run_idx}/{total_runs}] (quedan {total_runs - run_idx} despues de esta)")

            if skip_existing and not args.dry_run and is_already_complete(output_folder, n_stations, len(days_list)):
                print(f"=== {battery_dir}/{scenario_path.name} [{window}] === SKIP (ya completo en {output_folder})")
                total_cost, n_ok, n_infeasible = read_run_summary(output_folder)
                results.append((battery_dir, cost_dir, scenario_path.name, window, "skip", total_cost, n_ok, n_infeasible))
                continue

            t0 = time.time()
            ok = run_one(scenario_path, output_folder, window, log_path, args.dry_run,
                         gap=args.gap, timelimit=args.timelimit)
            elapsed = time.time() - t0

            if args.dry_run:
                results.append((battery_dir, cost_dir, scenario_path.name, window, "dry_run", None, None, None))
                continue

            total_cost, n_ok, n_infeasible = read_run_summary(output_folder)
            status = "ok" if ok else "error_proceso"
            results.append((battery_dir, cost_dir, scenario_path.name, window, status, total_cost, n_ok, n_infeasible))
            print(f"  costo_total={total_cost:,.2f}  ok={n_ok}  infactibles={n_infeasible}  ({elapsed:.0f}s)")

    if args.dry_run:
        return

    print("\n" + "=" * 110)
    print(f"RESUMEN ({time.time() - t_batch_start:.0f}s total)")
    print("=" * 110)
    print(f"{'bateria':12s} {'cost_dir':14s} {'escenario':40s} {'ventana':12s} {'estado':14s} {'costo_total':>15s} {'ok':>4s} {'infact.':>8s}")
    for battery_dir, cost_dir, name, window, status, total_cost, n_ok, n_infeasible in results:
        cost_str = f"{total_cost:,.2f}" if total_cost is not None else "-"
        print(f"{battery_dir:12s} {cost_dir:14s} {name:40s} {window:12s} {status:14s} {cost_str:>15s} "
              f"{str(n_ok) if n_ok is not None else '-':>4s} {str(n_infeasible) if n_infeasible is not None else '-':>8s}")


if __name__ == "__main__":
    main()
