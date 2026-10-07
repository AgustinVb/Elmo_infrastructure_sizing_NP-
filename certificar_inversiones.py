"""Certificado del gap por enumeracion de inversiones (portado de
battery_swapping_multiaño, 2026-10, adaptado a carga on-board).

  1  maestro de BendersDias (etapas 1 y 2): cota operacional.
  2  pool: TODAS las inversiones enteras con cota operacional bajo
     (1 - gap_objetivo) * UB. Las demas ya quedan sobre el umbral.
  3  por cada candidata x, con sus enteras FIJAS, Benders LP hasta converger:
     LB(x). Los cortes de una candidata solo valen para ella: se sacan del
     maestro antes de pasar a la siguiente.
  4  LB certificada = min(umbral, min_x LB(x)).
  5  (--evaluar_vivas) para cada candidata que quede VIVA (LB(x) < umbral), su
     UB(x): dias MILP con la inversion fija + pulido LP. Si alguna baja el UB,
     esa solucion se escribe en <out>/solucion (usable como
     --incumbente_inicial y por consumer.py tras escribir_parameters.py).

DIFERENCIA CON SWAP. Alla LB(x) se armaba con el corte de cargas por LHD
(cota_cargas_lhd.py, sum X_ini >= n_corte en cada dia), porque la operacion de
swap tiene una relajacion floja: cada carga es una bateria entera, y los dias
quedaban con gaps de 13-39 % a 900 s. En carga on-board no hace falta:
diagnostico_gap_dias.py sobre la solucion de P_red (año 4, el de mayor
produccion) dio LP = MILP = solucion en los cuatro dias (184 USD de diferencia
en 165.767), con los MILP cerrando en segundos. La operacion es casi exacta en
su relajacion, asi que LB(x) queda practicamente en el costo real de x y el
certificado, mas la evaluacion de las vivas, basta para cerrar el gap.

    python -u certificar_inversiones.py --data_folder data/Resultados_finales_tesis/Mina_modelo/P_red \
        --free_charging --free_maintenance --solucion output/<corrida> --gap_objetivo 0.01 \
        --evaluar_vivas --jobs 4 --out output/certificado_<escenario>
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

from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import particion                    # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.decomposition.respaldo import cargar_respaldo      # noqa: E402
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
    ronda = 0
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
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--model", default="elmo_data.xlsx")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, choices=[2, 4], default=4)
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp1")
    ap.add_argument("--wp2_consumption_json", default=None)
    ap.add_argument("--free_charging", action="store_true",
                    help="mismo regimen que la corrida que dio el incumbente")
    ap.add_argument("--free_maintenance", action="store_true",
                    help="mismo regimen que la corrida que dio el incumbente")
    ap.add_argument("--solucion", default=None, help="carpeta con los JSON del incumbente")
    ap.add_argument("--respaldo", default=None, help="incumbente_respaldo.pkl (alternativa)")
    ap.add_argument("--ub_esperado", type=float, default=None,
                    help="costo del incumbente; si se da, el pulido tiene que reproducirlo")
    ap.add_argument("--out", required=True)
    ap.add_argument("--gap_objetivo", type=float, default=0.01)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max_pool", type=int, default=5000)
    ap.add_argument("--max_rondas", type=int, default=40)
    ap.add_argument("--tol", type=float, default=1e-5)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--capacidad", default="station_1=1",
                    help="cotas del presolve de capacidad, n_ssee_k >= n (nave=n,...)")
    ap.add_argument("--evaluar_vivas", action="store_true",
                    help="evaluar la UB de cada candidata viva (dias MILP + pulido)")
    ap.add_argument("--ub_timelimit", type=float, default=300,
                    help="[--evaluar_vivas] segundos por dia MILP")
    ap.add_argument("--ub_gap", type=float, default=1e-3,
                    help="[--evaluar_vivas] MIPGap de cada dia MILP")
    a = ap.parse_args()
    if (a.respaldo is None) == (a.solucion is None):
        ap.error("se necesita exactamente uno de --respaldo o --solucion")
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model=a.model, series="time_series.xlsx",
                   consumption_model=a.consumption_model,
                   wp2_consumption_json=a.wp2_consumption_json, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=a.free_charging,
                  free_maintenance=a.free_maintenance)
    esperado = a.ub_esperado
    if a.respaldo:
        esperado = cargar_respaldo(om, ms, ts, a.respaldo)["best_cost"]
    info, por_var = particion.export(om.model, os.path.join(a.out, "_mps"), mode="year_day")
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
    print(f"[cert] incumbente pulido {UB:,.2f} (esperado "
          f"{'-' if esperado is None else f'{esperado:,.2f}'})", flush=True)
    if esperado is not None and abs(UB - esperado) > 1e-6 * abs(UB):
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

    # 3) cada candidata
    filas = []
    clave_inc = tuple(int(round(completo0[P.maestro_vars[k]])) for k in M.ent)
    for i, (clave, cota_pool, xn) in enumerate(sorted(candidatas, key=lambda c: c[1])):
        t_c = time.time()
        xh = np.zeros(P.nv)
        xh[P.maestro_vars] = xn
        n_antes = M.m.NumConstrs
        lb, costo_lp, rondas = lb_fijo(bd, clave, a.alpha, a.max_rondas, a.tol)
        M.m.update()
        M.m.remove(M.m.getConstrs()[n_antes:])
        M.m.update()
        estado = ("INFACTIBLE" if lb is None else
                  "DESCARTADA" if lb >= umbral else "VIVA")
        fila = {"i": i, "es_incumbente": clave == clave_inc, "cota_operacional": cota_pool,
                "lb_fija": lb, "costo_lp": costo_lp, "rondas": rondas, "estado": estado,
                "inversion": bd.inversion(xh)}
        if estado == "VIVA" and a.evaluar_vivas:
            ub_x, _ = bd.evaluar_ub(xh, a.ub_timelimit, a.ub_gap)
            fila["ub_evaluada"] = ub_x
        filas.append(fila)
        extra = (f"  UB(x) {fila['ub_evaluada']:,.2f}" if fila.get("ub_evaluada") is not None
                 else "")
        print(f"[cert] {i + 1}/{len(candidatas)} [{fila['inversion']}]"
              f"{'  <- INCUMBENTE' if fila['es_incumbente'] else ''}\n"
              f"[cert]     cota operacional {cota_pool:,.2f} -> LB con la inversion fija "
              f"{(lb if lb is not None else float('nan')):,.2f}  ({rondas} rondas, "
              f"{time.time() - t_c:.0f}s)  {estado}{extra}", flush=True)
        with open(os.path.join(a.out, "certificado.json"), "w", encoding="utf-8") as fh:
            json.dump({"UB": UB, "cota_operacional": cota_op, "umbral": umbral,
                       "pool_completo": completo_pool, "candidatas": filas}, fh, indent=2,
                      default=str)

    UB_final = min(UB, bd.UB)
    lbs = [f["lb_fija"] for f in filas if f["lb_fija"] is not None]
    lb_cert = min([(1 - a.gap_objetivo) * UB] + lbs) if completo_pool else None
    print("\n[cert] ================= RESUMEN =================")
    print(f"[cert] UB {UB:,.2f}   cota operacional {cota_op:,.2f} ({(UB - cota_op) / UB:.3%})")
    for f in filas:
        extra = (f"  UB(x) {f['ub_evaluada']:,.2f}" if f.get("ub_evaluada") is not None else "")
        print(f"[cert]   {f['estado']:>10}  LB {(f['lb_fija'] or float('nan')):>14,.2f}  "
              f"[{f['inversion']}]{'  <- INCUMBENTE' if f['es_incumbente'] else ''}{extra}")
    if UB_final < UB * (1 - 1e-9):
        print(f"[cert] UB MEJORADA por una candidata viva: {UB:,.2f} -> {UB_final:,.2f}")
    if lb_cert is not None:
        print(f"[cert] LB CERTIFICADA {lb_cert:,.2f}  ->  gap {(UB_final - lb_cert) / UB_final:.3%}"
              f" (contra UB {UB_final:,.2f})")

    resumen = {"UB_inicial": UB, "UB_final": UB_final, "cota_operacional": cota_op,
               "lb_certificada": lb_cert, "pool_completo": completo_pool,
               "gap_objetivo": a.gap_objetivo, "tiempo_s": time.time() - t0}
    if UB_final < UB * (1 - 1e-7) and bd.mejor_vec is not None:
        from src.io.printer import Printer
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            if vd is not None and not vd.fixed:
                x = bd.mejor_vec[j]
                vd.set_value(round(x) if not vd.is_continuous() else x, skip_validation=True)
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        resumen["solucion"] = destino
        print(f"[cert] solucion mejorada en {destino} (usable como --incumbente_inicial)")
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as fh:
        json.dump(resumen, fh, indent=2)
    print(f"[cert] tiempo total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
