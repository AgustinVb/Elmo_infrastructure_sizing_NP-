"""Valida que el corte de Benders sea un SUBESTIMADOR del costo futuro, en
puntos deliberadamente distintos de su ancla (rama carga_ob_multianio).

Por que hace falta un test aparte: el gate evalua el corte en el optimo del
monolitico, y si el forward acierta ese optimo, el ancla y el punto de
evaluacion COINCIDEN. Ahi todos los terminos mu*(x_hat - x) se anulan, el corte
colapsa a phi_lp y la comparacion se reduce a "la relajacion LP vale menos que
el MILP" -- cierto por construccion y sin ninguna informacion sobre la pendiente
del corte. Un corte con el signo invertido pasa ese chequeo sin problema.

Aca se evalua el corte en varios puntos perturbados alrededor del ancla y se
compara contra el costo futuro REAL en cada uno (el MILP del anio siguiente
resuelto con ese estado heredado). La direccion que discrimina es AUMENTAR el
estado: con mu >= 0 el corte tiene que BAJAR, igual que el costo real; con el
signo invertido subiria y cruzaria por encima.

Las perturbaciones se generan a partir de block.state_links, no de una lista
escrita a mano, asi que el test sigue valiendo si cambia el vector de estado.

Uso:  python tests/test_corte_fuera_del_ancla_ob.py [escenario] [anios]
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

DEFAULT_SCENARIO = "data/DCH/640kW_2dias/"
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


def es_conteo(valores):
    """True si la familia parece un conteo entero y chico (bahias, cargadores,
    modulos): ahi perturbar de a 1 unidad tiene sentido. Para magnitudes
    continuas grandes (la degradacion D) hay que escalar en cambio."""
    vals = list(valores.values()) if isinstance(valores, dict) else [valores]
    return all(abs(v - round(v)) < 1e-6 and abs(v) < 100 for v in vals)


def generar_perturbaciones(ancla, links):
    """Una perturbacion por familia (y por indice, hasta dos por familia para
    acotar el tiempo), mas una que sube todo a la vez y una que baja."""
    pruebas = [("ancla (sin perturbar)", copia(ancla))]
    familias_conteo = []

    for link in links:
        fam = link["state"]
        if fam not in ancla:
            continue
        base = ancla[fam]
        if es_conteo(base):
            if isinstance(base, dict):
                familias_conteo.append(fam)
                for idx in list(base)[:2]:
                    x = copia(ancla)
                    x[fam][idx] = x[fam][idx] + 1
                    pruebas.append((f"{fam}[{idx}] +1", x))
            else:
                x = copia(ancla)
                x[fam] = x[fam] + 1
                pruebas.append((f"{fam} +1", x))
        else:
            for factor, etiqueta in ((0.9, "-10%"), (1.05, "+5%")):
                x = copia(ancla)
                if isinstance(base, dict):
                    for idx in base:
                        x[fam][idx] = x[fam][idx] * factor
                else:
                    x[fam] = x[fam] * factor
                pruebas.append((f"{fam} {etiqueta}", x))

    if familias_conteo:
        x = copia(ancla)
        for fam in familias_conteo:
            for idx in x[fam]:
                x[fam][idx] = x[fam][idx] + 1
        pruebas.append(("todas las familias de conteo +1", x))

        fam = familias_conteo[0]
        idx = list(ancla[fam])[0]
        if ancla[fam][idx] >= 1:
            x = copia(ancla)
            x[fam][idx] = x[fam][idx] - 1
            pruebas.append((f"{fam}[{idx}] -1", x))

    return pruebas


def main(scenario, n_years):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=n_years)
    _s, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    exo = infer_exogenous_stations(mine_system, time_series)

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

    opt_cuts = [c for c in cm.history if not str(c.get("kind", "")).startswith("feasibility")]
    if not opt_cuts:
        print("no se genero ningun corte de optimalidad: nada que validar", flush=True)
        return 1
    cut = opt_cuts[-1]
    ancla = cut["x_hat_base"]
    print("", flush=True)
    print("    ancla del corte:", flush=True)
    for fam in sorted(ancla):
        print(f"      {fam:<12} {ancla[fam]}", flush=True)

    pruebas = generar_perturbaciones(ancla, b2.state_links)

    print("", flush=True)
    print("[2] corte contra costo futuro real en cada punto", flush=True)
    print("    {:<34} {:>16} {:>16}   veredicto".format("perturbacion", "corte(x)",
                                                        "Phi_2(x) real"), flush=True)
    for nombre, x in pruebas:
        b2.set_heritage(x)
        res = resolver(b2.model)
        tc = res.solver.termination_condition
        if tc in (TerminationCondition.infeasible, TerminationCondition.infeasibleOrUnbounded):
            print("    {:<34} {:>16} {:>16}   (sin informacion)".format(
                nombre, "", "INFACTIBLE"), flush=True)
            continue
        phi_real = value(b2.model.obj)
        c = valor_del_corte(cut, x)
        ok = c <= phi_real * (1 + 2 * GAP) + 1e-6
        if not ok:
            FAILS.append(nombre)
        print("    {:<34} {:>16,.2f} {:>16,.2f}   {}".format(
            nombre, c, phi_real, "OK" if ok else "FALLA -- el corte SOBREESTIMA"), flush=True)

    print("", flush=True)
    print("TODO OK" if not FAILS else "{} FALLAS: {}".format(len(FAILS), FAILS), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    escenario = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    anios = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    raise SystemExit(main(escenario, anios))
