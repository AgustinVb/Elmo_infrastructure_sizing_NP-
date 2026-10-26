"""Consulta de la DP de degradacion del pool de baterias de swap (el nucleo esta
en src/optimization/decomposition/degradacion_dp.py, que tambien usa el driver
con --reparar_dp).

Reporta, para un caso y la energia de una solucion (o la nominal del plan de
produccion con --energia nominal):
  1. validacion: el plan de baterias de la solucion reproduce su capacidad D;
  2. plan optimo de degradacion desde cero (libre, sin y con reemplazo);
  3. V en los estados pedidos con --estado año,pool,naves,D (estado al FINAL
     de ese año);
  4. con --reparar, la reparacion del plan de la solucion (solo agrega o
     adelanta compras) con --margen kWh sobre el piso.

    python -u dp_degradacion.py \
        --data_folder data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte/05C/RED_GEN_BESS \
        --solucion output/.../RED_GEN_BESS/fijarR/mejorar_ub_1/solucion \
        --estado "7,2,1,394.9" --reparar
"""
import argparse
import json
import os
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition.degradacion_dp import (              # noqa: E402
    DPDegradacion, energia_nominal, fmt_plan, parametros_desde, reparar_plan)


def cargar_caso(data_folder, n_years):
    ns = Namespace(data_folder=data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                   consumption_model="wp1", wp2_consumption_json=None, n_years=n_years,
                   days_per_year=4)
    _s, ms, ts = build_mine(ns)
    return ms, ts


def leer_parametros(data_folder, n_years, tope=None):
    ms, ts = cargar_caso(data_folder, n_years)
    return parametros_desde(ms, ts, tope)


def leer_json_anual(carpeta, nombre):
    """{año: valor} de un JSON de solucion indexado solo por año."""
    with open(os.path.join(carpeta, nombre + ".json")) as f:
        d = json.load(f)
    while isinstance(d, dict) and len(d) == 1 and not next(iter(d)).isdigit():
        d = next(iter(d.values()))
    return {int(k): float(v) for k, v in d.items()}


def leer_json_estacion(carpeta, nombre):
    """{año: suma sobre estaciones} de un JSON indexado por (estacion, año)."""
    with open(os.path.join(carpeta, nombre + ".json")) as f:
        d = json.load(f)
    tot = {}
    for est in d["k"].values():
        for y, v in est["y"].items():
            tot[int(y)] = tot.get(int(y), 0.0) + float(v)
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--solucion", required=True,
                    help="carpeta de JSON de una solucion: su plan y (por defecto) su energia")
    ap.add_argument("--energia", choices=("solucion", "nominal"), default="solucion")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--tope", type=int, default=None, help="tope de baterias por estacion")
    ap.add_argument("--pool_min", type=int, default=1)
    ap.add_argument("--estado", action="append", default=[],
                    help="año,pool,naves,D: estado al FINAL de ese año a evaluar")
    ap.add_argument("--reparar", action="store_true")
    ap.add_argument("--margen", type=float, default=0.5, help="kWh sobre el piso (--reparar)")
    a = ap.parse_args()

    ms, ts = cargar_caso(a.data_folder, a.n_years)
    P = parametros_desde(ms, ts, a.tope)
    E_sol = leer_json_anual(a.solucion, "EnergyConsumed")
    E_nom = energia_nominal(ms, ts)
    E = E_sol if a.energia == "solucion" else E_nom
    print("[dp] parametros: b_max %.1f  B_L %.1f  gamma %.6f kWh/ciclo  sf %.2f  r %.3f"
          % (P["b_max"], P["B_L"], P["gamma"], P["sf"], P["r"]))
    print("[dp] costos (sin descontar): bateria %.0f  nave %.0f  cargador %.0f  reemplazo %.0f "
          "por bateria" % (P["c_bat"], P["c_nave"], P["c_carg"], P["c_repl"]))
    print("[dp] pool <= %d (%d por nave, <= %d naves), LHD activos %s, energia: %s"
          % (P["p_max"], P["por_nave"], P["max_naves"], [P["n_lhd"][y] for y in P["years"]],
             a.energia))
    print("[dp] energia nominal vs solucion: " + "  ".join(
        f"{y}:{100 * (E_nom[y] - E_sol[y]) / E_sol[y]:+.1f}%" for y in P["years"]))

    dp = DPDegradacion(P, E, pool_min=a.pool_min)

    # --- 1. validacion: el plan de la solucion reproduce su capacidad --------
    pool = {y: int(round(v)) for y, v in leer_json_estacion(a.solucion, "N_batteries").items()}
    naves = {y: int(round(v)) for y, v in leer_json_estacion(a.solucion, "N_bays").items()}
    carg = {y: int(round(v)) for y, v in leer_json_estacion(a.solucion, "N_chargers").items()}
    D_sol = leer_json_anual(a.solucion, "D")
    tray = dp.simular(pool)
    print("\n[dp] 1. validacion contra la solucion (mismo pool, R=0)")
    print("   año  pool   E[y] kWh   D modelo   D dp     dif")
    for y in P["years"]:
        print("   %3d  %4d  %9.1f   %8.2f  %8.2f  %+6.2f"
              % (y, pool[y], E[y], D_sol[y], tray[y][1], tray[y][1] - D_sol[y]))

    # --- 2. plan optimo desde cero -------------------------------------------
    y0 = P["years"][0] - 1
    print("\n[dp] 2. plan optimo de degradacion desde cero (baterias, naves, cargadores y reemplazos)")
    for modo, nombre in (("libre", "libre"), ("sin", "sin reemplazo"), ("con", "con >=1 reemplazo")):
        c, plan = dp.V(y0, 0, 0, P["b_max"], modo)
        print("   %-18s %12s   %s" % (nombre, "-" if plan is None else f"{c:,.0f}", fmt_plan(plan or ())))

    # --- 3. estados pedidos ----------------------------------------------------
    for txt in a.estado:
        y, p, nb, D = txt.split(",")
        y, p, nb, D = int(y), int(p), int(nb), float(D)
        print("\n[dp] 3. estado al final del año %d: pool %d, naves %d, D %.1f kWh" % (y, p, nb, D))
        for modo, nombre in (("libre", "libre"), ("sin", "sin reemplazo"), ("con", "con >=1 reemplazo")):
            c, plan = dp.V(y, p, nb, D, modo)
            print("   %-18s %12s   %s" % (nombre, "infactible" if plan is None else f"{c:,.0f}",
                                         fmt_plan(plan or ())))
        print("   escalones de V(D) (libre): " + "  ".join(
            f"D>={thr:.1f}: {c:,.0f}" for thr, c, _ in dp.escalones(y, p, nb)))

    # --- 4. reparacion del plan de la solucion -------------------------------
    if a.reparar:
        print(f"\n[dp] 4. reparacion del plan de la solucion (margen {a.margen} kWh)")
        res = reparar_plan(P, E, pool, naves, carg, margen=a.margen)
        if res is None:
            print("   sin plan factible")
        else:
            costo, por_año, D_fin = res
            print("   año   pool sol -> rep   naves sol -> rep   carg sol -> rep   R")
            for y in P["years"]:
                p2, nb2, c2, R = por_año[y]
                print(f"   {y:3d}   {pool[y]:4d} -> {p2:<4d}     {naves[y]:4d} -> {nb2:<4d}"
                      f"      {carg[y]:4d} -> {c2:<4d}   {R}")
            print(f"   D al final del horizonte {D_fin:.2f} kWh; costo DP {costo:,.0f}")


if __name__ == "__main__":
    main()
