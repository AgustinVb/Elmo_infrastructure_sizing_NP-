"""¿Cuantas inversiones enteras podrian esconder una solucion mejor que el
incumbente por mas de --gap_objetivo?

Paso previo a certificar el gap por enumeracion. Con el maestro de
BendersDias (inversion de todo el horizonte; un subproblema por (año, dia)):

  1  etapas 1 y 2 de BendersDias: el maestro entero con cortes LP converge a la
     COTA OPERACIONAL (inversion entera, operacion relajada).
  2  solution pool de Gurobi sobre el maestro entero, con la fila
     costo <= (1 - gap_objetivo) * UB: TODAS las inversiones enteras cuyo
     costo aproximado queda bajo el umbral. Los cortes subestiman siempre, asi
     que el conjunto es un SUPERCONJUNTO de las inversiones que podrian ganarle
     al incumbente por mas del gap (si el pool no llega a --max_pool).
  3  refinamiento: cada candidata se fija y se itera Benders LP sobre ella
     (cortes en su propio punto, level-set con las enteras fijas) hasta que su
     cota supera el umbral (DESCARTADA) o converge bajo el umbral (VIVA: su
     operacion relajada no alcanza para descartarla; ahi hace falta el MILP).

Si no queda ninguna viva, la cota operacional ya certifica el gap objetivo.
Las vivas son las que habria que resolver con los dias MILP.

    python -u contar_inversiones.py --data_folder data/Tesis_final/Mina_modelo/RED \
        --n_years 10 --respaldo output/.../P_red_only_r3/incumbente_respaldo.pkl \
        --out output/.../contar_inversiones_r3
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

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def refinar(bd, clave, umbral, alpha, max_rondas, tol):
    """Fija las enteras del maestro en `clave` e itera cortes LP en ese punto.
    Devuelve (estado, cota, costo_lp_en_el_punto, rondas)."""
    M = bd.maestro
    ents = [M.x[k] for k in M.ent]
    guard = (M.m.getAttr("LB", ents), M.m.getAttr("UB", ents))
    M.m.setAttr("LB", ents, list(clave))
    M.m.setAttr("UB", ents, list(clave))
    U, L = float("inf"), -float("inf")
    try:
        for ronda in range(1, max_rondas + 1):
            M._entero(False)  # con todas las enteras fijas, el maestro es un LP
            M.m.optimize()
            if M.m.Status != GRB.OPTIMAL:
                return "infactible", None, None, ronda
            L = M.m.ObjVal
            if L >= umbral:
                return "descartada", L, U, ronda
            pt = M.regularizar(L, U if np.isfinite(U) else L * 1.05, alpha,
                               fijar_enteras=True)
            xh, eh, _ = pt if pt is not None else M.punto()
            res = bd.cortes_lp(xh, eh)
            U = min(U, bd._costo_aprox(xh, [r["valor"] for r in res]))
            if (U - L) / abs(U) <= tol:
                break
        M._entero(False)
        M.m.optimize()
        L = M.m.ObjVal
        return ("descartada" if L >= umbral else "viva"), L, U, ronda
    finally:
        M.m.setAttr("LB", ents, guard[0])
        M.m.setAttr("UB", ents, guard[1])
        M._entero(True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--respaldo", required=True, help="incumbente_respaldo.pkl de Nested Benders")
    ap.add_argument("--out", required=True)
    ap.add_argument("--gap_objetivo", type=float, default=0.01)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max_pool", type=int, default=5000,
                    help="tope de soluciones del pool; si se alcanza, el conteo es una cota inferior")
    ap.add_argument("--pool_timelimit", type=float, default=3600)
    ap.add_argument("--max_rondas", type=int, default=20, help="rondas de refinamiento por candidata")
    ap.add_argument("--tol_refino", type=float, default=1e-4)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--capacidad", default="station_1=1",
                    help="cotas del presolve de capacidad (n_ssee_k >= n), k=n por coma")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    r = cargar_respaldo(om, ms, ts, a.respaldo)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    capacidad = {k: int(n) for k, n in (p.split("=") for p in a.capacidad.split(",") if p)}
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, alpha=a.alpha,
                     cotas_capacidad=capacidad, out=a.out)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    UB, completo0, e0 = bd.cargar_incumbente(vec0)
    print(f"[contar] incumbente pulido {UB:,.2f} (respaldo {r['best_cost']:,.2f})", flush=True)
    if abs(UB - r["best_cost"]) > 1e-6 * abs(UB):
        print("[contar] ABORTA: el incumbente no se reconstruye bien")
        return 1

    # 1) cota operacional
    L = bd.correr(completo0, e0, max_it=(30, 60, 0), tol=(1e-3, 1e-4),
                  etapas=(1, 2), ub_en_etapa2=False)
    cota_op = L.get(2)
    umbral = (1 - a.gap_objetivo) * UB
    print(f"\n[contar] cota operacional {cota_op:,.2f} (gap {(UB - cota_op) / UB:.3%}); "
          f"umbral {umbral:,.2f} = (1 - {a.gap_objetivo:g}) x UB", flush=True)

    # 2) pool de inversiones bajo el umbral
    M = bd.maestro
    M._entero(True)
    nivel = M.m.addLConstr(M.obj_expr, GRB.LESS_EQUAL, umbral)
    M.m.Params.PoolSearchMode = 2
    M.m.Params.PoolSolutions = a.max_pool
    M.m.Params.TimeLimit = a.pool_timelimit
    t_pool = time.time()
    M.m.optimize()
    n_sol = M.m.SolCount if M.m.Status != GRB.INFEASIBLE else 0
    # INFEASIBLE: ninguna inversion bajo el umbral, el conjunto (vacio) es completo.
    completo_pool = M.m.Status in (GRB.OPTIMAL, GRB.INFEASIBLE) and n_sol < a.max_pool
    candidatas = []
    for i in range(n_sol):
        M.m.Params.SolutionNumber = i
        xn = M.m.getAttr("Xn", M.x)
        candidatas.append({"clave": tuple(int(round(xn[k])) for k in M.ent),
                           "cota_pool": M.m.PoolObjVal,
                           "xn": xn})
    M.m.remove(nivel)
    M.m.Params.PoolSearchMode = 0
    M.m.Params.TimeLimit = GRB.INFINITY
    print(f"[contar] pool: {n_sol} inversiones bajo el umbral en {time.time() - t_pool:.0f}s "
          f"(status {M.m.Status}; "
          f"{'conjunto COMPLETO' if completo_pool else 'conjunto INCOMPLETO: cota inferior del conteo'})",
          flush=True)

    # 3) refinamiento
    nombres_ent = [P.nombre[P.maestro_vars[k]] for k in M.ent]
    clave_inc = tuple(int(round(completo0[P.maestro_vars[k]])) for k in M.ent)
    filas = []
    for i, c in enumerate(sorted(candidatas, key=lambda c: c["cota_pool"])):
        t_c = time.time()
        estado, cota, costo_lp, rondas = refinar(bd, c["clave"], umbral, a.alpha,
                                                 a.max_rondas, a.tol_refino)
        xh = np.zeros(P.nv)
        xh[P.maestro_vars] = c["xn"]
        difs = {n: (vi, vc) for n, vi, vc in zip(nombres_ent, clave_inc, c["clave"])
                if vi != vc and not n.startswith("Delta")}
        fila = {"i": i, "estado": estado, "cota_pool": c["cota_pool"], "cota_refinada": cota,
                "costo_lp_en_el_punto": costo_lp, "rondas": rondas,
                "es_incumbente": c["clave"] == clave_inc,
                "inversion": bd.inversion(xh), "difiere_del_incumbente": difs}
        filas.append(fila)
        print(f"[contar] {i + 1:>4}/{len(candidatas)}  {estado:>10}  pool {c['cota_pool']:,.0f} -> "
              f"{(cota if cota is not None else float('nan')):,.0f}  ({rondas} rondas, "
              f"{time.time() - t_c:.0f}s)  [{fila['inversion']}]"
              f"{'  <- INCUMBENTE' if fila['es_incumbente'] else ''}", flush=True)
        with open(os.path.join(a.out, "candidatas.json"), "w", encoding="utf-8") as fh:
            json.dump({"UB": UB, "cota_operacional": cota_op, "umbral": umbral,
                       "pool_completo": completo_pool, "candidatas": filas}, fh, indent=2,
                      default=str)

    vivas = [f for f in filas if f["estado"] == "viva"]
    print("\n[contar] ================= RESUMEN =================")
    print(f"[contar] UB {UB:,.2f}   cota operacional {cota_op:,.2f} "
          f"(gap {(UB - cota_op) / UB:.3%})   umbral {umbral:,.2f}")
    print(f"[contar] candidatas del pool: {len(candidatas)}"
          f"{'' if completo_pool else ' (POOL INCOMPLETO)'}")
    print(f"[contar] descartadas por la cota LP refinada: "
          f"{sum(f['estado'] == 'descartada' for f in filas)}")
    print(f"[contar] VIVAS (requieren dias MILP): {len(vivas)}")
    for f in vivas:
        print(f"[contar]    cota {f['cota_refinada']:,.0f}  [{f['inversion']}]"
              f"{'  <- INCUMBENTE' if f['es_incumbente'] else ''}  difiere en {f['difiere_del_incumbente']}")
    print(f"[contar] tiempo total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
