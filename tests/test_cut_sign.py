"""Regresion del signo del multiplicador con que se arman los cortes.

Ejercita el BendersCutManager REAL (read_duals + add_cut) sobre una instancia
de dos años y una estacion con optimo conocido:

  - año 1: hacen falta N1 >= 3 cargadores, costo unitario 10
  - año 2: hacen falta N2 >= 7, costo unitario 25 sobre el INCREMENTO
           (N2 = N1 + Delta2), es decir Phi_2(N1) = 25 * max(0, 7 - N1)

  costo total = 10*N1 + 25*max(0, 7-N1)  ->  optimo 70 con N1 = 7.

El forward sin cortes elige N1 = 3 (miope, cada año mira solo su costo). El
corte generado en ese punto tiene que llevar al año 1 hasta N1 = 7. Si el
multiplicador entra con el signo invertido --el dual crudo pi del solver en vez
de mu = -pi, ver cuts.read_duals-- el corte penaliza invertir en lugar de
premiarlo, sobreestima el costo futuro fuera del punto ancla y la cota inferior
termina por encima del optimo real (el sintoma que se observa como LB > UB).

Uso:  python tests/test_cut_sign.py      (requiere Gurobi)
"""
import os
import sys

import pyomo.environ as pyo
from pyomo.core.base import Suffix
from pyomo.environ import SolverFactory, value

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.optimization.decomposition.cuts import BendersCutManager  # noqa: E402

TRUE_OPT = 70.0
C1, C2 = 10.0, 25.0
DEM1, DEM2 = 3.0, 7.0
NMAX = 20.0


class FakeBlock(object):
    """Lo minimo que consumen read_duals/add_cut: model, year, state_links."""

    def __init__(self, model, year, state_links):
        self.model = model
        self.year = year
        self.state_links = state_links


def _solve(m):
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["OutputFlag"] = 0
    opt.solve(m, load_solutions=True)


def build_parent():
    m = pyo.ConcreteModel()
    m.K = pyo.Set(initialize=[1])
    m.Y = pyo.Set(initialize=[1])
    m.N_chargers = pyo.Var(m.K, m.Y, domain=pyo.NonNegativeReals, bounds=(0, NMAX))
    m.demanda = pyo.Constraint(m.K, rule=lambda mm, k: mm.N_chargers[k, 1] >= DEM1)
    m.alpha = pyo.Var(domain=pyo.NonNegativeReals)
    m.cuts = pyo.ConstraintList()
    m.obj = pyo.Objective(
        expr=C1 * sum(m.N_chargers[k, 1] for k in m.K) + m.alpha, sense=pyo.minimize
    )
    links = [{"state": "N_chargers", "state_var": "N_chargers", "hat": "N_hat",
              "prev": "N_prev", "index_set": m.K, "kind": "simple"}]
    return FakeBlock(m, 1, links)


def build_child():
    m = pyo.ConcreteModel()
    m.K = pyo.Set(initialize=[1])
    m.Y = pyo.Set(initialize=[2])
    m.N_hat = pyo.Param(m.K, initialize=0.0, mutable=True)
    m.N_prev = pyo.Var(m.K, domain=pyo.NonNegativeReals, bounds=(0, NMAX))
    m.Delta = pyo.Var(m.K, domain=pyo.NonNegativeReals)
    m.N_chargers = pyo.Var(m.K, m.Y, domain=pyo.NonNegativeReals, bounds=(0, NMAX))
    # Mismo patron que year_block.py: link_<estado> + accum_<estado>.
    m.link_N_chargers = pyo.Constraint(m.K, rule=lambda mm, k: mm.N_prev[k] == mm.N_hat[k])
    m.accum_N_chargers = pyo.Constraint(
        m.K, rule=lambda mm, k: mm.N_chargers[k, 2] == mm.N_prev[k] + mm.Delta[k]
    )
    m.demanda = pyo.Constraint(m.K, rule=lambda mm, k: mm.N_chargers[k, 2] >= DEM2)
    m.alpha = pyo.Var(domain=pyo.NonNegativeReals)
    m.alpha.fix(0.0)  # ultimo año
    m.obj = pyo.Objective(expr=C2 * sum(m.Delta[k] for k in m.K) + m.alpha, sense=pyo.minimize)
    m.dual = Suffix(direction=Suffix.IMPORT)
    links = [{"state": "N_chargers", "state_var": "N_chargers", "hat": "N_hat",
              "prev": "N_prev", "index_set": m.K, "kind": "simple"}]
    return FakeBlock(m, 2, links)


def phi_child(child, n_hat):
    for k in child.model.K:
        child.model.N_hat[k].set_value(n_hat)
    _solve(child.model)
    return value(child.model.obj)


def main():
    parent, child = build_parent(), build_child()
    cm = BendersCutManager()

    # Forward sin cortes: el año 1 es miope y se queda en su cota inferior.
    _solve(parent.model)
    x_hat = {"N_chargers": {k: value(parent.model.N_chargers[k, 1]) for k in parent.model.K}}
    n1 = x_hat["N_chargers"][1]
    print(f"forward k=1: N1 = {n1:.2f}  (costo año 1 = {C1 * n1:,.2f})")

    # Backward: LP del hijo con ese estado + lectura de multiplicadores.
    phi_lp = phi_child(child, n1)
    mu = cm.read_duals(child.model, 2, child.state_links)
    mu_v = mu["N_chargers"][1]
    print(f"backward   : Phi_LP(N1={n1:.0f}) = {phi_lp:,.2f}   mu = {mu_v:+.3f}")

    ok = True
    if mu_v <= 0:
        print("  FALLA: mu deberia ser positivo (heredar mas cargadores ABARATA "
              "el año siguiente; mu = -dPhi/dx_hat)")
        ok = False

    # El corte tal como lo arma add_cut, evaluado FUERA del punto ancla: es ahi
    # donde se nota el signo (en el ancla los dos signos coinciden).
    cm.add_cut(parent, phi_lp, mu, x_hat, iteration=1)
    for n_test in (4.0, 7.0):
        cut_val = phi_lp + mu_v * (n1 - n_test)
        real = phi_child(child, n_test)
        valido = cut_val <= real + 1e-6
        print(f"  corte en N1={n_test:.0f}: {cut_val:8.2f}   Phi real = {real:8.2f}   "
              + ("ok" if valido else "INVALIDO: sobreestima el costo futuro"))
        ok = ok and valido

    # El año 1 se vuelve a resolver con el corte: esa es la cota inferior.
    _solve(parent.model)
    lb = value(parent.model.obj)
    n1_new = value(parent.model.N_chargers[1, 1])
    print(f"\nLB = {lb:,.2f}   (N1 = {n1_new:.2f})   optimo real = {TRUE_OPT:,.2f}")
    if lb > TRUE_OPT + 1e-6:
        print("FALLA: LB > optimo real -- la cota inferior no es valida")
        ok = False
    if abs(lb - TRUE_OPT) > 1e-6:
        print("FALLA: el corte no llevo al año 1 al nivel de inversion correcto")
        ok = False

    print("OK" if ok else "HAY FALLAS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
