import os
import sys
import time
import threading
import signal
import atexit
from contextlib import redirect_stdout
from pathlib import Path

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value
from pyomo.core.base import Suffix
from pyomo.opt import TerminationCondition
from pyomo.util.infeasible import log_infeasible_constraints

from gurobipy import GRB, read as grb_read

from src.optimization.functions import (
    OptSets,
    OptParameters,
    BoundRules,
    ConstraintRules,
    ObjectiveRules,
    OutputManager,
)


class OptModel(object):

    def __init__(self, mine_system, time_series, output_folder):
        self.output_folder   = output_folder
        self.set_builder      = OptSets(mine_system, time_series)
        self.param_rules      = OptParameters(mine_system, time_series)
        self.bound_rules      = BoundRules(mine_system, time_series)
        self.constraint_rules = ConstraintRules(mine_system, time_series)
        self.objective_rules  = ObjectiveRules(mine_system, time_series)
        self.output_manager   = OutputManager(mine_system, time_series)
        self.model            = self.build_model()
        
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
            self.model.branch_priority = Suffix(direction=Suffix.EXPORT, datatype=Suffix.INT)
            
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
            opt.options['TimeLimit'] = timelimit 

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
            result = opt.solve(self.model, tee=True, load_solutions=True)
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
                    print(f"⚠️ Error al escribir log: {e}")
        except Exception as e:
            print(f"⚠️ Ocurrió un error inesperado: {e}")
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
                print("⚠️ Se detuvo el proceso, pero no se encontró ninguna solución factible todavía.")
                self.limited_infeasible_log(self.model)

        else:
            print(f"⚠️ Termination condition: {self.solution_status}")
            self.limited_infeasible_log(self.model, timeout=60, log_file="infeasible_log.txt")

        try:
            if 'result' in locals() and hasattr(result.solver, 'relative_gap'):
                self.mip_gap = result.solver.relative_gap
            else:
                self.mip_gap = None
        except:
            self.mip_gap = None
            
        return 0
