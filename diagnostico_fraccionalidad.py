"""Diagnostico de fraccionalidad: ¿donde "hace trampa" la relajacion LP del dia?

diagnostico_gap_dias.py mostro que en cada dia la cota del MILP es EXACTAMENTE
el LP de la raiz (ni los cortes de Gurobi ni 60.000 nodos la mueven). Aca se
resuelve cada dia con la inversion de B fija, como LP y como MILP, y se
compara familia por familia:

  - variables fraccionarias del LP y suma de cada familia (swaps, baterias que
    inician carga, viajes, ...) contra el MILP;
  - costo del dia por familia (donde esta el ahorro del LP);
  - perfil por hora: costo de energia, swaps e inicios de carga;
  - energia consumida del dia (el presupuesto).

    python -u diagnostico_fraccionalidad.py --anios 4 --timelimit 300 \
        --out output/.../diagnostico_fraccionalidad
"""
import argparse
import collections
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
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias, INF  # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

M_DIR = "output/Resultados_finales_tesis/Mina_modelo"
TOL = 1e-6
MIN_POR_INTERVALO = 8


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n_years", type=int, default=4)
    ap.add_argument("--solucion", default=f"{M_DIR}/estab_4anios_B_box")
    ap.add_argument("--anios", default="4")
    ap.add_argument("--timelimit", type=float, default=300)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder="data/Tesis_final/Mina_modelo/", model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    costo, xB, eB = bd.cargar_incumbente(vec0)
    print(f"[frac] B pulido: {costo:,.2f}", flush=True)

    anios = {int(x) for x in a.anios.split(",")}
    bloques = [b for b, k in enumerate(P.claves) if eval(k)[0] in anios]

    def lp_fijo(s):
        """LP del dia con la inversion de B fija y la energia libre."""
        m = s.m
        s._entera(False)
        s._enlaces(s.M, 0.0, 0.0, INF)
        s._copias()
        s._rhs(xB, np.zeros(max(s.pres, default=-1) + 1))
        m.optimize()
        return m.ObjVal, m.getAttr("X", s.vloc), m.getAttr("X", s.vpres)

    def tarea(b):
        s = bd.subs[b]
        v_lp, x_lp, e_lp = lp_fijo(s)
        r = s.ub(xB, a.timelimit, 1e-4, mip_focus=1)
        e_milp = s.m.getAttr("X", s.vpres) if r["ok"] else None
        return b, v_lp, np.array(x_lp), e_lp, r, e_milp

    t0 = time.time()
    res = bd._paralelo(tarea, bloques)
    print(f"[frac] {len(res)} dias en {time.time() - t0:.0f}s", flush=True)

    salida = {}
    for b, v_lp, x_lp, e_lp, r, e_milp in res:
        s = bd.subs[b]
        clave = P.claves[b]
        y, d = eval(clave)
        x_mi = np.array(r["x"]) if r["ok"] else xB[s.loc]
        x_B = xB[s.loc]
        obj = P.obj[s.loc]
        fams = collections.OrderedDict()
        horas = collections.defaultdict(lambda: collections.defaultdict(float))
        for k, j in enumerate(s.loc):
            vd = por_var[P.nombre[j]]
            fam = vd.parent_component().name
            f = fams.setdefault(fam, {"n": 0, "ent": P.vtype[j] != "C", "lp": 0.0, "milp": 0.0,
                                      "B": 0.0, "frac_n": 0, "frac_sum": 0.0,
                                      "costo_lp": 0.0, "costo_milp": 0.0, "costo_B": 0.0})
            f["n"] += 1
            f["lp"] += x_lp[k]
            f["milp"] += x_mi[k]
            f["B"] += x_B[k]
            f["costo_lp"] += obj[k] * x_lp[k]
            f["costo_milp"] += obj[k] * x_mi[k]
            f["costo_B"] += obj[k] * x_B[k]
            if f["ent"]:
                fr = abs(x_lp[k] - round(x_lp[k]))
                if fr > TOL:
                    f["frac_n"] += 1
                    f["frac_sum"] += fr
            # Perfil por hora: t es el indice que sigue a (y, d).
            idx = vd.index()
            idx = idx if isinstance(idx, tuple) else (idx,)
            t = None
            for p in range(len(idx) - 2):
                if idx[p] == y and idx[p + 1] == d:
                    t = idx[p + 2]
                    break
            if t is None and fam in ("P_red", "P_gen", "Curt_g", "P_bat", "X_ini", "Z_swap"):
                t = idx[-1]          # en estas familias t es el ultimo indice
            try:
                t = int(t) if t is not None else None
            except (TypeError, ValueError):
                t = None
            if t is not None and t >= 1:
                h = (t - 1) * MIN_POR_INTERVALO // 60
                if obj[k] != 0:
                    horas[h]["costo_lp"] += obj[k] * x_lp[k]
                    horas[h]["costo_milp"] += obj[k] * x_mi[k]
                if fam in ("Z_swap", "X_ini", "P_red", "P_gen", "Curt_g", "Sv", "P_bat"):
                    horas[h][f"{fam}_lp"] += x_lp[k]
                    horas[h][f"{fam}_milp"] += x_mi[k]

        print(f"\n================ dia {clave} ================")
        print(f"LP {v_lp:,.2f}   MILP {r.get('valor', float('nan')):,.2f} "
              f"(gap {r.get('gap', float('nan')):.2%}, {r.get('t', 0):.0f}s)   "
              f"B {float(np.dot(obj, x_B)):,.2f}")
        print(f"energia del dia (presupuesto): LP {e_lp}  MILP {e_milp}  "
              f"B {[P.energia(xB, kk) for kk in s.pres]}")
        print(f"{'familia':22s} {'n':>6s} {'suma LP':>12s} {'suma MILP':>12s} {'suma B':>10s} "
              f"{'frac LP':>8s} {'costo LP':>11s} {'costo MILP':>11s} {'costo B':>11s}")
        for fam, f in fams.items():
            if f["n"] == 0:
                continue
            if not f["ent"] and abs(f["costo_lp"]) < 1e-6 and abs(f["costo_milp"]) < 1e-6:
                continue      # continuas sin costo: ruido
            print(f"{fam:22s} {f['n']:>6d} {f['lp']:>12,.2f} {f['milp']:>12,.2f} {f['B']:>10,.2f} "
                  f"{(str(f['frac_n']) if f['ent'] else '-'):>8s} {f['costo_lp']:>11,.1f} "
                  f"{f['costo_milp']:>11,.1f} {f['costo_B']:>11,.1f}")
        pch = value(om.model.p_charger)
        print(f"\n{'hora':>4s} {'costo LP':>9s} {'costo MI':>9s} {'red LP':>8s} {'red MI':>8s} "
              f"{'sol LP':>8s} {'sol MI':>8s} {'curt LP':>8s} {'curt MI':>8s} "
              f"{'carga LP':>9s} {'carga MI':>9s} {'bess LP':>8s} {'bess MI':>8s} "
              f"{'swp LP':>6s} {'swp MI':>6s} {'ini LP':>6s} {'ini MI':>6s}")
        tot = collections.defaultdict(float)
        for h in sorted(horas):
            hh = horas[h]
            for kk, vv in hh.items():
                tot[kk] += vv
            print(f"{h:>4d} {hh['costo_lp']:>9,.0f} {hh['costo_milp']:>9,.0f} "
                  f"{hh['P_red_lp']:>8,.0f} {hh['P_red_milp']:>8,.0f} "
                  f"{hh['P_gen_lp']:>8,.0f} {hh['P_gen_milp']:>8,.0f} "
                  f"{hh['Curt_g_lp']:>8,.0f} {hh['Curt_g_milp']:>8,.0f} "
                  f"{pch * hh['Sv_lp']:>9,.0f} {pch * hh['Sv_milp']:>9,.0f} "
                  f"{hh['P_bat_lp']:>8,.0f} {hh['P_bat_milp']:>8,.0f} "
                  f"{hh['Z_swap_lp']:>6.2f} {hh['Z_swap_milp']:>6.0f} "
                  f"{hh['X_ini_lp']:>6.2f} {hh['X_ini_milp']:>6.0f}")
        print(f" TOT {tot['costo_lp']:>9,.0f} {tot['costo_milp']:>9,.0f} "
              f"{tot['P_red_lp']:>8,.0f} {tot['P_red_milp']:>8,.0f} "
              f"{tot['P_gen_lp']:>8,.0f} {tot['P_gen_milp']:>8,.0f} "
              f"{tot['Curt_g_lp']:>8,.0f} {tot['Curt_g_milp']:>8,.0f} "
              f"{pch * tot['Sv_lp']:>9,.0f} {pch * tot['Sv_milp']:>9,.0f} "
              f"{tot['P_bat_lp']:>8,.0f} {tot['P_bat_milp']:>8,.0f}")
        salida[clave] = {"lp": v_lp, "milp": r.get("valor"), "B": float(np.dot(obj, x_B)),
                         "familias": fams, "horas": {h: dict(v) for h, v in horas.items()},
                         "energia_lp": e_lp, "energia_milp": e_milp}
    with open(os.path.join(a.out, "fraccionalidad.json"), "w", encoding="utf-8") as fh:
        json.dump(salida, fh, indent=1, default=float)
    return 0


if __name__ == "__main__":
    sys.exit(main())
