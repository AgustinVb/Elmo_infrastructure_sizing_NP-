"""Certificado del gap por enumeracion de inversiones + corte de cargas por LHD.

  1  maestro de BendersDias (etapas 1 y 2): cota operacional.
  2  pool: TODAS las inversiones enteras con cota operacional bajo
     (1 - gap_objetivo) * UB (contar_inversiones.py). Las demas ya quedan sobre
     el umbral.
  3  por cada candidata x, con sus enteras FIJAS:
       - b_bar_max[y]: la mayor capacidad de bateria alcanzable con x (maestro
         LP, energia de cada dia en su rango factible);
       - n_corte[y, d]: cota de cargas por LHD con esa capacidad
         (cota_cargas_lhd.cota_dia), valida para todo b_bar[y] <= b_bar_max[y];
       - se agrega sum_{t<tf} X_ini >= n_corte a cada dia y se itera Benders
         LP con las enteras fijas hasta converger: LB(x).
     Los cortes de una candidata solo valen para ella: se sacan del maestro
     antes de pasar a la siguiente.
  4  LB certificada = min(umbral, min_x LB(x)).

    python -u certificar_inversiones.py --solucion output/.../mejorar_ub_r4/solucion \
        --ub_esperado 2569771.29 --out output/.../certificado_r4
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

import gurobipy as gp                                                    # noqa: E402
from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from cota_cargas_lhd import cota_dia                                     # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def lb_fijo(bd, clave, alpha, max_rondas, tol):
    """Benders LP con las enteras del maestro fijas en `clave`. Devuelve
    (LB, costo LP en el ultimo punto, rondas). Los cortes que agrega quedan en
    el maestro: el que llama los saca."""
    M = bd.maestro
    ents = [M.x[k] for k in M.ent]
    guard = (M.m.getAttr("LB", ents), M.m.getAttr("UB", ents))
    M.m.setAttr("LB", ents, list(clave))
    M.m.setAttr("UB", ents, list(clave))
    U, L = float("inf"), -float("inf")
    try:
        M._entero(False)
        for ronda in range(1, max_rondas + 1):
            M.m.optimize()
            if M.m.Status != GRB.OPTIMAL:
                return None, None, ronda
            L = M.m.ObjVal
            pt = M.regularizar(L, U if np.isfinite(U) else L * 1.05, alpha, fijar_enteras=True)
            xh, eh, _ = pt if pt is not None else M.punto()
            res = bd.cortes_lp(xh, eh)
            U = min(U, bd._costo_aprox(xh, [r["valor"] for r in res]))
            if (U - L) / abs(U) <= tol:
                break
        M.m.optimize()
        return M.m.ObjVal, U, ronda
    finally:
        M.m.setAttr("LB", ents, guard[0])
        M.m.setAttr("UB", ents, guard[1])
        M._entero(True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del incumbente")
    ap.add_argument("--ub_esperado", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gap_objetivo", type=float, default=0.01)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max_pool", type=int, default=5000)
    ap.add_argument("--max_rondas", type=int, default=40)
    ap.add_argument("--tol", type=float, default=1e-5)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--capacidad", default="station_1=1")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    capacidad = {k: int(n) for k, n in (p.split("=") for p in a.capacidad.split(",") if p)}
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, alpha=a.alpha,
                     cotas_capacidad=capacidad, out=a.out)
    P, M = bd.P, bd.maestro
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    UB, completo0, e0 = bd.cargar_incumbente(vec0)
    print(f"[cert] incumbente pulido {UB:,.2f} (esperado {a.ub_esperado:,.2f})", flush=True)
    if abs(UB - a.ub_esperado) > 1e-6 * abs(UB):
        print("[cert] ABORTA: el incumbente no se reconstruye bien")
        return 1

    # 1) cota operacional
    L = bd.correr(completo0, e0, max_it=(30, 60, 0), tol=(1e-3, 1e-4),
                  etapas=(1, 2), ub_en_etapa2=False)
    cota_op = L.get(2)
    umbral = (1 - a.gap_objetivo) * UB
    print(f"[cert] cota operacional {cota_op:,.2f} (gap {(UB - cota_op) / UB:.3%}); "
          f"umbral {umbral:,.2f}", flush=True)

    # 2) candidatas
    M._entero(True)
    nivel = M.m.addLConstr(M.obj_expr, GRB.LESS_EQUAL, umbral)
    M.m.Params.PoolSearchMode = 2
    M.m.Params.PoolSolutions = a.max_pool
    M.m.optimize()
    n_sol = M.m.SolCount if M.m.Status != GRB.INFEASIBLE else 0
    # INFEASIBLE: ninguna inversion bajo el umbral, el conjunto (vacio) es completo.
    completo_pool = M.m.Status in (GRB.OPTIMAL, GRB.INFEASIBLE) and n_sol < a.max_pool
    candidatas = []
    for i in range(n_sol):
        M.m.Params.SolutionNumber = i
        xn = M.m.getAttr("Xn", M.x)
        candidatas.append((tuple(int(round(xn[k])) for k in M.ent), M.m.PoolObjVal, xn))
    M.m.remove(nivel)
    M.m.Params.PoolSearchMode = 0
    print(f"[cert] {n_sol} candidatas bajo el umbral "
          f"({'pool COMPLETO' if completo_pool else 'POOL INCOMPLETO: no certifica'})", flush=True)

    # Rango de energia de cada dia (valido para cualquier inversion) y piezas
    # fijas: columnas b_bar del maestro y X_ini (t < tf) de cada dia.
    por_bloque = bd._paralelo(lambda b: bd.subs[b].rango_presupuestos())
    for b, rs in enumerate(por_bloque):
        for kk, (lo, hi) in zip(bd.subs[b].pres, rs):
            M.e[kk].LB, M.e[kk].UB = lo, hi
    cols_bb = {int(P.nombre[j][len("b_bar("):-1]): j for j in P.maestro_vars
               if P.nombre[j].startswith("b_bar(")}
    cap_pool = value(om.model.b_max_pool)
    tf = max(om.model.time_intervals_set)
    fila_corte = {}
    for b, s in enumerate(bd.subs):
        kx = []
        for k, j in enumerate(s.loc):
            vd = por_var.get(P.nombre[j])
            if vd is not None and vd.parent_component().name == "X_ini" and vd.index()[-1] < tf:
                kx.append(k)
        fila_corte[b] = s.m.addLConstr(gp.quicksum(s.vloc[k] for k in kx), GRB.GREATER_EQUAL, 0.0)
        s.m.update()

    # 3) cada candidata
    ents = [M.x[k] for k in M.ent]
    filas = []
    clave_inc = tuple(int(round(completo0[P.maestro_vars[k]])) for k in M.ent)
    for i, (clave, cota_pool, xn) in enumerate(sorted(candidatas, key=lambda c: c[1])):
        t_c = time.time()
        xh = np.zeros(P.nv)
        xh[P.maestro_vars] = xn
        # b_bar_max con estas enteras
        guard = (M.m.getAttr("LB", ents), M.m.getAttr("UB", ents))
        M.m.setAttr("LB", ents, list(clave))
        M.m.setAttr("UB", ents, list(clave))
        rg = M.rangos(list(cols_bb.values()))
        M.m.setAttr("LB", ents, guard[0])
        M.m.setAttr("UB", ents, guard[1])
        M._entero(True)
        bb_max = {y: (min(rg[j][1], cap_pool) if np.isfinite(rg[j][1]) else cap_pool)
                  for y, j in cols_bb.items()}
        # n_corte por dia y fila en cada subproblema
        n_corte = {}
        for b, k in enumerate(P.claves):
            y, d = eval(k)
            n, _, _ = cota_dia(om, y, d, bb_max.get(y, cap_pool))
            n_corte[k] = 0 if n is None else n
            fila_corte[b].RHS = float(n_corte[k])
        for s in bd.subs:
            s.m.update()
        # LB con las enteras fijas
        n_antes = M.m.NumConstrs
        lb, costo_lp, rondas = lb_fijo(bd, clave, a.alpha, a.max_rondas, a.tol)
        M.m.update()
        nuevas = M.m.getConstrs()[n_antes:]
        M.m.remove(nuevas)
        M.m.update()
        estado = ("INFACTIBLE" if lb is None else
                  "DESCARTADA" if lb >= umbral else "VIVA")
        fila = {"i": i, "es_incumbente": clave == clave_inc, "cota_operacional": cota_pool,
                "lb_con_corte": lb, "costo_lp": costo_lp, "rondas": rondas, "estado": estado,
                "inversion": bd.inversion(xh), "b_bar_max": bb_max, "n_corte": n_corte}
        filas.append(fila)
        print(f"[cert] {i + 1}/{len(candidatas)} [{fila['inversion']}]"
              f"{'  <- INCUMBENTE' if fila['es_incumbente'] else ''}\n"
              f"[cert]     cota operacional {cota_pool:,.2f} -> LB con corte de cargas "
              f"{(lb if lb is not None else float('nan')):,.2f}  ({rondas} rondas, "
              f"{time.time() - t_c:.0f}s)  {estado}", flush=True)
        print("[cert]     n_corte por año: " + ", ".join(
            f"{y}:{n_corte[k]}" for k in P.claves if eval(k)[1] == 15
            for y in [eval(k)[0]]), flush=True)
        with open(os.path.join(a.out, "certificado.json"), "w", encoding="utf-8") as fh:
            json.dump({"UB": UB, "cota_operacional": cota_op, "umbral": umbral,
                       "pool_completo": completo_pool, "candidatas": filas}, fh, indent=2,
                      default=str)

    lbs = [f["lb_con_corte"] for f in filas if f["lb_con_corte"] is not None]
    lb_cert = min([umbral] + lbs) if completo_pool else None
    print("\n[cert] ================= RESUMEN =================")
    print(f"[cert] UB {UB:,.2f}   cota operacional {cota_op:,.2f} ({(UB - cota_op) / UB:.3%})")
    for f in filas:
        print(f"[cert]   {f['estado']:>10}  LB {(f['lb_con_corte'] or float('nan')):>14,.2f}  "
              f"[{f['inversion']}]{'  <- INCUMBENTE' if f['es_incumbente'] else ''}")
    if lb_cert is not None:
        print(f"[cert] LB CERTIFICADA {lb_cert:,.2f}  ->  gap {(UB - lb_cert) / UB:.3%}")
    print(f"[cert] tiempo total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
