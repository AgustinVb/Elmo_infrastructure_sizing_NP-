import os
import sys
import time
import threading
import json
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

    def _load_y_warmstart(self, y_init_path):
        if not y_init_path:
            return

        if not os.path.exists(y_init_path):
            print(f"⚠️ Warm start Y no encontrado: {y_init_path}")
            return

        try:
            with open(y_init_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ No se pudo leer warm start Y: {e}")
            return

        d_root = data.get("d", {}) if isinstance(data, dict) else {}
        if not isinstance(d_root, dict):
            print("⚠️ Warm start Y con formato no soportado (falta raíz 'd').")
            return

        loaded = 0
        skipped = 0

        for lhd, lhd_block in d_root.items():
            if not isinstance(lhd_block, dict):
                continue
            t_nodes = lhd_block.get("t", {})
            if not isinstance(t_nodes, dict):
                continue

            for node, node_block in t_nodes.items():
                if not isinstance(node_block, dict):
                    continue
                day_map = node_block.get("i", {})
                if not isinstance(day_map, dict):
                    continue

                for day_key, day_block in day_map.items():
                    if not isinstance(day_block, dict):
                        continue
                    interval_map = day_block.get("j", {})
                    if not isinstance(interval_map, dict):
                        continue

                    try:
                        day = int(float(day_key))
                    except Exception:
                        skipped += 1
                        continue

                    for interval_key, val in interval_map.items():
                        try:
                            t = int(float(interval_key))
                            v = float(val)
                        except Exception:
                            skipped += 1
                            continue

                        if v < 0.5:
                            continue

                        idx = (lhd, str(node), day, t)
                        if idx in self.model.Y:
                            self.model.Y[idx].value = 1.0
                            loaded += 1
                        else:
                            skipped += 1

        print(f"✔ Warm start Y cargado desde '{y_init_path}': {loaded} valores, {skipped} omitidos")

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

    def __init__(self, mine_system, time_series, output_folder, y_init_path=None):
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
        self._load_y_warmstart(y_init_path)

    def solve_model(self, gap, solvername, timelimit=172800, relax_integrality=False): 
        # Log file now in output folder to avoid conflicts
        # Normalize output_folder path to avoid double backslashes
        output_folder_normalized = os.path.normpath(self.output_folder)
        os.makedirs(output_folder_normalized, exist_ok=True)
        log_file = os.path.join(output_folder_normalized, "ELMO_log.txt")
        if os.path.exists(log_file):
            os.remove(log_file)

        model_to_solve = self.model
        if relax_integrality:
            self.original_model = self.model
            model_to_solve = self.model.clone()
            pyo.TransformationFactory('core.relax_integer_vars').apply_to(model_to_solve)
            self.relaxed_model = model_to_solve
            self.model = model_to_solve

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
            opt.options['TimeLimit']    = timelimit
            opt.options['LogFile']      = log_file
            opt.options['Threads']      = 24
            opt.options['Heuristics']   = 0.5
            opt.options['MIPFocus']     = 3      
            opt.options['Presolve']     = 2      
            opt.options['FlowCoverCuts'] = 2
            try:
                result = opt.solve(model_to_solve, tee=True, load_solutions=True, warmstart=True)
                self.solution_status = result.solver.termination_condition
            except KeyboardInterrupt:
                print("\n🛑 ¡Interrupción manual detectada! Intentando recuperar la mejor solución hasta ahora...")
                self.solution_status = TerminationCondition.maxTimeLimit 
            except Exception as e:
                print(f"⚠️ Ocurrió un error inesperado: {e}")
                self.solution_status = TerminationCondition.error
