"""Precalcula el corte de cargas por LHD CONDICIONAL al reemplazo, valido para
TODA inversion, para agregarlo a la cota operacional de Nested Benders
(setup.py --corte_cargas_lhd <json>).

El certificado (certificar_inversiones.py) aplica el corte de cargas por LHD
(cota_cargas_lhd.cota_dia) con la inversion de cada candidata FIJA: la
capacidad maxima de la bateria b_bar_max[y] sale del maestro con esas enteras,
incluido R (reemplazo). Sin reemplazo la bateria esta degradada y hacen falta
mas cargas; eso es buena parte de lo que la LB del certificado le gana a la cota
operacional (RED_GEN v2: 2,113 M contra 2,072 M).

En la cota operacional R es variable, asi que el corte tiene que valer con y sin
reemplazo:

    sum_{k, t<tf} X_ini[k,y,d,t]  >=  n_sinR[y,d] - (n_sinR[y,d] - n_nom[y,d]) * sum_{s<=y} R[s]

  n_nom[y,d]   cota_dia con la capacidad nominal b_max_pool (bateria nueva):
               vale siempre.
  n_sinR[y,d]  cota_dia con cap_sinR[y] = max b_bar[y] con R[s] = 0 para s <= y
               y el resto LIBRE (relajacion lineal de las filas del maestro de
               BendersDias, con la energia de cada dia en su rango factible, como
               el certificado): vale si no hubo reemplazo hasta y.

Si hubo algun reemplazo hasta y, el lado derecho queda <= n_nom (R binaria).
El maestro incluye las filas de degradacion del MODELO (McCormick), asi que
cap_sinR es una cota valida para el modelo y no solo para la fisica.

No depende de la generacion ni del BESS (cota_dia usa produccion, energia de
viaje y capacidad), asi que el mismo JSON vale para RED, RED_GEN y RED_GEN_BESS.

    python -u corte_cargas_op.py --data_folder data/Tesis_final/Mina_modelo/RED_GEN \
        --out data/Tesis_final/Mina_modelo/corte_cargas_lhd.json
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

from cota_cargas_lhd import cota_dia                                     # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED_GEN")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True, help="JSON de salida")
    ap.add_argument("--tmp", default=None, help="carpeta para el MPS (default: junto al --out)")
    a = ap.parse_args()
    t0 = time.time()
    tmp = a.tmp or os.path.join(os.path.dirname(os.path.abspath(a.out)), "_corte_cargas_op")

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                   consumption_model="wp1", wp2_consumption_json=None, n_years=a.n_years,
                   days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(tmp, "_monolitico"),
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    model = om.model
    cap_nom = float(value(model.b_max_pool))
    years = sorted(model.years)
    days = sorted(model.days)

    info, por_var = gb.export(model, os.path.join(tmp, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, model, por_var, jobs=a.jobs, out=None, verbose=False)
    P, M = bd.P, bd.maestro
    # Energia de cada dia en su rango factible (igual que el certificado).
    por_bloque = bd._paralelo(lambda b: bd.subs[b].rango_presupuestos())
    for b, rs in enumerate(por_bloque):
        for kk, (lo, hi) in zip(bd.subs[b].pres, rs):
            M.e[kk].LB, M.e[kk].UB = lo, hi
    pos = {P.nombre[j]: k for k, j in enumerate(P.maestro_vars)}
    col = {P.nombre[j]: j for j in P.maestro_vars}

    # cap_sinR[y]: max b_bar[y] con R[s] = 0 para s <= y (lo demas libre, LP).
    faltan = [f"R({s})" for s in years[1:] if f"R({s})" not in pos]
    if faltan or f"b_bar({years[1]})" not in col:
        raise RuntimeError(f"el maestro no tiene las columnas esperadas: {faltan or 'b_bar'}")
    cap_sinR = {years[0]: cap_nom}
    for y in years[1:]:
        fijadas = []
        for s in years:
            if s <= y and f"R({s})" in pos:
                v = M.x[pos[f"R({s})"]]
                fijadas.append((v, v.LB, v.UB))
                v.LB = v.UB = 0.0
        try:
            rg = M.rangos([col[f"b_bar({y})"]])
            hi = rg[col[f"b_bar({y})"]][1]
        finally:
            for v, lb, ub in fijadas:
                v.LB, v.UB = lb, ub
        cap_sinR[y] = min(cap_nom, hi) if np.isfinite(hi) else cap_nom
        print(f"[corte] cap_sinR[{y}] = {cap_sinR[y]:.2f} kWh", flush=True)

    filas = []
    for y in years:
        for d in days:
            n_nom, _, _ = cota_dia(om, y, d, cap_nom)
            n_sinR, _, _ = cota_dia(om, y, d, cap_sinR[y])
            n_nom = int(n_nom or 0)
            n_sinR = max(int(n_sinR or 0), n_nom)
            filas.append({"y": int(y), "d": int(d), "n_nom": n_nom, "n_sinR": n_sinR,
                          "cap_sinR": cap_sinR[y]})
            print(f"[corte] ({y},{d:>3})  n_nom {n_nom:>3}  n_sinR {n_sinR:>3}", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"data_folder": a.data_folder, "b_max_pool": cap_nom,
                   "cap_sinR": {str(k): v for k, v in cap_sinR.items()}, "cortes": filas,
                   "generado": time.strftime("%Y-%m-%d %H:%M")}, fh, indent=1)
    print(f"[corte] {len(filas)} dias en {a.out} ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
