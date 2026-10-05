"""Un BLOQUE ANUAL del Nested Benders resuelto de tres formas, con la misma
herencia, para comparar:

  actual   el esquema del forward de hoy: MIP start por dias (fase 1 libre, max,
           fase 2 fija, pulido; --day_timelimit/--day_gap/--day_jobs) + el MILP
           anual en Gurobi con --solve_timelimit (60 s en la v4).
  gurobi   el mismo start + Gurobi con el tope largo (--timelimit): referencia
           de cota e incumbente.
  cg       generacion de columnas propia (cg_propia) EN FRIO sobre el bloque,
           con un bloque de pricing por dia (capacidades separadas, copia <=
           capacidad) y la UB de precio-y-fijacion; mismo tope (--timelimit).

La herencia sale del reporte de una corrida (por defecto la v4): stocks y D del
año anterior, y n_ssee_k/G/H. Por defecto el año 6, el que en la v4 abria una
2da bahia por un dia mal resuelto.

    python -u cg_bloque_anual.py --year 6 --timelimit 1800 \
        --out output/.../cg_bloque_y6
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

from setup import build_mine                                                # noqa: E402
from src.optimization.decomposition import gcg_block as gb                  # noqa: E402
from src.optimization.decomposition.cg_propia import (EstructuraDW,         # noqa: E402
                                                      GeneracionColumnas, _env)
from src.optimization.decomposition.day_blocks import day_warm_start        # noqa: E402
from src.optimization.decomposition.driver import infer_exogenous_stations  # noqa: E402
from src.optimization.decomposition.passes import _solve                    # noqa: E402
from src.optimization.decomposition.year_block import YearBlockBuilder      # noqa: E402

V4 = "output/Resultados_finales_tesis/Mina_modelo/P_red_gen_bat_swap_libre_v4_estab"


def herencia_de(carpeta, year):
    """Estado al final del año year-1 en el reporte de `carpeta`."""
    def leer(n):
        with open(os.path.join(carpeta, f"{n}.json"), encoding="utf-8") as f:
            return json.load(f)
    yp = str(year - 1)
    h = {}
    for n in ("N_bays", "N_chargers", "N_batteries"):
        h[n] = {k: v["y"].get(yp, 0) for k, v in leer(n)["k"].items()}
    h["n_ssee_k"] = dict(leer("n_ssee_k")["k"])
    h["G"] = dict(leer("G_g")["g"])
    h["H"] = leer("H")
    D = leer("D")
    h["D"] = next(iter(D.values()))[yp]
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--year", type=int, default=6)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--herencia_de", default=V4, help="carpeta del reporte de donde leer la herencia")
    ap.add_argument("--esquemas", default="actual,gurobi,cg")
    ap.add_argument("--timelimit", type=float, default=1800, help="gurobi largo y cg")
    ap.add_argument("--solve_timelimit", type=float, default=60, help="MILP anual del esquema actual")
    ap.add_argument("--day_timelimit", type=float, default=120)
    ap.add_argument("--day_gap", type=float, default=0.05)
    ap.add_argument("--day_jobs", type=int, default=4)
    ap.add_argument("--estab", default="barrier+wentges")
    ap.add_argument("--jobs", type=int, default=4, help="cg: dias resueltos a la vez")
    ap.add_argument("--tl_heur", type=float, default=30.0)
    ap.add_argument("--tl_exacto", type=float, default=300.0)
    ap.add_argument("--ub_cada", type=int, default=3)
    ap.add_argument("--ramificar_R", action="store_true",
                    help="cg: ramifica sobre el reemplazo de baterias R del año: la rama "
                         "R=1 se acota con un LP; la CG resuelve la rama R=0")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    heritage = herencia_de(a.herencia_de, a.year)
    print(f"[bloque] año {a.year}, herencia de {a.herencia_de}: {heritage}", flush=True)
    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    exo = infer_exogenous_stations(ms, ts)

    def bloque():
        blk = YearBlockBuilder(ms, ts, year=a.year, is_last_year=(a.year == a.n_years),
                               exogenous_stations=exo[a.year],
                               free_charging=True, free_maintenance=True)
        blk.set_heritage(heritage)
        return blk

    day_kw = {"solvername": "gurobi", "timelimit": a.day_timelimit, "gap": a.day_gap,
              "jobs": a.day_jobs}
    filas = []
    for esquema in a.esquemas.split(","):
        t0 = time.time()
        fila = {"esquema": esquema}
        try:
            if esquema in ("actual", "gurobi"):
                blk = bloque()
                t_b = time.time()
                warm = day_warm_start(blk, heritage, day_kw, verbose=True)
                fila["t_start_dias"] = time.time() - t_b
                fila["costo_start"] = value(blk.model.obj) if warm else None
                tl = a.solve_timelimit if esquema == "actual" else a.timelimit
                r = _solve(blk.model, label=f"{esquema} y={a.year}", gap=0.01 if esquema ==
                           "actual" else 1e-4, timelimit=tl, warmstart=warm,
                           extra_options={"MIPFocus": 1})
                fila.update(primal=value(blk.model.obj), dual=r.problem.lower_bound,
                            status=str(r.solver.termination_condition))
                fila["inversion"] = {n: {str(i): v.value for i, v in getattr(blk.model, n).items()}
                                     for n in ("N_bays", "N_chargers", "N_batteries")}
            else:
                blk = bloque()
                info, _ = gb.export(blk.model, os.path.join(a.out, "_mps"), mode="day")
                fijar = None
                if a.ramificar_R:
                    # Rama R = 1: cota por el LP del bloque con R fijo en 1. Si
                    # supera la mejor UB conocida, la rama se descarta y la cota
                    # del bloque es la de la rama R = 0.
                    import gurobipy as gp_
                    F1 = gp_.read(info["mps"], env=_env(os.cpu_count() or 8))
                    vR = F1.getVarByName(f"R({a.year})")
                    vR.LB = vR.UB = 1.0
                    F1.update()
                    F1 = F1.relax()
                    F1.optimize()
                    fila["cota_rama_R1_lp"] = F1.ObjVal if F1.Status == 2 else None
                    print(f"[bloque] rama R=1: cota LP {fila['cota_rama_R1_lp']}", flush=True)
                    fijar = {f"R({a.year})": 0.0}
                E = EstructuraDW(info["mps"], info["decomp"], _env(os.cpu_count() or 8),
                                 fijar=fijar)
                print(f"[bloque] cg: {E.resumen()}", flush=True)
                cg = GeneracionColumnas(E, jobs=a.jobs, out=a.out)
                r = cg.correr(estab=a.estab, heur=(a.tl_heur, 0.02, 5),
                              exacto=(a.tl_exacto, 1e-4), tiempo_max=a.timelimit,
                              ub_cada=a.ub_cada)
                frac = cg.fraccionalidad()
                for f_ in frac:
                    print(f"[bloque] maestro LP, dia {f_['bloque']}: {f_['usadas']} columnas "
                          f"usadas, lambda max {f_['lambda_max']:.3f}, enlace {f_['enlace']}",
                          flush=True)
                fila.update(primal=r["UB"] if r["UB"] < float("inf") else None,
                            dual=r["LB"], rmp=r["z_rmp"], rondas=r["rondas"],
                            columnas=r["cols"], status="cg", fraccionalidad=frac,
                            ub_con_enlace=getattr(cg, "ub_con_enlace", None))
        except Exception as exc:
            fila.update(primal=None, dual=None, status=f"{type(exc).__name__}: {exc}"[:200])
        fila["tiempo"] = time.time() - t0
        filas.append(fila)
        print(f"[bloque] {fila}", flush=True)
        with open(os.path.join(a.out, "comparacion.json"), "w", encoding="utf-8") as f:
            json.dump({"year": a.year, "heritage": heritage, "filas": filas}, f, indent=2,
                      default=str)

    def fmt(x):
        return "-" if x is None else f"{x:,.2f}"
    print(f"\n{'esquema':<10}{'incumbente':>16}{'cota':>16}{'gap':>9}{'tiempo':>9}  estado")
    for r in filas:
        p, d = r.get("primal"), r.get("dual")
        g = (f"{(p - d) / abs(p):.2%}" if p not in (None, 0) and d is not None
             and abs(d) < 1e19 else "-")
        print(f"{r['esquema']:<10}{fmt(p):>16}{fmt(d):>16}{g:>9}{r['tiempo']:>8.0f}s  {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
