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
BB = {8: 407.2, 9: 395.5, 10: 389.5}
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

ent = [j for j, t in enumerate(P.vtype) if t != "C"]
idx = {n: j for j, n in enumerate(P.nombre)}
x = np.load(os.path.join(OUT, "x.npy"))
# sin reemplazo: R = 0, Z_repl = 0 y b_bar de los años 8-10 degradado (simulado)
for y in range(2, 11):
    x[idx[f"R({y})"]] = 0.0
for y in range(1, 11):
    x[idx[f"Z_repl({y})"]] = 0.0
for y, b in BB.items():
    x[idx[f"b_bar({y})"]] = b
bloques = [b for b, k in enumerate(P.claves) if eval(k)[0] in BB]
print(f"[eval2] reoptimizando {len(bloques)} dias de los años {sorted(BB)} con b_bar degradado", flush=True)
res = bd._paralelo(lambda b: bd.subs[b].ub(x, 900, 1e-3), bloques)
for b, r in zip(bloques, res):
    print(f"[eval2] {P.claves[b]}: ok={r['ok']}" + (f" valor {r['valor']:,.2f} gap {r['gap']:.2%}" if r["ok"] else ""), flush=True)
    if r["ok"]:
        x[bd.subs[b].loc] = r["x"]
if not all(r["ok"] for r in res):
    print("[eval2] algun dia sin operacion factible con baterias degradadas", flush=True)
    sys.exit(0)

LIBRES = ("u_nf", "z_bb", "N_batteries", "N_bays", "N_chargers", "n_battery_fleet",
          "N_batteries_total", "n_ssee_k", "G", "H", "X", "Z_repl", "S", "Sv", "X_dch", "X_ini", "W")
vs = F.getVars()
for j in ent:
    fam = P.nombre[j].split("(")[0]
    if fam in LIBRES:
        vd = por_var.get(P.nombre[j])
        lb, ub = (vd.lb, vd.ub) if vd is not None else (0, GRB.INFINITY)
        vs[j].LB = 0 if lb is None else lb
        vs[j].UB = GRB.INFINITY if ub is None else ub
        vs[j].Start = round(x[j])
    else:
        vs[j].LB = vs[j].UB = float(round(x[j]))
F.Params.TimeLimit = 1800
F.Params.MIPGap = 1e-4
F.optimize()
print(f"[eval2] status {F.Status}  SolCount {F.SolCount}", flush=True)
if F.SolCount:
    xs = np.array(F.getAttr("X", vs))
    print(f"[eval2] UB sin reemplazo (bateria+bahia+cargador desde año 3): {F.ObjVal:,.2f}  "
          f"(cota {F.ObjBound:,.2f}; incumbente Benders {UB0:,.2f}, diferencia {UB0 - F.ObjVal:,.2f})")
    for fam in ("R", "b_bar", "D", "n_battery_fleet", "N_batteries", "Z_repl"):
        filas = [(P.nombre[j], xs[j]) for j in range(P.nv) if P.nombre[j].split("(")[0] == fam]
        print(fam, " ".join(f"{n.split('(')[1].rstrip(')')}:{v:.1f}" for n, v in filas))
    np.save(os.path.join(OUT, "x_sin_reemplazo.npy"), xs)
elif F.Status == GRB.INFEASIBLE:
    F.computeIIS()
    print("[eval2] IIS filas:", [c.ConstrName for c in F.getConstrs() if c.IISConstr][:40])
    print("[eval2] IIS cotas:", [v.VarName for v in F.getVars() if v.IISLB or v.IISUB][:40])
