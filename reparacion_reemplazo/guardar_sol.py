"""Evalua el incumbente de Benders (caso 6 v4) con +1 bateria y +1 bahia desde
el año ANIO y la degradacion/reemplazo libres: la operacion entera del
incumbente queda fija, las binarias de degradacion (R, u_nf, z_bb, ...) y los
stocks derivados de la inversion quedan libres; MIP sobre el monolitico."""
import collections
import os
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import numpy as np                                                       # noqa: E402
from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

DATA, RESPALDO, OUT = sys.argv[1:4]
ANIO = int(sys.argv[4]) if len(sys.argv) > 4 else 3
os.makedirs(OUT, exist_ok=True)

ns = Namespace(data_folder=DATA, model="elmo_data.xlsx", series="time_series.xlsx",
               consumption_model="wp1", wp2_consumption_json=None, n_years=10, days_per_year=4)
_s, ms, ts = build_mine(ns)
om = OptModel(ms, ts, os.path.join(OUT, "_monolitico"), mccormick_degradation=True,
              free_charging=True, free_maintenance=True)
esperado = cargar_respaldo(om, ms, ts, RESPALDO)["best_cost"]
info, por_var = gb.export(om.model, os.path.join(OUT, "_mps"), mode="year_day", split=False)
bd = BendersDias(info, om.model, por_var, jobs=4, out=None)
P = bd.P
vec0 = np.zeros(P.nv)
for j, n in enumerate(P.nombre):
    vd = por_var.get(n)
    v = value(vd, exception=False) if vd is not None else None
    vec0[j] = 0.0 if v is None else v

F = P.F

xs = np.load(os.path.join(OUT, "x_sin_reemplazo.npy"))
costo, completo = bd.pulir(np.where(np.array([t != "C" for t in P.vtype]), xs, 0.0))
print(f"[guardar] pulido con las enteras de la solucion: {costo}", flush=True)
from src.io.printer import Printer
for j, n in enumerate(P.nombre):
    vd = por_var.get(n)
    if vd is not None and not vd.fixed:
        x = completo[j]
        vd.set_value(round(x) if not vd.is_continuous() else x, skip_validation=True)
destino = os.path.join(OUT, "solucion")
Printer(om, destino, ts, ms).write_variables_jsons()
print(f"[guardar] solucion en {destino}  UB {costo:.4f}", flush=True)
