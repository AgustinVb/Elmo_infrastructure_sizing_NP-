"""Validacion de ConstraintRules.apertura_solo_primer_anio.

La restriccion fuerza Delta_X[k, y] == 0 para y > 1: las naves se abren todas
al inicio del horizonte o no se abren. Existe para que el monolitico (X libre)
y la descomposicion (X exogena, abierta desde el anio 1 por
infer_exogenous_stations) resuelvan EL MISMO problema.

Lo que se verifica:
  1. en el monolitico (X libre) la restriccion existe, con una fila por nave y
     anio posterior al primero, y en un bloque de la descomposicion (X
     exogena) NO existe -- alli X ni siquiera es variable;
  2. resuelto el monolitico, Delta_X[k, y>1] = 0 y X[k, y] es constante en y;
  3. su optimo no es menor que el del monolitico con X fijada al calendario
     que infiere la descomposicion (X libre relaja X fija), y no es mayor que
     el del monolitico SIN la restriccion (la restriccion restringe). Se
     informa si la restriccion ata, es decir si el monolitico sin ella habria
     diferido alguna apertura.

Uso:  python tests/test_apertura_primer_anio_swap.py [escenario] [anios]
"""
import os
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value
from pyomo.opt import TerminationCondition

from setup import build_mine
from src.optimization.decomposition.driver import infer_exogenous_stations
from src.optimization.decomposition.year_block import YearBlockBuilder
from src.optimization.functions import (
    BoundRules, ConstraintRules, ObjectiveRules, OptParameters, OptSets,
)

DEFAULT_SCENARIO = "data/DCH/160kW_2dias/"
GAP = 0.01
TIMELIMIT = 900
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAILS.append(label)


def monolitico(mine_system, time_series):
    m = pyo.ConcreteModel()
    OptSets(mine_system, time_series).build_sets(m)
    OptParameters(mine_system, time_series).build_parameters(m)
    BoundRules(mine_system, time_series).build_all_variables(m)
    ConstraintRules(mine_system, time_series, mccormick_degradation=True).build_all_constraints(m)
    m.obj = pyo.Objective(rule=ObjectiveRules(mine_system, time_series).total_cost,
                          sense=pyo.minimize)
    return m


def solve(m, label):
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["MIPGap"] = GAP
    opt.options["TimeLimit"] = TIMELIMIT
    opt.options["OutputFlag"] = 0
    res = opt.solve(m, load_solutions=True)
    optimo = res.solver.termination_condition == TerminationCondition.optimal
    print(f"       {label}: {res.solver.termination_condition}, obj = {value(m.obj):,.2f}"
          + ("" if optimo else "  (SIN optimo probado: la comparacion no es rigurosa)"),
          flush=True)
    return value(m.obj), optimo


def main(scenario, n_years):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=n_years)
    _s, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    y1 = years[0]
    stations = list(mine_system.get_system_stations())
    exo = infer_exogenous_stations(mine_system, time_series)
    print(f"escenario: {scenario}  ({len(years)} anios {years}, {len(stations)} naves)",
          flush=True)

    print("\n[1] la restriccion existe solo donde X es variable", flush=True)
    mono = monolitico(mine_system, time_series)
    check(hasattr(mono, "apertura_solo_primer_anio"),
          "el monolitico (X libre) tiene apertura_solo_primer_anio")
    esperadas = len(stations) * (len(years) - 1)
    n_filas = len(mono.apertura_solo_primer_anio) if hasattr(mono, "apertura_solo_primer_anio") else -1
    check(n_filas == esperadas,
          "una fila por nave y anio posterior al primero (el primero se salta)",
          f"{n_filas} filas, esperadas {esperadas}")
    check(all((k, y1) not in mono.apertura_solo_primer_anio for k in stations),
          "ninguna fila en el primer anio")
    blk = YearBlockBuilder(mine_system, time_series, year=years[-1], is_last_year=True,
                           exogenous_stations=exo[years[-1]])
    check(not hasattr(blk.model, "apertura_solo_primer_anio"),
          "un bloque de la descomposicion (X exogena) NO la tiene")
    check(not isinstance(blk.model.X, pyo.Var), "en el bloque X no es variable")

    print("\n[2] monolitico con la restriccion: aperturas solo en el primer anio", flush=True)
    obj_con, opt_con = solve(mono, "monolitico con apertura_solo_primer_anio")
    dx_tarde = {(k, y): round(value(mono.Delta_X[k, y])) for k in stations for y in years if y != y1}
    check(all(v == 0 for v in dx_tarde.values()), "Delta_X[k, y > 1] = 0 en la solucion",
          str({ky: v for ky, v in dx_tarde.items() if v != 0}))
    x_sol = {k: [round(value(mono.X[k, y])) for y in years] for k in stations}
    check(all(len(set(v)) == 1 for v in x_sol.values()), "X[k, y] constante en y", str(x_sol))
    x_exo = {k: [exo[y][k] for y in years] for k in stations}
    print(f"       X del monolitico: {x_sol}", flush=True)
    print(f"       X que infiere la descomposicion: {x_exo}", flush=True)
    if x_sol == x_exo:
        print("       coinciden: el monolitico y la descomposicion resuelven el mismo layout",
              flush=True)
    else:
        print("       NO coinciden: el monolitico elige otro layout (que naves abrir sigue "
              "siendo suyo)", flush=True)

    print("\n[3] orden de los optimos: X fija >= X libre con restriccion >= X libre sin ella",
          flush=True)
    fijo = monolitico(mine_system, time_series)
    for y in years:
        for k in stations:
            fijo.X[k, y].fix(exo[y][k])
            fijo.Delta_X[k, y].fix(exo[y][k] if y == y1 else 0)
    obj_fijo, opt_fijo = solve(fijo, "monolitico con X fija al calendario inferido")
    libre = monolitico(mine_system, time_series)
    libre.apertura_solo_primer_anio.deactivate()
    obj_sin, opt_sin = solve(libre, "monolitico SIN la restriccion")
    tol = 2 * GAP
    check(obj_fijo >= obj_con * (1 - tol) - 1e-6,
          "X fija >= X libre con restriccion", f"{obj_fijo:,.2f} >= {obj_con:,.2f}")
    check(obj_con >= obj_sin * (1 - tol) - 1e-6,
          "X libre con restriccion >= X libre sin ella", f"{obj_con:,.2f} >= {obj_sin:,.2f}")
    if not (opt_con and opt_fijo and opt_sin):
        print("       aviso: algun solve no probo optimalidad; el orden se compara con "
              "tolerancia 2*GAP pero no es una verificacion estricta", flush=True)
    dx_sin = {(k, y): round(value(libre.Delta_X[k, y])) for k in stations for y in years if y != y1}
    difiere = {ky: v for ky, v in dx_sin.items() if v != 0}
    if difiere:
        print(f"       la restriccion ATA: sin ella el monolitico difiere aperturas {difiere} "
              f"y ahorra {obj_con - obj_sin:,.2f}", flush=True)
    else:
        print("       la restriccion no ata en esta instancia: sin ella el monolitico "
              "tampoco difiere ninguna apertura", flush=True)

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    escenario = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    anios = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    raise SystemExit(main(escenario, anios))
