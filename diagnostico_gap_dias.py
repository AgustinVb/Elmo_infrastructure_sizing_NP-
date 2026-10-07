"""Diagnostico: ¿el gap es de COTA o de SOLUCION? (portado de
battery_swapping_multiaño, 2026-10)

Con la inversion de una solucion B FIJA se resuelve cada (año, dia) con mucho
tiempo y se compara:

    LP       el dia con la operacion relajada (lo que ve la cota operacional)
    B        la operacion de la solucion B (lo que da la UB)
    MILP     incumbente y cota dual de Gurobi tras --timelimit

- B - LP es lo que la relajacion operacional no ve con esa inversion.
- Si la cota del MILP sube hasta cerca de B, el gap es de COTA: la relajacion
  es floja y hace falta una desigualdad valida que la apriete (en swap fue el
  corte de cargas por LHD, min_charges_cut / cota_cargas_lhd.py).
- Si el incumbente MILP baja claramente de B, hay gap de SOLUCION: lo recupera
  mejorar_ub.py.

En swap este diagnostico mostro, por dia, que la cota del MILP cerraba muy por
encima del LP (gap de cota) en los años de mayor produccion; de ahi salio el
corte de cargas. Aca sirve para decidir si carga on-board necesita algo
equivalente.

    python -u diagnostico_gap_dias.py --data_folder data/Resultados_finales_tesis/Mina_modelo/P_red \
        --solucion output/P_red_hibrido_strong_ob --anios 4 --timelimit 1800 --jobs 4 \
        --out output/diagnostico_gap_dias_ob
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

from pyomo.environ import value                                          # noqa: E402

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import particion                    # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.decomposition.respaldo import cargar_respaldo      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--model", default="elmo_data.xlsx")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, choices=[2, 4], default=4)
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp1")
    ap.add_argument("--wp2_consumption_json", default=None)
    ap.add_argument("--free_charging", action="store_true")
    ap.add_argument("--free_maintenance", action="store_true")
    ap.add_argument("--solucion", default=None, help="carpeta con los JSON de la solucion B")
    ap.add_argument("--respaldo", default=None,
                    help="incumbente_respaldo.pkl de Nested Benders; reemplaza a --solucion")
    ap.add_argument("--ub_esperado", type=float, default=None,
                    help="costo de B; el pulido tiene que reproducirlo (si no, aborta)")
    ap.add_argument("--anios", default="4", help="años a diagnosticar, por coma")
    ap.add_argument("--timelimit", type=float, default=1800)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if (a.respaldo is None) == (a.solucion is None):
        ap.error("se necesita exactamente uno de --respaldo o --solucion")
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder=a.data_folder, model=a.model, series="time_series.xlsx",
                   consumption_model=a.consumption_model,
                   wp2_consumption_json=a.wp2_consumption_json, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"),
                  init_solution_folder=a.solucion, mccormick_degradation=True,
                  free_charging=a.free_charging, free_maintenance=a.free_maintenance)
    esperado = a.ub_esperado
    if a.respaldo:
        esperado = cargar_respaldo(om, ms, ts, a.respaldo)["best_cost"]
    info, por_var = particion.export(om.model, os.path.join(a.out, "_mps"), mode="year_day")
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, out=None)
    P = bd.P
    print(f"[diag] particion: {P.resumen()}", flush=True)
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    costo, xB, eB = bd.cargar_incumbente(vec0)
    print(f"[diag] B pulido: {costo:,.2f} (esperado "
          f"{'-' if esperado is None else f'{esperado:,.2f}'})", flush=True)
    if esperado is not None and abs(costo - esperado) > 1e-6 * abs(costo):
        print("[diag] ABORTA: B no se reconstruye (¿otro regimen, datos o consumo?)")
        return 1

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
        return {"bloque": P.claves[b], "lp": lp.get("valor"), "B": opex_B,
                "milp_inc": r.get("valor"), "milp_cota": cota, "t": r.get("t"),
                "status": s.m.Status}

    t0 = time.time()
    filas = bd._paralelo(tarea, bloques)
    print(f"\n[diag] {len(filas)} dias en {time.time() - t0:.0f}s\n")
    print(f"{'dia':>10} {'LP':>12} {'cota MILP':>12} {'MILP inc':>12} {'B':>12} "
          f"{'gap B-cota':>11} {'B-inc':>9}")
    tot = {"lp": 0, "cota": 0, "inc": 0, "B": 0}
    for f in filas:
        den = f["B"] if f["B"] else 1.0
        print(f"{f['bloque']:>10} {(f['lp'] or float('nan')):>12,.0f} {f['milp_cota']:>12,.0f} "
              f"{(f['milp_inc'] or float('nan')):>12,.0f} {f['B']:>12,.0f} "
              f"{(f['B'] - f['milp_cota']) / den:>10.2%} "
              f"{f['B'] - (f['milp_inc'] or f['B']):>9,.0f}")
        tot["lp"] += f["lp"] or 0.0
        tot["cota"] += f["milp_cota"]
        tot["inc"] += f["milp_inc"] or f["B"]
        tot["B"] += f["B"]
    print(f"{'TOTAL':>10} {tot['lp']:>12,.0f} {tot['cota']:>12,.0f} {tot['inc']:>12,.0f} "
          f"{tot['B']:>12,.0f}")
    print(f"\n[diag] B - LP = {tot['B'] - tot['lp']:,.0f} (lo que la cota no ve); "
          f"B - cota MILP = {tot['B'] - tot['cota']:,.0f}; B - mejor MILP = "
          f"{tot['B'] - tot['inc']:,.0f}")
    with open(os.path.join(a.out, "diagnostico.json"), "w", encoding="utf-8") as fh:
        json.dump({"filas": filas, "totales": tot, "timelimit": a.timelimit,
                   "costo_B": costo}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
