#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redimensionamiento ex post de generacion, BESS y red con una carga fija: el
segundo ajuste de la comparacion swap vs on-board (el primero es el de energia,
ajuste_energia_swap.py).

POR QUE. El modelo de swap cobra cada recarga como el bloque completo bmin ->
100 % (~20-25 % mas energia que la real) y dimensiona la solar y la BESS para esa
demanda inflada (caso 2 v4: 1.004 kW de solar contra 876 de on-board). El ajuste
de energia descuenta el sobrante intervalo a intervalo pero deja la inversion
como esta y, en las horas de sol, recorta solar en vez de guardarla. Este LP
vuelve a elegir solar, eolica, BESS, red y potencia de punta para la carga REAL
de swap (dem_aj de ajuste_energia_swap), con la inversion en estaciones, la
subestacion y los reemplazos fijos.

QUE RESUELVE. Las mismas ecuaciones y costos del modelo (functions.py; swap y
on-board son identicos en esto):
  P_red + sum_g P_gen + P_bat = carga                     (power_balance)
  P_gen <= G_g * p_max_g * alpha_g                         (gen_limit, con curtailment)
  -p_max_h H <= P_bat <= p_max_h H,  a_min H <= A <= a_max H
  A[t] = A[t-1] - P_bat[t] / eta_h * dt,  A[0] = 0,  A[1] = A[T]
  P_red <= p_peak;  P_red <= P_pot[y] en los dias 91..244 e intervalos de punta
  costo = red (x scaling_factor_op_cost, VP) + punta (demand_charge_coef, VP)
        + inversion G_g y H neta del valor residual lineal + O&M (VP)
G_g y H son continuas, como en el modelo. El horario de la carga queda fijo.

CONTROL. Con la carga de la propia solucion (P_red + P_gen + P_bat), el LP tiene
que reproducir lo que pago el modelo en esos seis terminos: menos si la solucion
no era optima en su dimensionamiento, nunca mas (eso seria una diferencia de
formulacion). Medido en los 8 casos v4, swap y on-board: diferencia 0.

    python cost_comparisons/redimensionar_gen_bess.py <carpeta solucion swap>
"""
import json
import sys
from pathlib import Path

import gurobipy as gp
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ajuste_energia_swap import DIAS, _serie                    # noqa: E402

TERMINOS = ("grid_energy_cost", "peak_power_cost", "gen_inv_cost", "gen_op_cost",
            "bess_inv_cost", "bess_op_cost")


def _u(d):
    """Quita el nivel "_1" de los diccionarios de parameters.json."""
    return d["_1"] if isinstance(d, dict) and "_1" in d else d


def _por_intervalo(nodo, T):
    acc = np.zeros(T)
    for k, v in nodo.items():
        acc[int(float(k)) - 1] = float(v)
    return acc


def carga_de_solucion(leer, years, T):
    """Carga de la solucion (P_red + P_gen + P_bat) por (año, dia), en kW.
    leer(nombre) devuelve el JSON de la solucion, o {} si no existe."""
    red, gen, bat = leer("P_red.json"), leer("P_gen.json"), leer("P_bat.json")
    return {(y, d): (_serie(red, y, d, T)
                     + (_serie(gen, y, d, T, "g") if gen else 0.0)
                     + (_serie(bat, y, d, T, "h") if bat else 0.0))
            for y in years for d in DIAS}


def redimensionar(p, carga):
    """LP de generacion + BESS + red para la carga {(y, d): array kW}.

    p: parameters.json (dict). Devuelve los seis TERMINOS en USD (VP), su "suma",
    la potencia instalada por tecnologia "G_kw" y las unidades de BESS "H"."""
    T = len(next(iter(carga.values())))
    dt, esc = float(p["delta_t"]), float(p["scaling_factor_op_cost"])
    years = sorted(int(y) for y in p["years"])
    df = {y: 1.0 / (1.0 + float(p["discount_rate"])) ** y for y in years}
    sdf = sum(df.values())
    gens, stor = list(p.get("gen_set") or []), list(p.get("storage_set") or [])
    if len(stor) > 1:
        raise SystemExit("redimensionar: el modelo comparte H entre BESS; hay {}".format(stor))
    punta = {int(float(t)) for t in p["time_intervals_peak_set"]}
    precio = p.get("costo_electricidad") or p["costo_red"]

    def neto(vida):  # _net_investment_factor de functions.py
        res = 0.0 if vida <= 0 else 1.0 - len(years) / vida
        return df[years[0]] - res * df[years[-1]]

    m = gp.Model()
    m.Params.OutputFlag = 0
    G = {g: m.addVar(ub=float(_u(p["g_max_g"])[g])) for g in gens}
    H = m.addVar(ub=float(p.get("h_max", 10))) if stor else None
    P_pot = {y: m.addVar() for y in years}
    c_red = gp.LinExpr()
    for y in years:
        for d in DIAS:
            pr = _por_intervalo(_u(precio)[str(y)]["_2"][str(d)]["_3"], T)
            P_red = m.addVars(T, ub=float(p["p_peak"]))
            oferta = {t: P_red[t] for t in range(T)}
            for g in gens:
                a = _por_intervalo(_u(p["alpha_g"])[g]["_2"][str(y)]["_3"][str(d)]["_4"], T)
                P_gen = m.addVars(T)
                cap = G[g] * float(_u(p["p_max_g"])[g])
                m.addConstrs(P_gen[t] <= cap * a[t] for t in range(T))
                for t in range(T):
                    oferta[t] += P_gen[t]
            if stor:
                h = stor[0]
                pmh, eta = float(_u(p["p_max_h"])[h]), float(_u(p["eta_h"])[h])
                P_bat = m.addVars(T, lb=-gp.GRB.INFINITY)
                A = m.addVars(T, lb=-gp.GRB.INFINITY)
                m.addConstrs(P_bat[t] <= pmh * H for t in range(T))
                m.addConstrs(P_bat[t] >= -pmh * H for t in range(T))
                m.addConstrs(A[t] <= float(_u(p["a_max_h"])[h]) * H for t in range(T))
                m.addConstrs(A[t] >= float(_u(p["a_min_h"])[h]) * H for t in range(T))
                m.addConstr(A[0] == -P_bat[0] / eta * dt)                  # A[t=0] = 0
                m.addConstrs(A[t] == A[t - 1] - P_bat[t] / eta * dt for t in range(1, T))
                m.addConstr(A[0] == A[T - 1])                              # ciclico
                for t in range(T):
                    oferta[t] += P_bat[t]
            m.addConstrs(oferta[t] == float(carga[(y, d)][t]) for t in range(T))
            if 91 <= d <= 244:
                m.addConstrs(P_red[t] <= P_pot[y] for t in range(T) if t + 1 in punta)
            c_red += gp.quicksum(P_red[t] * float(pr[t]) for t in range(T)) * (dt * esc * df[y])
    coef = float(p.get("demand_charge_coef", 120))   # on-board no lo guarda: 12 * 10 fijo
    c_punta = gp.quicksum(coef * df[y] * P_pot[y] for y in years)
    vida_g, vida_h = _u(p.get("vida_util_g") or {}), _u(p.get("vida_util_h") or {})
    c_ginv = gp.quicksum(G[g] * float(_u(p["c_inv_g"])[g]) * float(_u(p["p_max_g"])[g])
                         * neto(float(vida_g.get(g, 0))) for g in gens)
    c_gop = gp.quicksum(G[g] * float(_u(p["c_op_g"])[g]) * float(_u(p["p_max_g"])[g]) * sdf
                        for g in gens)
    c_binv = c_bop = gp.LinExpr()
    if stor:
        h = stor[0]
        c_binv = H * float(_u(p["c_inv_h"])[h]) * neto(float(vida_h.get(h, 0)))
        c_bop = H * float(_u(p["c_op_h"])[h]) * sdf
    m.setObjective(c_red + c_punta + c_ginv + c_gop + c_binv + c_bop)
    m.optimize()
    if m.Status != gp.GRB.OPTIMAL:
        raise SystemExit("redimensionar: el LP termino con status {}".format(m.Status))
    out = dict(zip(TERMINOS, (e.getValue() for e in (c_red, c_punta, c_ginv, c_gop, c_binv, c_bop))))
    out["suma"] = sum(out[k] for k in TERMINOS)
    out["G_kw"] = {g: G[g].X * float(_u(p["p_max_g"])[g]) for g in gens}
    out["H"] = H.X if H is not None else 0.0
    return out


if __name__ == "__main__":
    from ajuste_energia_swap import ajustar
    sol = Path(sys.argv[1])
    params = json.loads((sol / "parameters.json").read_text(encoding="utf-8"))
    T = len(params["time_intervals_set"])
    years = sorted(int(y) for y in params["years"])
    leer = lambda n: json.loads((sol / n).read_text(encoding="utf-8")) if (sol / n).exists() else {}
    mod = redimensionar(params, carga_de_solucion(leer, years, T))
    real = redimensionar(params, {k: v["dem_aj"] for k, v in ajustar(sol)[0].items()})
    for nombre, r in (("carga del modelo", mod), ("carga real", real)):
        print("{:17s} {:,.0f} USD | ".format(nombre, r["suma"])
              + ", ".join("{} {:,.0f}".format(k, r[k]) for k in TERMINOS)
              + " | " + ", ".join("{} {:,.0f} kW".format(g, v) for g, v in r["G_kw"].items())
              + ", BESS {:.2f} u".format(r["H"]))
