"""Solve the operational relaxation and print its objective breakdown."""
import argparse
import json
import os
import sys
from argparse import Namespace

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value
from pyomo.opt import TerminationCondition

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from setup import build_mine
from src.optimization.functions import OPERATIONAL_BINARY_VARS
from src.optimization.opt_model import OptModel


COMPONENTS = {
    "lhd_charge": "lhd_charge_cost",
    "investment": "inversion_cost",
    "stations": "station_constant_cost",
    "substation": "substation_investment_cost",
    "generation_investment": "gen_investment_cost",
    "generation_operation": "gen_op_cost",
    "bess_investment": "bess_investment_cost",
    "bess_operation": "bess_op_cost",
    "peak_power": "power_cost",
    "battery_replacement": "battery_replace_cost",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_folder", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timelimit", type=float, default=5400)
    parser.add_argument("--gap", type=float, default=1e-4)
    parser.add_argument("--free_charging", action="store_true")
    parser.add_argument("--free_maintenance", action="store_true")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    ns = Namespace(
        data_folder=args.data_folder,
        model="elmo_data.xlsx",
        series="time_series.xlsx",
        consumption_model="wp1",
        wp2_consumption_json=None,
        n_years=10,
        days_per_year=4,
    )
    _, mine, series = build_mine(ns)
    model_output = os.path.join(args.out, "model")
    opt_model = OptModel(
        mine,
        series,
        model_output,
        mccormick_degradation=True,
        free_charging=args.free_charging,
        free_maintenance=args.free_maintenance,
    )
    model = opt_model.model
    relaxed = 0
    for var_name in OPERATIONAL_BINARY_VARS:
        component = getattr(model, var_name, None)
        if component is None:
            continue
        for variable in component.values():
            if variable.is_integer() or variable.is_binary():
                variable.domain = pyo.UnitInterval
                relaxed += 1

    solver = SolverFactory("gurobi", solver_io="python")
    solver.options["OutputFlag"] = 1
    solver.options["LogFile"] = os.path.join(args.out, "op_bound_gurobi.log")
    solver.options["LogToConsole"] = 0
    solver.options["TimeLimit"] = args.timelimit
    solver.options["MIPGap"] = args.gap
    result = solver.solve(model, load_solutions=True)
    condition = result.solver.termination_condition
    if condition not in (TerminationCondition.optimal, TerminationCondition.maxTimeLimit,
                         TerminationCondition.feasible):
        raise SystemExit(f"relajacion sin solucion utilizable: {condition}")

    values = {}
    for name, method_name in COMPONENTS.items():
        method = getattr(opt_model.objective_rules, method_name)
        values[name] = float(value(method(model)))
    values["total_from_components"] = sum(values.values())
    values["objective_value"] = float(value(model.obj))
    values["relaxed_operational_binary_count"] = relaxed
    values["energy_cut_constraints"] = len(model.years)
    values["slot_energy_cut_constraints"] = len(model.years) * len(model.days)
    values["termination_condition"] = str(condition)
    values["solver_bound"] = float(getattr(solver._solver_model, "ObjBound", values["objective_value"]))
    with open(os.path.join(args.out, "operational_costs.json"), "w", encoding="utf-8") as stream:
        json.dump(values, stream, indent=2)
    for name, amount in values.items():
        if isinstance(amount, float):
            print(f"{name}: {amount:,.2f}")
        else:
            print(f"{name}: {amount}")


if __name__ == "__main__":
    main()