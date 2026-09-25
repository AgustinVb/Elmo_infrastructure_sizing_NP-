#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Desglose de costos del horizonte completo para los escenarios DET de
Tesis_final (rama battery_swapping_multiano): solo-red contra generacion+BESS.

Reusa consumer.py, que ya replica termino a termino ObjectiveRules.total_cost
de functions.py. Lo unico que agrega este script es abrir investment_cost en
sus cuatro componentes (naves, bahias+grua, cargadores, baterias del pool),
que consumer devuelve sumados y el grafico necesita por separado.

RECONCILIACION OBLIGATORIA. La suma de los terminos se compara contra el
"Best objective" del gurobi.log de la corrida y el script ABORTA si no cuadra.
Es la unica defensa real contra un desglose plausible pero mal: las barras se
apilan consigo mismas sin importar si cada termino esta bien.

Sobre el factor de descuento: year_position() en functions.py es 1-INDEXADA
(.index(y) + 1), asi que el PRIMER año ya viene descontado -- df(y) =
1/(1+r)^(i+1), no ^i. consumer._year_discount_factor lo replica bien, y este
script no recalcula descuentos por su cuenta justamente para no re-introducir
el error. Con ^i el total sale 10% alto (r=0,1) y el apilado igual cuadra
consigo mismo: el unico sintoma es que la reconciliacion falla.

Los JSON de salida del modelo son DISPERSOS (solo valores no nulos): una clave
ausente vale 0, nunca "falta el dato". Se accede siempre con .get(..., 0.0).
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import consumer as c

HERE = Path(__file__).resolve().parent
BASE = REPO_ROOT / "output" / "Resultados_finales_tesis" / "DET"

ESCENARIOS = {
    "p_red":   {"root": BASE / "P_red" / "241kW",   "scenario": "DET_241kW_solo_red"},
    "gen_bat": {"root": BASE / "Gen_Bat" / "241kW", "scenario": "DET_241kW_gen_bess"},
}

# Margen de reconciliacion en USD. Sobre totales de ~3e6 son 3e-9 relativos:
# absorbe el ruido de punto flotante de rearmar el objetivo y nada mas.
TOL_USD = 0.01


def best_objective(root):
    """Ultimo 'Best objective' del gurobi.log: el valor que el solver reporta
    para la solucion que quedo escrita en los JSON."""
    log = root / "gurobi.log"
    if not log.exists():
        raise SystemExit("falta " + str(log))
    hits = re.findall(r"Best objective\s+([0-9.eE+-]+)", log.read_text(errors="replace"))
    if not hits:
        raise SystemExit(str(log) + ": no trae ninguna linea 'Best objective'")
    return float(hits[-1])


def investment_breakdown(root):
    """Abre calculate_investment_cost() de consumer.py en sus componentes, con
    la MISMA aritmetica: pago unico al año de compra, descontado, sin anualizar.
    La suma de los cuatro tiene que dar exactamente investment_cost."""
    params = c.load_json(c.find_json_in_folder(root, "parameters.json"))

    charger_cost = c._as_float(params.get("charger_cost", 0.0))
    battery_cost = c._as_float(params.get("battery_cost", 0.0))
    station_cost_k = c._unwrap_named_tree(params.get("station_cost_k", {}))
    c_bays_k = c._unwrap_named_tree(params.get("c_bays_k", {}))
    c_crane_k = c._unwrap_named_tree(params.get("c_crane_k", {}))
    c_charger_space = c._unwrap_named_tree(params.get("c_charger_space_k", {}))
    c_battery_space = c._unwrap_named_tree(params.get("c_battery_space_k", {}))

    dx_k = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_X.json"))
    dba_k = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_bays.json"))
    dch_k = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_chargers.json"))
    dbt_k = c._indexed_vars_by_year(c.find_json_in_folder(root, "Delta_N_batteries.json"))

    stations = sorted(set(dx_k) | set(dba_k) | set(dch_k) | set(dbt_k))
    years_sorted = c._get_years_sorted(params)
    discount_r = c._get_discount_rate(params) if c._has_degradation_data(params) else 0.0

    out = {"cost_stations": 0.0, "cost_bays": 0.0,
           "cost_chargers": 0.0, "cost_batteries": 0.0}

    for k in stations:
        def par(tree):
            return c._as_float(tree.get(k, 0.0)) if isinstance(tree, dict) else 0.0

        c_station, c_bay, c_crane = par(station_cost_k), par(c_bays_k), par(c_crane_k)
        c_char_sp, c_bat_sp = par(c_charger_space), par(c_battery_space)

        for y in years_sorted:
            yk = str(y)
            df = c._year_discount_factor(discount_r, y, years_sorted)
            out["cost_stations"] += c_station * dx_k.get(k, {}).get(yk, 0.0) * df
            out["cost_bays"] += (c_bay + c_crane) * dba_k.get(k, {}).get(yk, 0.0) * df
            out["cost_chargers"] += (charger_cost + c_char_sp) * dch_k.get(k, {}).get(yk, 0.0) * df
            out["cost_batteries"] += (battery_cost + c_bat_sp) * dbt_k.get(k, {}).get(yk, 0.0) * df

    return out


def instalado(root):
    """Potencia instalada. G_g y H estan en UNIDADES, no en kW: la potencia es
    G_g * p_max_g y H * p_max_h, y la energia del BESS es H * a_max_h."""
    params = c.load_json(c.find_json_in_folder(root, "parameters.json"))
    p_max_g = c._unwrap_named_tree(params.get("p_max_g", {})) or {}
    p_max_h = c._unwrap_named_tree(params.get("p_max_h", {})) or {}
    a_max_h = c._unwrap_named_tree(params.get("a_max_h", {})) or {}

    def plano(path, unico=None):
        """Devuelve {nombre: unidades}. Con una sola unidad de almacenamiento el
        modelo escribe H.json como un ESCALAR pelado (0.958...), sin el nivel
        'h' ni el nombre; `unico` da el nombre que le corresponde en ese caso."""
        data = c.load_json(path) if path and Path(path).exists() else {}
        if isinstance(data, (int, float)):
            return {unico: float(data)} if unico else {}
        if not isinstance(data, dict):
            return {}
        for eje in ("g", "h", "_1"):
            if eje in data and isinstance(data[eje], dict):
                return {k: c._as_float(v) for k, v in data[eje].items()
                        if not isinstance(v, dict)}
        return {k: c._as_float(v) for k, v in data.items() if not isinstance(v, dict)}

    # Con un solo elemento en storage_set el nombre no viaja en H.json; sale de
    # los parametros, que si lo traen indexado.
    nombre_bess = next(iter(p_max_h), None)
    g = plano(c.find_json_in_folder(root, "G_g.json"))
    h = plano(c.find_json_in_folder(root, "H.json"), unico=nombre_bess)
    return {
        "gen_kw": {k: v * c._as_float(p_max_g.get(k, 0.0)) for k, v in g.items()},
        "bess_kw": {k: v * c._as_float(p_max_h.get(k, 0.0)) for k, v in h.items()},
        "bess_kwh": {k: v * c._as_float(a_max_h.get(k, 0.0)) for k, v in h.items()},
    }


def main():
    for clave, cfg in ESCENARIOS.items():
        root = cfg["root"]
        if not root.exists():
            raise SystemExit("no existe la carpeta de resultados " + str(root))

        obj = best_objective(root)
        tot = c.calculate_total_costs(root)
        inv_bd = investment_breakdown(root)

        # El desglose tiene que reproducir el agregado de consumer; si no,
        # alguna componente quedo fuera del reparto.
        inv_sum = sum(inv_bd.values())
        inv_ref = tot.get("investment_cost", 0.0)
        if abs(inv_sum - inv_ref) > TOL_USD:
            raise SystemExit(
                "{}: el desglose de inversion ({:,.4f}) no reproduce "
                "investment_cost ({:,.4f})".format(clave, inv_sum, inv_ref))

        filas = dict(inv_bd)
        filas.update({
            "substation_cost": tot.get("substation_cost", 0.0),
            "gen_inv_cost": tot.get("gen_inv_cost", 0.0),
            "bess_inv_cost": tot.get("bess_inv_cost", 0.0),
            "peak_power_cost": tot.get("peak_power_cost", 0.0),
            "grid_energy_cost": tot.get("grid_energy_cost", 0.0),
            "gen_op_cost": tot.get("gen_op_cost", 0.0),
            "bess_op_cost": tot.get("bess_op_cost", 0.0),
            "battery_replace_cost": tot.get("battery_replace_cost", 0.0),
        })

        # RECONCILIACION: los diez terminos contra el objetivo del solver.
        suma = sum(filas.values())
        if abs(suma - obj) > TOL_USD:
            raise SystemExit(
                "{}: el desglose NO reconcilia.\n"
                "  suma de terminos : {:,.4f}\n"
                "  Best objective   : {:,.4f}\n"
                "  desvio           : {:,.4f} USD\n"
                "  Revisa ObjectiveRules.total_cost: falta un termino, o el "
                "factor de descuento quedo corrido un año.".format(
                    clave, suma, obj, suma - obj))

        resultado = {"scenario": cfg["scenario"],
                     "root": str(root.relative_to(REPO_ROOT)).replace("\\", "/")}
        resultado.update(filas)
        resultado.update({
            "total_cost": suma,
            "best_objective_gurobi": obj,
            "desvio_reconciliacion_usd": suma - obj,
            "grid_energy_kwh": tot.get("grid_energy_kwh", 0.0),
            "instalado": instalado(root),
        })

        out = HERE / ("det_tesis_" + clave + "_costs.json")
        out.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")
        print("[{}] reconcilia: suma {:,.2f} contra Best objective {:,.2f} "
              "(desvio {:+.4f} USD) -> {}".format(clave, suma, obj, suma - obj, out.name))


if __name__ == "__main__":
    main()
