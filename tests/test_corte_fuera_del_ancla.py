"""Valida que el corte de Benders sea un SUBESTIMADOR del costo futuro, en
puntos deliberadamente distintos de su ancla.

Por que hace falta un test aparte: el gate (test_gate_cortes_swap.py) evalua el
corte en el optimo del monolitico, y cuando el forward acierta ese optimo en la
primera iteracion el ancla y el punto de evaluacion COINCIDEN. Ahi todos los
terminos mu*(x_hat - x) se anulan, el corte colapsa a phi_lp y la comparacion
se reduce a "la relajacion LP vale menos que el MILP" -- cierto por
construccion y sin ninguna informacion sobre la pendiente del corte. Un corte
con el signo invertido pasa ese chequeo sin problema.

Aca se evalua el corte en varios puntos perturbados alrededor del ancla y se
compara contra el costo futuro REAL en cada uno (el MILP del anio siguiente
resuelto con ese estado heredado). La direccion que discrimina es AUMENTAR el
estado: con mu >= 0 el corte tiene que BAJAR, igual que el costo real; con el
signo invertido subiria y cruzaria por encima.

Uso:  python tests/test_corte_fuera_del_ancla.py [carpeta_del_escenario]
"""
import os
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

from pyomo.environ import SolverFactory, value
from pyomo.opt import TerminationCondition

from setup import build_mine
from src.optimization.decomposition.cuts import BendersCutManager
from src.optimization.decomposition.driver import infer_exogenous_stations
from src.optimization.decomposition.passes import BackwardPass, ForwardPass
from src.optimization.decomposition.year_block import YearBlockBuilder

DEFAULT_SCENARIO = "data/DCH/160kW_2dias/"
GAP = 0.01
TIMELIMIT = 900
FAILS = []


def resolver(m):
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["MIPGap"] = GAP
    opt.options["TimeLimit"] = TIMELIMIT
    opt.options["OutputFlag"] = 0
    return opt.solve(m, load_solutions=True)


def valor_del_corte(cut, x):
    """phi_lp + sum_j mu_j * (x_hat_j - x_j)."""
    total = cut["phi_lp"]
    for fam, mu_fam in cut["mu"].items():
        base = cut["x_hat_base"][fam]
        if isinstance(mu_fam, dict):
            for idx, mu_v in mu_fam.items():
                total += mu_v * (base[idx] - x[fam][idx])
        else:
            total += mu_fam * (base - x[fam])
    return total


def copia(x):
    return {f: (dict(v) if isinstance(v, dict) else v) for f, v in x.items()}


def main(scenario):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=2)
    _s, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    exo = infer_exogenous_stations(mine_system, time_series)
    stations = list(mine_system.get_system_stations())
    s1, s2, s3 = stations[0], stations[1], stations[2]

    b1 = YearBlockBuilder(mine_system, time_series, year=years[0], is_last_year=False,
                          exogenous_stations=exo[years[0]])
    b2 = YearBlockBuilder(mine_system, time_series, year=years[1], is_last_year=True,
                          exogenous_stations=exo[years[1]])

    cm = BendersCutManager()
    kwargs = {"solvername": "gurobi", "gap": GAP, "timelimit": TIMELIMIT}
    print("[1] una iteracion para generar el corte", flush=True)
    ForwardPass([b1, b2], cut_manager=cm, solver_kwargs=kwargs).run(iteration=1, verbose=True)
    BackwardPass([b1, b2], cut_manager=cm, solver_kwargs=kwargs).run(
        {b1.year: b1.extract_state()}, iteration=1, verbose=True)

    cut = [c for c in cm.history if not str(c.get("kind", "")).startswith("feasibility")][-1]
    ancla = cut["x_hat_base"]
    print(f"\n    ancla del corte: N_bays={ancla['N_bays']}", flush=True)
    print(f"                     n_ssee_k={ancla['n_ssee_k']}  D={ancla['D']:.2f}", flush=True)

    # Perturbaciones. Las que AUMENTAN el estado son las discriminantes: con
    # mu >= 0 el corte baja, y un signo invertido lo haria subir por encima del
    # costo real.
    pruebas = []
    def agregar(nombre, cambios):
        x = copia(ancla)
        cambios(x)
        pruebas.append((nombre, x))

    agregar("N_bays[s1] +1",      lambda x: x["N_bays"].__setitem__(s1, x["N_bays"][s1] + 1))
    agregar("N_bays[s2] +1",      lambda x: x["N_bays"].__setitem__(s2, x["N_bays"][s2] + 1))
    agregar("N_chargers[s2] +1",  lambda x: x["N_chargers"].__setitem__(s2, x["N_chargers"][s2] + 1))
    agregar("N_batteries[s1] +1", lambda x: x["N_batteries"].__setitem__(s1, x["N_batteries"][s1] + 1))
    agregar("n_ssee_k[s3] +1",     lambda x: x["n_ssee_k"].__setitem__(s3, x["n_ssee_k"][s3] + 1))
    agregar("D -10%",             lambda x: x.__setitem__("D", x["D"] * 0.9))
    agregar("todo +1 en las 3 naves", lambda x: [
        x[f].__setitem__(k, x[f][k] + 1)
        for f in ("N_bays", "N_chargers", "N_batteries") for k in stations])
    agregar("N_bays[s1] -1",      lambda x: x["N_bays"].__setitem__(s1, x["N_bays"][s1] - 1))

    # El ancla primero: ahi el corte es EXACTO respecto de la relajacion (todos
    # los terminos mu*(x_hat - x) se anulan y queda phi_lp), asi que sirve de
    # linea base para leer las perturbaciones.
    pruebas.insert(0, ("ancla (sin perturbar)", copia(ancla)))

    print("\n[2] corte contra costo futuro real en cada punto", flush=True)
    print(f"    {'perturbacion':<30} {'corte(x)':>16} {'Phi_2(x) real':>16}   veredicto", flush=True)
    for nombre, x in pruebas:
        b2.set_heritage(x)
        res = resolver(b2.model)
        tc = res.solver.termination_condition
        if tc in (TerminationCondition.infeasible,
                  TerminationCondition.infeasibleOrUnbounded):
            print(f"    {nombre:<30} {'':>16} {'INFACTIBLE':>16}   (sin informacion)", flush=True)
            continue
        phi_real = value(b2.model.obj)
        c = valor_del_corte(cut, x)
        ok = c <= phi_real * (1 + 2 * GAP) + 1e-6
        if not ok:
            FAILS.append(nombre)
        print(f"    {nombre:<30} {c:>16,.2f} {phi_real:>16,.2f}   "
              f"{'OK' if ok else 'FALLA -- el corte SOBREESTIMA'}", flush=True)

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO))
