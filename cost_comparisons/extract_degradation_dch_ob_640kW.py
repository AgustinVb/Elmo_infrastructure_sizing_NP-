#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae la trayectoria de degradacion de bateria por año del escenario
carga on-board 640kW del caso de estudio MCHS.

Correr con la rama carga_ob_multiano checked out.

Lee las variables crudas del modelo y verifica que cumplan las ecuaciones
de degradacion de ConstraintRules (functions.py):

    (ec. 4) d_y_fade : D[y] = b_bar[y] - gamma_coef * N_ciclos[y]
    (ec. 2) b_y_link : b_bar[y] <= D[y-1] + replace_capacity_fraction*b_max*R[y]
    (ec. 5)            b_bar[y1] = b_max_pool   (fijado en BoundRules)

b_bar[y] es la capacidad al INICIO del año y, D[y] la capacidad al FINAL
del mismo año; sin reemplazo D[y] == b_bar[y+1].

En on-board b_bar/R se modelan fleet-wide, UNA vez para toda la flota de
ELHD (todos comparten la misma capacidad nominal e_max -- ver el guard en
BoundRules), asi que la trayectoria es una sola serie por año.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT = REPO_ROOT / "output" / "DCH_640kW_11anios"
SCENARIO = "carga_on_board_640kW"
OUT_NAME = "dch_ob_640kW_degradation.json"

# Fraccion de b_max que recupera un reemplazo (b_y_link). En on-board viaja
# en parameters.json como replace_capacity_fraction; esto es solo el fallback.
DEFAULT_REPLACE_CAPACITY_FRACTION = 0.3


def load_json(path: Path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def year_series(path: Path) -> dict:
    """Lee un JSON indexado solo por año -- el eje exterior es "y" o "_1"
    segun la variable -- y devuelve {year_int: float}."""
    data = load_json(path)
    if not isinstance(data, dict):
        return {}
    block = data
    for axis in ("y", "_1"):
        if axis in data and isinstance(data[axis], dict):
            block = data[axis]
            break
    out = {}
    for k, v in block.items():
        if isinstance(v, dict):
            continue
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def main():
    params = load_json(ROOT / "parameters.json")

    b_max = float(params.get("b_max_fleet", params.get("b_max_pool", 0.0)))
    min_frac = float(params.get("min_capacity_fraction", 0.0))
    gamma = float(params.get("gamma_coef", 0.0))
    repl_frac = float(params.get("replace_capacity_fraction", DEFAULT_REPLACE_CAPACITY_FRACTION))

    b_bar = year_series(ROOT / "b_bar.json")     # capacidad inicio de año
    d_end = year_series(ROOT / "D.json")         # capacidad fin de año
    n_ciclos = year_series(ROOT / "N_ciclos.json")
    r_repl = year_series(ROOT / "R.json")        # {} si no hubo reemplazos

    years = sorted(b_bar)

    # --- Verificacion ec. 4 (d_y_fade) -------------------------------------
    fade_residuals = {}
    for y in years:
        if y in d_end and y in n_ciclos:
            fade_residuals[y] = d_end[y] - (b_bar[y] - gamma * n_ciclos[y])
    max_fade_res = max((abs(v) for v in fade_residuals.values()), default=0.0)

    # --- Verificacion ec. 2 (b_y_link) -------------------------------------
    link_slacks = {}
    for y in years[1:]:
        prev = d_end.get(y - 1)
        if prev is None:
            continue
        bound = prev + repl_frac * b_max * r_repl.get(y, 0.0)
        link_slacks[y] = bound - b_bar[y]      # >= 0 si se cumple
    min_link_slack = min(link_slacks.values(), default=0.0)

    result = {
        "scenario": SCENARIO,
        "root": str(ROOT),
        "b_max_kwh": b_max,
        "min_capacity_fraction": min_frac,
        "capacity_floor_kwh": min_frac * b_max,
        "gamma_coef": gamma,
        "replace_capacity_fraction": repl_frac,
        "years": years,
        "b_bar_kwh_by_year": {str(y): b_bar[y] for y in years},
        "d_end_kwh_by_year": {str(y): d_end[y] for y in years if y in d_end},
        "soh_start_by_year": {str(y): b_bar[y] / b_max for y in years} if b_max else {},
        "soh_end_by_year": {str(y): d_end[y] / b_max for y in years if y in d_end} if b_max else {},
        "n_ciclos_by_year": {str(y): n_ciclos[y] for y in sorted(n_ciclos)},
        "replacement_years": sorted(y for y, v in r_repl.items() if v > 0.5),
        "checks": {
            "max_abs_residual_d_y_fade_kwh": max_fade_res,
            "min_slack_b_y_link_kwh": min_link_slack,
        },
    }

    out_path = Path(__file__).resolve().parent / OUT_NAME
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("b_bar_kwh_by_year", "d_end_kwh_by_year",
                                   "soh_start_by_year", "soh_end_by_year",
                                   "n_ciclos_by_year")},
                     indent=2, ensure_ascii=False))
    print(f"\nGuardado en {out_path}")


if __name__ == "__main__":
    main()
