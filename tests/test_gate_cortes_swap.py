"""M2 -- gate de validez del corte de Benders.

Es el chequeo que decide si la descomposicion esta bien planteada. Sobre un
horizonte de 2 anios, con X fijo al mismo vector exogeno en los dos modelos
para que sean el MISMO problema:

  1. los multiplicadores tienen el signo economico esperado, mu >= 0. OJO: es
     un chequeo de sanidad que atrapa un signo invertido, NO un teorema. En
     este modelo la monotonia NO se cumple: N_bays[k,y] <= N_chargers[k,y]
     (functions.py) hace que heredar una bahia de mas obligue a comprar un
     cargador, y como el costo se cobra sobre los Delta y nunca sobre el
     stock, heredar mas puede ENCARECER el anio siguiente. Un mu < 0 hay que
     interpretarlo, no tomarlo automaticamente como bug;
  2. el corte generado no corta el optimo conocido del monolitico -- evaluado
     FUERA de su punto ancla, que es donde un corte con el signo invertido se
     delata (en el ancla cualquier signo da el mismo valor);
  3. tras una iteracion completa, LB <= optimo monolitico <= UB.

Uso:  python tests/test_gate_cortes_swap.py [carpeta_del_escenario]
"""
import io
import hashlib
import os
import pickle
import tempfile
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, TransformationFactory, value

from setup import build_mine
from src.optimization.decomposition.driver import NestedBendersSolver, infer_exogenous_stations
from src.optimization.functions import (
    BoundRules, ConstraintRules, ObjectiveRules, OptParameters, OptSets,
)

DEFAULT_SCENARIO = "data/DCH/160kW_2dias/"
GAP = 0.01
TIMELIMIT = 900
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def solve(m, label):
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["MIPGap"] = GAP
    opt.options["TimeLimit"] = TIMELIMIT
    opt.options["OutputFlag"] = 0
    res = opt.solve(m, load_solutions=True)
    print(f"       {label}: {res.solver.termination_condition}, obj = {value(m.obj):,.2f}")
    return value(m.obj)


def _cache_path(scenario, args):
    """La referencia monolitica es el tramo caro del gate y no cambia entre
    corridas mientras no se toque el modelo. Se cachea en el temp del sistema,
    con el hash de functions.py en la clave para que se invalide sola si el
    modelo cambia. GATE_NO_CACHE=1 la ignora."""
    # El hash de functions.py invalida la cache si cambia el MODELO; el de
    # setup.py, si cambia la lista de dias representativos (que define el
    # horizonte y las series que se muestrean). Sin este segundo hash, cambiar
    # los dias dejaba la referencia vieja en pie y el test comparaba contra un
    # optimo de otro problema, sin avisar.
    model_src = io.open(os.path.join(REPO, "src", "optimization", "functions.py"),
                        "rb").read()
    setup_src = io.open(os.path.join(REPO, "setup.py"), "rb").read()
    # Y los datos de la instancia: editar costos o series en el Excel cambia el
    # optimo sin tocar una sola linea de codigo, y sin esto la referencia vieja
    # quedaria en pie sin avisar.
    datos = hashlib.md5()
    for nombre in (args.model, args.series):
        ruta = os.path.join(REPO, scenario, nombre)
        if os.path.exists(ruta):
            datos.update(io.open(ruta, "rb").read())
    key = "|".join(["v2", scenario, str(args.n_years), args.consumption_model,
                    hashlib.md5(model_src).hexdigest(),
                    hashlib.md5(setup_src).hexdigest(),
                    datos.hexdigest()])
    return os.path.join(tempfile.gettempdir(),
                        "gate_swap_mono_" + hashlib.md5(key.encode()).hexdigest()[:12] + ".pkl")


def main(scenario):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=2)
    _s, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    y1, y2 = years[0], years[1]
    stations = list(mine_system.get_system_stations())
    exo_by_year = infer_exogenous_stations(mine_system, time_series)
    print(f"escenario: {scenario}  ({len(years)} anios {years}, {len(stations)} naves)")

    print("\n[1] optimo del monolitico de 2 anios (referencia)")
    cache = _cache_path(scenario, args)
    if os.path.exists(cache) and not os.environ.get("GATE_NO_CACHE"):
        with open(cache, "rb") as fh:
            obj_mono, inv_est, x_star = pickle.load(fh)
        print(f"       monolitico 2 anios (de cache): obj = {obj_mono:,.2f}")
    else:
        mono = pyo.ConcreteModel()
        OptSets(mine_system, time_series).build_sets(mono)
        OptParameters(mine_system, time_series).build_parameters(mono)
        BoundRules(mine_system, time_series).build_all_variables(mono)
        ConstraintRules(mine_system, time_series,
                        mccormick_degradation=True).build_all_constraints(mono)
        rules = ObjectiveRules(mine_system, time_series)
        mono.obj = pyo.Objective(rule=rules.total_cost, sense=pyo.minimize)
        for y in years:
            for k in stations:
                mono.X[k, y].fix(exo_by_year[y][k])
                mono.Delta_X[k, y].fix(exo_by_year[y][k] if y == y1 else 0)
        obj_mono = solve(mono, "monolitico 2 anios")
        inv_est = (sum(value(mono.station_cost_k[k]) * exo_by_year[y1][k]
                       for k in stations)
                   * value(rules._discount_factor(mono, y1)))
        x_star = {
            "N_bays": {k: value(mono.N_bays[k, y1]) for k in stations},
            "N_chargers": {k: value(mono.N_chargers[k, y1]) for k in stations},
            "N_batteries": {k: value(mono.N_batteries[k, y1]) for k in stations},
            "N_max_k": {k: value(mono.N_max_k[k]) for k in stations},
            "D": value(mono.D[y1]) if mine_system.battery_degradation else None,
        }
        if len(list(mono.gen_set)) > 0:
            x_star["G"] = {g: value(mono.G_g[g]) for g in mono.gen_set}
        if len(list(mono.storage_set)) > 0:
            x_star["H"] = value(mono.H)
        with open(cache, "wb") as fh:
            pickle.dump((obj_mono, inv_est, x_star), fh)
    ref = obj_mono - inv_est
    print(f"       inversion en estaciones (solo en el monolitico): {inv_est:,.2f}")
    print(f"       referencia comparable = {ref:,.2f}")
    print(f"       estado optimo del anio 1: "
          f"bays={list(x_star['N_bays'].values())} "
          f"chg={list(x_star['N_chargers'].values())} "
          f"bat={list(x_star['N_batteries'].values())} "
          f"N_max_k={list(x_star['N_max_k'].values())} D={x_star['D']}")

    print("\n[2] una iteracion de Nested Benders")
    solver = NestedBendersSolver(
        mine_system, time_series, exo_by_year, gap_tol=GAP, max_iter=1,
        solver_kwargs={"solvername": "gurobi", "gap": GAP, "timelimit": TIMELIMIT},
        block_build_jobs=1,
    )
    res = solver.solve(verbose=True)
    print(f"       UB={res['ub']:,.2f}  LB={res['lb']:,.2f}  gap={res['gap']:.4%}")
    print(f"       cortes de factibilidad agregados en el forward: "
          f"{solver.forward_pass.feasibility_cuts_added}")

    check(res["lb"] <= ref * (1 + 2 * GAP) + 1e-6,
          "LB <= optimo monolitico", f"{res['lb']:,.2f} <= {ref:,.2f}")
    check(res["ub"] >= ref * (1 - 2 * GAP) - 1e-6,
          "UB >= optimo monolitico", f"{res['ub']:,.2f} >= {ref:,.2f}")

    print("\n[3] signos de los multiplicadores")
    # Los cortes de factibilidad tambien quedan en el historial: el que se
    # valida abajo es el de optimalidad que agrega el backward, y en un
    # horizonte de 2 anios con una iteracion tiene que haber exactamente uno.
    def _es_factibilidad(c):
        # "feasibility" o "feasibility-entero", segun si se pudo redondear.
        return str(c.get("kind", "")).startswith("feasibility")

    feas_cuts = [c for c in solver.cut_manager.history if _es_factibilidad(c)]
    opt_cuts = [c for c in solver.cut_manager.history if not _es_factibilidad(c)]
    for c in feas_cuts:
        atan = {f: (max(v.values()) if isinstance(v, dict) else v)
                for f, v in c["mu"].items()}
        atan = sorted(f for f, v in atan.items() if v > 1e-7)
        extra = ("" if c.get("rhs_redondeado") is None
                 else f"  -> redondeado a suma >= {c['rhs_redondeado']}")
        print(f"       [factibilidad] v={c['v_hat']:,.4f}  familias que atan: {atan}{extra}")
    assert len(opt_cuts) == 1, solver.cut_manager.history
    cut = opt_cuts[0]
    for fam, mu_fam in sorted(cut["mu"].items()):
        vals = list(mu_fam.values()) if isinstance(mu_fam, dict) else [mu_fam]
        print(f"       mu[{fam}] = {[f'{v:+.4g}' for v in vals]}")
        check(all(v >= -1e-6 for v in vals),
              f"mu[{fam}] >= 0 (sensibilidad con el signo economico esperado)")

    print("\n[4] el corte no corta el optimo conocido")
    # Costo futuro REAL en el estado optimo del monolitico: el bloque del anio 2
    # resuelto como MILP con ese estado heredado.
    child = solver.blocks[1]
    child.set_heritage({k: v for k, v in x_star.items() if v is not None})
    phi_real = solve(child.model, "Phi_2 en el optimo ")

    faltan = [f for f in cut["mu"] if f not in x_star or x_star[f] is None]
    if faltan:
        raise AssertionError(
            f"El corte tiene multiplicadores en {faltan} pero x_star no trae esas "
            f"familias: el chequeo [4] estaria evaluando el corte en un punto que "
            f"no es el optimo del monolitico."
        )
    cut_val = cut["phi_lp"]
    for fam, mu_fam in cut["mu"].items():
        base = cut["x_hat_base"][fam]
        if isinstance(mu_fam, dict):
            for idx, mu_v in mu_fam.items():
                cut_val += mu_v * (base[idx] - x_star[fam][idx])
        else:
            cut_val += mu_fam * (base - x_star[fam])
    print(f"       ancla del corte x_hat = bays={list(cut['x_hat_base']['N_bays'].values())}")
    print(f"       corte evaluado en x*  = {cut_val:,.2f}")
    print(f"       costo futuro real     = {phi_real:,.2f}")
    fuera_del_ancla = any(
        abs(cut["x_hat_base"][f][i] - x_star[f][i]) > 1e-6
        for f in ("N_bays", "N_chargers", "N_batteries") for i in stations
    )
    print(f"       (evaluado fuera del ancla: {fuera_del_ancla})")
    check(cut_val <= phi_real * (1 + 2 * GAP) + 1e-6,
          "el corte no sobreestima el costo futuro en el optimo del monolitico")

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO))
