#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae el desglose de costos (horizonte completo, mejor solucion MIP) del
escenario carga on-board 960kW.

Correr con la rama carga_ob_multiano checked out (usa el consumer.py de esa
rama). Replica calculate_investment_cost() de consumer.py (version on-board)
pero separando estaciones vs (bahias+cargadores) en vez de sumarlos.

IMPORTANTE: el costo de reemplazo de bateria NO se toma de
calculate_battery_degradation_metrics() de consumer.py -- esa funcion
calcula un costo basado en degradacion por EFC que no coincide con la
formula real del objetivo (ObjectiveRules.battery_replace_cost en
functions.py: n_elhd_bd[y] * R[y] * c_bat_replace * discount_factor(y)).
Aca se recalcula con la formula real para que el total reconciecle con el
"Best objective" de output/DET_960kW_2dias_bilineal/gurobi.log.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import consumer as c

ROOT = REPO_ROOT / "output" / "DET_960kW_2dias_bilineal"


def _station_param(params: dict, key: str) -> dict:
    raw = params.get(key, {})
    if isinstance(raw, dict) and "_1" in raw and isinstance(raw["_1"], dict):
        raw = raw["_1"]
    return {str(k): c._as_float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def investment_breakdown(root: Path, params: dict):
    charger_cost = c._as_float(params.get("charger_cost", 0.0))
    p_station = _station_param(params, "station_cost_k")
    p_bays    = _station_param(params, "c_bays_k")
    p_char_sp = _station_param(params, "c_charger_space_k")

    delta_x_by_station = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_X.json"))
    delta_n_by_station = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_chargers.json"))
    stations = sorted(set(delta_x_by_station) | set(delta_n_by_station))

    years_sorted = c._get_years_sorted(params)
    n_years = len(years_sorted)
    has_degradation = c._has_degradation_data(params)
    discount_r = c._get_discount_rate(params) if has_degradation else 0.0

    cost_stations = 0.0
    cost_bays = 0.0       # c_bays_k, aunque el modelo on-board la liga a Delta_N_chargers (no hay Delta_N_bays propia)
    cost_chargers = 0.0   # charger_cost + c_charger_space_k, exclusivamente

    for k in stations:
        dx = delta_x_by_station.get(k, {})
        dn = delta_n_by_station.get(k, {})
        for pos_y, y in enumerate(years_sorted, start=1):
            y_key = str(y)
            af = c._annuity_factor_year(discount_r, pos_y, n_years) if has_degradation else 1.0
            cost_stations += p_station.get(k, 0.0) * dx.get(y_key, 0.0) * af
            cost_bays     += p_bays.get(k, 0.0) * dn.get(y_key, 0.0) * af
            cost_chargers += (charger_cost + p_char_sp.get(k, 0.0)) * dn.get(y_key, 0.0) * af

    return {
        "cost_stations": cost_stations,
        "cost_bays": cost_bays,
        "cost_chargers": cost_chargers,
        "cost_batteries": 0.0,
    }


def battery_replace_cost_real(root: Path, params: dict) -> float:
    """n_elhd_bd[y] * R[y] * c_bat_replace * discount_factor(y) -- formula
    real de ObjectiveRules.battery_replace_cost en functions.py."""
    n_elhd_bd_raw = params.get("n_elhd_bd", {})
    n_elhd_bd = n_elhd_bd_raw.get("_1", n_elhd_bd_raw) if isinstance(n_elhd_bd_raw, dict) else {}
    c_bat_replace = c._as_float(params.get("c_bat_replace", 0.0))

    r_path = c.find_json_in_folder(root, "R.json")
    r_data = c.load_json(r_path)
    r_years = r_data.get("y", r_data) if isinstance(r_data, dict) else {}

    years_sorted = c._get_years_sorted(params)
    discount_r = c._get_discount_rate(params)

    total = 0.0
    for y_str, active in r_years.items():
        if not active:
            continue
        y = int(y_str)
        n = c._as_float(n_elhd_bd.get(y_str, n_elhd_bd.get(y, 0.0)))
        df = c._year_discount_factor(discount_r, y, years_sorted)
        total += n * c_bat_replace * df

    return total


def main():
    params_path = c.find_json_in_folder(ROOT, "parameters.json")
    params = c.load_json(params_path)

    inv_bd = investment_breakdown(ROOT, params)
    totals = c.calculate_total_costs(ROOT)
    battery_replace_cost = battery_replace_cost_real(ROOT, params)

    total_cost = (
        inv_bd["cost_stations"] + inv_bd["cost_bays"] + inv_bd["cost_chargers"] + inv_bd["cost_batteries"]
        + totals.get("power_cost", 0.0)
        + totals.get("grid_energy_cost", 0.0)
        + totals.get("penalty_cost", 0.0)
        + totals.get("gen_inv_cost", 0.0) + totals.get("gen_op_cost", 0.0)
        + totals.get("bess_inv_cost", 0.0) + totals.get("bess_op_cost", 0.0)
        + battery_replace_cost
    )

    result = {
        "scenario": "carga_on_board_960kW",
        "root": str(ROOT),
        **inv_bd,
        "peak_power_cost": totals.get("power_cost", 0.0),
        "grid_energy_cost": totals.get("grid_energy_cost", 0.0),
        "battery_replace_cost": battery_replace_cost,
        "battery_replace_cost_from_consumer_deg_metrics_UNUSED": totals.get("battery_replace_cost", 0.0),
        "gen_inv_cost": totals.get("gen_inv_cost", 0.0),
        "gen_op_cost": totals.get("gen_op_cost", 0.0),
        "bess_inv_cost": totals.get("bess_inv_cost", 0.0),
        "bess_op_cost": totals.get("bess_op_cost", 0.0),
        "penalty_cost": totals.get("penalty_cost", 0.0),
        "investment_cost_total": inv_bd["cost_stations"] + inv_bd["cost_bays"] + inv_bd["cost_chargers"] + inv_bd["cost_batteries"],
        "total_cost": total_cost,
    }

    out_path = Path(__file__).resolve().parent / "ob_960kW_costs.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nGuardado en {out_path}")


if __name__ == "__main__":
    main()
