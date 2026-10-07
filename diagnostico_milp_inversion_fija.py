"""Diagnostico: cuanto sube la LB del certificado con la inversion del
incumbente si a la operacion se le agregan cotas MILP por dia.

  1  como certificar_inversiones.py, con las enteras del maestro FIJAS en las
     del incumbente: rango de energia por dia, corte de cargas por LHD y
     Benders LP hasta converger (lb_fijo). Tiene que reproducir la LB del
     certificado (2,047,595.74 en RED_GEN).
  2  en el punto final del maestro, por cada dia: valor LP (con el corte de
     cargas) y el lagrangiano LOCAL MILP (Subproblema.local) con las copias
     continuas y la energia libres a precio -pi / -lam. Su ObjBound es una cota
     valida aunque corte por tiempo; se registra en varios instantes.
  3  por instante t: se agregan al maestro los cortes
         theta_b >= R_b(t) + pi_C . x_C + lam . e
     (validos para toda x con las enteras del incumbente) y se re-resuelve el
     maestro con las enteras fijas: LB(t). Sin rondas extra: es la subida de UNA
     ronda, una cota por abajo de lo que daria iterar.

    python -u diagnostico_milp_inversion_fija.py --data_folder data/Tesis_final/Mina_modelo/RED_GEN \
        --solucion output/.../mejorar_ub_red_gen/solucion --ub_esperado 2143677.604934195 \
        --objetivo 2085557.66 --out output/.../diagnostico_milp_red_gen
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

from certificar_inversiones import lb_fijo                               # noqa: E402
from cota_cargas_lhd import cota_dia                                     # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED_GEN")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del incumbente")
    ap.add_argument("--ub_esperado", type=float, required=True)
    ap.add_argument("--objetivo", type=float, default=None, help="LB que habria que superar")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=24)
    ap.add_argument("--timelimit", type=float, default=1800)
    ap.add_argument("--gap", type=float, default=1e-4)
    ap.add_argument("--instantes", default="60,300,600,900,1800")
    ap.add_argument("--max_rondas", type=int, default=60)
    ap.add_argument("--tol", type=float, default=1e-5)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--capacidad", default="station_1=1")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    instantes = sorted(float(t) for t in a.instantes.split(","))
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
    print(f"[diag] incumbente pulido {UB:,.2f} (esperado {a.ub_esperado:,.2f})", flush=True)
    if abs(UB - a.ub_esperado) > 1e-6 * abs(UB):
        print("[diag] ABORTA: el incumbente no se reconstruye bien")
        return 1
    clave = tuple(int(round(completo0[P.maestro_vars[k]])) for k in M.ent)
    ents = [M.x[k] for k in M.ent]
    print(f"[diag] inversion [{bd.inversion(completo0)}]", flush=True)

    # Como el certificado: etapas 1-2 (cota operacional) para que el maestro
    # llegue con cortes; con solo el del incumbente el LP de lb_fijo no
    # tiene optimo. Despues, rango de energia y corte de cargas.
    L = bd.correr(completo0, e0, max_it=(30, 60, 0), tol=(1e-3, 1e-4),
                  etapas=(1, 2), ub_en_etapa2=False)
    print(f"[diag] cota operacional {L.get(2):,.2f}", flush=True)
    por_bloque = bd._paralelo(lambda b: bd.subs[b].rango_presupuestos())
    for b, rs in enumerate(por_bloque):
        for kk, (lo, hi) in zip(bd.subs[b].pres, rs):
            M.e[kk].LB, M.e[kk].UB = lo, hi
    cols_bb = {int(P.nombre[j][len("b_bar("):-1]): j for j in P.maestro_vars
               if P.nombre[j].startswith("b_bar(")}
    cap_pool = value(om.model.b_max_pool)
    tf = max(om.model.time_intervals_set)
    copiadas_cont = sorted({s.cop[k] for s in bd.subs for k in s.cop_cont})
    guard = (M.m.getAttr("LB", ents), M.m.getAttr("UB", ents))
    M.m.setAttr("LB", ents, list(clave))
    M.m.setAttr("UB", ents, list(clave))
    rangos = M.rangos(sorted(set(copiadas_cont) | set(cols_bb.values())))
    M.m.setAttr("LB", ents, guard[0])
    M.m.setAttr("UB", ents, guard[1])
    M._entero(True)
    bb_max = {y: (min(rangos[j][1], cap_pool) if np.isfinite(rangos[j][1]) else cap_pool)
              for y, j in cols_bb.items()}
    for b, s in enumerate(bd.subs):
        kx = []
        for k, j in enumerate(s.loc):
            vd = por_var.get(P.nombre[j])
            if vd is not None and vd.parent_component().name == "X_ini" and vd.index()[-1] < tf:
                kx.append(k)
        y, d = eval(P.claves[b])
        n, _, _ = cota_dia(om, y, d, bb_max.get(y, cap_pool))
        s.m.addLConstr(gp.quicksum(s.vloc[k] for k in kx), GRB.GREATER_EQUAL,
                       float(0 if n is None else n))
        s.m.update()

    # 1) LB con corte de cargas (debe coincidir con el certificado)
    lb0, costo_lp, rondas = lb_fijo(bd, clave, a.alpha, a.max_rondas, a.tol)
    if lb0 is None:
        print(f"[diag] ABORTA: el maestro de lb_fijo no tiene optimo en la ronda {rondas} "
              f"(status {M.m.Status})")
        return 1
    print(f"[diag] LB con corte de cargas {lb0:,.2f} ({rondas} rondas, "
          f"{time.time() - t0:.0f}s)", flush=True)

    # Punto final del maestro con las enteras fijas
    M.m.setAttr("LB", ents, list(clave))
    M.m.setAttr("UB", ents, list(clave))
    M._entero(False)
    M.m.optimize()
    xh, eh, th = M.punto()

    # 2) dia por dia: LP y lagrangiano local MILP con la cota en cada instante
    def tarea(b):
        s = bd.subs[b]
        r = s.lp(xh, eh)
        if not r["ok"]:
            return {"ok": False, "status": r["status"]}
        cotas = {}

        def cb(model, where):
            if where == GRB.Callback.MIP:
                rt = model.cbGet(GRB.Callback.RUNTIME)
                bnd = model.cbGet(GRB.Callback.MIP_OBJBND)
                for t in instantes:
                    if rt <= t:
                        cotas[t] = bnd

        rc = [rangos[s.cop[k]] for k in s.cop_cont]
        lg = s.local(xh, r["pi"], r["lam"], rc, a.timelimit, a.gap, callback=cb)
        if not lg["ok"]:
            return {"ok": False, "status": lg["status"], "lp": r}
        # Instantes posteriores al final del solve: la cota final.
        for t in instantes:
            if t >= lg["t"] or t not in cotas:
                cotas[t] = lg["R"] if t >= lg["t"] else cotas.get(t, -float("inf"))
        # Valor del corte en el punto de prueba
        pi, lam = r["pi"], r["lam"]
        lin = sum(pi[k] * xh[s.cop[k]] for k in s.cop_cont) \
            + sum(l * eh[kk] for kk, l in zip(s.pres, lam))
        return {"ok": True, "lp": r["valor"], "pi": pi, "lam": lam, "lin": lin,
                "R": cotas, "t": lg["t"], "gap": lg["gap"], "status": lg["status"]}

    t_m = time.time()
    res = bd._paralelo(tarea)
    print(f"[diag] lagrangianos locales en {time.time() - t_m:.0f}s", flush=True)

    filas = []
    print(f"[diag] {'dia':>10} {'theta':>12} {'LP':>12} " +
          " ".join(f"{'sube@' + str(int(t)):>11}" for t in instantes) + "  gap MILP  t")
    for b, r in enumerate(res):
        k = P.claves[b]
        if not r["ok"]:
            print(f"[diag] {k:>10}  sin cota (status {r['status']})")
            filas.append({"dia": k, "ok": False})
            continue
        sube = {t: (r["R"][t] + r["lin"] - r["lp"]) for t in instantes}
        filas.append({"dia": k, "ok": True, "theta": float(th[b]), "lp": r["lp"],
                      "R": r["R"], "lin": r["lin"], "sube": sube, "t": r["t"],
                      "gap": r["gap"], "status": r["status"]})
        print(f"[diag] {k:>10} {th[b]:>12,.2f} {r['lp']:>12,.2f} " +
              " ".join(f"{sube[t]:>11,.2f}" for t in instantes) +
              f"  {(r['gap'] if r['gap'] is not None else float('nan')):>7.2%}  {r['t']:.0f}s",
              flush=True)

    # 3) LB del maestro con los cortes de cada instante
    lb_t = {}
    for t in instantes:
        n_antes = M.m.NumConstrs
        for b, r in enumerate(res):
            if not r["ok"] or not np.isfinite(r["R"][t]):
                continue
            s = bd.subs[b]
            pi_c = [r["pi"][k] if k in s.cop_cont else 0.0 for k in range(s.n_cop)]
            M.corte(b, r["R"][t], pi_c, r["lam"], s.cop, s.pres)
        M.m.optimize()
        lb_t[t] = M.m.ObjVal if M.m.Status == GRB.OPTIMAL else None
        M.m.update()
        M.m.remove(M.m.getConstrs()[n_antes:])
        M.m.update()
        # Un corte local bajo el LP no resta: el corte LP sigue en el maestro.
        suma = sum(max(0.0, f["sube"][t]) for f in filas if f["ok"] and np.isfinite(f["sube"][t]))
        txt = f"{lb_t[t]:,.2f} (+{lb_t[t] - lb0:,.2f})" if lb_t[t] is not None else "sin optimo"
        print(f"[diag] t={t:>6.0f}s  suma de subidas {suma:>12,.2f}   LB maestro {txt}"
              + (f"   falta {a.objetivo - lb_t[t]:,.2f}" if a.objetivo and lb_t[t] else ""),
              flush=True)
    M.m.setAttr("LB", ents, guard[0])
    M.m.setAttr("UB", ents, guard[1])

    with open(os.path.join(a.out, "diagnostico.json"), "w", encoding="utf-8") as fh:
        json.dump({"UB": UB, "objetivo": a.objetivo, "lb_corte_cargas": lb0,
                   "inversion": bd.inversion(completo0), "lb_por_instante": lb_t,
                   "dias": filas}, fh, indent=1, default=str)
    print(f"[diag] tiempo total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
