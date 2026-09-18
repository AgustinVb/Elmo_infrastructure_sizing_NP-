"""Mide el residuo del MIP start que la descomposicion le entrega al monolitico
(--mode hybrid), por familia de restricciones.

Contexto (CONTEXTO_pendientes_descomposicion.md sec. 3): a 6 anios Gurobi
descarto el arranque por una violacion de 5,7e-5 contra FeasibilityTol=1e-6 en
una restriccion sin identificar. A 2 anios el mayor residuo era 1,05e-9. Esto
corre una iteracion de la descomposicion, arma el modelo de reporte y lista las
familias con mayor violacion (NestedBendersSolver._mip_start_residuals).

Uso:  python tests/medir_residuo_mip_start_ob.py [escenario] [anios]
"""
import os
import sys
import tempfile
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

from setup import build_mine
from src.optimization.decomposition.driver import NestedBendersSolver, infer_exogenous_stations

DEFAULT_SCENARIO = "data/DCH/640kW_2dias_1MB/"
GAP = 0.01
TIMELIMIT = 600


def main(scenario, n_years):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=n_years)
    _s, mine_system, time_series = build_mine(args)
    exo = infer_exogenous_stations(mine_system, time_series)
    solver = NestedBendersSolver(
        mine_system, time_series, exo, gap_tol=GAP, max_iter=1,
        solver_kwargs={"solvername": "gurobi", "gap": GAP, "timelimit": TIMELIMIT},
        block_build_jobs=1, monolithic_lp_bound=False,
    )
    res = solver.solve(verbose=True)
    print(f"UB={res['ub']:,.2f}  cortes de factibilidad={res['feasibility_cuts']}", flush=True)
    out = tempfile.mkdtemp(prefix="residuo_mip_start_")
    solver.build_report_model(out)


if __name__ == "__main__":
    escenario = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    anios = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    main(escenario, anios)
