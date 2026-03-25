import time
from src.optimization.opt_model import OptModel
from src.io.printer import Printer


class OptimizationModel(object):
    def __init__(self, mine_system, time_series, gap,
                 solver_name, output_folder, warm_start_folder=None):

        t0 = time.time()

        # 1) Construye y resuelve el modelo
        self.opt_model = OptModel(mine_system, time_series, output_folder, warm_start_folder=warm_start_folder)
        print('formulation time:', time.time() - t0)
        self.opt_model.solve_model(gap, solver_name)

        # 2) Exporta CSV y genera gráficos
        printer = Printer(self.opt_model,      
                          output_folder,
                          time_series,
                          mine_system)
        printer.create_all_plots()
