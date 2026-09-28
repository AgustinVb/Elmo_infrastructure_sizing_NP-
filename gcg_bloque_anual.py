"""Prueba de fuego de GCG sobre el MILP de un BLOQUE ANUAL: resuelve el mismo
bloque con Gurobi y con GCG (bloques por dia y por vehiculo-dia) con el mismo
timelimit, y compara incumbente, cota y tiempo.

El caso por defecto es el que motivo todo: el año 4 de Mina_modelo (meta pico
23.500), que en el forward de Nested Benders no encontraba ninguna solucion en
600 s, con la herencia del año 3 que dejo la iteracion 1 de la corrida de 10
años (N_bays = N_chargers = N_batteries = 1, n_ssee_k = 1, sin gen/BESS).

Corre en .venv_elmo; GCG lo llama aparte en .venv_gcg (ver gcg_block.py).

    python -u gcg_bloque_anual.py --out output/gcg_bloque_y4 --timelimit 1200
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

from pyomo.environ import value

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from setup import build_mine                                            # noqa: E402
from src.optimization.decomposition.driver import infer_exogenous_stations  # noqa: E402
from src.optimization.decomposition.gcg_block import solve_with_gcg        # noqa: E402
from src.optimization.decomposition.passes import _solve                   # noqa: E402
from src.optimization.decomposition.year_block import YearBlockBuilder     # noqa: E402

HERENCIA_Y4 = {"n_ssee_k": {"station_1": 1}, "G": {"Solar_PV": 0, "Wind": 0}, "H": 0,
               "N_bays": {"station_1": 1}, "N_chargers": {"station_1": 1},
               "N_batteries": {"station_1": 1}, "D": 477.7}


def nuevo_bloque(ms, ts, year, n_years, heritage):
    exo = infer_exogenous_stations(ms, ts)
    blk = YearBlockBuilder(ms, ts, year=year, is_last_year=(year == n_years),
                           exogenous_stations=exo[year],
                           free_charging=True, free_maintenance=True)
    if heritage:
        blk.set_heritage(heritage)
    return blk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--year", type=int, default=4)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--heritage_json", default=None,
                    help="herencia del año anterior; por defecto la del año 4")
    ap.add_argument("--timelimit", type=float, default=1200)
    ap.add_argument("--gap", type=float, default=0.01)
    ap.add_argument("--modos", default="gurobi,day,vehicle_day")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    heritage = HERENCIA_Y4
    if a.heritage_json:
        with open(a.heritage_json, encoding="utf-8") as f:
            heritage = json.load(f)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)

    filas = []
    for modo in a.modos.split(","):
        blk = nuevo_bloque(ms, ts, a.year, a.n_years, heritage)
        t0 = time.time()
        if modo == "gurobi":
            try:
                r = _solve(blk.model, label=f"gurobi y={a.year}", solvername="gurobi",
                           gap=a.gap, timelimit=a.timelimit,
                           extra_options={"MIPFocus": 1})
                primal = value(blk.model.obj, exception=False)
                dual = r.problem.lower_bound
                estado = str(r.solver.termination_condition)
            except Exception as exc:
                primal, dual, estado = None, None, f"{type(exc).__name__}: {exc}"[:120]
            filas.append({"solver": "gurobi", "primal": primal, "dual": dual,
                          "status": estado, "tiempo": time.time() - t0})
        else:
            try:
                # "day_gurobi" = bloques por dia con el pricing de Gurobi.
                if modo.endswith("_gurobi"):
                    modo_dec, pricing = modo[:-len("_gurobi")], "gurobi"
                else:
                    modo_dec, pricing = modo, "scip"
                r = solve_with_gcg(blk.model, os.path.join(a.out, f"gcg_{modo}"),
                                   timelimit=a.timelimit, gap=a.gap, mode=modo_dec,
                                   use_incumbent=False, pricing=pricing,
                                   label=f"y={a.year} {modo}")
                filas.append({"solver": f"gcg_{modo}", "primal": r["primal"],
                              "dual": r["dual"], "dual_root": r["dual_root"],
                              "status": r["status"], "tiempo": time.time() - t0,
                              "maestro": r["n_master"], "bloques": r["n_blocks"],
                              "cargo_solucion": r["ok"],
                              "pricing_stats": r.get("pricing_stats")})
            except Exception as exc:
                filas.append({"solver": f"gcg_{modo}", "primal": None, "dual": None,
                              "status": f"{type(exc).__name__}: {exc}"[:160],
                              "tiempo": time.time() - t0})
        print(f"[prueba] {filas[-1]}", flush=True)

    with open(os.path.join(a.out, "comparacion.json"), "w", encoding="utf-8") as f:
        json.dump(filas, f, indent=2, default=str)

    def fmt(x):
        return "-" if x is None else f"{x:,.2f}"
    print("\n" + f"{'solver':<18}{'incumbente':>16}{'cota':>16}{'gap':>9}{'tiempo':>9}  estado")
    for r in filas:
        p, d = r["primal"], r["dual"]
        g = (f"{(p - d) / abs(p):.2%}" if p not in (None, 0) and d is not None
             and abs(p) < 1e19 else "-")
        print(f"{r['solver']:<18}{fmt(p):>16}{fmt(d):>16}{g:>9}{r['tiempo']:>8.0f}s  {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
