#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae el desglose de costos (horizonte completo, mejor solucion MIP) del
escenario DCH carga on-board 640kW (output/DCH_640kW_11anios, 11 años).

Correr con la rama carga_ob_multiano checked out (usa el consumer.py de esa
rama). Replica calculate_investment_cost() de consumer.py (version on-board)
pero separando estaciones vs bahias vs cargadores en vez de sumarlos. La
subestacion (calculate_substation_cost) va aparte: es inversion, pero no sale
de calculate_investment_cost().

Si el run vive en otra rama que la que esta checked out, se puede sacar a una
carpeta aparte y apuntar ahi los dos flags:

    git archive carga_ob_multiano consumer.py output/DCH_640kW_11anios | tar -x -C <dir>
    python cost_comparisons/extract_costs_dch_ob_640kW.py \
        --repo <dir> --root <dir>/output/DCH_640kW_11anios

IMPORTANTE: el costo de reemplazo de bateria NO se toma de
calculate_battery_degradation_metrics() de consumer.py -- esa funcion valoriza
cada reemplazo con len(elhd_set) (la flota completa, constante), mientras el
objetivo usa n_elhd_bd[y], la cantidad de LHD a bateria del año del reemplazo
(ObjectiveRules.battery_replace_cost en functions.py:
n_elhd_bd[y] * R[y] * c_bat_replace * discount_factor(y)). Con la rampa de
electrificacion de DCH (n_elhd_bd va de 1 a 12 y baja a 2) esa diferencia son
+448.723 USD de mas en el año 9. Aca se recalcula con la formula real para que
el total reconcilie con el "Best objective" de
output/DCH_640kW_11anios/gurobi.log (9.951.146,33).
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE.parent
DEFAULT_RUN = "output/DCH_640kW_11anios"
OUT_NAME = "dch_ob_640kW_costs.json"

TOL = 1.0  # USD; margen para el chequeo de reconciliacion


def _station_param(c, params: dict, key: str) -> dict:
    raw = params.get(key, {})
    if isinstance(raw, dict) and "_1" in raw and isinstance(raw["_1"], dict):
        raw = raw["_1"]
    return {str(k): c._as_float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def investment_breakdown(c, root: Path, params: dict):
    """Los mismos terminos de calculate_investment_cost() (rama on-board),
    sin sumarlos. Ahi c_bays_k va agrupado con el costo por cargador porque el
    modelo on-board no tiene Delta_N_bays propia: las bahias se compran junto
    con el cargador (Delta_N_chargers)."""
    charger_cost = c._as_float(params.get("charger_cost", 0.0))
    p_station = _station_param(c, params, "station_cost_k")
    p_bays    = _station_param(c, params, "c_bays_k")
    p_char_sp = _station_param(c, params, "c_charger_space_k")

    delta_x_by_station = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_X.json"))
    delta_n_by_station = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_chargers.json"))
    stations = sorted(set(delta_x_by_station) | set(delta_n_by_station))

    years_sorted = c._get_years_sorted(params)
    has_degradation = c._has_degradation_data(params)
    discount_r = c._get_discount_rate(params) if has_degradation else 0.0

    cost_stations = 0.0
    cost_bays = 0.0       # c_bays_k, ligada a Delta_N_chargers (no hay Delta_N_bays)
    cost_chargers = 0.0   # charger_cost + c_charger_space_k, exclusivamente

    for k in stations:
        dx = delta_x_by_station.get(k, {})
        dn = delta_n_by_station.get(k, {})
        for y in years_sorted:
            y_key = str(y)
            df = c._year_discount_factor(discount_r, y, years_sorted) if has_degradation else 1.0
            cost_stations += p_station.get(k, 0.0) * dx.get(y_key, 0.0) * df
            cost_bays     += p_bays.get(k, 0.0) * dn.get(y_key, 0.0) * df
            cost_chargers += (charger_cost + p_char_sp.get(k, 0.0)) * dn.get(y_key, 0.0) * df

    return {
        "cost_stations": cost_stations,
        "cost_bays": cost_bays,
        "cost_chargers": cost_chargers,
        "cost_batteries": 0.0,
    }


def battery_replace_cost_real(c, root: Path, params: dict) -> float:
    """n_elhd_bd[y] * R[y] * c_bat_replace * discount_factor(y) -- formula
    real de ObjectiveRules.battery_replace_cost en functions.py."""
    n_elhd_bd_raw = params.get("n_elhd_bd", {})
    n_elhd_bd = n_elhd_bd_raw.get("_1", n_elhd_bd_raw) if isinstance(n_elhd_bd_raw, dict) else {}
    c_bat_replace = c._as_float(params.get("c_bat_replace", 0.0))

    r_data = c.load_json(c.find_json_in_folder(root, "R.json"))
    r_years = r_data.get("y", r_data) if isinstance(r_data, dict) else {}

    years_sorted = c._get_years_sorted(params)
    discount_r = c._get_discount_rate(params) if c._has_degradation_data(params) else 0.0

    total = 0.0
    for y_str, active in r_years.items():
        if not active:
            continue
        y = int(float(y_str))
        n = c._as_float(n_elhd_bd.get(str(y), n_elhd_bd.get(y, 0.0)))
        total += n * c_bat_replace * c._year_discount_factor(discount_r, y, years_sorted)

    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                    help="raiz desde donde importar consumer.py (debe ser el de la rama on-board)")
    ap.add_argument("--root", type=Path, default=None,
                    help=f"carpeta del run (default: <repo>/{DEFAULT_RUN})")
    args = ap.parse_args()

    repo = args.repo.resolve()
    sys.path.insert(0, str(repo))
    import consumer as c

    root = (args.root or (repo / DEFAULT_RUN)).resolve()

    params = c.load_json(c.find_json_in_folder(root, "parameters.json"))

    inv_bd = investment_breakdown(c, root, params)
    substation_cost = c.calculate_substation_cost(root)
    totals = c.calculate_total_costs(root)
    battery_replace_cost = battery_replace_cost_real(c, root, params)

    inv_total = sum(inv_bd.values())
    inv_consumer = totals.get("investment_cost", 0.0)
    if abs(inv_total - inv_consumer) > TOL:
        raise SystemExit(
            f"Desglose de inversion ({inv_total:,.2f}) != calculate_investment_cost "
            f"({inv_consumer:,.2f}); revisar investment_breakdown()."
        )

    total_cost = (
        inv_total + substation_cost
        + totals.get("power_cost", 0.0)
        + totals.get("grid_energy_cost", 0.0)
        + battery_replace_cost
        + totals.get("gen_inv_cost", 0.0) + totals.get("gen_op_cost", 0.0)
        + totals.get("bess_inv_cost", 0.0) + totals.get("bess_op_cost", 0.0)
        + totals.get("penalty_cost", 0.0)
    )

    result = {
        "scenario": "carga_on_board_640kW",
        "case": "DCH",
        "root": str(root),
        **inv_bd,
        "substation_cost": substation_cost,
        "peak_power_cost": totals.get("power_cost", 0.0),
        "grid_energy_cost": totals.get("grid_energy_cost", 0.0),
        "battery_replace_cost": battery_replace_cost,
        "battery_replace_cost_from_consumer_deg_metrics_UNUSED": totals.get("battery_replace_cost", 0.0),
        "gen_inv_cost": totals.get("gen_inv_cost", 0.0),
        "gen_op_cost": totals.get("gen_op_cost", 0.0),
        "bess_inv_cost": totals.get("bess_inv_cost", 0.0),
        "bess_op_cost": totals.get("bess_op_cost", 0.0),
        "penalty_cost": totals.get("penalty_cost", 0.0),
        "investment_cost_total": inv_total + substation_cost,
        "total_cost": total_cost,
    }

    out_path = HERE / OUT_NAME
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nGuardado en {out_path}")


if __name__ == "__main__":
    main()
