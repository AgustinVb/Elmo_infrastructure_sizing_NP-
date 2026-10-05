"""Experimento: ¿que familia operacional conviene dejar ENTERA en la cota
operacional para subirla?

La cota operacional (driver._operational_bound) relaja las 206.112 enteras de
VARS_OPERACIONALES y deja enteras las ~50 de inversion. A 4 años queda 3,9 %
bajo el mejor UB (1.869.867,50). Aca se deja entera una parte de la operacion
y se mide cuanto sube la cota y cuanto cuesta:

    base  relaja todo VARS_OPERACIONALES (la cota actual)
    V1    deja entero Z_swap (el swap ocupa un intervalo completo)
    V2    deja enteras las baterias de la estacion: Sv, S, X_dch, X_ini, W
    V3    V1 + V2

Mismo modelo que el driver (_build_monolithic_model, con la cota de capacidad
del presolve) y mismos parametros que la cota de produccion (MIPGap 1 %). La
cota que se reporta es ObjBound: valida aunque corte por tiempo.

    python -u cota_operacional_variantes.py --variante V2 --n_years 4 \
        --out output/.../cota_op_variantes_4anios --threads 7 --timelimit 3600
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

import pyomo.environ as pyo                                               # noqa: E402
from pyomo.environ import SolverFactory                                   # noqa: E402

from setup import build_mine                                              # noqa: E402
from src.optimization.decomposition.driver import (                       # noqa: E402
    NestedBendersSolver, infer_exogenous_stations)
from src.optimization.opt_model import VARS_OPERACIONALES                 # noqa: E402

ESTACION = ["Sv", "S", "X_dch", "X_ini", "W"]
VARIANTES = {
    "base": [],
    "V1": ["Z_swap"],
    "V2": ESTACION,
    "V3": ["Z_swap"] + ESTACION,
}


def relajar(model, enteras):
    """Como opt_model.relax_operational_vars, salvo las familias `enteras`."""
    n = 0
    for nombre in VARS_OPERACIONALES:
        if nombre in enteras:
            continue
        for vd in getattr(model, nombre).values():
            if vd.is_continuous():
                continue
            lb, ub = vd.bounds
            vd.domain = pyo.Reals
            vd.setlb(lb)
            vd.setub(ub)
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variante", required=True, choices=list(VARIANTES))
    ap.add_argument("--n_years", type=int, default=4)
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--timelimit", type=float, default=3600)
    ap.add_argument("--mipgap", type=float, default=0.01)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--capacidad", default="station_1=1",
                    help="cotas del presolve de capacidad, k=n separadas por coma "
                         "(las que impuso el driver en la misma instancia)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)

    # _build_monolithic_model sin construir los bloques anuales: solo usa
    # estos atributos del driver.
    drv = NestedBendersSolver.__new__(NestedBendersSolver)
    drv.mine_system, drv.time_series = ms, ts
    drv.exogenous_stations_by_year = infer_exogenous_stations(ms, ts)
    drv.free_charging = drv.free_maintenance = True
    drv.autonomous_mode = False
    drv.capacity_bounds = {k: int(n) for k, n in
                           (p.split("=") for p in a.capacidad.split(",") if p)}
    drv.capacity_sum_bound = None

    t0 = time.time()
    model = drv._build_monolithic_model(name=f"CotaOp_{a.variante}")
    relajadas = relajar(model, VARIANTES[a.variante])
    enteras = sum(1 for v in model.component_objects(pyo.Var, active=True)
                  for vd in v.values() if not vd.is_continuous())
    t_build = time.time() - t0
    print(f"[{a.variante}] armado en {t_build:.0f}s: {relajadas:,} relajadas, "
          f"{enteras:,} enteras (deja enteras: {VARIANTES[a.variante] or 'ninguna'})",
          flush=True)

    opt = SolverFactory("gurobi", solver_io="python")
    # OutputFlag=1 explicito: sin tee, Pyomo apaga la salida de Gurobi y
    # LogFile queda con el encabezado solo (paso en la primera tanda).
    opt.options.update({"OutputFlag": 1, "TimeLimit": a.timelimit, "MIPGap": a.mipgap,
                        "LogFile": os.path.join(a.out, f"gurobi_{a.variante}.log"),
                        "LogToConsole": 0})
    if a.threads:
        opt.options["Threads"] = a.threads
    t1 = time.time()
    res = opt.solve(model, load_solutions=False)
    t_solve = time.time() - t1
    g = opt._solver_model
    fila = {"variante": a.variante, "n_years": a.n_years,
            "deja_enteras": VARIANTES[a.variante], "relajadas": relajadas,
            "enteras": enteras, "cota": g.ObjBound,
            "incumbente": g.ObjVal if g.SolCount else None,
            "gap": g.MIPGap if g.SolCount else None,
            "estado": str(res.solver.termination_condition),
            "t_armado_s": t_build, "t_solve_s": t_solve, "threads": a.threads,
            "timelimit": a.timelimit, "mipgap": a.mipgap}
    with open(os.path.join(a.out, f"resultado_{a.variante}.json"), "w", encoding="utf-8") as f:
        json.dump(fila, f, indent=2)
    inc = "-" if fila["incumbente"] is None else f"{fila['incumbente']:,.2f}"
    print(f"[{a.variante}] cota {fila['cota']:,.2f}  incumbente {inc}  "
          f"{fila['estado']}  solve {t_solve:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
