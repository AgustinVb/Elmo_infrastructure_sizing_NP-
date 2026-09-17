"""Gate de validez del corte de Benders (rama carga_ob_multianio).

Es el chequeo que decide si la descomposicion esta bien planteada. Sobre un
horizonte corto, con X fijo al mismo vector exogeno en los dos modelos para que
sean el MISMO problema:

  1. los multiplicadores tienen el signo economico esperado, mu >= 0. OJO: es un
     chequeo de sanidad que atrapa un signo invertido, NO un teorema -- si
     heredar mas estado obliga a comprar otra cosa, un mu < 0 es legitimo y hay
     que interpretarlo, no tomarlo como bug;
  2. el corte generado no corta el optimo conocido del monolitico -- evaluado
     FUERA de su punto ancla, que es donde un corte con el signo invertido se
     delata (en el ancla cualquier signo da el mismo valor);
  3. tras una iteracion completa, LB <= optimo monolitico <= UB.

Las familias de estado NO estan escritas a mano: se leen de block.state_links,
que es la interfaz que publica el bloque anual. Asi el test sigue valiendo si
cambia el vector de estado.

Uso:  python tests/test_gate_cortes_ob.py [carpeta_del_escenario] [anios]
"""
import hashlib
import io
import os
import pickle
import sys
import tempfile
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value

from setup import build_mine
from src.optimization.decomposition.driver import NestedBendersSolver, infer_exogenous_stations
from src.optimization.functions import (
    BoundRules, ConstraintRules, ObjectiveRules, OptParameters, OptSets,
)

DEFAULT_SCENARIO = "data/DCH/640kW_2dias/"
GAP = 0.01
TIMELIMIT = 900
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + "  " + label + ("  -- " + detail if detail else ""),
          flush=True)
    if not cond:
        FAILS.append(label)


def solve(m, label):
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["MIPGap"] = GAP
    opt.options["TimeLimit"] = TIMELIMIT
    opt.options["OutputFlag"] = 0
    res = opt.solve(m, load_solutions=True)
    print("       {}: {}, obj = {:,.2f}".format(label, res.solver.termination_condition,
                                                value(m.obj)), flush=True)
    return value(m.obj)


def _cache_path(scenario, args):
    """La referencia monolitica es el tramo caro del gate. Se cachea en el temp
    del sistema con TRES hashes en la clave: functions.py (el modelo), setup.py
    (la lista de dias representativos, que define el horizonte y las series que
    se muestrean) y los datos de la instancia (costos y series en los Excel).
    Sin alguno de ellos, un cambio dejaria en pie una referencia calculada sobre
    otro problema, sin ninguna señal. GATE_NO_CACHE=1 la ignora.
    """
    model_src = io.open(os.path.join(REPO, "src", "optimization", "functions.py"), "rb").read()
    setup_src = io.open(os.path.join(REPO, "setup.py"), "rb").read()
    datos = hashlib.md5()
    for nombre in (args.model, args.series):
        ruta = os.path.join(REPO, scenario, nombre)
        if os.path.exists(ruta):
            datos.update(io.open(ruta, "rb").read())
    key = "|".join(["ob1", scenario, str(args.n_years), args.consumption_model,
                    hashlib.md5(model_src).hexdigest(),
                    hashlib.md5(setup_src).hexdigest(),
                    datos.hexdigest()])
    return os.path.join(tempfile.gettempdir(),
                        "gate_ob_mono_" + hashlib.md5(key.encode()).hexdigest()[:12] + ".pkl")


def estado_desde_monolitico(mono, links, y1):
    """Lee del monolitico el estado del anio y1 guiandose por state_links, en vez
    de nombrar las familias a mano."""
    x = {}
    for link in links:
        comp = getattr(mono, link["state_var"], None)
        if comp is None:
            continue
        es_global = link.get("kind") == "global_once"
        if link["index_set"] is None:
            x[link["state"]] = value(comp if es_global else comp[y1])
        else:
            x[link["state"]] = {
                idx: value(comp[idx] if es_global else comp[idx, y1])
                for idx in link["index_set"]
            }
    return x


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


def fuera_del_ancla(cut, x):
    for fam in cut["mu"]:
        base = cut["x_hat_base"][fam]
        if isinstance(base, dict):
            for idx in base:
                if abs(base[idx] - x[fam][idx]) > 1e-6:
                    return True
        elif abs(base - x[fam]) > 1e-6:
            return True
    return False


def es_factibilidad(c):
    return str(c.get("kind", "")).startswith("feasibility")


def main(scenario, n_years):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=n_years)
    _s, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    y1 = years[0]
    stations = list(mine_system.get_system_stations())
    exo_by_year = infer_exogenous_stations(mine_system, time_series)
    print("escenario: {}  ({} anios {}, {} naves)".format(scenario, len(years), years,
                                                          len(stations)), flush=True)

    print("", flush=True)
    print("[1] construyendo los bloques anuales", flush=True)
    solver = NestedBendersSolver(
        mine_system, time_series, exo_by_year, gap_tol=GAP, max_iter=1,
        solver_kwargs={"solvername": "gurobi", "gap": GAP, "timelimit": TIMELIMIT},
        block_build_jobs=1,
    )
    links = solver.blocks[-1].state_links
    print("       familias de estado: {}".format([l["state"] for l in links]), flush=True)

    print("", flush=True)
    print("[2] optimo del monolitico (referencia)", flush=True)
    cache = _cache_path(scenario, args)
    if os.path.exists(cache) and not os.environ.get("GATE_NO_CACHE"):
        with open(cache, "rb") as fh:
            obj_mono, inv_est, x_star = pickle.load(fh)
        print("       monolitico (de cache): obj = {:,.2f}".format(obj_mono), flush=True)
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
        obj_mono = solve(mono, "monolitico")
        inv_est = sum(value(mono.station_cost_k[k]) * value(mono.Delta_X[k, y])
                      * value(rules._discount_factor(mono, y))
                      for k in stations for y in years)
        x_star = estado_desde_monolitico(mono, links, y1)
        with open(cache, "wb") as fh:
            pickle.dump((obj_mono, inv_est, x_star), fh)
    ref = obj_mono - inv_est
    print("       inversion en estaciones (solo en el monolitico): {:,.2f}".format(inv_est),
          flush=True)
    print("       referencia comparable = {:,.2f}".format(ref), flush=True)
    print("       estado del anio 1 en el optimo: {}".format(x_star), flush=True)

    print("", flush=True)
    print("[3] una iteracion de Nested Benders", flush=True)
    res = solver.solve(verbose=True)
    print("       UB={:,.2f}  LB={:,.2f}  gap={:.4%}".format(res["ub"], res["lb"], res["gap"]),
          flush=True)
    print("       cortes de factibilidad en el forward: {}".format(
        solver.forward_pass.feasibility_cuts_added), flush=True)

    check(res["lb"] <= ref * (1 + 2 * GAP) + 1e-6,
          "LB <= optimo monolitico", "{:,.2f} <= {:,.2f}".format(res["lb"], ref))
    check(res["ub"] >= ref * (1 - 2 * GAP) - 1e-6,
          "UB >= optimo monolitico", "{:,.2f} >= {:,.2f}".format(res["ub"], ref))

    print("", flush=True)
    print("[4] signos de los multiplicadores", flush=True)
    feas_cuts = [c for c in solver.cut_manager.history if es_factibilidad(c)]
    opt_cuts = [c for c in solver.cut_manager.history if not es_factibilidad(c)]
    for c in feas_cuts:
        atan = {f: (max(v.values()) if isinstance(v, dict) else v) for f, v in c["mu"].items()}
        atan = sorted(f for f, v in atan.items() if v > 1e-7)
        extra = ("" if c.get("rhs_redondeado") is None
                 else "  -> redondeado a suma >= {}".format(c["rhs_redondeado"]))
        print("       [factibilidad] v={:,.4f}  familias que atan: {}{}".format(
            c["v_hat"], atan, extra), flush=True)
    if not opt_cuts:
        check(False, "hay al menos un corte de optimalidad que validar")
        print("", flush=True)
        print("TODO OK" if not FAILS else "{} FALLAS: {}".format(len(FAILS), FAILS), flush=True)
        return 1 if FAILS else 0

    cut = opt_cuts[-1]
    for fam in sorted(cut["mu"]):
        mu_fam = cut["mu"][fam]
        vals = list(mu_fam.values()) if isinstance(mu_fam, dict) else [mu_fam]
        print("       mu[{}] = {}".format(fam, ["{:+.4g}".format(v) for v in vals]), flush=True)
        check(all(v >= -1e-6 for v in vals),
              "mu[{}] >= 0 (sensibilidad con el signo economico esperado)".format(fam))

    print("", flush=True)
    print("[5] el corte no corta el optimo conocido", flush=True)
    faltan = [f for f in cut["mu"] if f not in x_star or x_star[f] is None]
    if faltan:
        raise AssertionError(
            "El corte tiene multiplicadores en {} pero x_star no trae esas familias: "
            "el chequeo estaria evaluando el corte en un punto que no es el optimo "
            "del monolitico.".format(faltan)
        )
    child = solver.blocks[1]
    child.set_heritage({k: v for k, v in x_star.items() if v is not None})
    phi_real = solve(child.model, "Phi_2 en el optimo ")
    cut_val = valor_del_corte(cut, x_star)
    print("       corte evaluado en x*  = {:,.2f}".format(cut_val), flush=True)
    print("       costo futuro real     = {:,.2f}".format(phi_real), flush=True)
    print("       (evaluado fuera del ancla: {})".format(fuera_del_ancla(cut, x_star)), flush=True)
    check(cut_val <= phi_real * (1 + 2 * GAP) + 1e-6,
          "el corte no sobreestima el costo futuro en el optimo del monolitico")

    print("", flush=True)
    print("TODO OK" if not FAILS else "{} FALLAS: {}".format(len(FAILS), FAILS), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    escenario = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    anios = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    raise SystemExit(main(escenario, anios))
