"""Fase 2 de implementacion_dp_estacion_swap.md: valida la DP de la estacion
(dp_estacion.py) contra el bloque de estacion del MILP.

Para cada (año, dia) pedido, con un incumbente cargado:
  MILP  el dia de BendersDias con la inversion fija (copias del maestro) y
        ademas Y, Z y Z_swap fijos en el incumbente; libres solo la estacion
        (S, Sv, X_ini, X_dch, W) y la potencia (P_red, P_gen, Curt_g, BESS).
  DP    dp_evaluar con W[t] = sum_i Z_swap del incumbente, N = N_batteries,
        N_C = N_chargers, L = t_charge y el costo de energia por arco
        coef(P_red)_t * max(0, p_charger*Q - A_t), A_t = sum_g G_g p_max_g alpha.
        Topes de Q por t: cargadores, subestacion (500*n_ssee/p_charger), pico
        (n_peak_max y P_red <= p_peak) y, en dias de punta, P_red <= P_pot.
Se comparan la factibilidad, el costo de energia (exacto sin BESS; con BESS la
DP no modela la bateria estacionaria y solo se compara factibilidad y Q) y los
perfiles Q_t.

Con --pipeline_ciclico (ELMO_PIPELINE_CICLICO=1) el MILP se arma con el pipeline
de carga ciclico y se compara con la DP en ciclo="completo"; sin el, con
ciclo="modelo" (pipeline vacio al inicio y nada cargando en tf).

    python -u validar_dp_estacion.py \
        --data_folder data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte/05C/RED_GEN \
        --respaldo output/.../RED_GEN/benders/incumbente_respaldo_k2.pkl \
        --dias "4,196;5,288;7,105" --out output/.../validacion_dp_estacion
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

from pyomo.environ import value                                          # noqa: E402

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from dp_estacion import dp_evaluar                                       # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

EPS = 1e-6
P_SSEE_STEP = 500.0      # kW por modulo de subestacion (functions.P_SSEE_STEP)


def adaptar_incumbente_ciclico(m):
    """Lleva al pipeline ciclico una solucion calculada SIN el (cargada en m).

    Sin ciclo, X_dch[t0] es el inventario de descargadas al INICIO de t0 y las
    cargas de t0 salen de el; con ciclo es el inventario al FINAL de t0. Como
    sin ciclo no queda nada cargando en tf y los swaps de t0 no entran a la
    estacion, basta X_dch[t0] -= X_ini[t0] (los Sv que cruzan el borde quedan
    en 0) para que la solucion sea factible en el modelo ciclico, con el mismo
    costo: el modelo ciclico es una relajacion del otro. Si hay swaps en t0 la
    traduccion no es exacta (con ciclo si gastan una bateria) y se avisa."""
    t0 = min(m.time_intervals_set)
    n, con_swap_t0 = 0, 0
    for k in m.stations_set:
        for y in m.years:
            for d in m.days:
                xi = value(m.X_ini[k, y, d, t0], exception=False) or 0.0
                xd = value(m.X_dch[k, y, d, t0], exception=False)
                if xd is None:
                    continue
                if (value(m.W[k, y, d, t0], exception=False) or 0.0) > 0.5:
                    con_swap_t0 += 1
                if xi > 0.5:
                    m.X_dch[k, y, d, t0].set_value(round(xd - xi), skip_validation=True)
                    n += 1
    print(f"[val] incumbente llevado al pipeline ciclico: X_dch[t0] -= X_ini[t0] en {n} dias"
          + (f"; OJO: {con_swap_t0} dias con swaps en t0 (traduccion no exacta)"
             if con_swap_t0 else ""), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--respaldo", default=None, help="incumbente_respaldo.pkl de Nested Benders")
    ap.add_argument("--solucion", default=None, help="carpeta con los JSON (alternativa)")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--dias", default="4,196;5,288;7,105", help="(año,dia) separados por ;")
    ap.add_argument("--timelimit", type=float, default=600)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pipeline_ciclico", action="store_true",
                    help="MILP con ELMO_PIPELINE_CICLICO=1; se compara con la DP ciclo=completo")
    a = ap.parse_args()
    if a.pipeline_ciclico:
        os.environ["ELMO_PIPELINE_CICLICO"] = "1"
    ciclo_ref = "completo" if a.pipeline_ciclico else "modelo"
    if (a.respaldo is None) == (a.solucion is None):
        ap.error("se necesita exactamente uno de --respaldo o --solucion")
    os.makedirs(a.out, exist_ok=True)
    t_ini = time.time()

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                   consumption_model="wp1", wp2_consumption_json=None, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"),
                  init_solution_folder=a.solucion, mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    if a.respaldo:
        cargar_respaldo(om, ms, ts, a.respaldo)
    m = om.model
    if a.pipeline_ciclico:
        adaptar_incumbente_ciclico(m)
    info, por_var = gb.export(m, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, m, por_var, jobs=a.jobs, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    try:
        costo, xB, _eB = bd.cargar_incumbente(vec0)
        print(f"[val] incumbente pulido {costo:,.2f}  ({time.time() - t_ini:.0f}s)", flush=True)
    except RuntimeError:
        # Incumbente calculado con otra convencion de la estacion (p.ej. sin
        # pipeline ciclico): las variables de la estacion no cuadran, pero la
        # validacion solo usa la inversion y Y/Z/Z_swap, que se fijan por dia.
        costo, xB = None, vec0
        print("[val] el incumbente no es factible al pulirlo con este modelo: se usan "
              "sus valores sin pulir (solo inversion y Y/Z/Z_swap)", flush=True)

    # nombre de columna -> (componente, indice)
    comp_idx = {}
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        if vd is not None:
            idx = vd.index()
            idx = idx if isinstance(idx, tuple) else (() if idx is None else (idx,))
            comp_idx[j] = (vd.parent_component().name, idx)
    por_comp = {}
    for j, (c, idx) in comp_idx.items():
        por_comp.setdefault(c, {})[idx] = j

    tiempos = [float(t) for t in ts.get_time_intervals()]
    T = len(tiempos)
    pos_t = {round(t): k for k, t in enumerate(tiempos)}
    p_c = float(value(m.p_charger))
    L = int(value(m.t_charge))
    n_peak = int(value(m.n_peak_max))
    p_peak = float(value(m.p_peak))
    estaciones = list(m.stations_set)
    if len(estaciones) != 1:
        raise SystemExit("la validacion supone una sola nave (modo arc_cost)")
    k = estaciones[0]
    gens = list(m.gen_set) if hasattr(m, "gen_set") else []

    def val(comp, idx, defecto=0.0):
        j = por_comp.get(comp, {}).get(tuple(idx))
        return float(xB[j]) if j is not None else defecto

    filas = []
    for par in a.dias.split(";"):
        y, d = (int(v) for v in par.split(","))
        clave = str((y, d))
        if clave not in P.claves:
            print(f"[val] ({y},{d}) no es un bloque del modelo; se salta")
            continue
        b = P.claves.index(clave)
        s = bd.subs[b]
        # Fija Y, Z, Z_swap del incumbente en el dia.
        fijadas = 0
        nombres_loc = {}
        for pos, j in enumerate(s.loc):
            c, idx = comp_idx.get(j, (None, None))
            nombres_loc[pos] = (c, idx)
            if c in ("Y", "Z", "Z_swap"):
                vv = float(round(xB[j]))
                s.vloc[pos].LB = vv
                s.vloc[pos].UB = vv
                fijadas += 1
        s.m.update()
        s.start = None
        r = s.ub(xB, a.timelimit, 1e-6, mip_focus=0)
        milp_ok = r.get("ok", False)
        Q_milp = np.zeros(T)
        X_milp = np.zeros(T)
        Pred = np.zeros(T)
        coef = np.zeros(T)
        for pos, j in enumerate(s.loc):
            c, idx = nombres_loc[pos]
            if c == "P_red":
                t = pos_t[round(idx[-1])]
                coef[t] = P.obj[j]
                if milp_ok:
                    Pred[t] = r["x"][pos]
            if not milp_ok:
                continue
            if c == "Sv":
                Q_milp[pos_t[round(idx[3])]] += r["x"][pos]
            elif c == "X_ini":
                X_milp[pos_t[round(idx[-1])]] += r["x"][pos]
        energia_milp = float(np.dot(coef, Pred)) if milp_ok else None

        # Entradas de la DP desde el incumbente.
        W = np.zeros(T, dtype=int)
        for idx, j in por_comp["Z_swap"].items():
            if idx[2] == y and idx[3] == d:
                W[pos_t[round(idx[4])]] += int(round(xB[j]))
        N = int(round(val("N_batteries", (k, y))))
        NC = int(round(val("N_chargers", (k, y))))
        n_ssee = int(round(val("n_ssee_k", (k,))))
        P_pot = val("P_pot", (y,))
        H = val("H", (), 0.0) if "H" in por_comp else 0.0
        A = np.zeros(T)
        for g in gens:
            G = val("G_g", (g,))
            pmax = float(value(m.p_max_g[g]))
            for t in tiempos:
                A[pos_t[round(t)]] += G * pmax * float(value(m.alpha_g[g, y, d, t]))
        # P_red <= P_pot solo en las horas de punta (power_peak_limit se
        # registra sobre time_intervals_peak_set) de los dias de punta (91-244).
        horas_punta = {round(float(t)) for t in m.time_intervals_peak_set}
        q_max = []
        for t in range(T):
            q = min(NC, math.floor(P_SSEE_STEP * n_ssee / p_c + EPS), n_peak,
                    math.floor((p_peak + A[t]) / p_c + EPS))
            if 91 <= d <= 244 and round(tiempos[t]) in horas_punta:
                q = min(q, math.floor((P_pot + A[t]) / p_c + EPS))
            q_max.append(max(q, 0))
        Qs = np.arange(NC + 1)[None, :]
        costo_arco = coef[:, None] * np.maximum(0.0, p_c * Qs - A[:, None])
        t_dp = time.time()
        # dp: la que corresponde al MILP armado (ciclo_ref); dp_otro: la otra
        # convencion del borde, como referencia.
        dp = dp_evaluar(W.tolist(), N=N, NC=NC, L=L, T=T, costo_arco=costo_arco, q_max=q_max,
                        ciclo=ciclo_ref)
        dp_otro = dp_evaluar(W.tolist(), N=N, NC=NC, L=L, T=T, costo_arco=costo_arco,
                             q_max=q_max, ciclo="modelo" if a.pipeline_ciclico else "completo")
        t_dp = time.time() - t_dp
        con_bess = H > 1e-6
        dif = (None if (energia_milp is None or not dp["factible"])
               else (dp["costo"] - energia_milp) / max(abs(energia_milp), 1e-9))
        fila = {"dia": [y, d], "N": N, "NC": NC, "L": L, "n_ssee": n_ssee, "P_pot": P_pot,
                "H": H, "swaps": int(W.sum()), "LHD_fijadas": fijadas,
                "milp_factible": milp_ok, "milp_status": r.get("status"), "milp_t": r.get("t"),
                "milp_gap": r.get("gap"), "energia_milp": energia_milp,
                "cargas_milp": float(X_milp.sum()) if milp_ok else None,
                "dp_factible": dp["factible"], "energia_dp": dp["costo"] if dp["factible"] else None,
                "cargas_dp": sum(dp["X_ini"]) if dp["factible"] else None,
                "ciclo_dp": ciclo_ref,
                "dp_otro_ciclo_factible": dp_otro["factible"],
                "energia_dp_otro_ciclo": dp_otro["costo"] if dp_otro["factible"] else None,
                "dif_rel": dif, "con_bess": con_bess, "dp_t": t_dp,
                "Q_iguales": (bool(np.allclose(Q_milp, dp["Q"])) if (milp_ok and dp["factible"])
                              else None),
                "Q_milp": Q_milp.tolist(), "Q_dp": dp["Q"] if dp["factible"] else None,
                "W": W.tolist(), "q_max": q_max}
        filas.append(fila)
        print(f"[val] ({y},{d}) N={N} NC={NC} L={L} swaps={int(W.sum())} | MILP "
              f"{'factible' if milp_ok else 'INFACTIBLE'} energia "
              f"{energia_milp if energia_milp is None else round(energia_milp, 2)} "
              f"({r.get('t', 0):.0f}s) | DP {'factible' if dp['factible'] else 'INFACTIBLE'} "
              f"energia {round(dp['costo'], 2) if dp['factible'] else None} ({t_dp:.1f}s) | "
              f"dif {dif if dif is None else f'{dif:.2e}'}{' [con BESS: no comparable]' if con_bess else ''}",
              flush=True)

    with open(os.path.join(a.out, "validacion.json"), "w", encoding="utf-8") as fh:
        json.dump({"incumbente": costo, "pipeline_ciclico": a.pipeline_ciclico,
                   "filas": filas, "data_folder": a.data_folder,
                   "respaldo": a.respaldo, "solucion": a.solucion}, fh, indent=1)
    print(f"[val] listo en {time.time() - t_ini:.0f}s -> {a.out}/validacion.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
