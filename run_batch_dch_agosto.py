"""
Corre, con descomposicion (run_descomposicion.py --parallel_days) y consumo
WP2, los 7 escenarios de data/Escenarios_DCH_agosto (Costo_fijo + Costo_variable)
en las dos ventanas de carga DCH: restringida (solo colacion/entre-turnos) y
libre (cualquier momento salvo mantencion). En total 7 x 2 = 14 corridas,
cada una de 72 subproblemas (3 estaciones x 12 dias x 2 fases).

Requisitos verificados antes de escribir este script (ver conversacion):
- Los 7 escenarios ya tienen electric_routes_within_time.json (mismo archivo,
  mismos 290 nodos) y base_hour=9 en la hoja Shifts de time_series.xlsx.
- --pause_scheme dch y --charge_window {restringida,libre} estan validados
  contra output/DCH_taller_julio (Ventana_restringida: -0.14% total; Carga_libre
  (reconstruido): -0.55% total). La diferencia restante frente a julio es la
  eficiencia de bateria real (agregada 2026-07-23), no un error de reproducibilidad.

Salida: output/DCH_agosto/{Costo_fijo,Costo_variable}/{Ventana_restringida,Carga_libre}/<escenario>_Descomp/

Uso:
    python run_batch_dch_agosto.py
    python run_batch_dch_agosto.py --dry_run
    python run_batch_dch_agosto.py --only Costo_fijo
    python run_batch_dch_agosto.py --only P320kW,P640kW
    python run_batch_dch_agosto.py --windows restringida
    python run_batch_dch_agosto.py --skip_existing false

Cada combinacion (escenario x ventana) se corre en un subproceso separado de
run_descomposicion.py, de forma SECUENCIAL (una a la vez) para no
sobre-suscribir CPU/licencia de Gurobi -- cada subproceso ya usa
--parallel_days internamente con varios workers. Si una combinacion falla
(excepcion, no infactibilidad de un subproblema individual -- eso ya lo
maneja run_descomposicion.py sin abortar), se registra el error y se sigue
con la siguiente.
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DATA_ROOT = REPO_ROOT / "data" / "Escenarios_DCH_agosto"
OUTPUT_ROOT = REPO_ROOT / "output" / "DCH_agosto"

DAYS = "1,32,60,91,121,152,182,213,244,274,305,335"
SOLVER = "gurobi"

# (subcarpeta bajo data/Escenarios_DCH_agosto, subcarpeta de salida correspondiente)
COST_DIRS = ["Costo_fijo", "Costo_variable"]

# 'restringida' -> Ventana_restringida (carpeta de salida, sigue la convencion de julio)
# 'libre'       -> Carga_libre
WINDOW_OUTPUT_NAME = {
    "restringida": "Ventana_restringida",
    "libre": "Carga_libre",
}


def discover_scenarios(only=None):
    """Enumera (cost_dir, scenario_path) para los 7 escenarios, opcionalmente
    filtrados por --only (nombre de cost_dir o substring del nombre de escenario)."""
    scenarios = []
    for cost_dir in COST_DIRS:
        base = DATA_ROOT / cost_dir
        if not base.is_dir():
            continue
        for scenario_path in sorted(base.iterdir()):
            if not scenario_path.is_dir():
                continue
            if only:
                name_matches = any(
                    tok.lower() in scenario_path.name.lower() or tok.lower() == cost_dir.lower()
                    for tok in only
                )
                if not name_matches:
                    continue
            scenarios.append((cost_dir, scenario_path))
    return scenarios


def expected_station_count(scenario_path: Path) -> int:
    """Lee la hoja 'stations' de elmo_data.xlsx para saber cuantas estaciones
    tiene el escenario (asume todas activas en el layout, ver max_bays>0)."""
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


def run_one(scenario_path: Path, output_folder: Path, window: str, log_path: Path, dry_run: bool):
    cmd = [
        sys.executable, str(REPO_ROOT / "run_descomposicion.py"),
        "--data_folder", str(scenario_path) + "/",
        "--output_folder", str(output_folder) + "/",
        "--consumption_model", "wp2",
        "--pause_scheme", "dch",
        "--charge_window", window,
        "--parallel_days", "--days", DAYS,
        "--solver", SOLVER,
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
            env={"PYTHONIOENCODING": "utf-8", **__import__("os").environ},
        )
    ok = proc.returncode == 0
    print(f"  -> {'OK' if ok else 'FALLO (ver ' + str(log_path) + ')'} (exit={proc.returncode})")
    return ok


def main():
    parser = argparse.ArgumentParser(
        description="Corre con descomposicion + WP2, en ambas ventanas de carga DCH, "
                     "todos los escenarios de data/Escenarios_DCH_agosto."
    )
    parser.add_argument(
        "--only", default=None,
        help="Filtra escenarios: nombre de cost_dir (Costo_fijo/Costo_variable) o "
             "substring del nombre de escenario (ej. P320kW), separados por coma."
    )
    parser.add_argument(
        "--windows", default="restringida,libre",
        help="Ventanas de carga a correr, separadas por coma (subset de restringida,libre)."
    )
    parser.add_argument(
        "--skip_existing", default="true", choices=["true", "false"],
        help="Si 'true' (default), no re-corre una combinacion escenario x ventana "
             "cuyo output_folder ya tenga todos los summary.txt de fase 2."
    )
    parser.add_argument(
        "--dry_run", action="store_true",
        help="Solo imprime los 14 (o menos, segun filtros) comandos que se ejecutarian, sin correr nada."
    )
    args = parser.parse_args()

    only = [tok.strip() for tok in args.only.split(",")] if args.only else None
    windows = [w.strip() for w in args.windows.split(",")]
    skip_existing = args.skip_existing == "true"
    days_list = DAYS.split(",")

    scenarios = discover_scenarios(only=only)
    if not scenarios:
        sys.exit(f"No se encontraron escenarios en {DATA_ROOT} con --only={args.only!r}")

    print(f"Escenarios a correr ({len(scenarios)}):")
    for cost_dir, scenario_path in scenarios:
        print(f"  {cost_dir}/{scenario_path.name}")
    print(f"Ventanas: {windows}")
    print(f"skip_existing={skip_existing}  dry_run={args.dry_run}")

    results = []
    t_batch_start = time.time()

    for cost_dir, scenario_path in scenarios:
        n_stations = expected_station_count(scenario_path)
        for window in windows:
            window_out_name = WINDOW_OUTPUT_NAME[window]
            output_folder = OUTPUT_ROOT / cost_dir / window_out_name / f"{scenario_path.name}_Descomp"
            log_path = REPO_ROOT / "output" / "_batch_logs" / f"{cost_dir}_{scenario_path.name}_{window}.log"

            if skip_existing and not args.dry_run and is_already_complete(output_folder, n_stations, len(days_list)):
                print(f"\n=== {scenario_path.name} [{window}] === SKIP (ya completo en {output_folder})")
                total_cost, n_ok, n_infeasible = read_run_summary(output_folder)
                results.append((cost_dir, scenario_path.name, window, "skip", total_cost, n_ok, n_infeasible))
                continue

            t0 = time.time()
            ok = run_one(scenario_path, output_folder, window, log_path, args.dry_run)
            elapsed = time.time() - t0

            if args.dry_run:
                results.append((cost_dir, scenario_path.name, window, "dry_run", None, None, None))
                continue

            total_cost, n_ok, n_infeasible = read_run_summary(output_folder)
            status = "ok" if ok else "error_proceso"
            results.append((cost_dir, scenario_path.name, window, status, total_cost, n_ok, n_infeasible))
            print(f"  costo_total={total_cost:,.2f}  ok={n_ok}  infactibles={n_infeasible}  ({elapsed:.0f}s)")

    if args.dry_run:
        return

    print("\n" + "=" * 100)
    print(f"RESUMEN ({time.time() - t_batch_start:.0f}s total)")
    print("=" * 100)
    print(f"{'cost_dir':14s} {'escenario':45s} {'ventana':12s} {'estado':14s} {'costo_total':>15s} {'ok':>4s} {'infact.':>8s}")
    for cost_dir, name, window, status, total_cost, n_ok, n_infeasible in results:
        cost_str = f"{total_cost:,.2f}" if total_cost is not None else "-"
        print(f"{cost_dir:14s} {name:45s} {window:12s} {status:14s} {cost_str:>15s} "
              f"{str(n_ok) if n_ok is not None else '-':>4s} {str(n_infeasible) if n_infeasible is not None else '-':>8s}")


if __name__ == "__main__":
    main()
