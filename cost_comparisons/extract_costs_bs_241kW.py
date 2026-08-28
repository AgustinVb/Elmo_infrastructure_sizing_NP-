#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae el desglose de costos (horizonte completo, mejor solucion MIP) del
escenario battery-swapping 241kW.

Correr con la rama battery_swapping_multiano checked out (usa el consumer.py
de esa rama). Replica calculate_investment_cost() de consumer.py pero
separando los 4 componentes (estaciones, bahias+grua, cargadores, baterias)
en vez de sumarlos, para poder graficarlos por separado.

El total_cost resultante reconcilia exactamente con el "Best objective" de
output/Monolithic_241kW_2dias_mccormick/gurobi.log.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import consumer as c

ROOT = REPO_ROOT / "output" / "Monolithic_241kW_2dias_mccormick"


def investment_breakdown(root: Path):
    params_path = c.find_json_in_folder(root, "parameters.json")
    params_data = c.load_json(params_path)

    charger_cost = c._as_float(params_data.get("charger_cost", 0.0))
    battery_cost = c._as_float(params_data.get("battery_cost", 0.0))
    station_cost_k  = c._unwrap_named_tree(params_data.get("station_cost_k",   {}))
    c_bays_k        = c._unwrap_named_tree(params_data.get("c_bays_k",         {}))
    c_crane_k       = c._unwrap_named_tree(params_data.get("c_crane_k",        {}))
    c_charger_space = c._unwrap_named_tree(params_data.get("c_charger_space_k", {}))
    c_battery_space = c._unwrap_named_tree(params_data.get("c_battery_space_k", {}))

    delta_x_by_station    = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_X.json"))
    delta_bays_by_station = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_bays.json"))
    delta_ch_by_station   = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_chargers.json"))
    delta_bat_by_station  = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_batteries.json"))

    stations = sorted(
        set(delta_x_by_station) | set(delta_bays_by_station)
        | set(delta_ch_by_station) | set(delta_bat_by_station)
    )

    years_sorted = c._get_years_sorted(params_data)
    n_years = len(years_sorted)
    has_degradation = c._has_degradation_data(params_data)
    discount_r = c._get_discount_rate(params_data) if has_degradation else 0.0

    cost_stations = 0.0
    cost_bays = 0.0
    cost_chargers = 0.0
    cost_batteries = 0.0

    for k in stations:
        c_station = c._as_float(station_cost_k.get(k, 0.0))  if isinstance(station_cost_k,  dict) else 0.0
        c_bay     = c._as_float(c_bays_k.get(k, 0.0))        if isinstance(c_bays_k,        dict) else 0.0
        c_crane   = c._as_float(c_crane_k.get(k, 0.0))       if isinstance(c_crane_k,       dict) else 0.0
        c_char_sp = c._as_float(c_charger_space.get(k, 0.0)) if isinstance(c_charger_space, dict) else 0.0
        c_bat_sp  = c._as_float(c_battery_space.get(k, 0.0)) if isinstance(c_battery_space, dict) else 0.0

        dx  = delta_x_by_station.get(k, {})
        dba = delta_bays_by_station.get(k, {})
        dch = delta_ch_by_station.get(k, {})
        dbt = delta_bat_by_station.get(k, {})

        for pos_y, y in enumerate(years_sorted, start=1):
            y_key = str(y)
            af = c._annuity_factor_year(discount_r, pos_y, n_years) if has_degradation else 1.0
            cost_stations  += c_station * dx.get(y_key, 0.0) * af
            cost_bays      += (c_bay + c_crane) * dba.get(y_key, 0.0) * af
            cost_chargers  += (charger_cost + c_char_sp) * dch.get(y_key, 0.0) * af
            cost_batteries += (battery_cost + c_bat_sp) * dbt.get(y_key, 0.0) * af

    return {
        "cost_stations": cost_stations,
        "cost_bays": cost_bays,
        "cost_chargers": cost_chargers,
        "cost_batteries": cost_batteries,
    }


def main():
    inv_bd = investment_breakdown(ROOT)
    totals = c.calculate_total_costs(ROOT)

    result = {
        "scenario": "battery_swapping_241kW",
        "root": str(ROOT),
        **inv_bd,
        "peak_power_cost": totals.get("peak_power_cost", 0.0),
        "grid_energy_cost": totals.get("grid_energy_cost", 0.0),
        "battery_replace_cost": totals.get("battery_replace_cost", 0.0),
        "gen_inv_cost": totals.get("gen_inv_cost", 0.0),
        "gen_op_cost": totals.get("gen_op_cost", 0.0),
        "bess_inv_cost": totals.get("bess_inv_cost", 0.0),
        "bess_op_cost": totals.get("bess_op_cost", 0.0),
        "penalty_cost": totals.get("penalty_cost", 0.0),
        "investment_cost_total": totals.get("investment_cost", 0.0),
        "total_cost": totals.get("total_cost", 0.0),
    }

    out_path = Path(__file__).resolve().parent / "bs_241kW_costs.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nGuardado en {out_path}")


if __name__ == "__main__":
    main()
