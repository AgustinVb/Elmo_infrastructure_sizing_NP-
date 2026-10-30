"""Prueba: cuanto sube la cota de una candidata del certificado si a su
operacion se le exige INTEGRALIDAD por dia, sin maestro restringido.

Para la inversion fija de la candidata (nodo de cg_propia) y pi = duales del LP
de ese nodo (relajacion_nodo):

    L(pi) = pi'b + min_{x en la caja del nodo} (c_M - A_M' pi) x
            + sum_b Phi_b(pi),   Phi_b = min_{z en X_b} (c_b - pi A_b) z

con Phi_b el ObjBound del MILP del dia (valido aunque corte por tiempo; un dia
infactible da +inf). Con Phi_b = el LP del dia la misma formula da L_LP, que
por dualidad tiene que reproducir el LP del nodo (control numerico): la
diferencia L - L_LP es lo que aporta la integralidad de la operacion, dia por
dia. Tambien se reporta la cota con el maestro como MIP (lag_entera).

Por dia, Phi_b solo depende de la capacidad de su año (copia <= capacidad): si
la integralidad aporta, las mismas cotas se tabulan por (dia, capacidad) y
valen para todas las inversiones (ver cg_certificado.py).

    python -u cota_lagrangiana_dias.py --data_folder ... --solucion .../solucion \
        --ub_esperado ... --certificado .../certificado.json --out ...
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

T_INICIO = time.time()

import numpy as np                                                       # noqa: E402

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

import gurobipy as gp                                                    # noqa: E402
from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

import gcg_certificado                                                   # noqa: E402
from cg_certificado import cotas_de, posiciones_inversion, texto_inversion  # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.cg_propia import (EstructuraDW,      # noqa: E402
                                                      GeneracionColumnas, _env)
from src.optimization.decomposition.driver import NestedBendersSolver   # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

INF = float("inf")


def lp_dia(p, c):
    """LP del dia con el objetivo c (sobre una copia relajada del pricing)."""
    R = p.m.relax()
    R.setAttr("Obj", R.getVars(), c.tolist())
    R.Params.Method = 1
    R.optimize()
    st = R.Status
    v = R.ObjVal if st == GRB.OPTIMAL else (INF if st == GRB.INFEASIBLE else -INF)
    R.dispose()
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--solucion", required=True)
    ap.add_argument("--ub_esperado", type=float, required=True)
    ap.add_argument("--certificado", required=True)
    ap.add_argument("--corte_cargas_lhd", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--candidatas", type=int, default=1, help="las N vivas de menor LB")
    ap.add_argument("--tl_dia", type=float, default=900.0)
    ap.add_argument("--gap_dia", type=float, default=1e-4)
    ap.add_argument("--tl_relajacion", type=float, default=1800.0)
    ap.add_argument("--lag_entera", type=float, default=120.0)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--hilos", type=int, default=1)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    with open(a.certificado, encoding="utf-8") as fh:
        cert = json.load(fh)
    vivas = sorted((c for c in cert["candidatas"] if c["estado"] == "VIVA"),
                   key=lambda c: c["lb_con_corte"])
    UBc = float(cert["UB"])
    print(f"[LD] {a.data_folder}: UB {UBc:,.2f}, {len(vivas)} vivas, LB certificada "
          f"{vivas[0]['lb_con_corte']:,.2f} (gap {(UBc - vivas[0]['lb_con_corte']) / UBc:.3%})",
          flush=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    if a.corte_cargas_lhd:
        NestedBendersSolver._agregar_corte_cargas_lhd(None, om.model, a.corte_cargas_lhd)
    costo0, _, _ = gcg_certificado.reconstruir(om.model)
    print(f"[LD] incumbente reconstruido: {costo0:,.2f} (esperado {a.ub_esperado:,.2f})",
          flush=True)
    if abs(costo0 - a.ub_esperado) > 1e-6 * abs(a.ub_esperado):
        print("[LD] ABORTA: el incumbente no se reconstruye con el costo esperado", flush=True)
        return 1
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=True)
    E = EstructuraDW(info["mps"], info["decomp"], _env(os.cpu_count() or 8))
    print(f"[LD] estructura: {E.resumen()}", flush=True)
    cg = GeneracionColumnas(E, jobs=a.jobs, hilos=a.hilos, out=None, M_art=1e7)
    cg.tl_lag_entera = a.lag_entera if a.lag_entera > 0 else None

    vec = np.zeros(E.nv)
    for j, n in enumerate(E.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec[j] = (1.0 if n == "ONE_VAR_CONSTANT" else 0.0) if v is None else v
    cg.UB = costo0
    pos = posiciones_inversion(E)
    txt_inc = texto_inversion(pos, vec[E.mvars])
    inc = [c for c in cert["candidatas"] if c.get("es_incumbente")]
    if inc and inc[0]["inversion"] != txt_inc:
        print(f"[LD] ABORTA: inversion del incumbente distinta a la del certificado:\n"
              f"     MPS          {txt_inc}\n     certificado  {inc[0]['inversion']}", flush=True)
        return 1
    n = cg.acotar_por_costo()
    print(f"[LD] cotas por costo: {n} variables del maestro", flush=True)

    salida = {"data_folder": a.data_folder, "UB": costo0,
              "LB_certificado": vivas[0]["lb_con_corte"], "args": vars(a), "candidatas": []}
    for rank, c in enumerate(vivas[:a.candidatas]):
        t0 = time.time()
        sig = vivas[rank + 1]["lb_con_corte"] if rank + 1 < len(vivas) else UBc
        print(f"\n[LD] === candidata {c['i']} (#{rank + 1}): LB certificado "
              f"{c['lb_con_corte']:,.2f}, siguiente {sig:,.2f}  [{c['inversion']}]", flush=True)
        fila = {"i": c["i"], "inversion": c["inversion"], "lb_cert": c["lb_con_corte"],
                "lb_siguiente": sig}
        if not cg._aplicar_nodo(cotas_de(pos, c["inversion"])):
            print("[LD] cotas del nodo incompatibles: candidata infactible", flush=True)
            fila["L"] = INF
            salida["candidatas"].append(fila)
            continue
        z_lp, pi = cg.relajacion_nodo(a.tl_relajacion)
        fila["z_lp_nodo"] = z_lp
        if pi is None:
            print("[LD] sin duales del LP del nodo: se salta", flush=True)
            salida["candidatas"].append(fila)
            continue

        def tarea(b):
            p = cg.pricing[b]
            p.m.Params.DualReductions = 0       # INFEASIBLE explicito, no INF_OR_UNBD
            obj = p.objetivo(pi)
            v_lp = lp_dia(p, obj)
            r = p.resolver(obj, a.tl_dia, a.gap_dia, 1)
            return b, v_lp, r

        t_d = time.time()
        res = {b: (v_lp, r) for b, v_lp, r in cg.pool.map(tarea, range(E.nb))}
        print(f"[LD] {E.nb} dias (LP y MILP) en {time.time() - t_d:.0f}s", flush=True)
        xm0 = np.zeros(len(E.mvars))
        b_milp = [res[b][1]["bound"] for b in range(E.nb)]
        b_lp = [res[b][0] for b in range(E.nb)]
        cg.tl_lag_entera, tl_e = None, cg.tl_lag_entera
        L_lp, _ = cg.lagrangiano(pi, b_lp, xm0)
        L_caja, _ = cg.lagrangiano(pi, b_milp, xm0)
        cg.tl_lag_entera = tl_e
        L_ent = None
        if tl_e:
            L_ent, _ = cg.lagrangiano(pi, b_milp, xm0)
        dias = []
        for b in range(E.nb):
            v_lp, r = res[b]
            dias.append({"dia": E.claves[b], "lp": v_lp, "milp_bound": r["bound"],
                         "milp_val": r["val"], "sube": r["bound"] - v_lp,
                         "status": r["status"], "t": r["t"]})
        dias.sort(key=lambda d: -d["sube"] if np.isfinite(d["sube"]) else -INF)
        L = max(L_caja, L_ent if L_ent is not None else -INF)
        LB_c = max(c["lb_con_corte"], L)
        fila.update({"L_LP": L_lp, "L_caja": L_caja, "L_entera": L_ent, "L": L,
                     "LB_nueva": LB_c, "sube_vs_cert": LB_c - c["lb_con_corte"],
                     "suma_integralidad": float(sum(d["sube"] for d in dias
                                                    if np.isfinite(d["sube"]))),
                     "dias_cortados": sum(1 for d in dias if d["status"] == GRB.TIME_LIMIT),
                     "dias_infactibles": [d["dia"] for d in dias if d["status"] == GRB.INFEASIBLE],
                     "t": time.time() - t0, "dias": dias})
        salida["candidatas"].append(fila)
        f = lambda v: "-" if v is None else ("+inf" if v == INF else f"{v:,.2f}")
        print(f"[LD] z_LP nodo {f(z_lp)} | L con dias LP {f(L_lp)} (control: debe ~ z_LP) | "
              f"L con dias MILP {f(L_caja)} | con maestro entero {f(L_ent)}", flush=True)
        print(f"[LD] integralidad por dia: suma {fila['suma_integralidad']:,.2f}; "
              f"{fila['dias_cortados']} dias cortados por tiempo; infactibles "
              f"{fila['dias_infactibles']}", flush=True)
        for d in dias[:8]:
            print(f"[LD]    {d['dia']:>10}  LP {f(d['lp']):>14}  MILP>= {f(d['milp_bound']):>14}  "
                  f"sube {f(d['sube']):>12}  ({d['t']:.0f}s, status {d['status']})", flush=True)
        print(f"[LD] candidata {c['i']}: LB {c['lb_con_corte']:,.2f} -> {LB_c:,.2f} "
              f"(+{LB_c - c['lb_con_corte']:,.2f}); para pasar a la siguiente faltaban "
              f"{sig - c['lb_con_corte']:,.2f}; {'LA SUPERA' if LB_c >= sig else 'no la supera'}"
              f"{' y queda DESCARTADA (>= UB)' if LB_c >= costo0 else ''}  ({time.time() - t0:.0f}s)",
              flush=True)
        with open(os.path.join(a.out, "cota_dias.json"), "w", encoding="utf-8") as fh:
            json.dump(salida, fh, indent=1, default=float)
    cg._aplicar_nodo({})
    with open(os.path.join(a.out, "cota_dias.json"), "w", encoding="utf-8") as fh:
        json.dump(salida, fh, indent=1, default=float)
    print(f"[LD] fin en {time.time() - T_INICIO:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
