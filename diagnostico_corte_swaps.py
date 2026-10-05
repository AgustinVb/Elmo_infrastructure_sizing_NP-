"""Prueba del corte de redondeo entero de swaps por LHD.

diagnostico_fraccionalidad.py mostro que el LP del dia hace 16,35 swaps
fraccionarios contra 18 enteros, y que esa diferencia explica casi todo el gap
LP-MILP del dia (cada swap es una bateria que hay que cargar con energia de la
red).

EL CORTE. El SOC del dia es ciclico (battery_energy_conservation), asi que la
energia que descarga el LHD i en el dia solo se repone con swaps, y cada swap
repone a lo sumo (1 - bmin_b[i]) * b_bar (battery_soc_swap_update_1: la bateria
que entra queda llena y la que sale no puede bajar de bmin * b_bar). Por eso

    n_swaps_i * (1 - bmin_i) * b_bar  >=  E_i  >=  E_i^min

y como n_swaps_i es entero:

    sum_t Z_swap[., i, y, d, t]  >=  ceil( E_i^min / ((1 - bmin_i) * b_bar) )

con E_i^min una cota inferior VALIDA de la energia del LHD i en el dia: el
minimo de su descarga sobre el LP (o la cota dual del MILP) del dia.

Se prueba con la inversion de B fija, dia por dia: LP sin corte, LP con corte,
cota del MILP (diagnostico_gap_dias) y mejor MILP.

    python -u diagnostico_corte_swaps.py --anios 4 --out output/.../diagnostico_corte_swaps
"""
import argparse
import json
import math
import os
import sys
import time
from argparse import Namespace

import gurobipy as gp
import numpy as np
from gurobipy import GRB

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from pyomo.environ import value                                          # noqa: E402

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias, INF  # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

M_DIR = "output/Resultados_finales_tesis/Mina_modelo"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n_years", type=int, default=4)
    ap.add_argument("--solucion", default=f"{M_DIR}/estab_4anios_B_box")
    ap.add_argument("--anios", default="4")
    ap.add_argument("--dias", default="", help="dias a probar, por coma (vacio = todos)")
    ap.add_argument("--emin_milp_s", type=float, default=60,
                    help="segundos del MILP de energia minima (0 = solo LP)")
    ap.add_argument("--milp_s", type=float, default=120,
                    help="segundos del MILP del dia CON el corte (0 = no)")
    ap.add_argument("--min_swaps_s", type=float, default=300,
                    help="segundos del MILP de minimo de swaps del dia")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--modo", choices=["swaps", "cargas", "ramas", "rama17"], default="swaps")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder="data/Tesis_final/Mina_modelo/", model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    m = om.model
    info, por_var = gb.export(m, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, m, por_var, jobs=a.jobs, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    costo, xB, eB = bd.cargar_incumbente(vec0)
    print(f"[corte] B pulido: {costo:,.2f}", flush=True)
    col = {n: j for j, n in enumerate(P.nombre)}

    anios = {int(x) for x in a.anios.split(",")}
    bloques = [b for b, k in enumerate(P.claves) if eval(k)[0] in anios
               and (not a.dias or eval(k)[1] in {int(x) for x in a.dias.split(",")})]
    lhds = sorted(m.slhd_set)

    def b_bar_valor(y):
        vd = m.b_bar[y]
        if vd.fixed:
            return value(vd)
        return float(xB[col[por_var_inv[id(vd)]]])

    por_var_inv = {id(vd): n for n, vd in por_var.items()}

    def armar(b):
        """Coeficientes locales de energia y de swaps por LHD en el dia b."""
        s = bd.subs[b]
        y, d = eval(P.claves[b])
        pos = {int(j): k for k, j in enumerate(s.loc)}
        energia = {i: {} for i in lhds}
        swaps = {i: [] for i in lhds}
        for (i, j, yy, dd, t) in m.Y_INDEX:
            if yy != y or dd != d or i not in energia:
                continue
            n = por_var_inv.get(id(m.Y[i, j, yy, dd, t]))
            if n is None or col[n] not in pos:
                continue
            c = (value(m.pe_i[i, j]) * value(m.d_i[i, j]) * ts.get_n_trips(j, i)
                 / value(m.eta_discharge_i[i]))
            energia[i][pos[col[n]]] = c
        t0 = min(m.time_intervals_set)
        swaps_t0, ini = [], []
        for idx in m.Z_swap:
            k, i = idx[0], idx[1]
            if idx[2] != y or idx[3] != d:
                continue
            n = por_var_inv.get(id(m.Z_swap[idx]))
            if n is not None and col[n] in pos:
                swaps[i].append(pos[col[n]])
                if idx[4] == t0:
                    swaps_t0.append(pos[col[n]])
        for idx in m.X_ini:
            if idx[1] != y or idx[2] != d:
                continue
            n = por_var_inv.get(id(m.X_ini[idx]))
            if n is not None and col[n] in pos:
                ini.append(pos[col[n]])
        return s, y, d, energia, swaps, swaps_t0, ini

    def lp_fijo(s):
        mm = s.m
        s._entera(False)
        s._enlaces(s.M, 0.0, 0.0, INF)
        s._copias()
        s._rhs(xB, np.zeros(max(s.pres, default=-1) + 1))
        mm.optimize()
        return mm.ObjVal

    def tarea_cargas(b):
        """Corte sobre las CARGAS: sum_t X_ini >= n_min, con n_min la cota del
        MILP que minimiza las cargas del dia (objetivo entero -> se redondea)."""
        s, y, d, energia, swaps, swaps_t0, ini = armar(b)
        mm = s.m
        todas = mm.getVars()
        objs = mm.getAttr("Obj", todas)
        todas_sw = [s.vloc[k] for i in lhds for k in swaps[i]]
        v_lp = lp_fijo(s)
        x = mm.getAttr("X", s.vloc)
        fila = {"dia": P.claves[b], "lp": v_lp,
                "LP": {"cargas": sum(x[k] for k in ini),
                       "swaps": sum(x[k] for i in lhds for k in swaps[i]),
                       "swaps_t0": sum(x[k] for k in swaps_t0)},
                "B": {"cargas": sum(xB[s.loc[k]] for k in ini),
                      "swaps": sum(xB[s.loc[k]] for i in lhds for k in swaps[i]),
                      "swaps_t0": sum(xB[s.loc[k]] for k in swaps_t0)}}
        # Minimo de cargas del dia
        mm.setObjective(gp.quicksum(s.vloc[k] for k in ini), GRB.MINIMIZE)
        s._entera(False)
        mm.optimize()
        fila["min_cargas_lp"] = mm.ObjVal
        s._entera(True)
        mm.Params.TimeLimit = a.min_swaps_s
        mm.Params.MIPGap = 0
        mm.optimize()
        fila["min_cargas_cota"] = mm.ObjBound
        fila["min_cargas_inc"] = mm.ObjVal if mm.SolCount else None
        mm.Params.TimeLimit = INF
        n_min = math.ceil(mm.ObjBound - 1e-6)
        fila["n_min"] = n_min
        mm.setAttr("Obj", todas, objs)
        mm.ModelSense = GRB.MINIMIZE
        # LP con el corte (n_min) y, como referencia, con n_min + 1
        for extra in (0, 1):
            c = mm.addLConstr(gp.quicksum(s.vloc[k] for k in ini), GRB.GREATER_EQUAL,
                              n_min + extra)
            fila[f"lp_corte_{n_min + extra}"] = lp_fijo(s)
            if extra == 0 and a.milp_s > 0:
                r = s.ub(xB, a.milp_s, 1e-4, mip_focus=3)
                fila["milp_corte_cota"] = mm.ObjBound
                fila["milp_corte_inc"] = r.get("valor")
                xm = r.get("x")
                if xm is not None:
                    fila["MILP"] = {"cargas": sum(xm[k] for k in ini),
                                    "swaps": sum(xm[k] for i in lhds for k in swaps[i]),
                                    "swaps_t0": sum(xm[k] for k in swaps_t0)}
            mm.remove(c)
            mm.update()
        return fila

    def tarea_ramas(b):
        """El gap del dia es UNA carga: con sum X_ini >= 18 el LP ya da el
        costo del mejor MILP. Se prueba (1) ramificar primero sobre el NUMERO
        de cargas -- variable entera nC = sum X_ini con prioridad de branching
        alta -- y (2) la rama de 17 cargas sola: el mejor costo con
        sum X_ini <= 17."""
        s, y, d, energia, swaps, swaps_t0, ini = armar(b)
        mm = s.m
        fila = {"dia": P.claves[b]}
        # (2) rama <= 17
        c17 = mm.addLConstr(gp.quicksum(s.vloc[k] for k in ini), GRB.LESS_EQUAL, 17)
        r17 = s.ub(xB, a.milp_s, 1e-4, mip_focus=1)
        fila["rama17_cota"] = mm.ObjBound
        fila["rama17_inc"] = r17.get("valor")
        fila["rama17_status"] = mm.Status
        mm.remove(c17)
        # (1) nC con prioridad de branching
        nC = mm.addVar(vtype=GRB.INTEGER, lb=0, ub=100)
        cn = mm.addLConstr(nC == gp.quicksum(s.vloc[k] for k in ini))
        mm.update()
        nC.BranchPriority = 100
        s.enteras.append(nC)
        s.vt_orig.append(GRB.INTEGER)
        r = s.ub(xB, a.milp_s, 1e-4, mip_focus=3)
        fila["ramif_cota"] = mm.ObjBound
        fila["ramif_inc"] = r.get("valor")
        s.enteras.pop()
        s.vt_orig.pop()
        mm.remove(cn)
        mm.remove(nC)
        mm.update()
        return fila

    def tarea_rama17(b):
        """¿Existe una operacion con 17 cargas y cuanto cuesta? Fase A: MILP que
        minimiza las cargas y corta al llegar a 17 (BestObjStop). Fase B: MILP
        de COSTO con sum X_ini = 17, arrancando desde esa operacion."""
        s, y, d, energia, swaps, swaps_t0, ini = armar(b)
        mm = s.m
        todas = mm.getVars()
        objs = mm.getAttr("Obj", todas)
        fila = {"dia": P.claves[b]}
        lp_fijo(s)                       # deja la configuracion con inversion fija
        mm.setObjective(gp.quicksum(s.vloc[k] for k in ini), GRB.MINIMIZE)
        s._entera(True)
        mm.Params.TimeLimit = a.min_swaps_s
        mm.Params.MIPGap = 0
        mm.Params.MIPFocus = 1
        mm.Params.BestObjStop = 17.5
        t1 = time.time()
        mm.optimize()
        # El default de BestObjStop es -inf: con +inf Gurobi para en la PRIMERA
        # solucion de cualquier optimize posterior.
        mm.Params.BestObjStop = -INF
        mm.Params.MIPFocus = 0
        fila["faseA_cargas"] = mm.ObjVal if mm.SolCount else None
        fila["faseA_t"] = time.time() - t1
        x17 = mm.getAttr("X", s.vloc) if mm.SolCount else None
        mm.setAttr("Obj", todas, objs)
        mm.ModelSense = GRB.MINIMIZE
        if x17 is None or fila["faseA_cargas"] > 17.5:
            fila["faseB"] = None
            return fila
        fila["faseA_costo"] = float(sum(P.obj[s.loc[k]] * x17[k] for k in range(len(s.loc))))
        c = mm.addLConstr(gp.quicksum(s.vloc[k] for k in ini), GRB.EQUAL, 17)
        s.start = [round(x17[k]) for k in s.ent_loc]
        mm.Params.LogFile = os.path.join(a.out, f"faseB_{P.claves[b].replace(' ', '')}.log")
        mm.Params.OutputFlag = 1
        mm.Params.LogToConsole = 0
        t2 = time.time()
        r = s.ub(xB, a.milp_s, 1e-4, mip_focus=1)
        mm.Params.OutputFlag = 0
        fila["faseB_cota"] = mm.ObjBound
        fila["faseB_inc"] = r.get("valor")
        fila["faseB_status"] = mm.Status
        fila["faseB_t"] = time.time() - t2
        mm.remove(c)
        mm.update()
        return fila

    def tarea(b):
        s, y, d, energia, swaps, swaps_t0, ini = armar(b)
        mm = s.m
        todas = mm.getVars()
        objs = mm.getAttr("Obj", todas)
        v_lp = lp_fijo(s)
        xs_lp = mm.getAttr("X", s.vloc)
        fila = {"dia": P.claves[b], "lp": v_lp, "lhd": {}}
        bb = b_bar_valor(y)
        cortes = []
        for i in lhds:
            coefs = energia[i]
            # E_i^min: minimo de la descarga del LHD i sobre el LP del dia
            mm.setObjective(gp.LinExpr(list(coefs.values()),
                                       [s.vloc[k] for k in coefs]), GRB.MINIMIZE)
            s._entera(False)
            mm.optimize()
            e_lp = mm.ObjVal
            e_milp = None
            if a.emin_milp_s > 0:
                s._entera(True)
                mm.Params.TimeLimit = a.emin_milp_s
                mm.Params.MIPGap = 1e-4
                mm.optimize()
                e_milp = mm.ObjBound
                mm.Params.TimeLimit = INF
            e_min = max(e_lp, e_milp or -INF)
            cap = (1 - value(m.bmin_b[i])) * bb
            rhs = math.ceil(e_min / cap - 1e-6)
            sw_lp = sum(xs_lp[k] for k in swaps[i])
            sw_B = sum(xB[s.loc[k]] for k in swaps[i])
            e_B = sum(c * xB[s.loc[k]] for k, c in coefs.items())
            fila["lhd"][i] = {"E_min_lp": e_lp, "E_min_milp": e_milp, "E_B": e_B,
                              "cap_swap": cap, "E_min/cap": e_min / cap, "corte": rhs,
                              "swaps_lp": sw_lp, "swaps_B": sw_B}
            cortes.append((i, rhs))
        # Minimo de swaps TOTALES del dia (MILP, objetivo entero): su cota dual
        # redondeada hacia arriba es un corte valido sum_i swaps_i >= n_min.
        todas_sw = [s.vloc[k] for i in lhds for k in swaps[i]]
        mm.setObjective(gp.quicksum(todas_sw), GRB.MINIMIZE)
        s._entera(False)
        mm.optimize()
        fila["min_swaps_lp"] = mm.ObjVal
        s._entera(True)
        mm.Params.TimeLimit = a.min_swaps_s
        mm.Params.MIPGap = 0
        mm.optimize()
        fila["min_swaps_cota"] = mm.ObjBound
        fila["min_swaps_inc"] = mm.ObjVal if mm.SolCount else None
        mm.Params.TimeLimit = INF
        n_min = math.ceil(mm.ObjBound - 1e-6)
        fila["n_min_swaps"] = n_min
        mm.setAttr("Obj", todas, objs)
        mm.ModelSense = GRB.MINIMIZE
        cortes = [(i, r) for i, r in cortes]
        # LP con los cortes
        filas_c = [mm.addLConstr(gp.quicksum(s.vloc[k] for k in swaps[i]), GRB.GREATER_EQUAL, r)
                   for i, r in cortes]
        filas_c.append(mm.addLConstr(gp.quicksum(todas_sw), GRB.GREATER_EQUAL, n_min))
        v_lp_c = lp_fijo(s)
        fila["lp_con_corte"] = v_lp_c
        fila["swaps_lp_con_corte"] = {i: sum(mm.getAttr("X", [s.vloc[k] for k in swaps[i]]))
                                      for i in lhds}
        if a.milp_s > 0:
            r = s.ub(xB, a.milp_s, 1e-4, mip_focus=3)
            fila["milp_con_corte_cota"] = mm.ObjBound
            fila["milp_con_corte_inc"] = r.get("valor")
        for c in filas_c:
            mm.remove(c)
        mm.update()
        return fila

    t0 = time.time()
    if a.modo == "rama17":
        filas = bd._paralelo(tarea_rama17, bloques)
        print(f"[corte] {len(filas)} dias en {time.time() - t0:.0f}s\n", flush=True)
        for f in filas:
            print(f"==== dia {f['dia']} ====")
            print(f"  fase A (min cargas, corta en 17): {f['faseA_cargas']} cargas en "
                  f"{f['faseA_t']:.0f}s, costo de esa operacion "
                  f"{f.get('faseA_costo', float('nan')):,.2f}")
            if f.get("faseB_cota") is not None:
                print(f"  fase B (costo con 17 cargas): cota {f['faseB_cota']:,.2f}  inc "
                      f"{f['faseB_inc'] or float('nan'):,.2f}  status {f.get('faseB_status')} "
                      f"en {f.get('faseB_t', 0):.0f}s")
        with open(os.path.join(a.out, "rama17.json"), "w", encoding="utf-8") as fh:
            json.dump(filas, fh, indent=1, default=float)
        return 0
    if a.modo == "ramas":
        filas = bd._paralelo(tarea_ramas, bloques)
        print(f"[corte] {len(filas)} dias en {time.time() - t0:.0f}s\n", flush=True)
        for f in filas:
            print(f"==== dia {f['dia']} ====")
            print(f"  rama <=17 cargas: cota {f['rama17_cota']:,.2f}  inc "
                  f"{f['rama17_inc'] or float('nan'):,.2f}  (status {f['rama17_status']})")
            print(f"  ramificando en nC: cota {f['ramif_cota']:,.2f}  inc "
                  f"{f['ramif_inc'] or float('nan'):,.2f}")
        with open(os.path.join(a.out, "ramas.json"), "w", encoding="utf-8") as fh:
            json.dump(filas, fh, indent=1, default=float)
        return 0
    if a.modo == "cargas":
        filas = bd._paralelo(tarea_cargas, bloques)
        print(f"[corte] {len(filas)} dias en {time.time() - t0:.0f}s\n", flush=True)
        for f in filas:
            n = f["n_min"]
            print(f"==== dia {f['dia']} ====")
            for q in ("LP", "B", "MILP"):
                if q in f:
                    print(f"  {q:5s} cargas {f[q]['cargas']:6.2f}  swaps {f[q]['swaps']:6.2f}  "
                          f"swaps en t0 {f[q]['swaps_t0']:5.2f}")
            print(f"  min cargas: LP {f['min_cargas_lp']:.3f}, MILP cota {f['min_cargas_cota']:.3f}, "
                  f"inc {f['min_cargas_inc']}  -> corte sum X_ini >= {n}")
            print(f"  LP sin corte {f['lp']:,.2f} | con >= {n}: {f[f'lp_corte_{n}']:,.2f} | "
                  f"con >= {n + 1}: {f[f'lp_corte_{n + 1}']:,.2f}")
            if "milp_corte_cota" in f:
                print(f"  MILP con corte: cota {f['milp_corte_cota']:,.2f}  inc "
                      f"{f['milp_corte_inc'] or float('nan'):,.2f}")
        with open(os.path.join(a.out, "corte_cargas.json"), "w", encoding="utf-8") as fh:
            json.dump(filas, fh, indent=1, default=float)
        return 0
    filas = bd._paralelo(tarea, bloques)
    print(f"[corte] {len(filas)} dias en {time.time() - t0:.0f}s\n", flush=True)
    for f in filas:
        print(f"==== dia {f['dia']} ====")
        print(f"{'LHD':10s} {'E_min LP':>9s} {'E_min MILP':>10s} {'E de B':>8s} {'cap/swap':>9s} "
              f"{'E_min/cap':>9s} {'corte':>6s} {'swaps LP':>9s} {'swaps B':>8s}")
        for i, d in f["lhd"].items():
            em = d["E_min_milp"]
            print(f"{i:10s} {d['E_min_lp']:>9,.1f} {(em if em is not None else float('nan')):>10,.1f} "
                  f"{d['E_B']:>8,.1f} {d['cap_swap']:>9,.1f} {d['E_min/cap']:>9.3f} "
                  f"{d['corte']:>6d} {d['swaps_lp']:>9.2f} {d['swaps_B']:>8.0f}")
        print(f"min swaps del dia: LP {f['min_swaps_lp']:.3f}, MILP cota {f['min_swaps_cota']:.3f} "
              f"inc {f['min_swaps_inc']} -> corte total >= {f['n_min_swaps']}")
        print(f"LP sin corte {f['lp']:,.2f}  ->  LP CON corte {f['lp_con_corte']:,.2f}"
              + (f"   | MILP con corte: cota {f['milp_con_corte_cota']:,.2f}, "
                 f"inc {f['milp_con_corte_inc'] or float('nan'):,.2f}"
                 if "milp_con_corte_cota" in f else ""))
        print()
    with open(os.path.join(a.out, "corte_swaps.json"), "w", encoding="utf-8") as fh:
        json.dump(filas, fh, indent=1, default=float)
    return 0


if __name__ == "__main__":
    sys.exit(main())
