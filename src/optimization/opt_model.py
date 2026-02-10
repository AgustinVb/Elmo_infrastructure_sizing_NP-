import os
import sys
import time
import threading
from contextlib import redirect_stdout

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value
from pyomo.core.base import Suffix
from pyomo.opt import TerminationCondition
from pyomo.util.infeasible import log_infeasible_constraints

from src.optimization.functions import (
    OptSets,
    OptParameters,
    BoundRules,
    ConstraintRules,
    ObjectiveRules,
    OutputManager,
)


class OptModel(object):

    def build_model(self):
        model = pyo.ConcreteModel()

        # 1) Definición de sets
        self.set_builder.build_sets(model)

        # 2) Definición de parámetros
        self.param_rules.build_parameters(model)

        # 3) Definición de variables
        self.bound_rules.build_all_variables(model)

        # 4) Restricciones
        self.constraint_rules.build_all_constraints(model)

        # 5) Objetivo
        self.objective_rules.build_objective(model)

        return model

    def limited_infeasible_log(self, model, timeout=60, log_file="infeasible_log.txt"):
        def target():
            print(f"🔍 Checking for infeasible constraints (max {timeout}s)...")
            try:
                with open(log_file, "w") as f:
                    with redirect_stdout(f):
                        log_infeasible_constraints(model, log_expression=True)
            except Exception as e:
                print("⚠️ Error while logging infeasibilities:", e)

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(timeout)

        if thread.is_alive():
            print(f"⏳ Timeout reached ({timeout}s). Stopping infeasible log early.")
            print(f"📄 Partial results (if any) written to {log_file}")
        else:
            print(f"✅ Infeasible log finished. Full results written to {log_file}")

            # Mostrar un resumen en consola (primeras 10 líneas)
            try:
                with open(log_file, "r") as f:
                    lines = f.readlines()
                    print("\n🔝 First 10 infeasible constraints:")
                    for line in lines[:10]:
                        print(line.strip())
                    if len(lines) > 10:
                        print("... (see full log in infeasible_log.txt)")
            except Exception as e:
                print("⚠️ Could not read log file for summary:", e)

    def __init__(self, mine_system, time_series, output_folder):
        self.output_folder   = output_folder
        self.time_series     = time_series
        self.mine_system     = mine_system
        self.set_builder      = OptSets(mine_system, time_series)
        self.param_rules      = OptParameters(mine_system, time_series)
        self.bound_rules      = BoundRules(mine_system, time_series)
        self.constraint_rules = ConstraintRules(mine_system, time_series)
        self.objective_rules  = ObjectiveRules(mine_system, time_series)
        self.output_manager   = OutputManager(mine_system, time_series)
        self.model            = self.build_model()

    def solve_model(self, gap, solvername, timelimit=172800): 
        log_file = "ELMO_log.txt"
        if os.path.exists(log_file):
            os.remove(log_file)

        if solvername == 'glpk':
 
            solverpath_folder = 'C:\\glpk\\w64'
            sys.path.append(solverpath_folder)
            opt = SolverFactory('glpk', tee=True)
            opt.options['mipgap'] = gap


        elif solvername == 'gurobi':
            opt = SolverFactory('gurobi', solver_io="python")
            opt.options['OutputFlag']   = 1
            opt.options['LogToConsole'] = 1
            opt.options['MIPGap']       = gap
            opt.options['LogFile']      = log_file
            opt.options['Threads']      = 24
            opt.options['Heuristics']   = 0.5
            opt.options['MIPFocus']     = 3      
            opt.options['Presolve']     = 2      
            opt.options['FlowCoverCuts'] = 2
            try:
                result = opt.solve(self.model, tee=True, load_solutions=True)
                self.solution_status = result.solver.termination_condition
            except KeyboardInterrupt:
                print("\n🛑 ¡Interrupción manual detectada! Intentando recuperar la mejor solución hasta ahora...")
                self.solution_status = TerminationCondition.maxTimeLimit 
            except Exception as e:
                print(f"⚠️ Ocurrió un error inesperado: {e}")
                self.solution_status = TerminationCondition.error
