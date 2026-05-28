import os
import sys
import time
import json
import logging
import threading
import signal
import atexit
from pathlib import Path
from typing import Any, Dict, List

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

    def __init__(self, mine_system, time_series, output_folder, warm_start_folder=None, y_init_path=None, init_solution_folder=None, relax_integrality=False):
        self.output_folder   = output_folder
        os.makedirs(self.output_folder, exist_ok=True)
        self.gurobi_log_path = os.path.join(self.output_folder, "gurobi.log")
        self.infeasible_log_path = os.path.join(self.output_folder, "infeasible_log.txt")
        self.warm_start_folder = warm_start_folder
        self.y_init_path = y_init_path
        self.init_solution_folder = init_solution_folder if init_solution_folder is not None else warm_start_folder
        self.relax_integrality = relax_integrality
        self.has_warm_start = False
        self.set_builder      = OptSets(mine_system, time_series)
        self.param_rules      = OptParameters(mine_system, time_series)
        self.bound_rules      = BoundRules(mine_system, time_series)
        self.constraint_rules = ConstraintRules(mine_system, time_series)
        self.objective_rules  = ObjectiveRules(mine_system, time_series)
        self.output_manager   = OutputManager(mine_system, time_series)
        self.model            = self.build_model()
        self.var_index_order: Dict[str, List[str]] = {
            "Z": ["i", "d", "t"],
            "Z_charge": ["k", "i", "d", "t"],
            "Y": ["i", "j", "d", "t"],
            "P": ["k", "i", "d", "t"],
            "P_pot": ["y"],
            "B": ["i", "d", "t"],
            "N_chargers": ["k"],
            "X": ["k"],
            "StartCharge": ["k", "i", "d", "t"],
            "EndCharge": ["k", "i", "d", "t"],
            "M": ["i", "j", "d", "t"],
            "F": ["j", "d"],
            "F_seg": ["j", "d", "seg"],
        }
        if self.init_solution_folder:
            self._load_solution_warmstart_folder(self.init_solution_folder)
        if self.y_init_path:
            self._load_y_warmstart(self.y_init_path)
        
        # No guardar archivos del modelo formulado
        # self.save_formulated_model()



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

    @staticmethod
    def _flatten_nested_solution_tree(tree):
        records = []

        def walk(node, tokens):
            if not isinstance(node, dict):
                return

            for axis_name, axis_values in node.items():
                if not isinstance(axis_values, dict):
                    final_tokens = tokens + [str(axis_name)]
                    if final_tokens:
                        records.append((final_tokens, axis_values))
                    continue

                for axis_value, child in axis_values.items():
                    if isinstance(child, dict):
                        walk(child, tokens + [str(axis_value)])
                    else:
                        final_tokens = tokens + [str(axis_value)]
                        records.append((final_tokens, child))

        walk(tree, [])
        return records

    @staticmethod
    def _cast_token_like(example_value, token):
        if example_value is None:
            return token

        try:
            if isinstance(example_value, str):
                return str(token)
            if isinstance(example_value, (int,)):
                return int(float(token))
            if isinstance(example_value, (float,)):
                return float(token)
        except Exception:
            pass
        return token

    def _load_solution_warmstart_folder(self, init_solution_folder):
        if not init_solution_folder:
            return

        folder = Path(os.path.normpath(init_solution_folder))
        if not folder.exists():
            print(f"WARN Warm start folder no existe: {folder}")
            return

        total_loaded = 0
        total_skipped = 0

        for var_comp in self.model.component_objects(pyo.Var, active=True):
            var_name = str(var_comp.name)
            json_path = os.path.join(str(folder), f"{var_name}.json")
            if not os.path.exists(json_path):
                continue

            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception as exc:
                print(f"WARN No se pudo leer warm start de {os.path.basename(json_path)}: {exc}")
                continue

            count = 0
            skipped = 0
            if var_comp.is_indexed():
                index_lookup = {}
                for idx in var_comp:
                    idx_tuple = idx if isinstance(idx, tuple) else (idx,)
                    normalized = tuple(str(v) for v in idx_tuple)
                    index_lookup[normalized] = idx

                axis_order = self.var_index_order.get(var_name)
                records = self._flatten_nested_solution_tree(payload)
                for tokens, raw_value in records:
                    if not axis_order:
                        skipped += 1
                        continue

                    if len(tokens) < len(axis_order):
                        skipped += 1
                        continue

                    normalized_tokens = tuple(tokens[:len(axis_order)])
                    if normalized_tokens not in index_lookup:
                        skipped += 1
                        continue
                    try:
                        vardata = var_comp[index_lookup[normalized_tokens]]
                        value_to_set = float(raw_value)
                        if vardata.is_binary() or vardata.is_integer():
                            value_to_set = int(round(value_to_set))
                        vardata.set_value(value_to_set, skip_validation=True)
                        count += 1
                    except Exception:
                        skipped += 1
                        continue
            else:
                try:
                    value_to_set = float(payload)
                    vardata = var_comp
                    if vardata.is_binary() or vardata.is_integer():
                        value_to_set = int(round(value_to_set))
                    vardata.set_value(value_to_set, skip_validation=True)
                    count = 1
                except Exception:
                    count = 0
                    skipped = 1

            print(f"OK Warm start {var_name}: {count} cargados, {skipped} omitidos")
            total_loaded += count
            total_skipped += skipped

        self.has_warm_start = total_loaded > 0
        print(f"OK Warm start completo desde '{folder}': {total_loaded} valores cargados, {total_skipped} omitidos")

    def _load_y_warmstart(self, y_init_path):
        if not y_init_path:
            return

        if not os.path.exists(y_init_path):
            print(f"WARN Warm start Y no encontrado: {y_init_path}")
            return

        try:
            with open(y_init_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"WARN No se pudo leer warm start Y: {e}")
            return

        d_root = data.get("d", {}) if isinstance(data, dict) else {}
        if not isinstance(d_root, dict):
            print("WARN Warm start Y con formato no soportado (falta raíz 'd').")
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

        print(f"OK Warm start Y cargado desde '{y_init_path}': {loaded} valores, {skipped} omitidos")

    def _build_infeasible_logger(self, log_file):
        logger_name = f"infeasible_logger_{id(self)}"
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger, handler

    def _configure_solver(self, solvername, gap, timelimit, log_file):
        if solvername == 'glpk':
            solverpath_folder = 'C:\\glpk\\w64'
            if solverpath_folder not in sys.path:
                sys.path.append(solverpath_folder)
            opt = SolverFactory('glpk', tee=True)
            opt.options['mipgap'] = gap
            return opt

        if solvername == 'gurobi':
            self.model.branch_priority = Suffix(direction=Suffix.EXPORT, datatype=Suffix.INT)
            opt = SolverFactory('gurobi', solver_io="python")
            opt.options['OutputFlag'] = 1
            opt.options['LogToConsole'] = 1
            opt.options['MIPGap'] = gap
            opt.options['LogFile'] = log_file
            opt.options['Threads'] = 32
            opt.options['Heuristics'] = 0.5
            opt.options['MIPFocus'] = 3
            opt.options['Presolve'] = 2
            opt.options['FlowCoverCuts'] = 2
            opt.options['TimeLimit'] = timelimit
            return opt

        raise ValueError(f"Solver no soportado: {solvername}")

    def _find_fallback_solver(self):
        for candidate in ['highs', 'cbc', 'glpk']:
            try:
                candidate_opt = SolverFactory(candidate)
                if candidate_opt is not None and candidate_opt.available(exception_flag=False):
                    return candidate
            except Exception:
                continue
        return None

    def limited_infeasible_log(self, model, timeout=60, log_file="infeasible_log.txt"):
        def target():
            print(f"🔍 Checking for infeasible constraints (max {timeout}s)...")
            logger = None
            handler = None
            try:
                logger, handler = self._build_infeasible_logger(log_file)
                log_infeasible_constraints(model, log_expression=True, logger=logger)
            except Exception as e:
                print("WARN Error while logging infeasibilities:", e)
            finally:
                if handler is not None:
                    handler.close()
                if logger is not None:
                    logger.handlers.clear()

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
                print("WARN Could not read log file for summary:", e)

    def solve_model(self, gap, solvername, timelimit=172800, relax_integrality=False): 
        log_file = self.gurobi_log_path
        if os.path.exists(log_file):
            os.remove(log_file)

        result = None
        model_to_solve = self.model
        if relax_integrality:
            self.original_model = self.model
            model_to_solve = self.model.clone()
            pyo.TransformationFactory('core.relax_integer_vars').apply_to(model_to_solve)
            self.relaxed_model = model_to_solve
            self.model = model_to_solve

        opt = self._configure_solver(solvername, gap, timelimit, log_file)

        print("Solving opt model... (Puedes presionar Ctrl+C para detener y guardar la mejor solución actual)")
        start_time = time.time()
        
        # --- Manejador para forzar escritura de log al interrumpir ---
        def flush_and_exit(signum, frame):
            print("\n🛑 ¡Interrupción manual detectada! Forzando escritura de log...")
            # Forzar flush de archivos abiertos
            sys.stdout.flush()
            sys.stderr.flush()
            # Dar tiempo a Gurobi para escribir el log
            time.sleep(0.5)
            raise KeyboardInterrupt()
        
        # Registrar el manejador
        old_sigint_handler = signal.signal(signal.SIGINT, flush_and_exit)
        
        # --- Bloque de seguridad para interrupción manual ---
        try:
            solve_kwargs = {"tee": True, "load_solutions": True}
            if solvername == 'gurobi' and self.has_warm_start:
                solve_kwargs["warmstart"] = True
            result = opt.solve(model_to_solve, **solve_kwargs)
            self.solution_status = result.solver.termination_condition
            
        except KeyboardInterrupt:
            # Esto se activa si presionas "Stop" o Ctrl+C
            print("\nIntentando recuperar la mejor solución hasta ahora...")
            self.solution_status = TerminationCondition.maxTimeLimit
            # Forzar escritura del log file
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'a') as f:
                        f.write(f"\n--- Ejecución interrumpida por usuario ---\n")
                    print(f"✅ Log guardado exitosamente en {log_file}")
                except Exception as e:
                    print(f"WARN Error al escribir log: {e}")
        except Exception as e:
            error_text = str(e)
            is_gurobi_license_error = (
                solvername == 'gurobi' and 'license expired' in error_text.lower()
            )

            if is_gurobi_license_error:
                print("WARN La licencia de Gurobi está expirada. Intentando solver alternativo...")
                fallback_solver = self._find_fallback_solver()
                if fallback_solver is None:
                    print("❌ No se encontró solver alternativo disponible (highs/cbc/glpk).")
                    print("   Renueva licencia de Gurobi o instala otro solver MIP.")
                    self.solution_status = TerminationCondition.error
                else:
                    print(f"🔁 Reintentando con solver fallback: {fallback_solver}")
                    try:
                        opt = self._configure_solver(fallback_solver, gap, timelimit, log_file)
                        result = opt.solve(model_to_solve, tee=True, load_solutions=True)
                        self.solution_status = result.solver.termination_condition
                        print(f"✅ Solver fallback ejecutado con estado: {self.solution_status}")
                    except Exception as fallback_error:
                        print(f"❌ Falló también el solver fallback ({fallback_solver}): {fallback_error}")
                        self.solution_status = TerminationCondition.error
            else:
                print(f"WARN Ocurrió un error inesperado: {e}")
                self.solution_status = TerminationCondition.error
        finally:
            # Restaurar el manejador anterior
            signal.signal(signal.SIGINT, old_sigint_handler)

        self.time_total      = time.time() - start_time
        

        condiciones_aceptables = [
            TerminationCondition.optimal,
            TerminationCondition.maxTimeLimit, 
            TerminationCondition.feasible
        ]

        if self.solution_status in condiciones_aceptables:
            
            if hasattr(self.model, 'obj') and value(self.model.obj) is not None:
                self.opt_cost_result = value(self.model.obj)
                print(f"✅ Solution time [sec]: {self.time_total:.2f}")
                print(f"📊 Status: {self.solution_status}") 
                print("💰 Operation Cost:", self.opt_cost_result)
                
                # --- CÁLCULO DE PRODUCCIÓN TOTAL ---
                #total_prod_val = 0
                #for j in self.model.nodes_set:
                #    prod_expression = self.objective_rules.production_total(self.model, j)
                #    total_prod_val += value(prod_expression)
                
                #self.total_production = total_prod_val
                #print(f"⛏️  Total production: {self.total_production:,.2f} Ton")
                # -----------------------------------
            else:
                print("WARN Se detuvo el proceso, pero no se encontró ninguna solución factible todavía.")
                self.limited_infeasible_log(self.model, log_file=self.infeasible_log_path)

        else:
            print(f"WARN Termination condition: {self.solution_status}")
            self.limited_infeasible_log(self.model, timeout=60, log_file=self.infeasible_log_path)

        try:
            if 'result' in locals() and hasattr(result.solver, 'relative_gap'):
                self.mip_gap = result.solver.relative_gap
            else:
                self.mip_gap = None
        except:
            self.mip_gap = None
            
        return 0
