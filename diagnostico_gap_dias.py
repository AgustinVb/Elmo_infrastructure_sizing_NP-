"""Diagnostico: ¿el gap de Mina_modelo es de COTA o de SOLUCION?

El Benders por dias mostro que la cota operacional ya elige la inversion de la
solucion B, asi que el gap (~73.000 a 4 años) vive en la operacion de los dias
con esa inversion. Aca se resuelve cada dia con la inversion de B FIJA y mucho
tiempo, y se compara:

    LP       el dia con la operacion relajada (lo que ve la cota)
    B        la operacion de la solucion B (lo que da la UB)
    MILP     incumbente y cota dual de Gurobi tras --timelimit

Si la cota del MILP sube hasta cerca de B, el gap es de COTA (B esta cerca del
optimo con esa inversion). Si el incumbente baja claramente de B, hay gap de
SOLUCION.

    python -u diagnostico_gap_dias.py --anios 4 --timelimit 1800 --jobs 4 \
        --out output/.../diagnostico_gap_dias
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
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

M_DIR = "output/Resultados_finales_tesis/Mina_modelo"


def cargar_respaldo(om, ms, ts, ruta):
    """Carga sobre om.model el incumbente de un incumbente_respaldo.pkl de
    Nested Benders (mas X/Delta_X exogenos). Devuelve el dict del respaldo."""
    # best_full_solution: {año: {variable: {indice: valor}}}, con el
    # incumbente de cada bloque anual (ver driver._guardar_respaldo).
    import pickle
    with open(ruta, "rb") as fh:
        r = pickle.load(fh)
    n_ok = n_falta = 0
    for _y, vars_y in r["best_full_solution"].items():
        for nombre, vals in vars_y.items():
            comp = getattr(om.model, nombre, None)
            # Las escalares (H, y D_prev/alpha que solo existen en el
            # bloque) vienen como un float suelto, no como {indice: valor}.
            if not isinstance(vals, dict):
                if comp is not None and not comp.is_indexed():
                    comp.set_value(vals, skip_validation=True)
                    n_ok += 1
                else:
                    n_falta += 1
                continue
            if comp is None:
                n_falta += len(vals)
                continue
            for idx, v in vals.items():
                if idx in comp:
                    vd = comp[idx]
                    # Ruido de IntFeasTol en las enteras (como en
                    # driver.build_report_model).
                    if vd.is_binary() or vd.is_integer():
                        v = int(round(v))
                    vd.set_value(v, skip_validation=True)
                    n_ok += 1
                else:
                    n_falta += 1
    # X y Delta_X son exogenas en el modo descompuesto: no estan en el
    # respaldo y, si quedan en 0, el pulido con las enteras fijas es
    # infactible. Se reconstruyen como en driver.build_report_model.
    from src.optimization.decomposition.driver import infer_exogenous_stations
    exo = infer_exogenous_stations(ms, ts)
    anterior = {k: 0 for k in om.model.stations_set}
    for y in om.model.years:
        for k in om.model.stations_set:
            om.model.X[k, y].value = exo[y][k]
            om.model.Delta_X[k, y].value = max(exo[y][k] - anterior[k], 0)
        anterior = {k: exo[y][k] for k in om.model.stations_set}
    # El pulido fija TODAS las enteras: las que quedan sin valor van a 0.
    import pyomo.environ as pyo
    sin_valor = {}
    for comp in om.model.component_objects(pyo.Var, active=True):
        for vd in comp.values():
            if (vd.is_binary() or vd.is_integer()) and vd.value is None:
                sin_valor[comp.name] = sin_valor.get(comp.name, 0) + 1
    if sin_valor:
        print(f"[diag] enteras sin valor (iran a 0 en el pulido): {sin_valor}", flush=True)
    print(f"[diag] respaldo {ruta} (iteracion {r['iteracion']}, "
          f"incumbente {r['best_cost']:,.2f}): {n_ok:,} valores cargados, "
          f"{n_falta:,} sin variable en el modelo", flush=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n_years", type=int, default=4)
    ap.add_argument("--solucion", default=f"{M_DIR}/estab_4anios_B_box")
    ap.add_argument("--respaldo", default=None,
                    help="incumbente_respaldo.pkl de Nested Benders; reemplaza a --solucion")
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--anios", default="4", help="años a diagnosticar, por coma")
    ap.add_argument("--timelimit", type=float, default=1800)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"),
                  init_solution_folder=None if a.respaldo else a.solucion,
                  mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    if a.respaldo:
        cargar_respaldo(om, ms, ts, a.respaldo)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    costo, xB, eB = bd.cargar_incumbente(vec0)
    print(f"[diag] B pulido: {costo:,.2f}", flush=True)

    anios = {int(x) for x in a.anios.split(",")}
    bloques = [b for b, k in enumerate(P.claves) if eval(k)[0] in anios]

    def tarea(b):
        s = bd.subs[b]
        lp = s.lp(xB, eB)
        # Operacion de B en ese dia: su costo con la inversion fija.
        opex_B = float(np.dot(P.obj[s.loc], xB[s.loc]))
        s.m.Params.LogFile = os.path.join(a.out, f"dia_{P.claves[b].replace(' ', '')}.log")
        s.m.Params.OutputFlag = 1
        s.m.Params.LogToConsole = 0
        # MIPFocus 3: lo que interesa es cuanto sube la COTA del dia.
        r = s.ub(xB, a.timelimit, 1e-4, mip_focus=3)
        cota = s.m.ObjBound
        return {"bloque": P.claves[b], "lp": lp["valor"], "B": opex_B,
                "milp_inc": r.get("valor"), "milp_cota": cota, "t": r.get("t"),
                "status": s.m.Status}

    t0 = time.time()
    filas = bd._paralelo(tarea, bloques)
    print(f"\n[diag] {len(filas)} dias en {time.time() - t0:.0f}s\n")
    print(f"{'dia':>10} {'LP':>12} {'cota MILP':>12} {'MILP inc':>12} {'B':>12} "
          f"{'gap B-cota':>11} {'B-inc':>9}")
    tot = {"lp": 0, "cota": 0, "inc": 0, "B": 0}
    for f in filas:
        print(f"{f['bloque']:>10} {f['lp']:>12,.0f} {f['milp_cota']:>12,.0f} "
              f"{(f['milp_inc'] or float('nan')):>12,.0f} {f['B']:>12,.0f} "
              f"{(f['B'] - f['milp_cota']) / f['B']:>10.2%} "
              f"{f['B'] - (f['milp_inc'] or f['B']):>9,.0f}")
        tot["lp"] += f["lp"]
        tot["cota"] += f["milp_cota"]
        tot["inc"] += f["milp_inc"] or f["B"]
        tot["B"] += f["B"]
    print(f"{'TOTAL':>10} {tot['lp']:>12,.0f} {tot['cota']:>12,.0f} {tot['inc']:>12,.0f} "
          f"{tot['B']:>12,.0f}")
    print(f"\n[diag] B - LP = {tot['B'] - tot['lp']:,.0f} (lo que la cota no ve); "
          f"B - cota MILP = {tot['B'] - tot['cota']:,.0f}; B - mejor MILP = "
          f"{tot['B'] - tot['inc']:,.0f}")
    with open(os.path.join(a.out, "diagnostico.json"), "w", encoding="utf-8") as fh:
        json.dump({"filas": filas, "totales": tot, "timelimit": a.timelimit}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
