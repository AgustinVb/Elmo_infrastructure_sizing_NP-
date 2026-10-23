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
F.Params.Threads = 16
UB0, _ = bd.pulir(vec0)
print(f"[eval] incumbente pulido {UB0:,.2f} (esperado {esperado:,.2f})", flush=True)

ent = [j for j, t in enumerate(P.vtype) if t != "C"]
pref = collections.Counter(P.nombre[j].split("(")[0] for j in ent)
print("[eval] enteras por familia:", dict(pref), flush=True)

# Inversion nueva: +1 bateria y +1 bahia en ANIO (Delta_*); los stocks se recalculan.
nuevo = vec0.copy()
for j in ent:
    n = P.nombre[j]
    if n.startswith(("Delta_N_batteries(", "Delta_N_bays(")) and n.endswith(f"_{ANIO})"):
        nuevo[j] = round(vec0[j]) + 1
        print(f"[eval] {n}: {vec0[j]:.0f} -> {nuevo[j]:.0f}", flush=True)
    # cada bahia necesita un cargador (bays_le_chargers): el 2o cargador, que el
    # incumbente compra en el año 4, se adelanta al año ANIO
    if n.startswith("Delta_N_chargers(") and n.endswith((f"_{ANIO})", "_4)")) and ANIO < 4:
        nuevo[j] = round(vec0[j]) + (1 if n.endswith(f"_{ANIO})") else -1)
        print(f"[eval] {n}: {vec0[j]:.0f} -> {nuevo[j]:.0f}", flush=True)

FIJAR_INV = ("Delta_",)
LIBRES = ("R", "u_nf", "z_bb", "N_batteries", "N_bays", "N_chargers", "n_battery_fleet",
          "N_batteries_total", "n_ssee_k", "G", "H", "X", "Z_repl", "S", "Sv", "X_dch", "X_ini", "W")
vs = F.getVars()
n_lib = 0
for j in ent:
    fam = P.nombre[j].split("(")[0]
    if fam in LIBRES and not fam.startswith(FIJAR_INV):
        # se restauran las cotas originales del modelo
        vd = por_var.get(P.nombre[j])
        lb, ub = (vd.lb, vd.ub) if vd is not None else (0, GRB.INFINITY)
        vs[j].LB = 0 if lb is None else lb
        vs[j].UB = GRB.INFINITY if ub is None else ub
        vs[j].Start = round(vec0[j])
        n_lib += 1
    else:
        vs[j].LB = vs[j].UB = float(round(nuevo[j]))
print(f"[eval] {n_lib} enteras libres (degradacion y stocks), resto fijas", flush=True)
F.Params.TimeLimit = 1800
F.Params.MIPGap = 1e-4
F.optimize()
print(f"[eval] status {F.Status}  SolCount {F.SolCount}", flush=True)
if F.SolCount:
    x = np.array(F.getAttr("X", vs))
    print(f"[eval] UB con bateria+bahia extra desde año {ANIO}: {F.ObjVal:,.2f}  "
          f"(cota {F.ObjBound:,.2f}; incumbente {UB0:,.2f}, diferencia {UB0 - F.ObjVal:,.2f})")
    for fam in ("R", "b_bar", "D", "n_battery_fleet", "N_batteries", "N_bays", "Z_repl"):
        filas = [(P.nombre[j], x[j]) for j in range(P.nv) if P.nombre[j].split("(")[0] == fam]
        print(fam, " ".join(f"{n.split('(')[1].rstrip(')')}:{v:.1f}" for n, v in filas))
    np.save(os.path.join(OUT, "x.npy"), x)
if F.Status == GRB.INFEASIBLE:
    F.Params.TimeLimit = 900
    F.computeIIS()
    filas = [c.ConstrName for c in F.getConstrs() if c.IISConstr]
    cols = [v.VarName for v in F.getVars() if v.IISLB or v.IISUB]
    print(f"[eval] IIS: {len(filas)} filas, {len(cols)} cotas", flush=True)
    print("[eval] IIS filas:", filas[:60], flush=True)
    print("[eval] IIS cotas:", [(n, P.nombre[P.nombre.index(n)] if False else n) for n in cols[:60]], flush=True)
