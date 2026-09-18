"""Validacion del presolve de capacidad de subestacion
(NestedBendersSolver._capacity_presolve / YearBlockBuilder.capacity_presolve_mode).

Lo que afirma el presolve: como n_ssee_k se decide una sola vez para todo el
horizonte, n_ssee_k[k] >= max_y n*_y(k), con n*_y(k) el minimo de capacidad que
hace factible al anio y con todo su estado heredado libre. Es una relajacion
del anio en contexto, asi que la cota es valida para CUALQUIER solucion
factible del problema completo -- no solo para el optimo.

Lo que se verifica:
  1. el modo presolve deja el bloque exactamente como lo encontro (mismas
     restricciones activas, mismo objetivo, sin componentes de mas);
  2. la cota es valida: no supera el n_ssee_k de una solucion factible del
     monolitico (la referencia cacheada del gate, o un incumbente con limite
     de tiempo -- para la validez basta un punto factible);
  3. la corrida descompuesta con el presolve respeta la cota y mantiene
     LB <= UB; el numero de cortes de factibilidad se informa (la cota es
     valida pero no completa: las naves se acoplan por la meta diaria total,
     asi que el minimo por nave con las otras libres puede quedar corto --
     medido en carga on board);
  4. (opcional, --comparar) la misma corrida sin presolve: con presolve no
     puede haber MAS cortes de factibilidad ni peor LB.

Uso:  python tests/test_presolve_capacidad_swap.py [escenario] [anios]
          [--modo peak|all] [--sin_referencia] [--comparar]
  --modo           que anios resuelve el presolve (default peak, como el driver)
  --sin_referencia salta [3], que resuelve el monolitico (hasta TIMELIMIT s)
"""
import os
import pickle
import sys
import time
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value

from setup import build_mine
from src.optimization.decomposition.driver import NestedBendersSolver, infer_exogenous_stations
from src.optimization.functions import (
    BoundRules, ConstraintRules, ObjectiveRules, OptParameters, OptSets,
)
from test_gate_cortes_swap import _cache_path

DEFAULT_SCENARIO = "data/DCH/160kW_2dias/"
GAP = 0.01
TIMELIMIT = 600
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAILS.append(label)


def firma(model):
    """(restricciones activas, objetivos activos, nombres de componentes)."""
    activas = sorted(
        c.name for c in model.component_objects(pyo.Constraint, active=True)
    )
    objetivos = sorted(o.name for o in model.component_objects(pyo.Objective, active=True))
    nombres = sorted(c.name for c in model.component_objects(descend_into=False))
    return activas, objetivos, nombres


def n_ssee_de_una_solucion_factible(mine_system, time_series, exo_by_year, links, cache):
    """n_ssee_k de una solucion factible del monolitico con X exogena. Lee la
    referencia cacheada del gate si existe (es el optimo); si no, resuelve el
    monolitico con limite de tiempo y usa el incumbente, que no se cachea
    porque el gate espera un optimo, no un incumbente."""
    years = sorted(time_series.years)
    y1 = years[0]
    if os.path.exists(cache) and not os.environ.get("GATE_NO_CACHE"):
        with open(cache, "rb") as fh:
            _obj, _inv, x_star = pickle.load(fh)
        return x_star["n_ssee_k"], "optimo monolitico (cache del gate)"

    mono = pyo.ConcreteModel()
    OptSets(mine_system, time_series).build_sets(mono)
    OptParameters(mine_system, time_series).build_parameters(mono)
    BoundRules(mine_system, time_series).build_all_variables(mono)
    ConstraintRules(mine_system, time_series,
                    mccormick_degradation=True).build_all_constraints(mono)
    mono.obj = pyo.Objective(rule=ObjectiveRules(mine_system, time_series).total_cost,
                             sense=pyo.minimize)
    for y in years:
        for k in mono.stations_set:
            mono.X[k, y].fix(exo_by_year[y][k])
            mono.Delta_X[k, y].fix(exo_by_year[y][k] if y == y1 else 0)
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["MIPGap"] = GAP
    opt.options["TimeLimit"] = TIMELIMIT
    opt.options["OutputFlag"] = 0
    res = opt.solve(mono, load_solutions=True)
    print(f"       monolitico: {res.solver.termination_condition}, "
          f"obj = {value(mono.obj):,.2f}", flush=True)
    x = {k: value(mono.n_ssee_k[k]) for k in mono.stations_set}
    return x, f"incumbente monolitico ({res.solver.termination_condition})"


def main(scenario, n_years, comparar, modo, con_referencia):
    args = Namespace(data_folder=scenario, model="elmo_data.xlsx",
                     series="time_series.xlsx", consumption_model="wp2",
                     wp2_consumption_json=None, n_years=n_years)
    _s, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    exo_by_year = infer_exogenous_stations(mine_system, time_series)
    stations = list(mine_system.get_system_stations())
    print(f"escenario: {scenario}  ({len(years)} anios {years}, {len(stations)} naves)",
          flush=True)

    kwargs = dict(
        gap_tol=GAP, max_iter=1,
        solver_kwargs={"solvername": "gurobi", "gap": GAP, "timelimit": TIMELIMIT},
        block_build_jobs=1,
    )
    print("\n[1] el modo presolve restaura el bloque", flush=True)
    solver = NestedBendersSolver(mine_system, time_series, exo_by_year,
                                 capacity_presolve=modo, **kwargs)
    blk = solver.blocks[-1]   # el ultimo anio hereda todo el estado
    antes = firma(blk.model)
    k0 = stations[0]
    with blk.capacity_presolve_mode(k0) as aux:
        durante = firma(aux)
        check("presolve_obj" in durante[1] and durante[1] == ["presolve_obj"],
              "en modo presolve el unico objetivo activo es presolve_obj", str(durante[1]))
        links_hat = [l["state"] for l in blk.state_links if l["hat"] is not None]
        desactivadas = [f"link_{s}" for s in links_hat]
        check(all(name not in durante[0] for name in desactivadas),
              "en modo presolve todas las igualdades de fijacion estan desactivadas",
              str(desactivadas))
        check(all(f"feas_link_{s}" not in durante[0] for s in links_hat),
              "las igualdades elasticas siguen desactivadas")
    despues = firma(blk.model)
    check(antes == despues, "al salir, el bloque queda identico (activas/objetivo/componentes)")
    check(blk.model.obj.active and not hasattr(blk.model, "presolve_obj"),
          "objetivo original activo y sin presolve_obj residual")

    print(f"\n[2] presolve de capacidad ({modo})", flush=True)
    t0 = time.time()
    bounds = solver._capacity_presolve(verbose=True)
    t_pre = time.time() - t0
    print(f"       cotas = {bounds}   ({t_pre:.0f}s)", flush=True)
    first = solver.blocks[0].model
    check(all(first.n_ssee_k[k].lb == bounds[k] for k in stations),
          "la cota quedo impuesta como lb de n_ssee_k en el anio 1",
          str({k: first.n_ssee_k[k].lb for k in stations}))
    check(any(bounds[k] > 0 for k in stations),
          "al menos una nave recibio una cota no trivial", str(bounds))
    if solver.capacity_sum_bound is not None:
        print(f"       cota agregada: sum n_ssee_k >= {solver.capacity_sum_bound}", flush=True)
        check(hasattr(first, "presolve_capacity_sum"),
              "la cota agregada quedo impuesta como restriccion en el anio 1")
    else:
        print("       cota agregada: redundante con las cotas por nave", flush=True)

    if con_referencia:
        print("\n[3] validez: cota <= n_ssee_k de una solucion factible del monolitico",
              flush=True)
        links = solver.blocks[-1].state_links
        n_ref, origen = n_ssee_de_una_solucion_factible(
            mine_system, time_series, exo_by_year, links, _cache_path(scenario, args))
        print(f"       referencia: {origen}: {n_ref}", flush=True)
        for k in stations:
            check(bounds[k] <= int(round(n_ref[k])) + 1e-9,
                  f"{k}: cota {bounds[k]} <= n_ssee_k factible {int(round(n_ref[k]))}")
            if bounds[k] == int(round(n_ref[k])):
                print(f"       {k}: la cota es AJUSTADA contra la referencia", flush=True)
            else:
                print(f"       {k}: la cota queda {int(round(n_ref[k])) - bounds[k]} "
                      f"modulos por debajo de la referencia", flush=True)
    else:
        print("\n[3] (salteado: --sin_referencia)", flush=True)

    print("\n[4] una iteracion con el presolve ya aplicado", flush=True)
    solver.capacity_presolve = None   # ya corrio a mano en [2]; no repetirlo
    solver.capacity_bounds = bounds
    res = solver.solve(verbose=True)
    n_fwd = res["best_solution"][years[0]]["n_ssee_k"]
    print(f"       UB={res['ub']:,.2f}  LB={res['lb']:,.2f}  gap={res['gap']:.4%}  "
          f"cortes de factibilidad={res['feasibility_cuts']}  n_ssee_k={n_fwd}", flush=True)
    check(all(int(round(n_fwd[k])) >= bounds[k] for k in stations),
          "el forward respeta la cota", str(n_fwd))
    if solver.capacity_sum_bound is not None:
        check(sum(int(round(v)) for v in n_fwd.values()) >= solver.capacity_sum_bound,
              "el forward respeta la cota agregada")
    print(f"       cortes de factibilidad con el presolve: {res['feasibility_cuts']} "
          f"(informativo; la cota es valida, no completa)", flush=True)
    check(res["lb"] <= res["ub"] * (1 + 2 * GAP) + 1e-6, "LB <= UB",
          f"{res['lb']:,.2f} <= {res['ub']:,.2f}")

    if comparar:
        print("\n[5] la misma iteracion SIN presolve (referencia de costo)", flush=True)
        solver_off = NestedBendersSolver(mine_system, time_series, exo_by_year,
                                         capacity_presolve="off", **kwargs)
        t0 = time.time()
        res_off = solver_off.solve(verbose=True)
        t_off = time.time() - t0
        n_off = res_off["best_solution"][years[0]]["n_ssee_k"]
        print(f"       sin presolve: UB={res_off['ub']:,.2f}  LB={res_off['lb']:,.2f}  "
              f"cortes de factibilidad={res_off['feasibility_cuts']}  "
              f"n_ssee_k={n_off}  tiempo={t_off:.0f}s", flush=True)
        print(f"       con presolve: UB={res['ub']:,.2f}  LB={res['lb']:,.2f}  "
              f"cortes de factibilidad={res['feasibility_cuts']}  "
              f"n_ssee_k={n_fwd}  tiempo={res['total_time_sec'] + t_pre:.0f}s "
              f"(presolve {t_pre:.0f}s)", flush=True)
        for k in stations:
            check(bounds[k] <= int(round(n_off[k])) + 1e-9,
                  f"{k}: cota {bounds[k]} <= n_ssee_k del forward sin presolve "
                  f"{int(round(n_off[k]))}")
        check(res["lb"] >= res_off["lb"] - 1e-6,
              "el LB con presolve no es peor que sin presolve",
              f"{res['lb']:,.2f} >= {res_off['lb']:,.2f}")
        check(res["feasibility_cuts"] <= res_off["feasibility_cuts"],
              "con presolve no hay mas cortes de factibilidad que sin presolve",
              f"{res['feasibility_cuts']} <= {res_off['feasibility_cuts']}")

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    flags = sys.argv[1:]
    modo = "peak"
    if "--modo" in flags:
        i = flags.index("--modo")
        modo = flags[i + 1]
        del flags[i:i + 2]
    argv = [a for a in flags if not a.startswith("--")]
    escenario = argv[0] if len(argv) > 0 else DEFAULT_SCENARIO
    anios = int(argv[1]) if len(argv) > 1 else 2
    raise SystemExit(main(escenario, anios, comparar="--comparar" in flags, modo=modo,
                          con_referencia="--sin_referencia" not in flags))
