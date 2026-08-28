import time
from src.optimization.opt_model import OptModel
from src.io.printer import Printer


class OptimizationModel(object):
    def __init__(self, mine_system, time_series, gap,
                 solver_name, output_folder, timelimit=172800, y_init_path=None,
                 init_solution_folder=None,
                 relax_integrality=False,
                 autonomous_mode=False,
                 mccormick_degradation=False):

        t0 = time.time()

        # 1) Construye y resuelve el modelo
        self.opt_model = OptModel(
            mine_system,
            time_series,
            output_folder,
            y_init_path=y_init_path,
            init_solution_folder=init_solution_folder,
            autonomous_mode=autonomous_mode,
            mccormick_degradation=mccormick_degradation,
        )
        print('formulation time:', time.time() - t0)
        self.opt_model.solve_model(gap, solver_name, timelimit=timelimit, relax_integrality=relax_integrality)

        # 2) Exporta CSV y genera gráficos
        printer = Printer(self.opt_model,      
                          output_folder,
                          time_series,
                          mine_system)
        printer.create_all_plots()
