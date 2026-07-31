import os
import sys
import time
import threading
import json
from contextlib import redirect_stdout
from typing import Dict, List, Tuple, Any

import numpy as np
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

    # Variables binarias/enteras de decisión: el resto del modelo (estados de
    # carga, inventarios, potencias) se deriva de estas vía restricciones de
    # igualdad, así que basta con darle el warm start a estas.
    HARD_VARS = {"Y", "Z", "Z_swap", "X", "N_chargers", "N_bays", "N_batteries"}

    @staticmethod
    def _flatten_nested_solution_tree(tree, var_axis_order: List[str] = None):
        """
        Flatten nested JSON solution trees produced by Printer into
        (index_tokens, value) records in the order specified by var_axis_order.
        
        Printer creates: i -> {LHD_values} -> j -> {node_values} -> d -> {day_values} -> t -> {time_values: leaf}
        Pattern: axis_name -> {axis_values_dict} -> axis_name -> ... -> leaf_value
        """
        records = []

        def walk(node, tokens):
            if not isinstance(node, dict):
                return
            
            for axis_name, axis_values in node.items():
                if not isinstance(axis_values, dict):
                    # Scalar value - this is a leaf
                    # axis_name is the last axis value
                    final_tokens = tokens + [(None, str(axis_name))]
                    
                    if var_axis_order:
                        axis_dict = {n: v for n, v in final_tokens if n is not None}
                        ordered = [axis_dict.get(ax, None) for ax in var_axis_order]
                        ordered = [t for t in ordered if t is not None]
                    else:
                        ordered = [v for n, v in final_tokens if n is not None]
                    
                    if ordered:
                        records.append((ordered, axis_values))
                    continue
                
                # axis_values is a dict - these are axis values
                for axis_value, child in axis_values.items():
                    if isinstance(child, dict):
                        # child is another level of nesting - recurse
                        walk(child, tokens + [(axis_name, str(axis_value))])
                    else:
                        # child is a scalar leaf value
                        final_tokens = tokens + [(axis_name, str(axis_value))]
                        
                        if var_axis_order:
                            axis_dict = {n: v for n, v in final_tokens}
                            ordered = [axis_dict.get(ax, None) for ax in var_axis_order]
                            ordered = [t for t in ordered if t is not None]
                        else:
                            ordered = [v for n, v in final_tokens]
                        
                        records.append((ordered, child))

        walk(tree, [])
        return records

    @staticmethod
    def _cast_token_like(example_value, token):
        """Cast a string token to the type of an example index value."""
        if example_value is None:
            return token

        try:
            if isinstance(example_value, str):
                return str(token)
            if isinstance(example_value, (int, np.integer)):
                return int(float(token))
            if isinstance(example_value, (float, np.floating)):
                return float(token)
        except Exception:
            pass
        return token

    def _load_solution_warmstart_folder(self, init_solution_folder, hard_vars_only=False):
        """
        Loads initial values for model variables from a folder containing
        JSON files named like <VarName>.json (Printer export format).

        If hard_vars_only is True, only the discrete decision variables in
        HARD_VARS are loaded; the rest of the model (SOC, inventarios,
        potencias) queda libre para que el solver lo derive sin choques de
        factibilidad con la solución anterior.
        """
        if not init_solution_folder:
            return

        folder = os.path.normpath(init_solution_folder)
        if not os.path.isdir(folder):
            print(f"⚠️ Carpeta de solución inicial no encontrada: {folder}")
            return

        total_loaded = 0
        total_skipped = 0

        for var_comp in self.model.component_objects(pyo.Var, active=True):
            var_name = str(var_comp.name)
            if hard_vars_only and var_name not in self.HARD_VARS:
                continue
            json_path = os.path.join(folder, f"{var_name}.json")
            if not os.path.exists(json_path):
                continue

            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"⚠️ No se pudo leer {json_path}: {e}")
                continue

            # Get axis order for this variable (if available)
            axis_order = self.var_axis_order.get(var_name)
            
            # Flatten with axis ordering to ensure correct token sequence
            records = self._flatten_nested_solution_tree(data, var_axis_order=axis_order)
            if not records:
                continue

            sample_key = None
            try:
                sample_key = next(iter(var_comp.keys()))
            except Exception:
                sample_key = None

            if isinstance(sample_key, tuple):
                sample_types = sample_key
            elif sample_key is None:
                sample_types = ()
            else:
                sample_types = (sample_key,)

            loaded = 0
            skipped = 0

            for idx_tokens, val in records:
                try:
                    v = float(val)
                    if hard_vars_only:
                        v = round(v)
                except Exception:
                    skipped += 1
                    continue

                try:
                    if sample_types:
                        casted = [
                            self._cast_token_like(sample_types[pos], idx_tokens[pos])
                            for pos in range(min(len(sample_types), len(idx_tokens)))
                        ]
                    else:
                        casted = list(idx_tokens)

                    if len(casted) == 1:
                        idx = casted[0]
                    else:
                        idx = tuple(casted)

                    if idx in var_comp and not var_comp[idx].fixed:
                        var_comp[idx].value = v
                        loaded += 1
                    else:
                        skipped += 1
                except Exception:
                    skipped += 1

            total_loaded += loaded
            total_skipped += skipped
            print(f"✔ Warm start {var_name}: {loaded} cargados, {skipped} omitidos")

        print(
            f"✔ Warm start completo desde '{folder}': "
            f"{total_loaded} valores cargados, {total_skipped} omitidos"
        )

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

        i_root = data.get("i", {}) if isinstance(data, dict) else {}
        if not isinstance(i_root, dict):
            print("⚠️ Warm start Y con formato no soportado (falta raíz 'i').")
            return

        loaded = 0
        skipped = 0
        set_indices = set()

        for lhd, lhd_block in i_root.items():
            if not isinstance(lhd_block, dict):
                continue
            j_nodes = lhd_block.get("j", {})
            if not isinstance(j_nodes, dict):
                continue

            for node, node_block in j_nodes.items():
                if not isinstance(node_block, dict):
                    continue
                day_map = node_block.get("d", {})
                if not isinstance(day_map, dict):
                    continue

                for day_key, day_block in day_map.items():
                    if not isinstance(day_block, dict):
                        continue
                    interval_map = day_block.get("t", {})
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
                            set_indices.add(idx)
                            loaded += 1
                        else:
                            skipped += 1

        # El exportador omite los 0 en variables binarias, así que cualquier
        # índice no presente en el JSON era 0 en la solución previa: se fija
        # explícitamente para entregar un MIP start completo.
        completed = 0
        for idx in self.model.Y:
            if idx not in set_indices:
                self.model.Y[idx].value = 0.0
                completed += 1

        print(
            f"✔ Warm start Y cargado desde '{y_init_path}': {loaded} valores, "
            f"{skipped} omitidos, {completed} completados en 0"
        )

    def _apply_fixed_infra(self, fixed_infra):
        """Fija las variables de infraestructura (N_chargers, N_bays,
        N_batteries, X) a valores ya decididos (Fase 1), para que la Fase 2
        (agendamiento por dia) no las vuelva a optimizar por separado.

        fixed_infra: dict {'N_chargers': v, 'N_bays': v, 'N_batteries': v, 'X': v}
        Se aplica a todas las estaciones del modelo (pensado para subproblemas
        de una sola estacion, donde stations_set tiene un unico elemento).
        """
        if not fixed_infra:
            return
        for k in self.model.stations_set:
            if 'X' in fixed_infra:
                self.model.X[k].fix(fixed_infra['X'])
            if 'N_chargers' in fixed_infra:
                self.model.N_chargers[k].fix(fixed_infra['N_chargers'])
            if 'N_bays' in fixed_infra:
                self.model.N_bays[k].fix(fixed_infra['N_bays'])
            if 'N_batteries' in fixed_infra:
                self.model.N_batteries[k].fix(fixed_infra['N_batteries'])

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

    def __init__(self, mine_system, time_series, output_folder, y_init_path=None, init_solution_folder=None,
                 warmstart_hard_only=False, fixed_infra=None, daily_target_override=None):
        self.output_folder   = output_folder
        self.time_series     = time_series
        self.mine_system     = mine_system
        self.set_builder      = OptSets(mine_system, time_series)
        self.param_rules      = OptParameters(mine_system, time_series)
        self.bound_rules      = BoundRules(mine_system, time_series)
        self.constraint_rules = ConstraintRules(mine_system, time_series, daily_target_override=daily_target_override)
        self.objective_rules  = ObjectiveRules(mine_system, time_series)
        self.output_manager   = OutputManager(mine_system, time_series)
        self.model            = self.build_model()
        self._apply_fixed_infra(fixed_infra)
        
        # Variable axis ordering (must match printer.py var_axis_order)
        # CRITICAL: Order must match variable definition in functions.py
        # Variable definitions (functions.py lines ~351-389):
        # Y: (i,j,d,t); Z: (i,d,t); Z_swap: (k,i,d,t); B: (i,d,t); B_s: (b,d,t)
        # S,X_dch,X_ini,W: (k,d,t); Sv: (k,d,t,t_start); N_chargers,X,N_batteries: (k)
        # M: (b,j,d); F: (j,d); F_seg: (j,d,seg)
        self.var_axis_order: Dict[str, List[str]] = {
            "Z":        ["i", "d", "t"],
            "Z_swap":   ["k", "i", "d", "t"],
            "Y":        ["i", "j", "d", "t"],
            "StartAssign": ["i", "d", "t"],
            "EndAssign":   ["i", "d", "t"],
            "B":        ["i", "d", "t"],
            "B_s":      ["b", "d", "t"],
            "N_chargers": ["k"],
            "X":        ["k"],
            "N_batteries": ["k"],
            "S":        ["k", "d", "t"],
            "Sv":       ["k", "d", "t", "t_start"],
            "X_dch":    ["k", "d", "t"],
            "X_ini":    ["k", "d", "t"],
            "W":        ["k", "d", "t"],
            "M":        ["b", "j", "d"],
            "F":        ["j", "d"],
            "F_seg":    ["j", "d", "seg"],
            "P_red":    ["d", "t"],
            "G_g":      ["g"],
            "P_gen":    ["g", "d", "t"],
            "H_h":      ["h"],
            "P_bat":    ["h", "d", "t"],
            "A_h":      ["h", "d", "t"],
        }
        
        self._load_solution_warmstart_folder(init_solution_folder, hard_vars_only=warmstart_hard_only)
        self._load_y_warmstart(y_init_path)

    def solve_model(self, gap, solvername, timelimit=172800, relax_integrality=False, threads=24):
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
            opt.options['Threads']      = threads
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
