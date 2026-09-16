"""Corrida multi-iteracion vigilando LB <= UB en CADA iteracion.

Por que existe: un corte invalido -- uno que corta parte del espacio factible --
no se detecta evaluandolo en un punto estatico, porque suele ser valido
justamente alrededor del punto donde se calibro. Se delata cuando el algoritmo
explora otros puntos y la cota inferior se cruza por encima de la superior. En
la rama carga_ob_multianio un corte fortalecido paso un chequeo de un solo
punto y recien aparecio como LB > UB en la iteracion 2 de una corrida real.

Invariantes que se verifican:
  1. LB <= UB en todas las iteraciones (si se rompe, hay un corte invalido);
  2. LB no decrece (la cota inferior solo puede mejorar al acumular cortes);
  3. el UB final no queda por debajo del optimo monolitico de referencia.

Con 3 anios o mas se ejercita ademas la propagacion ANIDADA: el bloque del
medio recibe un corte del anio siguiente y emite otro al anterior dentro de la
misma iteracion, que es lo que distingue este esquema de aplicar Benders anio
por anio. Con 2 anios ese camino no se toca.

Uso:  python tests/test_multiiteracion_swap.py [escenario] [iteraciones] [anios]
"""
import os
import pickle
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
# tests/ tambien, para reusar _cache_path del gate.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)

from setup import build_mine
from src.optimization.decomposition.driver import NestedBendersSolver, infer_exogenous_stations
from test_gate_cortes_swap import _cache_path

DEFAULT_SCENARIO = "data/DCH/160kW_2dias/"
GAP = 0.01
TIMELIMIT = 900
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAILS.append(label)


def main(scenario, max_iter, n_years):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=n_years)
    _s, mine_system, time_series = build_mine(args)
    exo = infer_exogenous_stations(mine_system, time_series)

    ref = None
    cache = _cache_path(scenario, args)
    if os.path.exists(cache):
        with open(cache, "rb") as fh:
            obj_mono, inv_est, _x = pickle.load(fh)
        ref = obj_mono - inv_est
        print(f"referencia monolitica comparable (de cache) = {ref:,.2f}\n", flush=True)

    solver = NestedBendersSolver(
        mine_system, time_series, exo, gap_tol=GAP, max_iter=max_iter,
        solver_kwargs={"solvername": "gurobi", "gap": GAP, "timelimit": TIMELIMIT},
        block_build_jobs=1,
    )
    res = solver.solve(verbose=True)

    print(f"\n{'iter':>5} {'UB':>18} {'LB':>18} {'gap':>10} {'seg':>8}", flush=True)
    for h in res["gap_history"]:
        print(f"{h['iteration']:>5} {h['ub']:>18,.2f} {h['lb']:>18,.2f} "
              f"{h['gap']:>9.2%} {h['iter_time_sec']:>8.0f}", flush=True)

    print("", flush=True)
    print("trayectoria de las decisiones de inversion del primer anio:", flush=True)
    for h in res["gap_history"]:
        x = h.get("x_hat_primer_anio", {})
        g = {k2: round(v2, 4) for k2, v2 in x.get("G", {}).items()}
        nmk = {k2: round(v2, 2) for k2, v2 in x.get("N_max_k", {}).items()}
        print(f"  k={h['iteration']:<3} G={g}  H={round(x.get('H', 0.0), 4)}  "
              f"N_max_k={nmk}", flush=True)

    print(f"\ncortes de factibilidad en total: "
          f"{solver.forward_pass.feasibility_cuts_added}", flush=True)

    print("\ninvariantes:", flush=True)
    for h in res["gap_history"]:
        check(h["lb"] <= h["ub"] * (1 + 2 * GAP) + 1e-6,
              f"iteracion {h['iteration']}: LB <= UB",
              f"{h['lb']:,.2f} <= {h['ub']:,.2f}")

    lbs = [h["lb"] for h in res["gap_history"]]
    check(all(b >= a - 1e-6 for a, b in zip(lbs, lbs[1:])),
          "LB no decrece", " -> ".join(f"{v:,.0f}" for v in lbs))

    if ref is not None:
        check(res["ub"] >= ref * (1 - 2 * GAP) - 1e-6,
              "UB final >= optimo monolitico", f"{res['ub']:,.2f} >= {ref:,.2f}")
        check(res["lb"] <= ref * (1 + 2 * GAP) + 1e-6,
              "LB final <= optimo monolitico", f"{res['lb']:,.2f} <= {ref:,.2f}")

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    escenario = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    iteraciones = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    anios = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    raise SystemExit(main(escenario, iteraciones, anios))
