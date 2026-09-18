"""Validacion 3 de implementacion_descomposicion_carga_ob.md sec. 11: regimen
SIN degradacion. Sin hoja BatteryDegradation en elmo_data.xlsx el estado de
degradacion (D) tiene que desaparecer del acople y la descomposicion debe
comportarse como el multi-anio puro de infraestructura.

Lo que se verifica, sobre una copia del escenario sin esa hoja (se genera en
un directorio temporal a partir del escenario dado):
  1. mine_system.battery_degradation es None y las familias de estado de los
     bloques son exactamente las de infraestructura (N_chargers, n_ssee_k y,
     si existen, G y H): ni D ni w_deg ni envolvente de McCormick;
  2. una iteracion completa (presolve + forward + backward) termina con
     LB <= UB y sin cortes de factibilidad inesperados... el numero se informa;
  3. el optimo del monolitico con X fija al mismo calendario queda entre LB y
     UB (con la tolerancia del MIPGap).

Uso:  python tests/test_sin_degradacion_ob.py [escenario] [anios]
"""
import os
import shutil
import sys
import tempfile
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import openpyxl
import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value
from pyomo.opt import TerminationCondition

from setup import build_mine
from src.optimization.decomposition.driver import NestedBendersSolver, infer_exogenous_stations
from src.optimization.functions import (
    BoundRules, ConstraintRules, ObjectiveRules, OptParameters, OptSets,
)

DEFAULT_SCENARIO = "data/DCH/640kW_2dias_1MB/"
GAP = 0.01
TIMELIMIT = 600
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAILS.append(label)


def copia_sin_degradacion(scenario):
    """Copia el escenario a un temporal y le quita la hoja BatteryDegradation
    (cargando con data_only para conservar valores, no formulas)."""
    dst = tempfile.mkdtemp(prefix="sin_degradacion_")
    for name in os.listdir(scenario):
        src = os.path.join(scenario, name)
        if os.path.isfile(src) and not name.startswith("~$") and not name.endswith(".ini"):
            shutil.copy2(src, dst)
    xlsx = os.path.join(dst, "elmo_data.xlsx")
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    if "BatteryDegradation" in wb.sheetnames:
        del wb["BatteryDegradation"]
    wb.save(xlsx)
    return dst + os.sep


def main(scenario, n_years):
    folder = copia_sin_degradacion(scenario)
    print(f"escenario sin degradacion: {folder}  (copia de {scenario})", flush=True)
    args = Namespace(data_folder=folder, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=n_years)
    _s, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    y1 = years[0]
    stations = list(mine_system.get_system_stations())
    exo = infer_exogenous_stations(mine_system, time_series)

    print("\n[1] el estado colapsa a infraestructura pura", flush=True)
    check(mine_system.battery_degradation is None, "mine_system.battery_degradation es None")
    solver = NestedBendersSolver(
        mine_system, time_series, exo, gap_tol=GAP, max_iter=1,
        solver_kwargs={"solvername": "gurobi", "gap": GAP, "timelimit": TIMELIMIT},
        block_build_jobs=1,
    )
    blk = solver.blocks[-1]
    familias = sorted(l["state"] for l in blk.state_links)
    esperadas = {"N_chargers", "n_ssee_k"}
    if len(list(blk.model.gen_set)) > 0:
        esperadas.add("G")
    if len(list(blk.model.storage_set)) > 0:
        esperadas.add("H")
    check(set(familias) == esperadas, "familias de estado = infraestructura",
          f"{familias} vs {sorted(esperadas)}")
    check("D" not in familias, "sin estado D")
    for comp in ("w_deg", "mccormick_lb1", "mccormick_energy", "D_hat", "D_prev", "b_y_link_local"):
        check(not hasattr(blk.model, comp), f"el bloque no tiene {comp}")
    check(blk.mccormick_residual() is None, "mccormick_residual() devuelve None")

    print("\n[2] una iteracion completa", flush=True)
    res = solver.solve(verbose=True)
    check(res["lb"] <= res["ub"] * (1 + 2 * GAP) + 1e-6, "LB <= UB",
          f"{res['lb']:,.2f} <= {res['ub']:,.2f}")
    check(res["best_solution"] is not None, "hay solucion factible")
    print(f"       cortes de factibilidad: {res['feasibility_cuts']}  "
          f"cotas del presolve: {res['capacity_bounds']}", flush=True)

    print("\n[3] el optimo del monolitico (X fija) queda entre LB y UB", flush=True)
    mono = pyo.ConcreteModel()
    OptSets(mine_system, time_series).build_sets(mono)
    OptParameters(mine_system, time_series).build_parameters(mono)
    BoundRules(mine_system, time_series).build_all_variables(mono)
    ConstraintRules(mine_system, time_series).build_all_constraints(mono)
    rules = ObjectiveRules(mine_system, time_series)
    mono.obj = pyo.Objective(rule=rules.total_cost, sense=pyo.minimize)
    check(not hasattr(mono, "n_ciclos_link") and not hasattr(mono, "d_y_fade"),
          "el monolitico tampoco tiene degradacion")
    for y in years:
        for k in stations:
            mono.X[k, y].fix(exo[y][k])
            mono.Delta_X[k, y].fix(exo[y][k] if y == y1 else 0)
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["MIPGap"] = GAP
    opt.options["TimeLimit"] = TIMELIMIT
    opt.options["OutputFlag"] = 0
    r = opt.solve(mono, load_solutions=True)
    inv_est = sum(value(mono.station_cost_k[k]) * value(mono.Delta_X[k, y])
                  * value(rules._discount_factor(mono, y)) for k in stations for y in years)
    ref = value(mono.obj) - inv_est
    optimo = r.solver.termination_condition == TerminationCondition.optimal
    print(f"       monolitico: {r.solver.termination_condition}, comparable = {ref:,.2f}"
          + ("" if optimo else "  (sin optimo probado)"), flush=True)
    check(res["ub"] >= ref * (1 - 2 * GAP) - 1e-6, "UB >= optimo monolitico",
          f"{res['ub']:,.2f} >= {ref:,.2f}")
    check(res["lb"] <= ref * (1 + 2 * GAP) + 1e-6, "LB <= optimo monolitico",
          f"{res['lb']:,.2f} <= {ref:,.2f}")

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    escenario = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    anios = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    raise SystemExit(main(escenario, anios))
