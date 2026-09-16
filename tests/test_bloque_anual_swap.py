"""M1 -- el bloque de un año reproduce el monolitico de un año.

Con horizonte de UN año no hay nada que heredar ni ningun costo futuro, asi que
el subproblema del año tiene que ser el mismo problema que el monolitico de ese
año. Lo unico que puede diferir es la inversion en estaciones, que el modo
descompuesto omite por diseño (X es exogeno), asi que se compara descontandola.

Tambien comprueba que el registro de estados que consumen cuts.py/passes.py
(`state_links`) sea el esperado, y que los estados heredados entren de verdad al
modelo (que `set_heritage` mueva la solucion).

Uso:  python tests/test_bloque_anual_swap.py [carpeta_del_escenario]
"""
import os
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value

from setup import build_mine
from src.optimization.decomposition.year_block import YearBlockBuilder
from src.optimization.functions import (
    BoundRules, ConstraintRules, ObjectiveRules, OptParameters, OptSets,
)

DEFAULT_SCENARIO = "data/DCH/160kW_2dias/"
GAP = 0.005
TIMELIMIT = 600
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def solve(m, label):
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["MIPGap"] = GAP
    opt.options["TimeLimit"] = TIMELIMIT
    opt.options["OutputFlag"] = 0
    res = opt.solve(m, load_solutions=True)
    print(f"       {label}: {res.solver.termination_condition}, obj = {value(m.obj):,.2f}")
    return value(m.obj)


def build_monolithic(mine_system, time_series, years):
    """Modelo de referencia: se arma SIN years_override.

    Pasar years_override --aunque sea el horizonte completo-- marca
    is_decomposed_block y por lo tanto omite las cuatro link_*_stock, dejando el
    stock desacoplado de los Delta: el solver pondria N_bays=4 con Delta=0 y sin
    pagar nada. years_override existe solo para los bloques de un año, donde esa
    acumulacion la agrega YearBlockBuilder con la copia local.
    """
    model = pyo.ConcreteModel()
    OptSets(mine_system, time_series).build_sets(model)
    OptParameters(mine_system, time_series).build_parameters(model)
    BoundRules(mine_system, time_series).build_all_variables(model)
    ConstraintRules(mine_system, time_series,
                    mccormick_degradation=True).build_all_constraints(model)
    rules = ObjectiveRules(mine_system, time_series)
    model.obj = pyo.Objective(rule=rules.total_cost, sense=pyo.minimize)
    return model, rules


def main(scenario):
    args = Namespace(
        data_folder=scenario, model="elmo_data.xlsx", series="time_series.xlsx",
        consumption_model="wp2", wp2_consumption_json=None, n_years=1,
    )
    _series, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    y1 = years[0]
    stations = list(mine_system.get_system_stations())
    print(f"escenario: {scenario}  (horizonte de {len(years)} año, {len(stations)} naves)")

    # X exogeno: se construye toda nave con al menos un equipo asignado.
    time_series.get_station_assignment(mine_system.get_system_lhds())
    time_series.get_elhd_at_station(stations)
    per_station = time_series.mapper.get('elhd_per_station', {})
    exo = {k: (1 if per_station.get(k) else 0) for k in stations}
    print(f"       X exogeno: {exo}")

    print("\n[1] state_links del bloque")
    block = YearBlockBuilder(mine_system, time_series, year=y1, is_last_year=True,
                             exogenous_stations=exo)
    names = sorted(l["state"] for l in block.state_links)
    print(f"       {names}")
    esperados = sorted(["N_bays", "N_chargers", "N_batteries", "N_max_k", "G", "H", "D"])
    esperados = [n for n in esperados
                 if not (n == "G" and len(list(block.model.gen_set)) == 0)
                 and not (n == "H" and len(list(block.model.storage_set)) == 0)
                 and not (n == "D" and mine_system.battery_degradation is None)]
    check(names == esperados, "el vector de estado es el esperado", f"faltan/sobran: "
          f"{set(esperados) ^ set(names)}")
    check("X" not in names, "X no es estado (es exogeno en modo descompuesto)")

    print("\n[2] condiciones de borde del primer año")
    # Las familias stock SI declaran heredado en y1, con valor 0: es como se
    # codifica el arranque greenfield (stock = 0 + Delta), equivalente a la rama
    # y == first_year de link_*_stock en el monolitico. Los estados que se
    # deciden una sola vez (N_max_k/G/H) y la degradacion no deben tenerlo: en
    # y1 son decision libre, no algo recibido.
    for link in block.state_links:
        if link["kind"] == "global_once" or link["state"] == "D":
            check(link["hat"] is None,
                  f"el estado {link['state']} se decide en y1, sin heredado")
        else:
            check(link["hat"] is not None,
                  f"el estado {link['state']} arranca de un stock heredado nulo")
    check(not hasattr(block.model, "b_y_link_local"),
          "no hay b_y_link_local en el primer año (rige la condicion de borde)")
    check(block.model.b_bar[y1].fixed if mine_system.battery_degradation else True,
          "b_bar del primer año queda fijo en la capacidad nominal")

    print("\n[3] bloque de 1 año vs monolitico de 1 año")
    mono, mono_rules = build_monolithic(mine_system, time_series, years)
    # El monolitico decide X; el bloque la recibe fija. Para comparar el MISMO
    # problema hay que fijarle a X los mismos valores exogenos -- si no, se
    # estaria comparando contra un monolitico que puede elegir no abrir ninguna
    # nave, que es un problema distinto y mucho mas barato.
    for k in stations:
        mono.X[k, y1].fix(exo[k])
        mono.Delta_X[k, y1].fix(exo[k])
    obj_mono = solve(mono, "monolitico")
    obj_block = solve(block.model, "bloque    ")

    # Con X fija, la apertura de naves es una constante que el monolitico cobra
    # y el modo descompuesto no.
    inv_estaciones = sum(
        value(mono.station_cost_k[k]) * exo[k] for k in stations
    ) * value(mono_rules._discount_factor(mono, y1))
    print(f"       inversion en estaciones (constante, solo en el monolitico): "
          f"{inv_estaciones:,.2f}")
    diff = abs((obj_mono - inv_estaciones) - obj_block)
    rel = diff / max(1.0, abs(obj_mono))
    print(f"       |mono - inversion_estaciones - bloque| = {diff:,.4f}  ({rel:.4%})")
    check(rel <= 2 * GAP,
          "el bloque reproduce el monolitico de un año dentro del gap del solver")

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO))
