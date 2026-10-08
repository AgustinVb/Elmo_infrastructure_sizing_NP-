"""Diagnostico del gap LP-MILP de los dias, con la inversion del incumbente.

diagnostico_milp_inversion_fija.py mostro que la cota MILP de Gurobi casi no
sube desde la raiz: el hueco de RED_GEN esta en la formulacion del dia. Este
script, para unos dias elegidos y con TODA la planificacion fija en el
incumbente (modo ub de Subproblema, mas el corte de cargas con su b_bar):

  1  COMPOSICION: costo del dia por familia de variables (P_red, X_ini, ...)
     en la relajacion LP y en la operacion entera del incumbente, y cuantas
     enteras quedan fraccionarias en el LP por familia. Dice que parte del
     costo "ahorra" el LP y con que variables.
  2  PARAMETROS: el mismo MILP con varios juegos de parametros de Gurobi, en
     paralelo, registrando la cota al terminar la raiz y en varios instantes.
     Dice si algun ajuste sube la cota mucho mas que el default.

    python -u diagnostico_gap_lp_milp.py --data_folder data/Tesis_final/Mina_modelo/RED_GEN \
        --solucion output/.../mejorar_ub_red_gen_2/solucion --ub_esperado 2131851.52654777 \
        --dias "(4, 105);(6, 196);(8, 288)" --out output/.../diagnostico_gap_lp_milp
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

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

JUEGOS = {
    "default": {},
    "focus3": {"MIPFocus": 3},
    "focus3_cuts3": {"MIPFocus": 3, "Cuts": 3},
    "focus3_sim2_pre2": {"MIPFocus": 3, "Symmetry": 2, "Presolve": 2},
}


def familia(nombre):
    return nombre.split("(")[0].split("[")[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED_GEN")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del incumbente")
    ap.add_argument("--ub_esperado", type=float, required=True)
    ap.add_argument("--dias", default="(4, 105);(6, 196);(8, 288)", help="claves separadas por ;")
    ap.add_argument("--juegos", default=",".join(JUEGOS), help="juegos de parametros, por coma")
    ap.add_argument("--timelimit", type=float, default=900)
    ap.add_argument("--hilos", type=int, default=5, help="hilos por MILP")
    ap.add_argument("--instantes", default="10,60,300,900")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    instantes = sorted(float(t) for t in a.instantes.split(","))
    juegos = [j.strip() for j in a.juegos.split(",") if j.strip()]
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, om.model, por_var, jobs=1, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    UB, completo0, _ = bd.cargar_incumbente(vec0)
    print(f"[gap] incumbente pulido {UB:,.2f} (esperado {a.ub_esperado:,.2f})", flush=True)
    if abs(UB - a.ub_esperado) > 1e-6 * abs(UB):
        print("[gap] ABORTA: el incumbente no se reconstruye bien")
        return 1

    tf = max(om.model.time_intervals_set)
    cols_bb = {int(P.nombre[j][len("b_bar("):-1]): j for j in P.maestro_vars
               if P.nombre[j].startswith("b_bar(")}
    claves = [k.strip() for k in a.dias.split(";") if k.strip()]
    claves = [str(eval(k)) for k in claves]
    salida = {"UB": UB, "dias": {}}
    tareas = []
    for k in claves:
        b = P.claves.index(k)
        s = bd.subs[b]
        y, d = eval(k)
        # Modo ub: copias del maestro fijas en el incumbente, energia libre.
        s._entera(True)
        s._enlaces(s.M, 0.0, 0.0, GRB.INFINITY)
        s._copias()
        s._rhs(completo0, np.zeros(max(s.pres, default=-1) + 1))
        # Corte de cargas con el b_bar del incumbente (valido con esa planificacion).
        kx = [i for i, j in enumerate(s.loc)
              if (vd := por_var.get(P.nombre[j])) is not None
              and vd.parent_component().name == "X_ini" and vd.index()[-1] < tf]
        n_corte, _, _ = cota_dia(om, y, d, float(completo0[cols_bb[y]]))
        s.m.addLConstr(gp.quicksum(s.vloc[i] for i in kx), GRB.GREATER_EQUAL,
                       float(n_corte or 0))
        s.m.update()
        idx_loc = [v.index for v in s.vloc]
        x_inc = completo0[s.loc]

        # 1) composicion LP vs incumbente
        lp = s.m.relax()
        lp.Params.OutputFlag = 0
        lp.optimize()
        x_lp = np.array(lp.getAttr("X", lp.getVars()))[idx_loc]
        comp = {}
        for i, j in enumerate(s.loc):
            f = familia(P.nombre[j])
            c = comp.setdefault(f, {"lp": 0.0, "inc": 0.0, "n": 0, "ent": 0, "frac": 0,
                                   "suma_lp": 0.0, "suma_inc": 0.0})
            c["lp"] += P.obj[j] * x_lp[i]
            c["inc"] += P.obj[j] * x_inc[i]
            c["n"] += 1
            c["suma_lp"] += x_lp[i]
            c["suma_inc"] += x_inc[i]
            if P.vtype[j] != "C":
                c["ent"] += 1
                if abs(x_lp[i] - round(x_lp[i])) > 1e-6:
                    c["frac"] += 1
        v_inc = float(np.dot(P.obj[s.loc], x_inc))
        print(f"\n[gap] {k}: LP {lp.ObjVal:,.2f}   incumbente {v_inc:,.2f}   "
              f"hueco {v_inc - lp.ObjVal:,.2f} ({(v_inc - lp.ObjVal) / max(abs(v_inc), 1):.1%})"
              f"   corte de cargas {n_corte}", flush=True)
        print(f"[gap]   {'familia':<22}{'LP':>12}{'incumbente':>12}{'inc - LP':>12}"
              f"{'enteras':>9}{'fracc. LP':>10}")
        for f, c in sorted(comp.items(), key=lambda kv: -abs(kv[1]["inc"] - kv[1]["lp"])):
            if abs(c["lp"]) < 1e-6 and abs(c["inc"]) < 1e-6 and not c["frac"]:
                continue
            print(f"[gap]   {f:<22}{c['lp']:>12,.2f}{c['inc']:>12,.2f}"
                  f"{c['inc'] - c['lp']:>12,.2f}{c['ent']:>9}{c['frac']:>10}")
        fr_sin_costo = {f: (c["frac"], c["ent"]) for f, c in comp.items()
                        if c["frac"] and abs(c["inc"] - c["lp"]) < 1e-6}
        if fr_sin_costo:
            print("[gap]   fraccionarias sin costo propio: " + ", ".join(
                f"{f} {fr}/{n}" for f, (fr, n) in sorted(fr_sin_costo.items())))
        # Suma de valores por familia (energia, potencia, conteos): donde se
        # separan las dos operaciones aunque la familia no tenga costo.
        print(f"[gap]   {'familia (suma de valores)':<26}{'LP':>14}{'incumbente':>14}{'inc - LP':>14}")
        for f, c in sorted(comp.items()):
            dif = c["suma_inc"] - c["suma_lp"]
            if abs(dif) > 1e-6 * max(1.0, abs(c["suma_inc"])):
                print(f"[gap]   {f:<26}{c['suma_lp']:>14,.2f}{c['suma_inc']:>14,.2f}{dif:>14,.2f}")
        salida["dias"][k] = {"lp": lp.ObjVal, "incumbente": v_inc, "n_corte": n_corte,
                             "composicion": comp, "juegos": {},
                             "valores": {P.nombre[j]: [float(x_lp[i]), float(x_inc[i])]
                                         for i, j in enumerate(s.loc)
                                         if abs(x_lp[i] - x_inc[i]) > 1e-6}}
        start = [round(x_inc[i]) for i in s.ent_loc]
        for jg in juegos:
            mm = s.m.copy()
            vs = mm.getVars()
            mm.setAttr("Start", [vs[idx_loc[i]] for i in s.ent_loc], start)
            tareas.append((k, jg, mm))

    # 2) juegos de parametros, todos en paralelo
    def correr(tarea):
        k, jg, mm = tarea
        mm.Params.OutputFlag = 0
        mm.Params.Threads = a.hilos
        mm.Params.TimeLimit = a.timelimit
        mm.Params.MIPGap = 1e-4
        for p, v in JUEGOS[jg].items():
            mm.setParam(p, v)
        reg = {"raiz": None, "cotas": {}}

        def cb(model, where):
            if where == GRB.Callback.MIP:
                rt = model.cbGet(GRB.Callback.RUNTIME)
                bnd = model.cbGet(GRB.Callback.MIP_OBJBND)
                if reg["raiz"] is None and model.cbGet(GRB.Callback.MIP_NODCNT) >= 1:
                    reg["raiz"] = (bnd, rt)
                for t in instantes:
                    if rt <= t:
                        reg["cotas"][t] = bnd

        mm.optimize(cb)
        fin = mm.ObjBound
        for t in instantes:
            if t >= mm.Runtime or t not in reg["cotas"]:
                reg["cotas"][t] = fin if t >= mm.Runtime else None
        reg.update({"final": fin, "inc": mm.ObjVal if mm.SolCount else None,
                    "t": mm.Runtime, "status": mm.Status, "nodos": mm.NodeCount})
        return k, jg, reg

    t_m = time.time()
    with ThreadPoolExecutor(max_workers=len(tareas)) as pool:
        res = list(pool.map(correr, tareas))
    print(f"\n[gap] {len(tareas)} MILP en {time.time() - t_m:.0f}s", flush=True)
    for k in claves:
        d = salida["dias"][k]
        print(f"\n[gap] {k}: LP {d['lp']:,.2f}  incumbente {d['incumbente']:,.2f}   "
              "(cota y, entre parentesis, la fraccion del hueco LP-incumbente que cierra)")
        print(f"[gap]   {'juego':<20}{'raiz':>16}" +
              "".join(f"{'@' + str(int(t)) + 's':>16}" for t in instantes) + f"{'nodos':>10}")
        hueco = max(d["incumbente"] - d["lp"], 1e-9)

        def fmt(c):
            return f"{'-':>16}" if c is None else f"{c:>9,.0f} ({(c - d['lp']) / hueco:>3.0%})"

        for kk, jg, reg in res:
            if kk != k:
                continue
            d["juegos"][jg] = reg
            raiz = reg["raiz"][0] if reg["raiz"] else None
            print(f"[gap]   {jg:<20}{fmt(raiz)}" + "".join(fmt(reg["cotas"][t]) for t in instantes)
                  + f"{reg['nodos']:>10,.0f}", flush=True)

    with open(os.path.join(a.out, "diagnostico.json"), "w", encoding="utf-8") as fh:
        json.dump(salida, fh, indent=1, default=str)
    print(f"\n[gap] tiempo total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
