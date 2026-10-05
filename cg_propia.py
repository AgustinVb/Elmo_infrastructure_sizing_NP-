"""Generacion de columnas propia sobre el monolitico de Mina_modelo (ver
src/optimization/decomposition/cg_propia.py): cota de Dantzig-Wolfe year_day con
pricing en Gurobi en paralelo, Column Sharing y estabilizacion a eleccion.

Con --solucion arranca con las columnas de una solucion conocida (los JSON de
un reporte), que se reconstruye y pule como en gcg_certificado.py. Sin ella
arranca de cero: solo artificiales con costo --M_art, como un CG en frio. La UB
sale de la heuristica de precio-y-fijacion (--ub_cada).

    python -u cg_propia.py --n_years 4 \
        --solucion output/.../exp_box_4anios --cota_operacional 1824373.30 \
        --estab wentges --out output/.../cg_propia_4anios_wentges --tiempo_max 21600
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from pyomo.environ import value                                          # noqa: E402

import gcg_certificado                                                   # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.cg_propia import (EstructuraDW,      # noqa: E402
                                                      GeneracionColumnas, _env)
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--n_years", type=int, required=True)
    ap.add_argument("--solucion", default=None,
                    help="carpeta con los JSON de una solucion inicial; sin ella, en frio")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cota_operacional", type=float, default=None,
                    help="para comparar; con --parar_en_operacional, objetivo de la LB")
    ap.add_argument("--estab", choices=["wentges", "barrier", "barrier+wentges", "ninguna"],
                    default="barrier+wentges")
    ap.add_argument("--M_art", type=float, default=1e7,
                    help="costo de las artificiales del maestro (arranque en frio)")
    ap.add_argument("--ub_cada", type=int, default=5,
                    help="UB heuristica cada tantas rondas (0 = solo al final)")
    ap.add_argument("--ub_timelimit", type=float, default=300.0)
    ap.add_argument("--alpha", type=float, default=0.5, help="alpha inicial de Wentges")
    ap.add_argument("--sin_compartir", action="store_true", help="sin Column Sharing")
    ap.add_argument("--igualdad", action="store_true",
                    help="capacidades como enlace con igualdad (sin separar por bloque)")
    ap.add_argument("--jobs", type=int, default=4, help="bloques resueltos a la vez")
    ap.add_argument("--hilos", type=int, default=None, help="hilos por bloque (cpu/jobs)")
    ap.add_argument("--tl_heur", type=float, default=30.0)
    ap.add_argument("--gap_heur", type=float, default=0.02)
    ap.add_argument("--pool", type=int, default=5, help="soluciones del pool por pricing")
    ap.add_argument("--tl_exacto", type=float, default=300.0)
    ap.add_argument("--gap_exacto", type=float, default=1e-4)
    ap.add_argument("--tl_compartir", type=float, default=30.0)
    ap.add_argument("--tiempo_max", type=float, default=None, help="segundos")
    ap.add_argument("--max_rondas", type=int, default=10_000)
    ap.add_argument("--tol_gap", type=float, default=1e-4,
                    help="para cuando (RMP - LB)/RMP <= tol_gap")
    ap.add_argument("--parar_en_operacional", action="store_true",
                    help="para cuando la LB supera --cota_operacional")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    t0 = time.time()
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    costo0 = None
    if a.solucion:
        costo0, _, fijadas = gcg_certificado.reconstruir(om.model)
        print(f"[CG] monolitico de {a.n_years} años armado; incumbente reconstruido: "
              f"{costo0:,.2f} ({fijadas:,} enteras fijas) en {time.time() - t0:.0f}s", flush=True)
    else:
        print(f"[CG] monolitico de {a.n_years} años armado en {time.time() - t0:.0f}s; "
              f"arranque en FRIO (sin solucion inicial)", flush=True)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day",
                              split=not a.igualdad)

    t0 = time.time()
    E = EstructuraDW(info["mps"], info["decomp"], _env(os.cpu_count() or 8))
    print(f"[CG] estructura en {time.time() - t0:.0f}s: {E.resumen()}", flush=True)
    cg = GeneracionColumnas(E, jobs=a.jobs, hilos=a.hilos, out=a.out, M_art=a.M_art)
    if a.solucion:
        vec = np.zeros(E.nv)
        for j, n in enumerate(E.nombre):
            vd = por_var.get(n)
            v = value(vd, exception=False) if vd is not None else None
            vec[j] = (1.0 if n == "ONE_VAR_CONSTANT" else 0.0) if v is None else v
        cg.inicializar(vec)
    objetivo = a.cota_operacional if a.parar_en_operacional else None
    r = cg.correr(estab=a.estab, alpha0=a.alpha, compartir=not a.sin_compartir,
                  heur=(a.tl_heur, a.gap_heur, a.pool), exacto=(a.tl_exacto, a.gap_exacto),
                  tl_compartir=a.tl_compartir, tiempo_max=a.tiempo_max,
                  max_rondas=a.max_rondas, tol_gap=a.tol_gap, objetivo=objetivo,
                  ub_cada=a.ub_cada, ub_timelimit=a.ub_timelimit,
                  UB0=costo0 if costo0 is not None else float("inf"))
    ub = r["UB"]

    gap = (ub - r["LB"]) / ub if ub < float("inf") else None
    resumen = {"n_years": a.n_years, "ub_inicial": costo0, "ub_final": ub,
               "cota_dw": r["LB"], "gap": gap, "rmp_final": r["z_rmp"],
               "rondas": r["rondas"], "columnas": r["cols"], "tiempo_s": r["t"],
               "cota_operacional": a.cota_operacional, "args": vars(a)}
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=2, default=float)
    print("\n[CG] ================= RESUMEN =================")
    print(f"[CG] UB                     {ub:>16,.2f}" if ub < float("inf") else "[CG] UB  (ninguna)")
    print(f"[CG] cota DW (lagrangiana)  {r['LB']:>16,.2f}"
          + (f"   gap {gap:.2%}" if gap is not None else ""))
    print(f"[CG] maestro restringido    {r['z_rmp']:>16,.2f}")
    if a.cota_operacional is not None:
        c = a.cota_operacional
        print(f"[CG] cota operacional       {c:>16,.2f}   -> la DW "
              f"{'SUPERA' if r['LB'] > c else 'NO supera'} a la operacional "
              f"({r['LB'] - c:+,.2f})")
    print(f"[CG] {r['rondas']} rondas, {r['cols']} columnas, {r['t']:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
