#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Resultados finales del caso DCH (11 años, dias 15 y 196, solo red, tres
estaciones): battery swapping contra carga on-board en las tres tasas de carga,
con la MISMA metodologia que resultados_finales.py de la mina modelo
(Elmo_infrastructure_sizing_NP-/cost_comparisons). Todo se escribe en
cost_comparisons/resultados_finales_dch/:

  resumen_swap_vs_ob_dch.xlsx     costos, infraestructura, energia, degradacion
                                  y las tablas del consumer de los dos lados
  costo_total_dch.png             swap ajustado contra on-board, desagregado
  caso_<tasa>/
    comparacion/                  costos (modelo, ajustes, OB), infraestructura
                                  por año, degradacion y despacho del año en
                                  regimen, swap contra OB
    battery_swapping_modelo/      json_plotter de esta rama sobre la solucion final
    battery_swapping_ajustado/    despacho y energia de swap despues de los dos
                                  ajustes, al lado del despacho del modelo y de OB
    on_board/                     json_plotter de Elmo_ob sobre la solucion OB
    LEEME.md

SWAP. Solucion final de cada tasa (menor UB): 0,5C el recorte con macrobloques
(el L-shaped no la mejoro), 1C el L-shaped v2 (mejorar UB) y 2C el L-shaped. LB:
la mayor de las corridas L-shaped de la tasa.

AJUSTES DE SWAP (los de la mina modelo, reusados tal cual con sus controles; el
script aborta si algo no cuadra):
  1. energia real de cada swap (ajuste_energia_swap.py);
  2. red y punta redimensionadas para esa carga real (redimensionar_gen_bess.py;
     en DCH no hay generacion ni BESS). Es el costo comparable de swap.
  Los dos scripts traen los 4 dias de la mina modelo: aca se fijan los 2 del DCH.

ON-BOARD (disco de ../Elmo_ob, carga libre): 1C Nested Benders, 2C busqueda local
(candidatas.py) con la LB del L-shaped; 0,5C infactible. El costo comparable de
OB pasa por el mismo LP con su propia carga.

    python cost_comparisons/resultados_finales_dch.py [--jobs 6] [--anio 7]
        [--sin_json_plotter] [--solo_json_plotter]
"""
import argparse
import importlib.util
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.ticker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

import ajuste_energia_swap as aes                                    # noqa: E402
import redimensionar_gen_bess as rgb                                 # noqa: E402

DIAS = (15, 196)                       # dias representativos de swap DCH (2 por año)
aes.DIAS = rgb.DIAS = DIAS


def con_dias(dias, fn, *args, **kw):
    """Corre fn con los dias de una solucion: los scripts de ajuste leen el DIAS de su
    modulo, y on-board DCH se corrio con 4 dias por año (swap con 2)."""
    antes = aes.DIAS, rgb.DIAS
    aes.DIAS = rgb.DIAS = tuple(dias)
    try:
        return fn(*args, **kw)
    finally:
        aes.DIAS, rgb.DIAS = antes

from comparar_swap_ob_v4 import (STACK_ORDER, TOL_USD, con_lp, controlar,  # noqa: E402
                                 costos_consumer, degradacion_consumer, energia_justa_swap,
                                 miles, reconciliar, redimensionado_swap)
from extract_costos_det import investment_breakdown                  # noqa: E402

ajustar, _serie = aes.ajustar, aes._serie
TERMINOS, carga_de_solucion, redimensionar = rgb.TERMINOS, rgb.carga_de_solucion, rgb.redimensionar

OUT = HERE / "resultados_finales_dch"
OB_REPO = REPO_ROOT.parent / "Elmo_ob"
SW = "output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d"
OBD = "output/Resultados_finales_tesis/DCH/casos_finales_v4_Carga_libre"
P_SSEE_STEP = 500.0     # kW por modulo de subestacion (functions.P_SSEE_STEP)
ESTACIONES = ("station_1", "station_2", "station_3")
OB_INFACTIBLE = ("infactible: 2 cargadores de 241 kW por estación no recargan 4 LHD en el año 7 "
                 "con las pausas DCH")

# tasa: titulo, swap (corrida con solucion/, consumer.log y resumen.json; corridas
# L-shaped para la LB) y on-board (solucion, consumer, LB y fuente) o None.
FINALES = {
    "05C": {"titulo": "DCH 0,5C, solo red",
            "swap": {"dir": SW + "/05C/RED/recorte_macro", "lbs": ["lshaped", "lshaped_v2"]},
            "ob": None},
    "1C": {"titulo": "DCH 1C, solo red",
           "swap": {"dir": SW + "/1C/RED/lshaped_v2", "lbs": ["lshaped", "lshaped_v2"]},
           "ob": {"sol": OBD + "/1C/P_red/benders", "consumer": OBD + "/1C/P_red/consumer.log",
                  "ub": 8217027.4505, "lb": 8208718.9189,
                  "fuente": "Nested Benders (gap del Benders, sin certificado)"}},
    "2C": {"titulo": "DCH 2C, solo red",
           "swap": {"dir": SW + "/2C/RED/lshaped", "lbs": ["lshaped"]},
           "ob": {"sol": OBD + "/2C/P_red/candidatas/solucion",
                  "consumer": OBD + "/2C/P_red/candidatas/consumer.log",
                  "ub": 10853114.70, "lb": 10267819.962182255, "fuente": "UB candidatas.py, LB L-shaped"}},
}

ETIQUETAS = {
    "cost_stations": "Estaciones", "cost_bays": "Naves", "cost_chargers": "Cargadores",
    "cost_batteries": "Baterías pool swap", "substation_cost": "Subestación",
    "gen_inv_cost": "Inversión generación", "gen_op_cost": "O&M generación",
    "bess_inv_cost": "Inversión BESS", "bess_op_cost": "O&M BESS",
    "peak_power_cost": "Potencia punta", "grid_energy_cost": "Energía de red",
    "battery_replace_cost": "Reemplazo baterías", "penalty_cost": "Penalidad",
    "total_cost": "COSTO TOTAL",
}
COLOR_SWAP, COLOR_OB = "#1F77B4", "#ED7D31"
VARIANTES_SWAP = ("Swap modelo", "Swap ajuste energía", "Swap ajustado")


# ------------------------------------------------------------- lectura --
def arreglar(s):
    """Los consumer.log de OB vienen con los acentos rotos (cp1252 leido como utf-8)."""
    try:
        return s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def tablas_consumer(path):
    """Todas las tablas de un consumer.log -> [(titulo, encabezados, filas)]."""
    L = [arreglar(x) for x in Path(path).read_text(encoding="utf-8", errors="replace").splitlines()]
    out, i = [], 0
    while i < len(L) - 1:
        if L[i].strip() and not L[i].startswith(("|", "+")) and L[i + 1].startswith("+-"):
            filas, j = [], i + 1
            while j < len(L) and L[j].startswith(("|", "+")):
                if L[j].startswith("|"):
                    filas.append([c.strip() for c in L[j].strip().strip("|").split("|")])
                j += 1
            out.append((L[i].strip(), filas[0], filas[1:]))
            i = j
        else:
            i += 1
    return out


def numero(v):
    m = re.fullmatch(r"\s*(-?[0-9.]+(?:[eE][-+]?\d+)?)\s*(%|[A-Za-z/ ]*)?\s*", str(v))
    return float(m.group(1)) if m else v


def leer_json(carpeta, nombre):
    p = Path(carpeta) / nombre
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def por_anio(data, *ejes):
    for e in ejes:
        data = data.get(e, {}) if isinstance(data, dict) else {}
    return {int(float(k)): float(v) for k, v in data.items() if not isinstance(v, dict)}


def inversion_ob(sol, params):
    """Inversion de OB por componente con la aritmetica del consumer de Elmo_ob: una
    nave por cargador, pago unico al año de compra con df(y) = 1/(1+r)^y."""
    r = float(params["discount_rate"])
    par = lambda n: params[n]["_1"]
    dx, dc = leer_json(sol, "Delta_X.json"), leer_json(sol, "Delta_N_chargers.json")
    out = {"cost_stations": 0.0, "cost_bays": 0.0, "cost_chargers": 0.0, "cost_batteries": 0.0}
    for k in ESTACIONES:
        for y, v in por_anio(dx, "k", k, "y").items():
            out["cost_stations"] += par("station_cost_k")[k] * v / (1 + r) ** y
        for y, v in por_anio(dc, "k", k, "y").items():
            out["cost_bays"] += par("c_bays_k")[k] * v / (1 + r) ** y
            out["cost_chargers"] += (float(params["charger_cost"]) + par("c_charger_space_k")[k]) * v / (1 + r) ** y
    return out


def reemplazo_ob(sol, params):
    """Reemplazo de baterias de OB como lo cobra el modelo (functions.battery_replace_cost):
    n_elhd_bd[y] * R[y] * c_bat_replace * df(y), con los LHD ACTIVOS del año. El
    consumer.py de Elmo_ob usa la flota completa (len(elhd_set)) y cobra de mas los
    reemplazos de años con menos LHD activos (OB 2C: año 11, 2 LHD)."""
    r = float(params["discount_rate"])
    n = {int(y): float(v) for y, v in params["n_elhd_bd"]["_1"].items()}
    rep = por_anio(leer_json(sol, "R.json"), "y")
    return sum(n[y] * v * float(params["c_bat_replace"]) / (1 + r) ** y for y, v in rep.items() if v >= 0.5)


def infraestructura(sol, years, swap, n_lhd):
    """Filas por estacion y año, y totales por año (las tres estaciones)."""
    J = lambda n: leer_json(sol, n)
    x, ch = J("X.json"), J("N_chargers.json")
    naves = J("N_bays.json") if swap else ch                 # OB: una nave por cargador
    pool = J("N_batteries.json") if swap else {}
    ssee = J("n_ssee_k.json").get("k", {})
    filas = []
    for k in ESTACIONES:
        sx, sn = por_anio(x, "k", k, "y"), por_anio(naves, "k", k, "y")
        sc, sb = por_anio(ch, "k", k, "y"), por_anio(pool, "k", k, "y")
        for y in years:
            filas.append({"Estación": k, "Año": y, "Estaciones": sx.get(y, 0.0), "Naves": sn.get(y, 0.0),
                          "Cargadores": sc.get(y, 0.0), "Baterías pool": sb.get(y, 0.0),
                          "Módulos subestación": float(ssee.get(k, 0.0))})
    flota = por_anio(J("n_battery_fleet.json"), "y") if swap else {y: float(n_lhd) for y in years}
    ppot = por_anio(J("P_pot.json"), "y")
    tot = []
    for y in years:
        f = [r for r in filas if r["Año"] == y]
        tot.append({"Año": y, **{c: sum(r[c] for r in f) for c in
                                 ("Estaciones", "Naves", "Cargadores", "Baterías pool", "Módulos subestación")},
                    "Flota de baterías": flota.get(y, 0.0), "P_pot [kW]": ppot.get(y, 0.0)})
    return filas, tot


def evolucion(serie):
    """{año: n} -> '1 → 2 (año 4)'."""
    anios = sorted(serie)
    txt, prev = "{:g}".format(serie[anios[0]]), serie[anios[0]]
    for y in anios[1:]:
        if serie[y] != prev:
            txt += " → {:g} (año {})".format(serie[y], y)
            prev = serie[y]
    return txt


def energia(series, esc_dt):
    """MWh del horizonte de un despacho {(y, d): {carga, red, gen, bat}} (kW)."""
    f = esc_dt / 1000.0
    tot = {"Carga [MWh]": 0.0, "Red [MWh]": 0.0, "Generación usada [MWh]": 0.0, "Descarga BESS [MWh]": 0.0}
    anual = {}
    for (y, d), v in series.items():
        a = anual.setdefault(y, {"carga": 0.0, "red": 0.0, "gen": 0.0})
        for k, col in (("carga", "Carga [MWh]"), ("red", "Red [MWh]"), ("gen", "Generación usada [MWh]")):
            tot[col] += float(np.sum(v[k])) * f
            a[k] += float(np.sum(v[k])) * f
        tot["Descarga BESS [MWh]"] += float(np.sum(np.maximum(v["bat"], 0))) * f
    tot["Penetración renovable"] = 1 - tot["Red [MWh]"] / tot["Carga [MWh]"] if tot["Carga [MWh]"] else 0.0
    return tot, anual


def despacho_solucion(sol, years, T, dias=DIAS):
    J = lambda n: leer_json(sol, n)
    red, gen, bat = J("P_red.json"), J("P_gen.json"), J("P_bat.json")
    out = {}
    for y in years:
        for d in dias:
            r = _serie(red, y, d, T)
            g = _serie(gen, y, d, T, "g") if gen else np.zeros(T)
            b = _serie(bat, y, d, T, "h") if bat else np.zeros(T)
            out[(y, d)] = {"red": r, "gen": g, "bat": b, "carga": r + g + b}
    return out


def mejor_lb(carpeta_caso, corridas):
    lbs = []
    for c in corridas:
        p = REPO_ROOT / carpeta_caso / c / "resumen.json"
        if p.exists():
            d = json.loads(p.read_text())
            if d.get("lb_final") is not None:
                lbs.append((float(d["lb_final"]), c))
    return max(lbs) if lbs else (float("nan"), "-")


# --------------------------------------------------------------- calculo --
def procesar(tasa):
    cfg = FINALES[tasa]
    sw = cfg["swap"]
    sol_rel = sw["dir"] + "/solucion"
    sol_s = REPO_ROOT / sol_rel
    resumen = json.loads((REPO_ROOT / sw["dir"] / "resumen.json").read_text())
    ub = float(resumen.get("ub_final", resumen.get("ub")))
    print("[{}] swap {} | OB {}".format(tasa, sol_rel, cfg["ob"]["sol"] if cfg["ob"] else "-"), flush=True)

    texto = (REPO_ROOT / sw["dir"] / "consumer.log").read_text(encoding="utf-8", errors="replace")
    c_s = costos_consumer(texto, sw["dir"])
    inv_s = investment_breakdown(sol_s)
    if abs(sum(inv_s.values()) - c_s["estaciones"]) > TOL_USD:
        raise SystemExit("swap {}: la inversion abierta ({:,.2f}) no reproduce la del consumer ({:,.2f})"
                         .format(tasa, sum(inv_s.values()), c_s["estaciones"]))
    params = json.loads((sol_s / "parameters.json").read_text(encoding="utf-8"))
    years = sorted(int(y) for y in params["years"])
    T = len(params["time_intervals_set"])
    dt, esc = float(params["delta_t"]), float(params["scaling_factor_op_cost"])
    modelo = reconciliar("swap " + tasa, c_s, inv_s, ub)

    # Ajuste 1 (energia real de cada swap) y 2 (LP de red y punta con esa carga).
    ajuste = ajustar(sol_s)
    aj = energia_justa_swap(sol_s, c_s["grid_energy_cost"], ajuste)
    redim = redimensionado_swap(sol_rel, params, c_s, ajuste)
    energia_aj = dict(modelo)
    energia_aj["grid_energy_cost"] = aj["central"]
    energia_aj["total_cost"] = modelo["total_cost"] - modelo["grid_energy_cost"] + aj["central"]
    swap_aj = con_lp(modelo, redim["lp_carga_real"])
    out_aj = ajuste[0]
    lp = redimensionar(params, {k: v["dem_aj"] for k, v in out_aj.items()}, series=True)
    if abs(lp["suma"] - redim["lp_carga_real"]["suma"]) > 1e-3:
        raise SystemExit("{}: el LP con series no reproduce el redimensionamiento".format(tasa))

    desp = {
        "modelo": {k: {"carga": v["dem"], "red": v["red"], "gen": v["gen"], "bat": v["bat"]}
                   for k, v in out_aj.items()},
        "ajuste_energia": {k: {"carga": v["dem_aj"], "red": v["red_aj"], "gen": v["gen_aj"],
                               "bat": v["bat_aj"]} for k, v in out_aj.items()},
        "ajustado": {k: {"carga": v["carga"], "red": v["red"], "gen": sum(v["gen"].values())
                         if v["gen"] else np.zeros(T), "bat": v["bat"]}
                     for k, v in lp["series"].items()},
    }
    lb, lb_fuente = mejor_lb(str(Path(sw["dir"]).parent), sw["lbs"])
    r = {"clave": tasa, "titulo": cfg["titulo"], "swap_sol": sol_rel,
         "swap_consumer": sw["dir"] + "/consumer.log", "ub": ub, "lb": lb, "lb_fuente": lb_fuente,
         "costos": {"Swap modelo": modelo, "Swap ajuste energía": energia_aj, "Swap ajustado": swap_aj},
         "control_swap": redim["control"], "control_ob": None,
         "lp": {"swap_modelo": redim["lp_carga_modelo"], "swap_ajustado": redim["lp_carga_real"]},
         "ajuste": aj["energia_mwh"], "deg": {"Swap": degradacion_consumer(texto, sw["dir"])},
         "infra": {"Swap": infraestructura(sol_s, years, True, len(params.get("lhd_set") or []))},
         "despacho": desp, "esc_dt": esc * dt, "dt": dt, "P_pot_aj": lp["P_pot"], "params": params,
         "tablas": {"Swap": tablas_consumer(REPO_ROOT / sw["dir"] / "consumer.log")},
         "ob": None}

    if cfg["ob"] is not None:
        o = cfg["ob"]
        sol_o = OB_REPO / o["sol"]
        texto_o = (OB_REPO / o["consumer"]).read_text(encoding="utf-8", errors="replace")
        c_o = costos_consumer(texto_o, o["consumer"])
        params_o = json.loads((sol_o / "parameters.json").read_text(encoding="utf-8"))
        inv_o = inversion_ob(sol_o, params_o)
        if abs(sum(inv_o.values()) - c_o["estaciones"]) > TOL_USD:
            raise SystemExit("OB {}: la inversion abierta ({:,.2f}) no reproduce la del consumer ({:,.2f})"
                             .format(tasa, sum(inv_o.values()), c_o["estaciones"]))
        rep = reemplazo_ob(sol_o, params_o)
        if abs(rep - c_o["battery_replace_cost"]) > TOL_USD:
            print("[aviso] OB {}: reemplazo del consumer {:,.2f} -> {:,.2f} con los LHD activos de cada año "
                  "(como el modelo)".format(tasa, c_o["battery_replace_cost"], rep), flush=True)
            c_o["total_cost"] += rep - c_o["battery_replace_cost"]
            c_o["battery_replace_cost"] = rep
        ob_modelo = reconciliar("OB " + tasa, c_o, inv_o, o["ub"])
        # El LP de OB con sus propios dias, precios y escala (4 dias por año) y el
        # resto de los parametros de swap (en lo que entra al LP son identicos).
        dias_o = tuple(int(d) for d in params_o["days"])
        p_lp = {**params, **params_o}
        if "costo_electricidad" not in params_o:      # el precio de OB viene en costo_red
            p_lp.pop("costo_electricidad", None)
        leer_o = lambda n: leer_json(sol_o, n)
        lp_ob = con_dias(dias_o, lambda: redimensionar(p_lp, carga_de_solucion(leer_o, years, T)))
        r["control_ob"] = controlar("OB " + tasa, c_o, lp_ob)
        r["costos"]["OB modelo"] = ob_modelo
        r["costos"]["OB comparable"] = con_lp(ob_modelo, lp_ob)
        r["lp"]["ob"] = lp_ob
        r["deg"]["OB"] = degradacion_consumer(texto_o, o["consumer"])
        r["infra"]["OB"] = infraestructura(sol_o, years, False, len(params_o.get("lhd_set") or []))
        r["despacho"]["ob"] = despacho_solucion(sol_o, years, T, dias_o)
        r["esc_dt_ob"] = float(params_o["scaling_factor_op_cost"]) * float(params_o["delta_t"])
        r["dias_ob"] = dias_o
        r["tablas"]["OB"] = tablas_consumer(OB_REPO / o["consumer"])
        r["ob"] = {"sol": str(sol_o), "consumer": str(OB_REPO / o["consumer"]), "lb": o["lb"],
                   "fuente": o["fuente"]}
    return r


# ----------------------------------------------------------------- excel --
def gap(ub, lb):
    return (ub - lb) / ub if ub else float("nan")


def hojas(res):
    resumen, costos, infra, anual, ener, deg, cons = [], [], [], [], [], [], []
    for r in res:
        c = r["costos"]
        s_m, s_e, s_a = (c[k]["total_cost"] for k in VARIANTES_SWAP)
        o_m = c["OB modelo"]["total_cost"] if r["ob"] else None
        o_c = c["OB comparable"]["total_cost"] if r["ob"] else None
        resumen.append({
            "Tasa": r["clave"], "Escenario": r["titulo"],
            "Swap UB modelo [USD]": s_m, "Swap LB [USD]": r["lb"], "Swap gap": gap(s_m, r["lb"]),
            "Swap ajuste energía [USD]": s_e, "Swap ajustado [USD]": s_a,
            "OB modelo [USD]": o_m, "OB LB [USD]": r["ob"]["lb"] if r["ob"] else None,
            "OB gap": gap(o_m, r["ob"]["lb"]) if r["ob"] else None, "OB comparable [USD]": o_c,
            "Fuente OB": r["ob"]["fuente"] if r["ob"] else OB_INFACTIBLE,
            "Swap vs OB (modelo)": s_m / o_m - 1 if r["ob"] else None,
            "Swap vs OB (ajuste energía)": s_e / o_c - 1 if r["ob"] else None,
            "Swap vs OB (ajustado)": s_a / o_c - 1 if r["ob"] else None,
            "Control LP swap [USD]": r["control_swap"], "Control LP OB [USD]": r["control_ob"],
            "Solución swap": r["swap_sol"], "LB swap (corrida)": r["lb_fuente"],
            "Solución OB": r["ob"]["sol"] if r["ob"] else "-",
        })
        for variante, fila in c.items():
            costos.append({"Tasa": r["clave"], "Escenario": r["titulo"], "Variante": variante,
                           **{ETIQUETAS[k]: fila[k] for k in ETIQUETAS}})
        for tec in r["infra"]:
            filas, tot = r["infra"][tec]
            nombre_tec = "Battery swapping" if tec == "Swap" else "On-board"
            for k in ESTACIONES + ("Total",):
                fk = tot if k == "Total" else [f for f in filas if f["Estación"] == k]
                serie = lambda col: {f["Año"]: f[col] for f in fk}
                fila = {"Tasa": r["clave"], "Tecnología": nombre_tec, "Estación": k,
                        "Estaciones": evolucion(serie("Estaciones")), "Naves": evolucion(serie("Naves")),
                        "Cargadores": evolucion(serie("Cargadores")),
                        "Baterías pool": evolucion(serie("Baterías pool")),
                        "Módulos subestación": fk[-1]["Módulos subestación"],
                        "Subestación [kW]": fk[-1]["Módulos subestación"] * P_SSEE_STEP}
                if k == "Total":
                    fila["Flota de baterías"] = evolucion(serie("Flota de baterías"))
                    fila["P_pot máx [kW]"] = max(serie("P_pot [kW]").values())
                    if tec == "Swap":
                        fila["P_pot máx ajustado [kW]"] = max(r["P_pot_aj"].values())
                infra.append(fila)
            for f in filas:
                anual.append({"Tasa": r["clave"], "Tecnología": nombre_tec, **f})
            for f in tot:
                anual.append({"Tasa": r["clave"], "Tecnología": nombre_tec, "Estación": "Total", **f})
        for variante, clave_d in (("Swap modelo", "modelo"), ("Swap ajuste energía", "ajuste_energia"),
                                  ("Swap ajustado", "ajustado"), ("OB", "ob")):
            if clave_d in r["despacho"]:
                tot, _ = energia(r["despacho"][clave_d], r["esc_dt_ob"] if clave_d == "ob" else r["esc_dt"])
                ener.append({"Tasa": r["clave"], "Escenario": r["titulo"], "Variante": variante, **tot})
        for tec, filas in r["deg"].items():
            for anio, b_ini, d_fin, rep in filas:
                deg.append({"Tasa": r["clave"], "Tecnología": "Battery swapping" if tec == "Swap" else "On-board",
                            "Año": anio, "Capacidad inicio [kWh]": b_ini, "Capacidad fin [kWh]": d_fin,
                            "Reemplazo": "SI" if rep else "NO"})
        for tec, tablas in r["tablas"].items():
            for titulo, enc, filas in tablas:
                if titulo == "INPUTS" or titulo.startswith("DETALLE"):
                    continue
                for f in filas:
                    for col, v in zip(enc[1:], f[1:]):
                        cons.append({"Tasa": r["clave"],
                                     "Tecnología": "Battery swapping" if tec == "Swap" else "On-board",
                                     "Tabla": titulo, "Fila": f[0], "Columna": col,
                                     "Valor": numero(v), "Texto": v})
    cons = pd.DataFrame(cons)[["Tasa", "Tecnología", "Tabla", "Fila", "Columna", "Valor", "Texto"]]
    return {"Resumen": pd.DataFrame(resumen), "Costos": pd.DataFrame(costos),
            "Infraestructura": pd.DataFrame(infra), "Infraestructura anual": pd.DataFrame(anual),
            "Energía": pd.DataFrame(ener), "Degradación": pd.DataFrame(deg), "Consumer": cons}


NOTAS = [
    ("Qué es", "Resultados finales del caso DCH (11 años, días 15 y 196, solo red, tres estaciones), battery "
               "swapping (BS) contra carga on-board (OB), tasas 0,5C, 1C y 2C. Generado por "
               "cost_comparisons/resultados_finales_dch.py con la metodología de resultados_finales.py de la "
               "mina modelo."),
    ("Swap", "Solución final de cada tasa (menor UB): 0,5C recorte con macrobloques, 1C L-shaped v2, 2C "
             "L-shaped. LB: la mayor de las corridas L-shaped de la tasa."),
    ("Swap modelo", "Costo de la solución tal como la pagó el modelo (= UB, = COSTO TOTAL del consumer)."),
    ("Swap ajuste energía", "Ajuste 1 (ajuste_energia_swap.py): el modelo cobra cada recarga como el bloque "
                            "completo bmin → 100 %; se cambia por la energía real de cada swap."),
    ("Swap ajustado", "Ajuste 2 (redimensionar_gen_bess.py): con la carga real fija, un LP con las ecuaciones "
                      "del modelo vuelve a elegir red y potencia de punta (en DCH no hay generación ni BESS). "
                      "Es el costo comparable de BS (POST AJUSTE)."),
    ("OB comparable", "Mismo LP con la carga de la solución OB (Control LP OB = modelo − LP)."),
    ("On-board", "1C: Nested Benders (gap del Benders); 2C: búsqueda local candidatas.py, LB del L-shaped; "
                 "0,5C: " + OB_INFACTIBLE + "."),
    ("Gaps de swap 0,5C y 1C", "La LB no demuestra que el año 7 (12 LHD) necesita la segunda batería por "
                               "estación: con una batería no se encontró operación, pero la cota de producción "
                               "por estación queda en 54,50 contra una meta de 54,44."),
    ("Reemplazo OB", "El consumer.py de Elmo_ob cobra cada reemplazo por la flota completa (12 LHD); el modelo "
                     "por los LHD activos del año (n_elhd_bd). Se usa el del modelo: en OB 2C (reemplazo en el "
                     "año 11, 2 LHD activos) el consumer cobra 1,28 M de más; en OB 1C no cambia."),
    ("Controles", "El script aborta si: los términos no suman el COSTO TOTAL del consumer, el COSTO TOTAL no es "
                  "la UB, la inversión abierta no reproduce la del consumer, o el LP con la carga de la propia "
                  "solución paga más que el modelo."),
    ("Días muestreados", "Swap DCH se corrió con 2 días por año (15 y 196, escala 182,5) y on-board DCH con "
                         "4 (15, 105, 196 y 288, escala 91,25). Cada costo es el del horizonte con su propia "
                         "muestra; on-board además tiene que operar los días 105 y 288. Los despachos se "
                         "comparan en los días comunes (15 y 196)."),
    ("Energía", "MWh del horizonte (días muestreados × factor de escala de cada lado)."),
    ("Consumer", "Todas las tablas que imprime consumer.py para cada solución (sin INPUTS ni el detalle por "
                 "swap), en formato largo. Las de OB vienen del consumer de Elmo_ob."),
    ("Infraestructura", "Por estación y total, evolución 'a → b (año y)'. OB: una nave por cargador; la flota "
                        "de baterías de OB es una por LHD. Subestación: módulos de 500 kW."),
    ("Descuento", "Valores presentes con year_position 1-indexada, df(y) = 1/(1+r)^y, r = 8 %."),
]


def escribir_excel(res, path):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    dfs = hojas(res)
    dfs["Notas"] = pd.DataFrame(NOTAS, columns=["Tema", "Detalle"])
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for nombre, df in dfs.items():
            df.to_excel(xw, sheet_name=nombre, index=False)
            ws = xw.sheets[nombre]
            ws.freeze_panes = "A2" if nombre == "Notas" else "C2"
            for cel in ws[1]:
                cel.font = Font(bold=True, color="FFFFFF")
                cel.fill = PatternFill("solid", fgColor="1F4E79")
                cel.alignment = Alignment(wrap_text=True, vertical="center")
            ws.row_dimensions[1].height = 45
            for j, col in enumerate(df.columns, start=1):
                letra = get_column_letter(j)
                ancho = max([len(str(col)) * 0.8] + [len(str(v)) for v in df[col].head(200)]) + 2
                ws.column_dimensions[letra].width = min(max(ancho, 9), 70 if nombre == "Notas" else 45)
                es_pct = col in ("Swap gap", "OB gap", "Penetración renovable") or col.startswith("Swap vs OB")
                fmt = "0.00%" if es_pct else ("#,##0.00" if "USD" in col or nombre == "Costos"
                                              else ("#,##0.0" if "MWh" in col or "kW" in col else None))
                if fmt and df[col].dtype.kind == "f":
                    for cel in ws[letra][1:]:
                        cel.number_format = fmt
            if nombre == "Notas":
                for cel in ws["B"][1:]:
                    cel.alignment = Alignment(wrap_text=True, vertical="top")
    print("ok ->", path, flush=True)


# -------------------------------------------------------------- figuras --
def figura_costos(r, out_png):
    variantes = list(VARIANTES_SWAP) + (["OB comparable"] if r["ob"] else [])
    filas = [r["costos"][v] for v in variantes]
    xs = np.arange(len(filas))
    tot = np.array([f["total_cost"] for f in filas]) / 1000.0
    ymax = tot.max() * 1.12
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    bottom = np.zeros(len(filas))
    for claves, color, etiqueta in STACK_ORDER:
        vals = np.array([sum(f[k] for k in claves) for f in filas]) / 1000.0
        if not np.any(vals > 0):
            continue
        ax.bar(xs, vals, 0.65, bottom=bottom, color=color, label=etiqueta, edgecolor="black", linewidth=0.5)
        for xi, v, b0 in zip(xs, vals, bottom):
            if v >= 0.03 * ymax:
                ax.text(xi, b0 + v / 2, miles(v), ha="center", va="center", fontsize=8, color="white",
                        fontweight="bold", path_effects=[pe.withStroke(linewidth=1.6, foreground="#111")])
        bottom += vals
    for xi, t in zip(xs, tot):
        ax.text(xi, t + 0.01 * ymax, miles(t), ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax.set_xticks(xs)
    ax.set_xticklabels(["BS modelo", "BS ajuste\nenergía", "BS ajustado\n(comparable)", "On-board"][:len(filas)])
    ax.set_ylim(0, ymax)
    ax.set_ylabel("Costo total [kUSD, VP 11 años]")
    sub = ("BS ajustado vs on-board: {:+.2%}".format(tot[2] / tot[3] - 1) if r["ob"]
           else "On-board " + OB_INFACTIBLE)
    ax.set_title("{}\n{}".format(r["titulo"], sub), fontsize=11)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    h, l = ax.get_legend_handles_labels()
    ax.legend(h[::-1], l[::-1], loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_infra(r, out_png):
    cols = ["Naves", "Cargadores", "Flota de baterías", "Módulos subestación", "P_pot [kW]"]
    fig, axes = plt.subplots(1, len(cols), figsize=(20, 3.8))
    # Escalon desde el inicio de cada año (lo instalado en el año y rige todo el año y).
    escalon = lambda ys, vs: (list(ys) + [ys[-1] + 1], list(vs) + [vs[-1]])
    for ax, col in zip(axes, cols):
        for tec, color, et, dz in (("Swap", COLOR_SWAP, "Battery swapping", 0.0),
                                   ("OB", COLOR_OB, "On-board", 0.04)):
            if tec not in r["infra"]:
                continue
            _, f = r["infra"][tec]
            xs, vs = escalon([x["Año"] for x in f], [x[col] + dz * (col != "P_pot [kW]") for x in f])
            ax.step(xs, vs, where="post", color=color, linewidth=2, label=et)
        if col == "P_pot [kW]":
            ys = sorted(r["P_pot_aj"])
            xs, vs = escalon(ys, [r["P_pot_aj"][y] for y in ys])
            ax.step(xs, vs, where="post", color=COLOR_SWAP, linestyle="--", label="BS ajustado")
        ax.set_title(col if col == "P_pot [kW]" else col + " (3 estaciones)")
        ax.set_xlabel("Año (inicio)")
        ax.set_xticks(range(1, 12))
        ax.set_xlim(1, 12)
        if col != "P_pot [kW]":
            ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
            ax.set_ylim(0, None)
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    axes[-1].legend(fontsize=8)
    fig.suptitle("{}: infraestructura por año".format(r["titulo"]), fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_degradacion(r, out_png, b_max=482.0):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4), sharey=True)
    for ax, (tec, titulo) in zip(axes, (("Swap", "Battery swapping"), ("OB", "On-board"))):
        ax.set_title(titulo)
        ax.set_ylim(0, b_max * 1.1)
        ax.grid(axis="y", alpha=0.3)
        if tec not in r["deg"]:
            ax.text(0.5, 0.5, "infactible", transform=ax.transAxes, ha="center", va="center", fontsize=12,
                    color="#555")
            ax.set_xticks([])
            continue
        deg = r["deg"][tec]
        x = np.arange(len(deg))
        ax.bar(x, [d[1] for d in deg], 0.6, color=[COLOR_OB if d[3] else COLOR_SWAP for d in deg],
               label="Capacidad al inicio (naranjo: reemplazo)")
        ax.plot(x, [d[2] for d in deg], "o-", color="#7F7F7F", label="Capacidad al final")
        ax.axhline(b_max, color="#333", linestyle="--", linewidth=1)
        ax.axhline(0.8 * b_max, color="#333", linestyle=":", linewidth=1, label="Piso 80 %")
        ax.set_xticks(x)
        ax.set_xticklabels([d[0] for d in deg])
        ax.set_xlabel("Año")
    axes[0].set_ylabel("Capacidad por batería [kWh]")
    axes[0].legend(fontsize=8, loc="lower left")
    fig.suptitle("{}: degradación de baterías".format(r["titulo"]), fontsize=11)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def horas_reloj(r):
    """Hora de reloj del inicio de cada intervalo (base_hour de la hoja Shifts) y el
    orden que la deja creciente en 0-24."""
    T = len(next(iter(r["despacho"]["modelo"].values()))["carga"])
    h = (float(r["params"]["base_hour"]) + np.arange(T) * r["dt"]) % 24.0
    return h, np.argsort(h, kind="stable")


def panel_despacho(ax, v, horas, orden, titulo):
    hx = horas[orden]
    dis, carga_bess = np.maximum(v["bat"], 0)[orden], np.minimum(v["bat"], 0)[orden]
    capas = [(v["red"][orden], "#ED7D31", "Red"), (v["gen"][orden], "#70AD47", "Generación"),
             (dis, "#5B9BD5", "Descarga BESS")]
    base = np.zeros_like(hx)
    for val, color, et in capas:
        if np.any(val > 1e-6):
            ax.fill_between(hx, base, base + val, step="post", color=color, alpha=0.85, label=et, linewidth=0)
        base = base + val
    if np.any(carga_bess < -1e-6):
        ax.fill_between(hx, carga_bess, 0, step="post", color="#A5A5A5", alpha=0.85, label="Carga BESS",
                        linewidth=0)
    ax.step(hx, v["carga"][orden], where="post", color="black", linewidth=1.2, label="Consumo estaciones")
    ax.set_title(titulo, fontsize=10)
    ax.set_xlabel("Hora del día")
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 4))
    ax.grid(alpha=0.3)


def paneles(r):
    out = [("modelo", "BS modelo"), ("ajustado", "BS ajustado (comparable)")]
    return out + ([("ob", "On-board")] if "ob" in r["despacho"] else [])


def figuras_ajustado(r, carpeta):
    horas, orden = horas_reloj(r)
    for (y, d) in sorted(r["despacho"]["modelo"]):
        ps = paneles(r)
        fig, axes = plt.subplots(1, len(ps), figsize=(6 * len(ps), 4.2), sharey=True)
        for ax, (k, tit) in zip(axes, ps):
            panel_despacho(ax, r["despacho"][k][(y, d)], horas, orden, tit)
        axes[0].set_ylabel("Potencia [kW]")
        h, l = axes[0].get_legend_handles_labels()
        for ax in axes[1:]:
            for hh, ll in zip(*ax.get_legend_handles_labels()):
                if ll not in l:
                    h.append(hh)
                    l.append(ll)
        fig.legend(h, l, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=len(l), fontsize=9)
        fig.suptitle("{}: despacho año {} día {}".format(r["titulo"], y, d), fontsize=11)
        fig.savefig(carpeta / "despacho_Y{}_d{}.png".format(y, d), dpi=130, bbox_inches="tight")
        plt.close(fig)
    # Energia por año
    fig, axes = plt.subplots(1, 2, figsize=(14, 4.2))
    for ax, campo, titulo in ((axes[0], "carga", "Consumo de las estaciones"),
                              (axes[1], "red", "Energía comprada a la red")):
        for k, color, ls, et in (("modelo", COLOR_SWAP, "-", "BS modelo"), ("ajustado", COLOR_SWAP, "--", "BS ajustado"),
                                 ("ob", COLOR_OB, "-", "On-board")):
            if k not in r["despacho"]:
                continue
            _, anual = energia(r["despacho"][k], r["esc_dt_ob"] if k == "ob" else r["esc_dt"])
            ys = sorted(anual)
            ax.plot(ys, [anual[y][campo] for y in ys], ls, color=color, marker="o", markersize=4, label=et)
        ax.set_title(titulo)
        ax.set_xlabel("Año")
        ax.set_ylabel("MWh/año")
        ax.set_xticks(range(1, 12))
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=9)
    fig.suptitle("{}: energía por año".format(r["titulo"]), fontsize=11)
    fig.savefig(carpeta / "energia_anual.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_regimen(r, anio, dia, out_png):
    """Operacion en regimen (como plot_despacho_swap_vs_ob_v4.py): BS ajustado contra
    OB en un dia del año en regimen, con la ventana de punta y P_pot del año. Se
    verifica que la red no supere P_pot en la ventana (power_peak_limit)."""
    horas, orden = horas_reloj(r)
    dt = r["dt"]
    punta = sorted(int(float(t)) for t in (r["params"].get("time_intervals_peak_set") or []))
    con_cargo = 91 <= dia <= 244
    lados = [("ajustado", "Battery swapping (ajustado)", r["P_pot_aj"].get(anio, 0.0))]
    if "ob" in r["despacho"]:
        ppot_ob = por_anio(leer_json(r["ob"]["sol"], "P_pot.json"), "y").get(anio, 0.0)
        lados.append(("ob", "On-board", ppot_ob))
    fig, axes = plt.subplots(1, 2, figsize=(15, 4.6), sharey=True)
    for ax, (k, tit, ppot) in zip(axes, lados):
        v = r["despacho"][k][(anio, dia)]
        panel_despacho(ax, v, horas, orden, tit)
        if con_cargo and punta:
            for i, t in enumerate(punta):
                ax.axvspan(horas[t - 1], horas[t - 1] + dt, color="#FFD966", alpha=0.35, linewidth=0,
                           label="Ventana de punta" if i == 0 else None)
            ax.axhline(ppot, color="#C00000", linestyle="--", linewidth=1.2,
                       label="P_pot año {} = {:.0f} kW".format(anio, ppot))
            exceso = max(v["red"][t - 1] for t in punta) - ppot
            if exceso > 1e-3:
                raise SystemExit("{} {}: la red supera P_pot en {:.3f} kW en la ventana de punta".format(
                    r["clave"], k, exceso))
        ax.text(0.01, 0.97, "Energía del día: {:.2f} MWh\nPico de red: {:.0f} kW".format(
            v["carga"].sum() * dt / 1000.0, v["red"].max()), transform=ax.transAxes, va="top", fontsize=8,
            bbox=dict(boxstyle="round", facecolor="white", alpha=0.8))
        ax.legend(fontsize=8, loc="upper right")
    if len(lados) == 1:
        import textwrap
        axes[1].text(0.5, 0.5, textwrap.fill("On-board " + OB_INFACTIBLE, 45), transform=axes[1].transAxes,
                     ha="center", va="center", fontsize=10, color="#555")
        axes[1].set_title("On-board", fontsize=10)
        axes[1].set_xticks([])
        axes[1].tick_params(left=False)
    axes[0].set_ylabel("Potencia [kW]")
    fig.suptitle("{}: operación en régimen, año {}, día {}{}".format(
        r["titulo"], anio, dia, " (con cargo por potencia)" if con_cargo else ""), fontsize=11)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_total(res, out_png):
    barras, grupos = [], []
    for g, r in enumerate(res):
        x0 = g * 2.6
        barras += [(x0, "BS", r["costos"]["Swap ajustado"]), (x0 + 1.0, "OB", r["costos"].get("OB comparable"))]
        grupos.append((x0 + 0.5, r))
    xs = np.array([b[0] for b in barras])
    tot = np.array([b[2]["total_cost"] if b[2] else 0.0 for b in barras]) / 1000.0
    ymax = tot.max() * 1.18
    fig, ax = plt.subplots(figsize=(11, 6))
    bottom = np.zeros(len(barras))
    for claves, color, etiqueta in STACK_ORDER:
        vals = np.array([sum(b[2][k] for k in claves) if b[2] else 0.0 for b in barras]) / 1000.0
        if not np.any(vals > 0):
            continue
        ax.bar(xs, vals, 0.8, bottom=bottom, color=color, label=etiqueta, edgecolor="black", linewidth=0.5)
        bottom += vals
    for (xi, _, filas), t in zip(barras, tot):
        if filas:
            ax.text(xi, t + 0.01 * ymax, miles(t), ha="center", va="bottom", fontsize=9, fontweight="bold")
        else:
            ax.text(xi, 0.02 * ymax, "infactible", ha="center", va="bottom", fontsize=9, rotation=90, color="#555")
    for xg, r in grupos:
        ax.text(xg, -0.06, r["clave"].replace("05C", "0,5C"),
                transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=9)
    ax.set_xticks(xs)
    ax.set_xticklabels([b[1] for b in barras])
    ax.set_ylim(0, ymax)
    ax.set_ylabel("Costo total [kUSD, VP 11 años]")
    ax.set_title("DCH: costo total del horizonte, battery swapping vs on-board")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    h, l = ax.get_legend_handles_labels()
    ax.legend(h[::-1], l[::-1], loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=9)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------ json_plotter --
def correr_json_plotter(plotter_py, sol, destino):
    """json_plotter.py (de esta rama o de Elmo_ob) sobre una solucion; los PNG van
    a destino en vez de <sol>/plots. Cada grafico por separado: el de Elmo_ob no
    atrapa los errores y uno que falla cortaria los demas."""
    import os
    import traceback
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    nombre = "json_plotter_" + Path(plotter_py).parent.name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(nombre, plotter_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    creada = not (Path(sol) / "plots").exists()
    p = mod.JSONPlotter(str(sol))
    if creada:
        try:
            os.rmdir(p.plot_dir)
        except OSError:
            pass
    p.plot_dir = str(destino)
    fuente = Path(plotter_py).read_text(encoding="utf-8")
    cuerpo = fuente[fuente.index("def create_all_plots"):]
    cuerpo = cuerpo[:cuerpo.index("\n\n")]
    metodos = re.findall(r"^\s+self\.(plot_\w+)", cuerpo, flags=re.M)   # sin los comentados
    fallas = []
    for m in metodos:
        try:
            getattr(p, m)()
        except Exception as e:                                           # noqa: BLE001
            fallas.append("{}: {}".format(m, e))
            traceback.print_exc(limit=1)
        plt.close("all")
    return str(destino), len(list(destino.glob("*.png"))), fallas


def trabajos_json_plotter(claves):
    out = []
    for tasa in claves:
        cfg = FINALES[tasa]
        carpeta = OUT / "caso_{}".format(tasa)
        out.append((str(REPO_ROOT / "json_plotter.py"), str(REPO_ROOT / cfg["swap"]["dir"] / "solucion"),
                    str(carpeta / "battery_swapping_modelo")))
        if cfg["ob"]:
            out.append((str(OB_REPO / "json_plotter.py"), str(OB_REPO / cfg["ob"]["sol"]),
                        str(carpeta / "on_board")))
    return out


LEEME = """# {titulo}

| | Costo total [USD, VP 11 años] |
|---|---:|
| Battery swapping, modelo (UB) | {s_m:,.2f} |
| Battery swapping, ajuste de energía | {s_e:,.2f} |
| **Battery swapping, ajustado (comparable)** | **{s_a:,.2f}** |
| **On-board (comparable)** | **{o_c}** |
| BS ajustado vs OB | {dif} |

LB del swap: {lb:,.2f} ({lb_fuente}), gap {gap:.2%}.
On-board: {ob_txt}

## Carpetas

- `comparacion/`: costos (modelo, ajustes y OB), infraestructura por año (suma de las tres
  estaciones), degradación y operación en régimen (`regimen_Y{anio}_d<dia>.png`: BS ajustado
  contra OB con la ventana de punta y P_pot del año).
- `battery_swapping_modelo/`: `json_plotter.py` de esta rama sobre la solución final.
  **Son gráficos del modelo, antes del ajuste**: cada recarga aparece como el bloque completo
  bmin → 100 %.
- `battery_swapping_ajustado/`: despacho de cada año y día muestreado con BS modelo, BS
  ajustado (energía real de cada swap y red redimensionada) y on-board; `energia_anual.png`
  compara consumo y compra a la red por año.
- `on_board/`: `json_plotter.py` de Elmo_ob sobre la solución on-board.

## Fuentes

- Swap: `{swap_sol}`
- On-board: `{ob_sol}`

Generado por `cost_comparisons/resultados_finales_dch.py`; el detalle está en
`../resumen_swap_vs_ob_dch.xlsx`.
"""

LEEME_GENERAL = """# Resultados finales DCH: battery swapping vs on-board

Costo total del horizonte (USD, VP 11 años, solo red, tres estaciones, días 15 y 196).
Swap: solución final de cada tasa **después** de los dos ajustes (energía real de cada
swap y red redimensionada); on-board: su solución final por el mismo LP. Misma
metodología que `resultados_finales/` de la mina modelo.

| Tasa | BS modelo (UB) | BS LB | Gap BS | BS ajustado | On-board | OB LB | Gap OB | BS vs OB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{filas}

On-board 0,5C: {infactible}.

Gaps de swap 0,5C y 1C: la LB no demuestra que el año 7 (12 LHD) necesita la segunda
batería por estación (con una batería no se encontró operación, pero la cota de producción
por estación queda en 54,50 contra una meta de 54,44). En 2C el mismo método cierra al 1,7 %.

## Contenido

- `resumen_swap_vs_ob_dch.xlsx`: hojas Resumen, Costos (variantes por tasa), Infraestructura
  (evolución por estación y total), Infraestructura anual, Energía (MWh del horizonte),
  Degradación, Consumer (todas las tablas de consumer.py de los dos lados) y Notas.
- `costo_total_dch.png`: BS ajustado vs OB, desagregado.
- `caso_<tasa>/`: `comparacion/`, `battery_swapping_modelo/` (json_plotter, antes del ajuste),
  `battery_swapping_ajustado/` y `on_board/` (json_plotter de Elmo_ob); ver el LEEME.md de
  cada caso.

## Regenerar

Cada solución necesita `parameters.json` y `consumer.log`:

    python -u escribir_parameters.py --n_years 11 --days_per_year 2 --consumption_model wp2 \\
        --pause_scheme dch data/Tesis_final/DCH/Bateria_482/Costo_fijo/<tasa>/RED <solucion>
    python consumer.py <solucion> > <solucion>/../consumer.log
    (Elmo_ob) python -u escribir_parameters.py --data_folder \\
        data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo/<tasa>/P_red --n_years 11 \\
        --days_per_year 2 --consumption_model wp2 --pause_scheme dch --free_charging \\
        --free_maintenance <solucion OB>
    python cost_comparisons/resultados_finales_dch.py --jobs 6
"""


def escribir_leeme_general(res, path):
    filas = []
    for r in res:
        c = r["costos"]
        s_a = c["Swap ajustado"]["total_cost"]
        if r["ob"]:
            o_m, o_c = c["OB modelo"]["total_cost"], c["OB comparable"]["total_cost"]
            ob = "{:,.0f} | {:,.0f} | {:.2%} | {:+.2%}".format(o_c, r["ob"]["lb"], gap(o_m, r["ob"]["lb"]),
                                                             s_a / o_c - 1)
        else:
            ob = "infactible | – | – | –"
        filas.append("| {} | {:,.0f} | {:,.0f} | {:.2%} | {:,.0f} | {} |".format(
            r["clave"].replace("05C", "0,5C"), r["ub"], r["lb"], gap(r["ub"], r["lb"]), s_a, ob))
    path.write_text(LEEME_GENERAL.format(filas="\n".join(filas), infactible=OB_INFACTIBLE), encoding="utf-8")


# ------------------------------------------------------------------ main --
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--casos", default=",".join(FINALES))
    ap.add_argument("--anio", type=int, default=7,
                    help="año en régimen (default 7: meta de producción máxima, 12 LHD activos y toda la "
                         "infraestructura instalada en las tres tasas)")
    ap.add_argument("--sin_json_plotter", action="store_true")
    ap.add_argument("--solo_json_plotter", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    claves = a.casos.split(",")

    if not a.solo_json_plotter:
        res = []
        for tasa in claves:
            r = procesar(tasa)
            res.append(r)
            carpeta = OUT / "caso_{}".format(tasa)
            (carpeta / "comparacion").mkdir(parents=True, exist_ok=True)
            (carpeta / "battery_swapping_ajustado").mkdir(exist_ok=True)
            figura_costos(r, carpeta / "comparacion" / "costos_swap_vs_ob.png")
            figura_infra(r, carpeta / "comparacion" / "infraestructura_anual.png")
            figura_degradacion(r, carpeta / "comparacion" / "degradacion.png")
            for d in DIAS:
                figura_regimen(r, a.anio, d, carpeta / "comparacion" / "regimen_Y{}_d{}.png".format(a.anio, d))
            figuras_ajustado(r, carpeta / "battery_swapping_ajustado")
            c = r["costos"]
            s_a = c["Swap ajustado"]["total_cost"]
            o_c = c["OB comparable"]["total_cost"] if r["ob"] else None
            (carpeta / "LEEME.md").write_text(LEEME.format(
                titulo=r["titulo"], s_m=c["Swap modelo"]["total_cost"], s_e=c["Swap ajuste energía"]["total_cost"],
                s_a=s_a, o_c="{:,.2f}".format(o_c) if o_c else "infactible",
                dif="{:+.2%}".format(s_a / o_c - 1) if o_c else "–", lb=r["lb"], lb_fuente=r["lb_fuente"],
                gap=gap(r["ub"], r["lb"]), anio=a.anio,
                ob_txt=("LB {:,.2f} ({}), gap {:.2%}.".format(r["ob"]["lb"], r["ob"]["fuente"],
                                                               gap(c["OB modelo"]["total_cost"], r["ob"]["lb"]))
                        if r["ob"] else OB_INFACTIBLE + "."),
                swap_sol=r["swap_sol"], ob_sol=r["ob"]["sol"] if r["ob"] else "-"), encoding="utf-8")
            print("[{}] swap modelo {:,.0f} -> ajustado {:,.0f} | OB {} | controles {:+.2f} / {}".format(
                tasa, r["ub"], s_a, "{:,.0f} ({:+.2%})".format(o_c, s_a / o_c - 1) if o_c else "infactible",
                r["control_swap"], "{:+.2f}".format(r["control_ob"]) if r["control_ob"] is not None else "-"),
                flush=True)
        escribir_excel(res, OUT / "resumen_swap_vs_ob_dch.xlsx")
        if len(res) == len(FINALES):
            figura_total(res, OUT / "costo_total_dch.png")
            escribir_leeme_general(res, OUT / "LEEME.md")

    if not a.sin_json_plotter:
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(correr_json_plotter, *t): t for t in trabajos_json_plotter(claves)}
            for f in as_completed(futs):
                destino, n, fallas = f.result()
                print("json_plotter -> {} ({} png){}".format(
                    Path(destino).relative_to(OUT), n, "" if not fallas else " | fallaron: " + "; ".join(fallas)),
                    flush=True)


if __name__ == "__main__":
    main()
