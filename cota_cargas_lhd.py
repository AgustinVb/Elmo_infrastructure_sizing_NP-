"""Cota de cargas por dia, POR LHD: version entera de min_charges_n.

min_charges_n (functions.py) acota las cargas del dia con la energia TOTAL:
    cargas >= ceil(E_min / U),   U = (1 - bmin) * capacidad
Pero cada LHD tiene su propia bateria, ciclica en el dia
(battery_energy_conservation), y solo la reponen SUS swaps, cada uno con a lo
sumo U (battery_soc_swap_update_1). Con s_i los swaps utiles del LHD i:
    U * s_i >= E_i,   s_i entero   ->   cargas >= sum_i s_i
y como E_i depende de que nodos visita cada LHD (cada nodo es de UN solo LHD,
NodeAssignment), la cota es el MIP chico

    min  sum_i s_i
    s.a. U * s_i >= sum_{j de i} ener_ij * v_j          (energia de cada LHD)
         lb_j <= v_j <= ub_j, v_j entera                (production_visit_bounds)
         v_j <= intervalos con Y[i, j] en el dia
         sum_j prod_j * v_j >= meta del dia             (daily_production)

con las MISMAS cotas y coeficientes que el modelo (las de min_charges_n). Toda
solucion factible del dia lo cumple, asi que su optimo acota las cargas por
abajo. Sin ninguna ventaja sobre min_charges_n cuando un solo LHD hace todo.

Se calcula con dos capacidades:
  b_max_pool    valida en TODO el problema (b_bar[y] <= b_max_pool siempre)
  --respaldo    el b_bar[y] del incumbente: valida con su inversion y su
                degradacion fijas (para el certificado por enumeracion)

    python -u cota_cargas_lhd.py --respaldo output/.../P_red_only_r4/incumbente_respaldo.pkl \
        --out output/.../cota_cargas_lhd_r4.json
"""
import argparse
import json
import math
import os
import pickle
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)
import gurobipy as gp                                                    # noqa: E402
from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from setup import build_mine                                             # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def cota_dia(om, y, d, cap):
    """(cota por LHD, cota agregada de min_charges_n con la misma cap, detalle)."""
    model, rules = om.model, om.constraint_rules
    ts = rules.time_series
    slhd = set(model.slhd_set)
    bmin = {i: value(model.bmin_b[i]) for i in slhd}

    disp, i_rep = {}, {}
    for (i, j, y2, d2, t) in model.Y:
        if y2 == y and d2 == d:
            disp[(i, j)] = disp.get((i, j), 0) + 1
            i_rep.setdefault(j, i)
    prod, ener = {}, {}
    for (i, j) in disp:
        nt = ts.get_n_trips(j, i)
        prod[(i, j)] = value(model.g_i[i]) * nt * value(model.filling_factor[i])
        ener[(i, j)] = (value(model.pe_i[i, j]) * value(model.d_i[i, j]) * nt
                        / value(model.eta_discharge_i[i])) if i in slhd else 0.0
    meta = sum(value(model.m_j[j, y]) for j in model.nodes_set)

    m = gp.Model()
    m.Params.OutputFlag = 0
    v = {o: m.addVar(vtype=GRB.INTEGER, lb=0, ub=disp[o]) for o in disp}
    for j in {j for (_, j) in disp}:
        lb, ub = rules.production_visit_bounds(model, y, j, i_rep[j])
        m.addLConstr(gp.quicksum(v[o] for o in disp if o[1] == j), GRB.GREATER_EQUAL, max(0, lb))
        m.addLConstr(gp.quicksum(v[o] for o in disp if o[1] == j), GRB.LESS_EQUAL, ub)
    m.addLConstr(gp.quicksum(prod[o] * v[o] for o in disp), GRB.GREATER_EQUAL, meta)
    s = {i: m.addVar(vtype=GRB.INTEGER, lb=0) for i in slhd}
    for i in slhd:
        U = (1 - bmin[i]) * cap
        m.addLConstr(U * s[i] - gp.quicksum(ener[o] * v[o] for o in disp if o[0] == i),
                     GRB.GREATER_EQUAL, 0.0)
    m.setObjective(gp.quicksum(s.values()), GRB.MINIMIZE)
    m.Params.MIPGap = 0
    m.optimize()
    if m.Status != GRB.OPTIMAL:
        return None, None, {"status": m.Status}
    por_lhd = round(m.ObjVal)
    E = {i: sum(ener[o] * v[o].X for o in disp if o[0] == i) for i in slhd}
    detalle = {i: {"swaps": round(s[i].X), "energia": E[i],
                   "swaps_fraccion": E[i] / ((1 - bmin[i]) * cap)} for i in sorted(slhd)}

    # La agregada con la MISMA capacidad (min_charges_n usa b_max_pool):
    # relajacion continua de la energia total minima.
    m2 = gp.Model()
    m2.Params.OutputFlag = 0
    v2 = {o: m2.addVar(lb=0, ub=disp[o]) for o in disp}
    for j in {j for (_, j) in disp}:
        lb, ub = rules.production_visit_bounds(model, y, j, i_rep[j])
        m2.addLConstr(gp.quicksum(v2[o] for o in disp if o[1] == j), GRB.GREATER_EQUAL, max(0, lb))
        m2.addLConstr(gp.quicksum(v2[o] for o in disp if o[1] == j), GRB.LESS_EQUAL, ub)
    m2.addLConstr(gp.quicksum(prod[o] * v2[o] for o in disp), GRB.GREATER_EQUAL, meta)
    m2.setObjective(gp.quicksum(ener[o] * v2[o] for o in disp), GRB.MINIMIZE)
    m2.optimize()
    U_max = max((1 - bmin[i]) * cap for i in slhd)
    agregada = math.ceil(m2.ObjVal / U_max - 1e-9)
    return por_lhd, agregada, detalle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--respaldo", default=None,
                    help="incumbente_respaldo.pkl: ademas, la cota con su b_bar[y]")
    ap.add_argument("--out", required=True, help="JSON de salida")
    a = ap.parse_args()
    # El modelo se arma SIN el corte de cargas agregado (como cortes_cargas.py):
    # aca solo se leen sus parametros. Va en main() y no al importar, para no
    # quitarle el corte al modelo de quien importe cota_dia.
    os.environ["ELMO_SIN_CORTE_CARGAS"] = "1"

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(os.path.dirname(a.out) or ".", "_cota_cargas_lhd"),
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    model = om.model
    cap_max = value(model.b_max_pool)
    b_bar_inc = {}
    if a.respaldo:
        with open(a.respaldo, "rb") as fh:
            r = pickle.load(fh)
        for y, vars_y in r["best_full_solution"].items():
            bb = vars_y.get("b_bar")
            if isinstance(bb, dict) and bb:
                b_bar_inc[int(y)] = float(next(iter(bb.values())))

    filas = []
    print(f"{'dia':>10} {'agregada':>9} {'por LHD':>8}   "
          f"{'agr. b_bar':>10} {'LHD b_bar':>10}  swaps por LHD (b_bar del incumbente)")
    for y in sorted(model.years):
        for d in sorted(model.days):
            g_lhd, g_agr, _ = cota_dia(om, y, d, cap_max)
            fila = {"y": y, "d": d, "cap_global": cap_max,
                    "agregada_global": g_agr, "por_lhd_global": g_lhd}
            txt = ""
            if y in b_bar_inc:
                f_lhd, f_agr, det = cota_dia(om, y, d, b_bar_inc[y])
                fila.update({"b_bar_incumbente": b_bar_inc[y], "agregada_incumbente": f_agr,
                             "por_lhd_incumbente": f_lhd, "detalle_incumbente": det})
                txt = (f"{f_agr:>10} {f_lhd:>10}  "
                       + " ".join(f"{dd['swaps']}({dd['swaps_fraccion']:.2f})" for dd in det.values()))
            filas.append(fila)
            print(f"{str((y, d)):>10} {g_agr:>9} {g_lhd:>8}   {txt}", flush=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"data_folder": a.data_folder, "b_max_pool": cap_max,
                   "respaldo": a.respaldo, "cortes": filas}, fh, indent=2)
    print(f"\n[cota] {len(filas)} dias en {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
