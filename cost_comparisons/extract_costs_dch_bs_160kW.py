#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae el desglose de costos (horizonte completo, mejor solucion MIP) del
escenario DCH battery-swapping 160kW (output/DCH_160kW_2dias_wp2, 11 años).

Correr con la rama battery_swapping_multiano checked out (usa el consumer.py
de esa rama). Replica calculate_investment_cost() de consumer.py pero
separando los 4 componentes (estaciones, bahias+grua, cargadores, baterias)
en vez de sumarlos, para poder graficarlos por separado. La subestacion
(calculate_substation_cost) va aparte: es inversion, pero no sale de
calculate_investment_cost().

OJO: el modelo de costos vigente paga la inversion UNA sola vez, en el año de
compra, descontada a valor presente (_year_discount_factor), ya NO anualizada
como en la version anterior de estos scripts (_annuity_factor_year, que ya no
existe en consumer.py).

El total_cost resultante reconcilia exactamente con el "Best objective" de
output/DCH_160kW_2dias_wp2/gurobi.log (7.382.546,07).

Uso:
    python cost_comparisons/extract_costs_dch_bs_160kW.py
    python cost_comparisons/extract_costs_dch_bs_160kW.py --root <carpeta_run>
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE.parent
DEFAULT_RUN = "output/DCH_160kW_2dias_wp2"
OUT_NAME = "dch_bs_160kW_costs.json"

TOL = 1.0  # USD; margen para el chequeo de reconciliacion


def investment_breakdown(c, root: Path):
    """Los mismos terminos de calculate_investment_cost(), sin sumarlos."""
    params_data = c.load_json(c.find_json_in_folder(root, "parameters.json"))

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

        for y in years_sorted:
            y_key = str(y)
            df = c._year_discount_factor(discount_r, y, years_sorted) if has_degradation else 1.0
            cost_stations  += c_station * dx.get(y_key, 0.0) * df
            cost_bays      += (c_bay + c_crane) * dba.get(y_key, 0.0) * df
            cost_chargers  += (charger_cost + c_char_sp) * dch.get(y_key, 0.0) * df
            cost_batteries += (battery_cost + c_bat_sp) * dbt.get(y_key, 0.0) * df

    return {
        "cost_stations": cost_stations,
        "cost_bays": cost_bays,
        "cost_chargers": cost_chargers,
        "cost_batteries": cost_batteries,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                    help="raiz del repo desde donde importar consumer.py (default: la del script)")
    ap.add_argument("--root", type=Path, default=None,
                    help=f"carpeta del run (default: <repo>/{DEFAULT_RUN})")
    args = ap.parse_args()

    repo = args.repo.resolve()
    sys.path.insert(0, str(repo))
    import consumer as c

    root = (args.root or (repo / DEFAULT_RUN)).resolve()

    inv_bd = investment_breakdown(c, root)
    substation_cost = c.calculate_substation_cost(root)
    totals = c.calculate_total_costs(root)

    # El desglose tiene que sumar exactamente lo que reporta consumer.py: si
    # calculate_investment_cost() cambia de formula y este script no, el
    # grafico queda mostrando otro modelo de costos sin avisar.
    inv_total = sum(inv_bd.values())
    inv_consumer = totals.get("investment_cost", 0.0)
    if abs(inv_total - inv_consumer) > TOL:
        raise SystemExit(
            f"Desglose de inversion ({inv_total:,.2f}) != calculate_investment_cost "
            f"({inv_consumer:,.2f}); revisar investment_breakdown()."
        )

    total_cost = (
        inv_total + substation_cost
        + totals.get("peak_power_cost", 0.0)
        + totals.get("grid_energy_cost", 0.0)
        + totals.get("battery_replace_cost", 0.0)
        + totals.get("gen_inv_cost", 0.0) + totals.get("gen_op_cost", 0.0)
        + totals.get("bess_inv_cost", 0.0) + totals.get("bess_op_cost", 0.0)
        + totals.get("penalty_cost", 0.0)
    )
    if abs(total_cost - totals.get("total_cost", 0.0)) > TOL:
        raise SystemExit(
            f"Total del desglose ({total_cost:,.2f}) != total_cost de consumer.py "
            f"({totals.get('total_cost', 0.0):,.2f})."
        )

    result = {
        "scenario": "battery_swapping_160kW",
        "case": "DCH",
        "root": str(root),
        **inv_bd,
        "substation_cost": substation_cost,
        "peak_power_cost": totals.get("peak_power_cost", 0.0),
        "grid_energy_cost": totals.get("grid_energy_cost", 0.0),
        "battery_replace_cost": totals.get("battery_replace_cost", 0.0),
        "gen_inv_cost": totals.get("gen_inv_cost", 0.0),
        "gen_op_cost": totals.get("gen_op_cost", 0.0),
        "bess_inv_cost": totals.get("bess_inv_cost", 0.0),
        "bess_op_cost": totals.get("bess_op_cost", 0.0),
        "penalty_cost": totals.get("penalty_cost", 0.0),
        "investment_cost_total": inv_total + substation_cost,
        "total_cost": total_cost,
    }

    # Costo de recarga con la energia REAL de los swaps (b_bar[y] - B_llegada)
    # dividida por eta_charge, en vez de los bloques fijos que reserva Sv y
    # que paga P_red. Es la medida comparable con on-board, donde P_red ya ES
    # energia de red; sin esta correccion swap aparece consumiendo ~14% mas
    # que on-board para el mismo trabajo minero.
    #
    # total_cost (arriba) sigue siendo el del objetivo y es el que reconcilia
    # con el "Best objective" de Gurobi. total_cost_real_grid_energy NO
    # reconcilia con Gurobi por construccion: corrige una sobre-estimacion de
    # energia comprada que el modelo si pago.
    real_grid_cost = totals.get("real_grid_energy_cost", 0.0)
    result["real_grid_energy_cost"] = real_grid_cost
    result["grid_energy_kwh"] = totals.get("grid_energy_kwh", 0.0)

    # Los kWh reales no salen de calculate_total_costs; vienen del meta de
    # calculate_real_charged_energy_from_swaps, en kWh de dias representativos
    # -> se escalan a año completo para que sean comparables con
    # grid_energy_kwh. De paso se guardan los chequeos que respaldan el
    # numero: ambos contadores deben ser 0.
    try:
        _real, meta, _det = c.calculate_real_charged_energy_from_swaps(root)
        params = c.load_json(c.find_json_in_folder(root, "parameters.json"))
        op_scaling = float(params.get("scaling_factor_op_cost", 1.0))
        result["real_grid_energy_kwh"] = float(meta.get("real_grid_energy_kwh", 0.0)) * op_scaling
        result["real_energy_checks"] = {
            "events": int(meta.get("events", 0)),
            "events_missing_b": int(meta.get("events_missing_b", 0)),
            "events_b_arrival_above_cap": int(meta.get("events_b_arrival_above_cap", 0)),
        }
    except Exception as ex:
        print(f"Advertencia al recoger kWh reales: {ex}")
    result["total_cost_real_grid_energy"] = (
        total_cost - totals.get("grid_energy_cost", 0.0) + real_grid_cost
    )

    out_path = HERE / OUT_NAME
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nGuardado en {out_path}")


if __name__ == "__main__":
    main()
