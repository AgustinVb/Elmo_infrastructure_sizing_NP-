"""Capacidad minima PROBADA por estacion y año: cortes globales para el maestro.

La relajacion del L-shaped compra muy pocas baterias y cargadores (DCH 1C, 7-nov:
el 80 % del gap son baterias), porque la operacion relajada reparte las cargas en
fracciones. Este script prueba, macrobloque por macrobloque (año, dia, estacion;
BendersDias por_estacion), que con N[k,y] = n de una familia NO hay operacion
entera, con TODO lo demas libre:

  - las otras copias de la estacion (bahias, cargadores, baterias, X, modulos) en
    [0, tope] (las filas de capacidad del maestro, diagnostico_holgada.maximos) y
    b_bar en [0, b_max_pool] (vale para cualquier degradacion);
  - la potencia de la estacion libre (presupuestos de power_balance) y las filas
    RELAJAR fuera (peak_power_swap, min_charges_cut);
  - la produccion de la estacion >= su piso VALIDO: la meta del dia menos lo mas
    que pueden producir las otras estaciones (LP de su macrobloque, maximo de su
    presupuesto de daily_extraction con todo libre).

Todo eso relaja el problema completo, asi que si el MILP termina INFEASIBLE el
corte N[k,y] >= n + 1 vale para todo diseño. Se sube n desde 0 hasta el primero
factible (o hasta que un MILP no cierre: el corte es el ultimo n probado + 1).
El minimo de la estacion en el año es el maximo entre sus dias; LShaped lo pone
como cota inferior de la columna del maestro (lshaped.py --cortes_capacidad).

    python -u cortes_capacidad.py --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/1C/RED \\
        --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \\
        --out output/.../1C/RED/cortes_capacidad.json
"""
import argparse
import json
import math
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

from diagnostico_holgada import familia, maximos                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias, INF  # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

FAMILIAS = ("N_batteries", "N_chargers", "N_bays")


def cotas_copias(P, s, tope, b_max):
    """Rangos validos de las copias del macrobloque: [0, tope] en las de la estacion
    (las del modelo si son mas chicas) y b_bar en [0, b_max_pool]."""
    lbs, ubs = [], []
    for j in s.cop:
        f, k, _y = familia(P.nombre[j])
        lo, hi = float(P.lb[j]), float(P.ub[j])
        if k in tope and f in tope[k]:
            hi = min(hi, float(tope[k][f]))
        elif f == "b_bar":
            hi = min(hi, b_max)
        lbs.append(max(lo, 0.0) if f != "b_bar" else lo)
        ubs.append(hi)
    return lbs, ubs


def prod_maxima(s, k_prod, lbs, ubs):
    """Lo mas que produce el macrobloque (LP, todo libre): cota del presupuesto
    de daily_extraction."""
    m = s.m
    s._entera(False)
    s._enlaces(0.0, INF, 0.0, INF)
    s._copias(None, lbs, ubs)
    m.setAttr("Obj", s.vloc, [0.0] * len(s.vloc))
    e = s.vpres[s.pres.index(k_prod)]
    e.Obj = -1.0
    m.Params.TimeLimit = INF
    m.optimize()
    v = -m.ObjVal if m.Status == GRB.OPTIMAL else INF
    e.Obj = 0.0
    m.setAttr("Obj", s.vloc, s.obj_base.tolist())
    s._copias()
    return v


def factible(s, lbs, ubs, timelimit):
    """MILP de factibilidad del macrobloque con las copias en [lbs, ubs] y el piso de
    produccion de P.piso. Devuelve el status de Gurobi."""
    m = s.m
    s._entera(True)
    s._enlaces(0.0, INF, 0.0, INF)
    s._copias(None, lbs, ubs)
    s._pisos()
    m.setAttr("Obj", s.vloc, [0.0] * len(s.vloc))
    m.Params.TimeLimit = timelimit
    m.Params.MIPGap = 1.0
    m.Params.SolutionLimit = 1
    try:
        m.optimize()
        st = m.Status
        if st == GRB.SOLUTION_LIMIT or (st == GRB.TIME_LIMIT and m.SolCount > 0):
            st = GRB.OPTIMAL
    finally:
        m.Params.SolutionLimit = 2000000000
        m.Params.MIPGap = 1e-4
        m.Params.MIPFocus = 0
        m.setAttr("Obj", s.vloc, s.obj_base.tolist())
        s._copias()
    return st


def prod_entera(s, k_prod, lbs, ubs, timelimit, gap=1e-3, foco=0):
    """MILP: lo mas que produce el macrobloque con las copias en [lbs, ubs] (sin piso).
    Devuelve (ObjBound, ObjVal, status): ObjBound acota la produccion aunque no cierre.
    foco: MIPFocus de Gurobi (3 = mover la cota)."""
    m = s.m
    s._entera(True)
    s._enlaces(0.0, INF, 0.0, INF)
    s._copias(None, lbs, ubs)
    m.setAttr("Obj", s.vloc, [0.0] * len(s.vloc))
    e = s.vpres[s.pres.index(k_prod)]
    e.Obj = -1.0
    m.Params.TimeLimit = timelimit
    m.Params.MIPGap = gap
    m.Params.MIPFocus = foco
    try:
        m.optimize()
        st = m.Status
        if st == GRB.INF_OR_UNBD:
            m.Params.DualReductions = 0
            m.optimize()
            m.Params.DualReductions = 1
            st = m.Status
        if st == GRB.INFEASIBLE:
            return -INF, -INF, st
        return -m.ObjBound, (-m.ObjVal if m.SolCount else None), st
    finally:
        e.Obj = 0.0
        m.Params.MIPGap = 1e-4
        m.Params.MIPFocus = 0
        m.setAttr("Obj", s.vloc, s.obj_base.tolist())
        s._copias()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, default=11)
    ap.add_argument("--days_per_year", type=int, default=2)
    ap.add_argument("--consumption_model", default="wp2")
    ap.add_argument("--pause_scheme", default="dch")
    ap.add_argument("--familias", default=",".join(FAMILIAS))
    ap.add_argument("--solo", default="",
                    help="pruebas puntuales 'familia:estacion:año:n,...' (n = valor a probar)")
    ap.add_argument("--produccion", default="",
                    help="tabla de produccion maxima P_k(n) por macrobloque: 'familia:n1-n2' "
                         "(p. ej. N_batteries:0-3); solo con --anios")
    ap.add_argument("--anios", default="", help="años de la tabla de produccion (coma)")
    ap.add_argument("--niveles_bbar", default="",
                    help="topes de b_bar[y] para la tabla de produccion (coma; vacio = b_max_pool)")
    ap.add_argument("--t", type=float, default=1200.0, help="tiempo por MILP de factibilidad (s)")
    ap.add_argument("--foco", type=int, default=0, help="MIPFocus de los MILP de produccion (3 = cota)")
    ap.add_argument("--p_ssee_max", type=float, default=1500.0)
    ap.add_argument("--jobs", type=int, default=20)
    ap.add_argument("--cpu", type=int, default=60)
    ap.add_argument("--out", required=True, help="JSON de salida")
    a = ap.parse_args()
    t0 = time.time()

    def log(s):
        print(f"[CAP] {s} ({time.time() - t0:.0f}s)", flush=True)

    carpeta = os.path.join(os.path.dirname(a.out) or ".", "_cortes_capacidad")
    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                   consumption_model=a.consumption_model, wp2_consumption_json=None,
                   n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(carpeta, "_monolitico"), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True, pause_scheme=a.pause_scheme)
    info, por_var = gb.export(om.model, os.path.join(carpeta, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, om.model, por_var, M=1e6, jobs=a.jobs, alpha=0.5, cotas_capacidad={},
                     out=carpeta, cpu=a.cpu, por_estacion=True, acotar=False)
    P = bd.P
    tope = maximos(om.model, a.p_ssee_max)
    b_max = float(value(om.model.b_max_pool))
    fams = [f for f in a.familias.split(",") if f]

    est = {b: eval(c) for b, c in enumerate(P.claves)}
    est_b = [b for b, c in est.items() if c[2] != P.RED]
    rangos = {b: cotas_copias(P, bd.subs[b], tope, b_max) for b in est_b}
    for b0 in (est_b[0], next(b for b in est_b if est[b][0] == 3)):
        log(f"{len(est_b)} macrobloques de estacion; copias de {P.claves[b0]}: "
            + ", ".join(f"{P.nombre[j]} [{lo:g},{hi:g}]" for j, lo, hi in zip(bd.subs[b0].cop, *rangos[b0]))
            + "; presupuestos " + ", ".join(sorted({P.pres_tipo[k] for k in bd.subs[b0].pres})))

    # Piso valido de produccion: meta del dia - lo mas que producen las demas.
    # (Una estacion sin LHD activos en el año no tiene produccion: sin presupuesto.)
    k_prod = {b: k for b in est_b for k in bd.subs[b].pres if P.pres_tipo[k] == "daily_extraction"}
    pmax = dict(zip(k_prod, bd._paralelo(lambda b: prod_maxima(bd.subs[b], k_prod[b], *rangos[b]),
                                         list(k_prod))))
    piso = {}
    for b in k_prod:
        y, d, k = est[b]
        i, _ = P.presupuestos[k_prod[b]]
        otras = sum(pmax[b2] for b2 in k_prod if est[b2][:2] == (y, d) and b2 != b)
        piso[b] = max(0.0, float(P.rhs[i]) - otras)
        P.piso[k_prod[b]] = piso[b]
    log("piso valido / natural de produccion (dia 196): " + "; ".join(
        f"{est[b][0]}-{est[b][2][-1]}: {piso[b]:,.0f}/{P.piso_natural[k_prod[b]]:,.0f}"
        for b in k_prod if est[b][1] == 196))

    def col(b, f):
        y, _d, k = est[b]
        return next((kk for kk, j in enumerate(bd.subs[b].cop)
                     if familia(P.nombre[bd.subs[b].cop[kk]]) == (f, k, y)), None)

    def probar(b, f, n):
        kk = col(b, f)
        if kk is None:
            return None
        lbs, ubs = list(rangos[b][0]), list(rangos[b][1])
        if n > ubs[kk]:
            return GRB.OPTIMAL
        lbs[kk] = ubs[kk] = float(n)
        t = time.time()
        st = factible(bd.subs[b], lbs, ubs, a.t)
        log(f"  {P.claves[b]} {f}={n}: "
            + {GRB.INFEASIBLE: "INFACTIBLE", GRB.OPTIMAL: "factible"}.get(st, f"status {st}")
            + f" en {time.time() - t:.0f}s")
        return st

    salida = {"data_folder": a.data_folder, "t": a.t, "piso": {P.claves[b]: piso[b] for b in k_prod}}
    if a.produccion:
        f, rng = a.produccion.split(":")
        n1, n2 = (int(x) for x in rng.split("-"))
        anios = {int(x) for x in a.anios.split(",") if x}
        bloques = [b for b in k_prod if not anios or est[b][0] in anios]

        niveles = [float(c) for c in a.niveles_bbar.split(",") if c] or [b_max]

        def tarea_p(b):
            """Los (n, c) de un bloque en serie: el modelo del bloque es uno solo. Con
            b_bar <= c de mayor a menor: la produccion baja con c y con n."""
            out = {}
            kk = col(b, f)
            kb = next((i for i, j in enumerate(bd.subs[b].cop) if P.nombre[j].startswith("b_bar(")), None)
            for n in range(n1, n2 + 1):
                for c in niveles:
                    lbs, ubs = list(rangos[b][0]), list(rangos[b][1])
                    if kk is not None:
                        lbs[kk] = ubs[kk] = float(n)
                    if kb is not None:
                        ubs[kb] = min(ubs[kb], c)
                        if ubs[kb] < lbs[kb]:
                            out[(b, n, c)] = (-INF, -INF, GRB.INFEASIBLE)
                            continue
                    t1 = time.time()
                    out[(b, n, c)] = cota, val, st = prod_entera(bd.subs[b], k_prod[b], lbs, ubs, a.t, foco=a.foco)
                    log(f"  {P.claves[b]} {f}={n} b_bar<={c:g}: produccion <= {cota:.2f} (sol {val}, "
                        f"status {st}, {time.time() - t1:.0f}s)")
            return out
        res = {}
        for r in bd._paralelo(tarea_p, bloques):
            res.update(r)
        tabla = {}
        for (b, n, c), (cota, val, st) in res.items():
            tabla.setdefault(P.claves[b], {})[f"{n}|{c:g}"] = {"cota": cota, "sol": val, "status": st}
        salida["produccion"] = {"familia": f, "n": [n1, n2], "niveles": niveles,
                                "pmax_lp": {P.claves[b]: pmax[b] for b in k_prod}, "tabla": tabla}
        for (y, d) in sorted({est[b][:2] for b in k_prod}):
            bs = [b for b in k_prod if est[b][:2] == (y, d)]
            if anios and y not in anios:
                continue
            i, _ = P.presupuestos[k_prod[bs[0]]]
            meta = float(P.rhs[i])
            for c in niveles:
                log(f"(año {y}, dia {d}, b_bar<={c:g}) meta {meta:.2f}; cota por estacion y n: "
                    + "; ".join(f"{est[b][2][-1]}: " + " ".join(f"{max(res[(b, n, c)][0], -1):.2f}"
                                                                 for n in range(n1, n2 + 1)) for b in bs))
    elif a.solo:
        pruebas = []
        for p in a.solo.split(","):
            f, k, y, n = p.split(":")
            for b in est_b:
                if est[b][0] == int(y) and est[b][2] == k:
                    pruebas.append((b, f, int(n)))
        res = bd._paralelo(lambda t: probar(*t), pruebas)
        salida["pruebas"] = [{"bloque": P.claves[b], "familia": f, "n": n, "status": st}
                             for (b, f, n), st in zip(pruebas, res)]
    else:
        def tarea(b):
            """{familia: (n minimo probado, cerrado)}: n sube desde 0 mientras es infactible."""
            out = {}
            for f in fams:
                n = 0
                while True:
                    st = probar(b, f, n)
                    if st is None:
                        break
                    if st == GRB.INFEASIBLE:
                        n += 1
                        continue
                    out[f] = (n, st == GRB.OPTIMAL)
                    break
            return out
        res = dict(zip(est_b, bd._paralelo(tarea, est_b)))
        cortes = {}
        for b, r in res.items():
            y, _d, k = est[b]
            for f, (n, _ok) in r.items():
                nombre = f"{f}({k}_{y})"
                cortes[nombre] = max(cortes.get(nombre, 0), n)
        salida["por_bloque"] = {P.claves[b]: r for b, r in res.items()}
        salida["cortes"] = dict(sorted(cortes.items()))
        for f in fams:
            for k in sorted(tope):
                log(f"{f} {k}: " + " ".join(f"{y}:{cortes.get(f'{f}({k}_{y})', '-')}"
                                            for y in sorted({e[0] for e in est.values()})))
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(salida, fh, indent=2)
    log(f"listo -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
