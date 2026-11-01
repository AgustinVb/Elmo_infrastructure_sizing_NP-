"""Diagnostico: ¿alcanza con N cargadores por estacion en un año?

Parte del incumbente (--solucion), cambia N_chargers(k, año) a --cargadores y
resuelve los dias MILP de ese año (Subproblema.ub: copias fijas, presupuestos
LIBRES) en tres escenarios, de mas a menos restringido:

  A  b_bar y P_pot del incumbente
  B  P_pot = p_peak (sin tope de cobro por potencia; solo pesa en los dias 91-244)
  C  P_pot = p_peak y b_bar(año) = b_max (bateria nueva)

y el piso de b_bar (Subproblema.piso, cota dual) con las demas continuas libres.
Si C no tiene operacion, ese numero de cargadores no alcanza con ninguna
bateria; si C si y A no, la causa es la capacidad (degradacion) o P_pot.

    python -u diagnostico_año.py --data_folder data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo/2C/P_red \\
        --free_charging --pause_scheme dch --n_years 11 --consumption_model wp2 \\
        --solucion <incumbente> --año 7 --cargadores 1,1,1 --t 1800 --out <carpeta>
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
    ap.add_argument("--pause_scheme", choices=["det", "dch"], default="det")
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del incumbente")
    ap.add_argument("--año", type=int, default=7)
    ap.add_argument("--cargadores", default="1,1,1", help="N_chargers por estacion en --año")
    ap.add_argument("--t", type=float, default=1800.0, help="tope por MILP del dia")
    ap.add_argument("--gap", type=float, default=0.01)
    ap.add_argument("--escenarios", default="A,B,C,piso")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cpu", type=int, default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model=a.model, series="time_series.xlsx",
                   consumption_model=a.consumption_model,
                   wp2_consumption_json=a.wp2_consumption_json, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=a.free_charging,
                  free_maintenance=a.free_maintenance, pause_scheme=a.pause_scheme)
    p_peak = float(value(om.model.p_peak))
    info, por_var = particion.export(om.model, os.path.join(a.out, "_mps"), mode="year_day")
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, cpu=a.cpu)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    col = {n: j for j, n in enumerate(P.nombre)}
    y = a.año
    nuevos = [int(c) for c in a.cargadores.split(",")]
    estaciones = sorted({n[len("N_chargers("):-1].rsplit("_", 1)[0] for n in col
                         if n.startswith("N_chargers(")})
    x = vec0.copy()
    print(f"[DG] incumbente año {y}: " + ", ".join(
        f"{n} {vec0[col[n]]:g}" for n in [f"N_chargers({k}_{y})" for k in estaciones]
        + [f"X({k}_{y})" for k in estaciones] + [f"b_bar({y})", f"P_pot({y})"] if n in col)
        + f"; b_max {P.ub[col[f'b_bar({y})']]:g}, p_peak {p_peak:g}", flush=True)
    for k, nv in zip(estaciones, nuevos):
        x[col[f"N_chargers({k}_{y})"]] = nv
    print(f"[DG] prueba: N_chargers año {y} = {dict(zip(estaciones, nuevos))}", flush=True)

    dias = [b for b in range(P.nb) if eval(P.claves[b])[0] == y]
    jb, jp = col[f"b_bar({y})"], col[f"P_pot({y})"]
    xs = {"A": x}
    xs["B"] = x.copy()
    xs["B"][jp] = p_peak
    xs["C"] = xs["B"].copy()
    xs["C"][jb] = P.ub[jb]

    def rango(j):
        lo, hi = float(P.lb[j]), float(P.ub[j])
        return lo, (min(hi, p_peak) if P.nombre[j].startswith("P_pot(") else hi)

    def tarea(b):
        s = bd.subs[b]
        filas = []
        for esc in a.escenarios.split(","):
            s.start = [round(vec0[s.loc[k]]) for k in s.ent_loc]
            if esc == "piso":
                kb = list(s.cop).index(jb)
                r = s.piso(x, kb, [rango(s.cop[k]) for k in s.cop_cont], a.t)
                txt = f"piso b_bar {r['phi']:.2f}" if r["ok"] else "piso sin cota"
            else:
                r = s.ub(xs[esc], a.t, a.gap)
                txt = f"costo {r['valor']:,.2f} gap {r['gap']:.2%}" if r["ok"] else "sin operacion"
            fila = {"dia": P.claves[b], "esc": esc, "ok": r["ok"], "status": r.get("status"),
                    "t": r["t"], "valor": r.get("valor"), "phi": r.get("phi")}
            print(f"[DG] {P.claves[b]} {esc:4s} status {r.get('status', 2)} {r['t']:.0f}s: {txt}",
                  flush=True)
            filas.append(fila)
        return filas
    print(f"[DG] {len(dias)} dias, escenarios {a.escenarios}, tope {a.t:.0f}s ({time.time() - t0:.0f}s armando)",
          flush=True)
    res = [f for filas in bd._paralelo(tarea, dias) for f in filas]
    with open(os.path.join(a.out, "diagnostico.json"), "w", encoding="utf-8") as f:
        json.dump({"args": vars(a), "resultados": res}, f, indent=1, default=str)
    print(f"[DG] listo en {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
