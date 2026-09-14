"""Validacion estructural de la descomposicion por macrobloque.

No resuelve ningun MIP: comprueba que la particion de la mina sea exacta, que
las cuotas de potencia y las metas de produccion repartidas sumen lo que deben,
que un bloque de macrobloque quede efectivamente restringido a su nave, y que
el bloque del año completo --el que usa la fase backward y el que se construye
cuando el flag esta apagado-- quede intacto.

Uso:  python tests/test_macroblocks.py [carpeta_del_escenario]

Por defecto usa un escenario de varias naves, que es el unico que ejercita el
reparto; con una sola nave el reparto es la identidad y el test igual pasa,
pero no prueba nada interesante.
"""
import os
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import pyomo.environ as pyo
from pyomo.environ import value

from setup import build_mine
from src.optimization.decomposition.driver import infer_exogenous_stations
from src.optimization.decomposition.macroblocks import (
    aggregate_degradation,
    build_macroblocks,
    compute_power_shares,
    coordinate_b_bar,
    split_daily_targets,
)
from src.optimization.decomposition.year_block import YearBlockBuilder

DEFAULT_SCENARIO = (
    "data/Data antigua/Escenarios_DCH_costos_nuevos/Costo_fijo/"
    "Carga_on_board_fixed_3estaciones_P640kW/"
)

FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def size(m):
    nv = sum(len(v) if v.is_indexed() else 1 for v in m.component_objects(pyo.Var, active=True))
    nc = sum(len(c) if c.is_indexed() else 1
             for c in m.component_objects(pyo.Constraint, active=True))
    return nv, nc


def main(scenario):
    args = Namespace(
        data_folder=scenario, model="elmo_data.xlsx", series="time_series.xlsx",
        n_years=2, consumption_model="wp1", autonomous_mode=False,
    )
    _series, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    print(f"escenario: {scenario}\n           {len(years)} años {years}, "
          f"{len(mine_system.get_system_stations())} naves, "
          f"{len(mine_system.get_electric_lhds())} equipos electricos")

    # ------------------------------------------------------------ particion
    mbs = build_macroblocks(mine_system, time_series, years)
    print(f"\n[1] particion en {len(mbs)} macrobloques")
    all_lhds = [l for b in mbs.values() for l in b["lhds"]]
    all_nodes = [n for b in mbs.values() for n in b["nodes"]]
    check(len(all_lhds) == len(set(all_lhds)), "ningun equipo en dos macrobloques")
    check(len(all_nodes) == len(set(all_nodes)), "ningun nodo en dos macrobloques")
    check(set(all_lhds) == set(mine_system.get_electric_lhds()),
          "los macrobloques cubren todos los equipos electricos")
    for st, b in sorted(mbs.items()):
        print(f"       {st}: {len(b['lhds'])} equipos, {len(b['nodes'])} nodos")

    # --------------------------------------------------------------- cuotas
    shares = compute_power_shares(mine_system, time_series, mbs, years)
    print("\n[2] cuotas de potencia: " + ", ".join(f"{k}={v:.4f}" for k, v in sorted(shares.items())))
    check(abs(sum(shares.values()) - 1.0) < 1e-12,
          "las cuotas suman exactamente 1 (sin esto la solucion recombinada "
          "dejaria capacidad sin usar o violaria el limite global)",
          f"suma={sum(shares.values()):.15f}")
    check(all(v > 0 for v in shares.values()), "ninguna cuota es cero o negativa")

    # ----------------------------------------------------- metas repartidas
    targets = split_daily_targets(mine_system, time_series, mbs, years)
    print("\n[3] meta de produccion repartida")
    for y in years:
        repartida = sum(targets[(st, y)] for st in mbs)
        global_target = sum(float(time_series.get_extraction_goal(n, y))
                            for b in mbs.values() for n in b["nodes"])
        check(abs(repartida - global_target) < 1e-6 * max(1.0, global_target),
              f"año {y}: la suma de las metas repartidas es la meta global",
              f"{repartida:,.2f} vs {global_target:,.2f}")

    # ----------------------------------------------- bloque de macrobloque
    print("\n[4] bloque restringido vs bloque del año completo")
    y2 = years[1]
    exo = infer_exogenous_stations(mine_system, time_series)[y2]

    full = YearBlockBuilder(mine_system, time_series, year=y2, is_last_year=True,
                            exogenous_stations=exo)
    station0 = sorted(mbs)[0]
    b_placeholder = value(full.model.b_max_fleet) if mine_system.battery_degradation else 1.0
    mb_block = YearBlockBuilder(
        mine_system, time_series, year=y2, is_last_year=True, exogenous_stations=exo,
        macroblock={
            "station": station0, "lhds": mbs[station0]["lhds"],
            "nodes": mbs[station0]["nodes"], "share": shares[station0],
            "daily_target": {y2: targets[(station0, y2)]},
            "b_bar": b_placeholder, "replace": 0,
        },
    )

    nv_f, nc_f = size(full.model)
    nv_m, nc_m = size(mb_block.model)
    print(f"       año completo: {nv_f:,} variables / {nc_f:,} restricciones")
    print(f"       macrobloque : {nv_m:,} variables / {nc_m:,} restricciones "
          f"({nv_m / nv_f:.0%} / {nc_m / nc_f:.0%})")
    if len(mbs) > 1:
        check(nv_m < nv_f and nc_m < nc_f, "el bloque de macrobloque es mas chico")
    check(len(list(full.model.stations_set)) == len(mine_system.get_system_stations()),
          "el bloque del año completo conserva TODAS las naves (flag apagado intacto)")
    check(list(mb_block.model.stations_set) == [station0],
          "el bloque de macrobloque tiene solo su nave")
    check(set(mb_block.model.lhd_set) <= set(mbs[station0]["lhds"]),
          "el bloque de macrobloque tiene solo sus equipos")

    # ------------------------------------------ coordinacion de la flota
    print("\n[5] degradacion: estado de flota, no repartido")
    if mine_system.battery_degradation is not None:
        check("D" not in [l["state"] for l in mb_block.state_links],
              "el bloque de macrobloque no declara el estado D (lo recompone el forward)")
        check("D" in [l["state"] for l in full.state_links],
              "el bloque del año completo si declara el estado D")
        params = {
            "b_max": value(full.model.b_max_fleet),
            "rho_rep": value(full.model.replace_capacity_fraction),
            "b_upper": value(full.model.B_U),
            "gamma_coef": value(full.model.gamma_coef),
            "n_elhd": value(full.model.n_elhd_bd[y2]),
        }
        d_hat = params["b_max"] * 0.8
        b0 = coordinate_b_bar(params, d_hat, 0)
        b1 = coordinate_b_bar(params, d_hat, 1)
        print(f"       D_hat={d_hat:,.2f} -> b_bar(R=0)={b0:,.2f}  b_bar(R=1)={b1:,.2f}")
        check(b0 <= b1, "reemplazar nunca da menos capacidad que no reemplazar")
        check(b1 <= params["b_upper"] + 1e-9, "b_bar respeta su cota superior fisica")
        d_agg, n_cic = aggregate_degradation(b1, 1000.0, params["n_elhd"], params["gamma_coef"])
        check(d_agg <= b1, "la capacidad al final del año no supera la del inicio",
              f"D={d_agg:,.4f} <= b_bar={b1:,.4f}, N_ciclos={n_cic:,.4f}")
        check(mb_block.model.b_bar[y2].fixed and mb_block.model.R[y2].fixed,
              "b_bar y R quedan fijos en el bloque de macrobloque")
        check(not full.model.b_bar[y2].fixed,
              "b_bar sigue siendo variable en el bloque del año completo")
    else:
        print("       (escenario sin hoja BatteryDegradation: nada que coordinar)")

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO))
