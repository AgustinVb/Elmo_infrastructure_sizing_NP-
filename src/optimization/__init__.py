import time
from src.optimization.opt_model import OptModel
from src.io.printer import Printer


class OptimizationModel(object):
    def __init__(self, mine_system, time_series, gap,
                 solver_name, output_folder, timelimit=172800,
                 warm_start_folder=None, y_init_path=None,
                 init_solution_folder=None,
                 relax_integrality=False,
                 autonomous_mode=False,
                 mccormick_degradation=False,
                 mip_focus=3,
                 free_charging=False,
                 free_maintenance=False,
                 relax_operational=False,
                 warm_start_only_integers=False,
                 lb_externa=None):

        t0 = time.time()

        # 1) Construye y resuelve el modelo
        self.opt_model = OptModel(
            mine_system,
            time_series,
            output_folder,
            warm_start_folder=warm_start_folder,
            y_init_path=y_init_path,
            init_solution_folder=init_solution_folder,
            relax_integrality=relax_integrality,
            autonomous_mode=autonomous_mode,
            mccormick_degradation=mccormick_degradation,
            mip_focus=mip_focus,
            free_charging=free_charging,
            free_maintenance=free_maintenance,
            relax_operational=relax_operational,
            warm_start_only_integers=warm_start_only_integers,
        )
        print('formulation time:', time.time() - t0)
        if lb_externa is not None:
            # Cota inferior externa (p. ej. la operacional o la certificada),
            # igual que la fase monolitica de --mode hybrid en setup.py: la fila
            # obj >= LB es valida (toda solucion factible cumple obj >= optimo
            # >= LB) y hace que el BestBd de Gurobi parta en la cota real, asi
            # que su MIPGap vuelve a medir el gap verdadero. BestObjStop corta
            # cuando el incumbente cierra el gap contra esa cota. El margen
            # 1e-6 relativo protege de las tolerancias de Gurobi.
            import pyomo.environ as pyo
            lb_cut = lb_externa - abs(lb_externa) * 1e-6
            self.opt_model.model.lb_externa = pyo.Constraint(
                expr=self.opt_model.model.obj.expr >= lb_cut)
            self.opt_model.best_obj_stop = lb_externa / (1.0 - gap)
            print(f"[monolitico] fila obj >= {lb_cut:,.2f} (cota externa) y "
                  f"BestObjStop = {self.opt_model.best_obj_stop:,.2f}")
        self.opt_model.solve_model(gap, solver_name, timelimit=timelimit, relax_integrality=relax_integrality)

        if getattr(self.opt_model, 'opt_cost_result', None) is None:
            # Sin incumbente no hay nada que exportar: Printer evaluaria
            # variables sin valor y reventaria despues de horas de solve.
            print('WARN Sin solucion factible: se omite la exportacion de CSV y graficos.')
            return

        # 2) Exporta CSV y genera gráficos
        printer = Printer(self.opt_model,      
                          output_folder,
                          time_series,
                          mine_system)
        printer.create_all_plots()
