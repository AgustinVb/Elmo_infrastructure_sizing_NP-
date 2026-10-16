"""Convierte la mejor solucion de una corrida de cg_propia.py (mejor_solucion.npz,
con nombres del MPS) en un reporte JSON como el de setup.py, para usarla de
--incumbente_inicial en Nested Benders o de --solucion en la CG.

    python cg_solucion_a_json.py --data_folder data/Tesis_final/Mina_modelo/RED \
        --n_years 4 --npz output/.../cg_v2/mejor_solucion.npz --out output/.../cg_v2/solucion
"""
import argparse
import os
import sys
import tempfile
from argparse import Namespace

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from cg_propia import escribir_reporte                                   # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, required=True)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--npz", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, tempfile.mkdtemp(), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    # El mismo export que hizo la CG, para tener los mismos nombres del MPS.
    _info, por_var = gb.export(om.model, tempfile.mkdtemp(), mode="year_day", split=True)
    d = np.load(a.npz)
    valores = dict(zip(d["nombres"].tolist(), d["valores"].tolist()))
    nombres = list(por_var)
    vec = np.array([valores.get(n, 0.0) for n in nombres])
    faltan = [n for n in valores if n not in por_var and n != "ONE_VAR_CONSTANT"]
    if faltan:
        raise RuntimeError(f"{len(faltan)} variables del npz no estan en el modelo "
                           f"(primeras: {faltan[:3]}): ¿mismo caso y horizonte?")
    escribir_reporte(om, por_var, nombres, vec, ts, ms, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
