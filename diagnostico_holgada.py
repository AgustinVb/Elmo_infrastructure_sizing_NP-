"""Incumbente HOLGADA para el L-shaped: primero factibilidad, despues costo.

El L-shaped y el Nested Benders arrancan del maestro relajado (o de la DP), que
dimensiona justo, y en DCH no cierran el año en que sube la produccion: los dias
de esa inversion no tienen operacion entera. Aca se parte desde arriba:

  Fase A  los dias MILP con TODO al maximo (todas las estaciones construidas
          desde el año 1, bahias, baterias, cargadores y modulos de subestacion
          al tope, P_pot = p_peak y bateria nueva, b_bar = b_max). Si un dia no
          tiene operacion asi, no la tiene con ninguna inversion: el problema
          esta en los datos (pausas, produccion, flota), no en el algoritmo.
  Fase B  con la energia real de esos dias, el maestro (theta = 0, la misma
          inversion fija) elige el calendario de reemplazos R mas barato que
          cumple la degradacion; esa inversion se evalua completa
          (LShaped.evaluar_inversion: dias MILP con b_bar corregido + pulido)
          y queda como UB factible, en JSON (usable como --solucion).

Despues: recortar_holgada.py baja la inversion paso a paso desde aca.

    python -u diagnostico_holgada.py --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/1C/RED \\
        --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \\
        --out output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d/1C/RED/holgada
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

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.decomposition.lshaped import LShaped               # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def maximos(model, p_ssee_max):
    """{estacion: {familia de copia: tope}} con los parametros del modelo (hoja stations);
    la subestacion, como en LShaped.preparar_maestro: P_SSEE_STEP * n_ssee_k <= p_ssee_max."""
    from pyomo.environ import value
    from src.optimization.functions import P_SSEE_STEP
    tope = {}
    for k in model.stations_set:
        bahias = int(value(model.max_bays_k[k]))
        tope[k] = {"X": 1, "N_bays": bahias,
                   "N_batteries": bahias * int(value(model.max_batteries_per_bay_k[k])),
                   "N_chargers": bahias * int(value(model.max_chargers_per_bay_k[k])),
                   "n_ssee_k": int(p_ssee_max // P_SSEE_STEP + 1e-9)}
    return tope


def familia(nombre):
    """'N_bays(station_1_4)' -> ('N_bays', 'station_1', 4); 'n_ssee_k(station_1)' ->
    ('n_ssee_k', 'station_1', None); 'P_pot(4)' -> ('P_pot', None, 4)."""
    if "(" not in nombre:                      # H, sin indice
        return nombre, None, None
    f, resto = nombre.split("(", 1)
    resto = resto.rstrip(")")
    if f == "n_ssee_k":
        return f, resto, None
    if "_" in resto and resto.rsplit("_", 1)[1].isdigit() and not resto.isdigit():
        est, y = resto.rsplit("_", 1)
        return f, est, int(y)
    return f, None, int(resto) if resto.isdigit() else resto


def holgada(P, tope, p_peak, b_max):
    """Vector sobre todas las columnas con la inversion maxima en las copias y
    en las enteras de capacidad del maestro (Delta_* y R quedan en 0)."""
    x = np.zeros(P.nv)
    for j in P.maestro_vars:
        f, est, _y = familia(P.nombre[j])
        if est is not None and est in tope and f in tope[est]:
            x[j] = tope[est][f]
        elif f == "P_pot":
            x[j] = p_peak
        elif f == "b_bar":
            x[j] = b_max
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, default=11)
    ap.add_argument("--days_per_year", type=int, choices=[1, 2, 4], default=2)
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp2")
    ap.add_argument("--pause_scheme", choices=["det", "dch"], default="dch")
    ap.add_argument("--t", type=float, default=1800.0, help="tope por MILP del dia (s)")
    ap.add_argument("--gap", type=float, default=0.01)
    ap.add_argument("--jobs", type=int, default=11, help="dias resueltos a la vez")
    ap.add_argument("--cpu", type=int, default=None)
    ap.add_argument("--p_peak", type=float, default=None, help="P_pot de la Fase A (por defecto p_peak_dist)")
    ap.add_argument("--p_ssee_max", type=float, default=1500.0, help="tope de la subestacion (kW)")
    ap.add_argument("--solo_A", action="store_true", help="solo la factibilidad de los dias")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    def log(s):
        print(f"[HOLG] {s} ({time.time() - t0:.0f}s)", flush=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                   consumption_model=a.consumption_model, wp2_consumption_json=None,
                   n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True, pause_scheme=a.pause_scheme)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, om.model, por_var, M=1e6, jobs=a.jobs, alpha=0.5,
                     cotas_capacidad={}, out=a.out, cpu=a.cpu)
    P = bd.P
    from pyomo.environ import value
    tope = maximos(om.model, a.p_ssee_max)
    p_peak = a.p_peak if a.p_peak is not None else float(value(om.model.p_peak))
    j_bb = [j for j in P.maestro_vars if P.nombre[j].startswith("b_bar(")]
    b_max = max(P.ub[j] for j in j_bb)
    log(f"modelo armado: {P.nb} dias; tope por estacion {tope}; P_pot {p_peak:.0f} kW; b_bar {b_max:.1f} kWh")

    # ---------------------------------------------------------------- Fase A
    x = holgada(P, tope, p_peak, b_max)
    cache = os.path.join(a.out, "fase_A.npz")
    if os.path.exists(cache):
        # Fase A de una corrida anterior con el mismo modelo: no se vuelve a resolver.
        z = np.load(cache, allow_pickle=True)
        res = list(z["res"])
        for s, r in zip(bd.subs, res):
            if r["ok"]:
                s.start = [round(r["x"][k]) for k in s.ent_loc]
        log(f"Fase A leida de {cache}")
    else:
        res = bd._paralelo(lambda b: bd.subs[b].ub(x, a.t, a.gap))
        np.savez(cache, res=np.array(res, dtype=object))
    filas = []
    for b, r in enumerate(res):
        filas.append({"dia": P.claves[b], "ok": r["ok"], "status": r.get("status", 2),
                      "t": round(r["t"], 1), "valor": r.get("valor"), "gap": r.get("gap")})
        log(f"A {P.claves[b]:>10s}: " + (f"ok {r['valor']:,.2f} gap {r['gap']:.2%} en {r['t']:.0f}s"
                                         if r["ok"] else f"SIN OPERACION status {r['status']} en {r['t']:.0f}s"))
    malos = [f["dia"] for f in filas if not f["ok"]]
    resumen = {"fase_A": filas, "dias_sin_operacion": malos, "tope": tope, "p_pot": p_peak,
               "b_bar": b_max, "args": vars(a)}

    def guardar():
        resumen["tiempo_s"] = time.time() - t0
        with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
            json.dump(resumen, f, indent=2, default=str)

    if malos:
        log(f"FINAL A: {len(malos)} dias SIN operacion aun con todo al maximo: {malos}")
        guardar()
        return 0
    log("FINAL A: todos los dias tienen operacion con la inversion maxima")
    if a.solo_A:
        guardar()
        return 0

    # ---------------------------------------------------------------- Fase B
    vec = x.copy()
    for b, r in enumerate(res):
        vec[bd.subs[b].loc] = r["x"]
    M = bd.maestro
    M._entero(True)
    m = M.m
    guard = []
    for j in P.maestro_vars:
        f, est, _y = familia(P.nombre[j])
        if est is not None and est in tope and f in tope[est]:
            v = M.x[M.pos[j]]
            guard.append((v, v.LB, v.UB))
            v.LB = v.UB = tope[est][f]
        elif f == "P_pot":
            # P_pot queda libre: la paga el maestro y el pulido la vuelve a optimizar.
            pass
    for k in P.pres_energia:
        v = M.e[k]
        guard.append((v, v.LB, v.UB))
        v.LB = v.UB = -abs(float(P.energia(vec, k)))
    for v in M.th:
        guard.append((v, v.LB, v.UB))
        v.LB = v.UB = 0.0
    m.update()
    m.Params.TimeLimit = 600
    m.optimize()
    if m.SolCount == 0:
        log(f"FINAL B: el maestro no encuentra reemplazos para la energia real (status {m.Status})")
        resumen["fase_B"] = {"status_maestro": m.Status}
        guardar()
        return 0
    x1, _e, _th = M.punto()
    inv_maestro = m.ObjVal
    for v, lo, hi in guard:
        v.LB, v.UB = lo, hi
    m.update()
    R = {P.nombre[j]: round(x1[j]) for j in P.maestro_vars if P.nombre[j].startswith("R(")}
    log("B reemplazos: " + (", ".join(n for n, v in R.items() if v) or "ninguno")
        + f"; inversion del maestro {inv_maestro:,.2f}")
    # P_pot de la Fase A (p_peak): los dias ya se resolvieron con ella.
    for j in P.maestro_vars:
        if P.nombre[j].startswith("P_pot("):
            x1[j] = p_peak
    ls = LShaped(bd, om, por_var, out=a.out)
    costo, vec1 = ls.evaluar_inversion(x1, a.t, a.gap, reparar=False)
    resumen["fase_B"] = {"reemplazos": R, "inversion_maestro": inv_maestro, "ub": costo}
    if costo is None:
        log("FINAL B: la inversion holgada no pasa el pulido")
        guardar()
        return 0
    from src.io.printer import Printer
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        if vd is not None and not vd.fixed:
            vd.set_value(round(bd.mejor_vec[j]) if not vd.is_continuous() else bd.mejor_vec[j],
                         skip_validation=True)
    destino = os.path.join(a.out, "solucion")
    Printer(om, destino, ts, ms).write_variables_jsons()
    resumen["fase_B"]["solucion"] = destino
    log(f"FINAL B: UB holgada {costo:,.2f}; JSON en {destino}")
    guardar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
