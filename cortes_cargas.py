"""VERIFICA el corte de cargas por dia que el modelo agrega solo
(min_charges_cut / ConstraintRules.min_charges_n en functions.py):

    sum_{k, t<tf} X_ini[k, y, d, t]  >=  n_min[y, d] = ceil(E_min / E_swap)

contra el minimo de las cargas de cada dia sobre su relajacion LP. Si la
formula diera MAS que el techo del LP, el corte cortaria soluciones factibles:
el script lo marca como ERROR. Igual o menos es valido.

El LP de referencia: ceil(minimo de las cargas del dia sobre su relajacion LP), con
TODO lo que el dia toma del resto del problema libre dentro de cotas validas
del problema original:

  - la inversion (X, N_bays, N_chargers, N_batteries, n_ssee_k, P_pot, G_g, H):
    libre en su rango segun las filas del maestro (limites de inversion);
  - b_bar[y] (capacidad degradada): acotado por arriba por su MAXIMO sobre las
    filas del maestro, con la energia de cada dia en su rango factible. Hace
    falta: la energia que repone un swap es (1 - bmin) * b_bar, y con la cota
    trivial (b_bar de bateria nueva) el redondeo se pierde -- en el año 4 daria
    ceil(15,9) = 16 en vez de ceil(16,35) = 17;
  - la energia del dia (presupuesto): libre.

Cualquier solucion factible del monolitico, restringida a un dia, es factible
para ese LP, asi que sus cargas son >= el minimo LP, y por ser enteras >= su
techo. Con --milp_s > 0 se intenta ademas subir n_min con la cota dual del MILP
que minimiza las cargas (objetivo entero: su cota tambien se redondea).

    python -u cortes_cargas.py --n_years 4 --out output/.../cortes_cargas_4anios.json
"""
import argparse
import json
import math
import os
import sys
import time
from argparse import Namespace

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)
# El corte NO puede estar activo mientras se mide el LP: se medirian las cargas
# de un modelo que ya las acota.
os.environ["ELMO_SIN_CORTE_CARGAS"] = "1"

import gurobipy as gp                                                    # noqa: E402
from gurobipy import GRB                                                 # noqa: E402

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias, INF  # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--n_years", type=int, required=True)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--milp_s", type=float, default=0,
                    help="segundos del MILP de minimo de cargas por dia (0 = solo LP)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", required=True, help="JSON de salida")
    a = ap.parse_args()
    tmp = os.path.join(os.path.dirname(os.path.abspath(a.out)) or ".", "_cortes_cargas_tmp")

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    t0 = time.time()
    om = OptModel(ms, ts, os.path.join(tmp, "_om"), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    m = om.model
    info, por_var = gb.export(m, os.path.join(tmp, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, m, por_var, jobs=a.jobs, out=None)
    P, M = bd.P, bd.maestro
    print(f"[cortes] modelo de {a.n_years} años armado en {time.time() - t0:.0f}s", flush=True)

    # 1) Rango de la energia de cada dia (LP, inversion libre) -> cotas de e.
    rpres = {}
    for b, rs in enumerate(bd._paralelo(lambda b: bd.subs[b].rango_presupuestos())):
        for kk, (lo, hi) in zip(bd.subs[b].pres, rs):
            rpres[kk] = (lo, hi)
            M.e[kk].LB, M.e[kk].UB = lo, hi
    # 2) Rango de todo lo que copian los dias, sobre las filas del maestro.
    copiadas = sorted({j for s in bd.subs for j in s.cop})
    rangos = M.rangos(copiadas)
    b_hi = {P.nombre[j]: hi for j, (lo, hi) in rangos.items() if P.nombre[j].startswith("b_bar")}
    print(f"[cortes] b_bar maximo valido por año: {b_hi}", flush=True)

    # 3) Minimo de cargas de cada dia.
    def tarea(b):
        s = bd.subs[b]
        mm = s.m
        ini = [k for k, j in enumerate(s.loc)
               if por_var[P.nombre[j]].parent_component().name == "X_ini"]
        s._entera(False)
        s._enlaces(0.0, INF, 0.0, INF)          # copias libres...
        lbs = [rangos[j][0] for j in s.cop]
        ubs = [rangos[j][1] for j in s.cop]
        s._copias(None, lbs, ubs)               # ...dentro de sus rangos validos
        mm.setObjective(gp.quicksum(s.vloc[k] for k in ini), GRB.MINIMIZE)
        mm.optimize()
        if mm.Status != GRB.OPTIMAL:
            return {"bloque": P.claves[b], "error": f"LP status {mm.Status}"}
        lp_min = mm.ObjVal
        cota = lp_min
        if a.milp_s > 0:
            s._entera(True)
            mm.Params.TimeLimit = a.milp_s
            mm.Params.MIPGap = 0
            mm.optimize()
            cota = max(cota, mm.ObjBound)
        return {"bloque": P.claves[b], "lp_min": lp_min, "cota": cota,
                "n_min": math.ceil(cota - 1e-6)}

    filas = bd._paralelo(tarea)
    cortes, errores = [], 0
    for f in filas:
        if "error" in f:
            print(f"[cortes] {f['bloque']}: {f['error']} -- sin referencia", flush=True)
            continue
        y, d = eval(f["bloque"])
        n_formula = om.constraint_rules.min_charges_n(m, y, d)
        estado = ("OK" if n_formula is None or n_formula <= f["n_min"] else "ERROR")
        errores += estado == "ERROR"
        cortes.append({"y": y, "d": d, "n_min": f["n_min"], "lp_min": f["lp_min"],
                       "cota": f["cota"], "n_formula": n_formula, "estado": estado})
        print(f"[cortes] {f['bloque']}: minimo LP de cargas {f['lp_min']:.3f}"
              + (f", cota MILP {f['cota']:.3f}" if a.milp_s > 0 else "")
              + f" -> techo {f['n_min']} | formula del modelo {n_formula}  {estado}",
              flush=True)
    print(f"[cortes] {'TODOS VALIDOS' if not errores else f'{errores} INVALIDOS'}", flush=True)
    salida = {"meta": {"data_folder": a.data_folder, "n_years": a.n_years,
                       "days_per_year": a.days_per_year, "milp_s": a.milp_s,
                       "b_bar_max": b_hi, "generado": time.strftime("%Y-%m-%d %H:%M")},
              "cortes": cortes}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(salida, f, indent=1)
    print(f"[cortes] {len(cortes)} cortes en {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
