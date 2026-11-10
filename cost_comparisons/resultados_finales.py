#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Resultados finales v4 de Mina_modelo: battery swapping contra carga on-board en
los 8 casos, con la solucion final del L-shaped para swap. Todo se escribe en
cost_comparisons/resultados_finales/:

  resumen_swap_vs_ob_v4.xlsx      costos, infraestructura, energia, degradacion
                                  y las tablas del consumer de los dos lados
  costo_total_8_casos.png         swap ajustado contra on-board, desagregado
  costo_total_base_norte.png      lo mismo solo para base norte: red, red + gen,
                                  red + gen + BESS
  despacho_<escenario>_<estacion>.png
                                  base norte, año 5: despacho OB y swap ajustado de
                                  solo red (invierno), red + gen + BESS (invierno y
                                  verano) y red + gen (otoño); ver FIGURAS_DESPACHO
  caso_<n>_<escenario>/
    comparacion/                  costos, infraestructura por año y degradacion,
                                  swap ajustado contra OB
    battery_swapping_modelo/      json_plotter de esta rama sobre la solucion
                                  del L-shaped (lo que decidio el modelo)
    battery_swapping_ajustado/    despacho (formato de plot_despacho_swap_vs_ob_v4.py)
                                  y energia de swap despues de los dos ajustes
    on_board/                     json_plotter de Elmo_ob sobre la solucion OB
    LEEME.md

SWAP. La solucion final de cada caso es la de menor UB del L-shaped (lshaped_A2,
su mejora/, lshaped_A3 o lshaped_A4; FINALES abajo). La LB es la mayor de las
corridas lshaped_A2/A3/A4 del caso (lshaped_A del caso 5 no: P_pot sin cota).
Cada solucion necesita parameters.json y consumer.log (escribir_parameters.py y
consumer.py, ver LEEME.md).

AJUSTES DE SWAP (los de comparar_swap_ob_v4.py, que se reusan tal cual, con sus
controles; el script aborta si algo no cuadra):
  1. energia real de cada swap (ajuste_energia_swap.py);
  2. generacion, BESS, red y punta redimensionadas para esa carga real
     (redimensionar_gen_bess.py). Es el costo comparable de swap.
  Controles: los terminos suman el COSTO TOTAL del consumer y este es la UB; la
  inversion abierta reproduce la del consumer; el LP con la carga de la propia
  solucion reproduce lo que pago el modelo (swap y OB).

ON-BOARD. Las mismas soluciones y la misma lectura que comparar_swap_ob_v4.py
(git origin/carga_ob_multiaño para costos; el disco de ../Elmo_ob, que es el
mismo commit, para graficos e infraestructura). El costo comparable de OB pasa
por el mismo LP con su propia carga.

    python cost_comparisons/resultados_finales.py [--jobs 8] [--sin_json_plotter]
        [--solo_json_plotter]
"""
import argparse
import importlib.util
import json
import re
import sys
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.gridspec as gridspec
import matplotlib.patches
import matplotlib.patheffects as pe
import matplotlib.ticker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ajuste_energia_swap import ajustar
from comparar_swap_ob_v4 import (CASOS as CASOS_V4, HERE, REPO_ROOT, STACK_ORDER, V4,
                                 con_lp, lado_ob, lado_swap, miles, reconciliar,
                                 redimensionado_ob)
from plot_despacho_det import DIAS_CON_CARGO, intervalos_punta
from plot_despacho_swap_vs_ob_v4 import dibujar as dibujar_despacho
from redimensionar_gen_bess import TERMINOS, _por_intervalo, _u, carga_de_solucion, redimensionar

OUT = HERE / "resultados_finales"
OB_REPO = REPO_ROOT.parent / "Elmo_ob"
OB_REF = "origin/carga_ob_multiaño"
P_SSEE_STEP = 500.0     # kW por modulo de subestacion (functions.P_SSEE_STEP)

B, E = "Produccion_base", "Produccion_escalada20"
# caso: (carpeta del caso swap, carpeta del L-shaped con la solucion final, nombre)
FINALES = {
    "1": (B + "/zona_norte/05C/RED", "lshaped_A2", "base_norte_05C_RED"),
    "2": (B + "/zona_norte/05C/RED_GEN_BESS", "lshaped_A2/mejora", "base_norte_05C_RED_GEN_BESS"),
    "3": (B + "/zona_norte/05C/RED_GEN", "lshaped_A2/mejora", "base_norte_05C_RED_GEN"),
    "4": (E + "/zona_norte/05C/RED", "lshaped_A2/mejora", "escalada_norte_RED"),
    "5": (E + "/zona_norte/05C/RED_GEN_BESS", "lshaped_A2/mejora", "escalada_norte_RED_GEN_BESS"),
    "6": (B + "/zona_centro/05C/RED_GEN_BESS", "lshaped_A2/mejora", "base_centro_05C_RED_GEN_BESS"),
    "7": (B + "/zona_norte/1C/RED_GEN_BESS", "lshaped_A3", "base_norte_1C_RED_GEN_BESS"),
    "8": (B + "/zona_norte/2C/RED_GEN_BESS", "lshaped_A4", "base_norte_2C_RED_GEN_BESS"),
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


def escalar(data, *ejes):
    for e in ejes:
        data = data.get(e, {}) if isinstance(data, dict) else {}
    return float(data) if not isinstance(data, dict) else sum(float(v) for v in data.values())


def infraestructura(sol, years, swap, params):
    """Infraestructura por año de una solucion (los dos lados tienen una estacion)."""
    J = lambda n: leer_json(sol, n)
    st = ("k", "station_1", "y")
    x, ch = por_anio(J("X.json"), *st), por_anio(J("N_chargers.json"), *st)
    naves = por_anio(J("N_bays.json"), *st) if swap else ch   # OB: una nave por cargador
    pool = por_anio(J("N_batteries.json"), *st) if swap else {y: 0.0 for y in years}
    flota = por_anio(J("n_battery_fleet.json"), "y") if swap else \
        {y: float(len(params.get("lhd_set") or [])) for y in years}
    ppot = por_anio(J("P_pot.json"), "y")
    filas = [{"Año": y, "Estaciones": x.get(y, 0.0), "Naves": naves.get(y, 0.0),
              "Cargadores": ch.get(y, 0.0), "Baterías pool": pool.get(y, 0.0),
              "Flota de baterías": flota.get(y, 0.0), "P_pot [kW]": ppot.get(y, 0.0)}
             for y in years]
    g = J("G_g.json").get("g", {})
    p_max = params.get("p_max_g", {})
    p_max = p_max.get("_1", p_max)
    fijo = {"Módulos subestación": escalar(J("n_ssee_k.json"), "k"),
            "G_kw": {k: float(v) * float(p_max.get(k, 0.0)) for k, v in g.items()},
            "H": escalar(J("H.json"))}
    return filas, fijo


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
    tot = {"Carga [MWh]": 0.0, "Red [MWh]": 0.0, "Generación usada [MWh]": 0.0,
           "Descarga BESS [MWh]": 0.0}
    anual = {}
    for (y, d), v in series.items():
        a = anual.setdefault(y, {"carga": 0.0, "red": 0.0, "gen": 0.0})
        for k, col in (("carga", "Carga [MWh]"), ("red", "Red [MWh]"), ("gen", "Generación usada [MWh]")):
            tot[col] += float(np.sum(v[k])) * f
            a[k] += float(np.sum(v[k])) * f
        tot["Descarga BESS [MWh]"] += float(np.sum(np.maximum(v["bat"], 0))) * f
    tot["Penetración renovable"] = 1 - tot["Red [MWh]"] / tot["Carga [MWh]"] if tot["Carga [MWh]"] else 0.0
    return tot, anual


def despacho_lp(lp, T):
    """Series de redimensionar(series=True) -> {(y, d): {carga, red, gen, bat}} (kW)."""
    return {k: {"carga": v["carga"], "red": v["red"], "bat": v["bat"],
                "gen": sum(v["gen"].values()) if v["gen"] else np.zeros(T)}
            for k, v in lp["series"].items()}


def mejor_lb(caso_dir):
    lbs = []
    for r in sorted((REPO_ROOT / V4 / caso_dir).glob("lshaped_A[234]/resumen.json")):
        d = json.loads(r.read_text())
        if d.get("lb_final") is not None:
            lbs.append((float(d["lb_final"]), r.parent.name))
    return max(lbs) if lbs else (float("nan"), "-")


# --------------------------------------------------------------- calculo --
def procesar(clave):
    caso_dir, sub, nombre = FINALES[clave]
    base = "{}/{}/{}".format(V4, caso_dir, sub)
    ub = json.loads((REPO_ROOT / base / "resumen.json").read_text())["ub_final"]
    cfg = {"sol": base + "/solucion", "consumer": base + "/consumer.log", "ub": ub}
    ob_cfg = CASOS_V4[clave]["ob"]
    print("[{}] swap {} | OB {}/{}".format(clave, cfg["sol"], ob_cfg["caso"], ob_cfg["sol"]), flush=True)

    c_s, inv_s, deg_s, ub_s, flota, aj, redim, params = lado_swap(cfg)
    modelo = reconciliar("swap " + clave, c_s, inv_s, ub_s)
    energia_aj = dict(modelo)
    energia_aj["grid_energy_cost"] = aj["central"]
    energia_aj["total_cost"] = modelo["total_cost"] - modelo["grid_energy_cost"] + aj["central"]
    swap_aj = con_lp(modelo, redim["lp_carga_real"])

    c_o, inv_o, deg_o, ub_o, n_lhd, ub_fuente = lado_ob(ob_cfg, OB_REF)
    ob_modelo = reconciliar("OB " + clave, c_o, inv_o, ub_o)
    redim_ob = redimensionado_ob(ob_cfg, OB_REF, params, ob_modelo)
    ob = con_lp(ob_modelo, redim_ob["lp_carga_ob"])

    # Series para los graficos y la hoja de energia (el ajuste y el LP otra vez,
    # ahora guardando el despacho). Swap ajustado y OB se dibujan con el despacho
    # del LP (el de menor ciclado del BESS a igual costo, ver redimensionar).
    sol_s = REPO_ROOT / cfg["sol"]
    sol_o = OB_REPO / ob_cfg["caso"] / ob_cfg["sol"]
    years = sorted(int(y) for y in params["years"])
    T = len(params["time_intervals_set"])
    dt, esc = float(params["delta_t"]), float(params["scaling_factor_op_cost"])
    out_aj, _ = ajustar(sol_s)
    lp = redimensionar(params, {k: v["dem_aj"] for k, v in out_aj.items()}, series=True)
    lp_ob = redimensionar(params, carga_de_solucion(lambda n: leer_json(sol_o, n), years, T),
                          series=True)
    for nombre_lp, a, b in (("swap", lp, redim["lp_carga_real"]), ("OB", lp_ob, redim_ob["lp_carga_ob"])):
        if abs(a["suma"] - b["suma"]) > 1e-3:
            raise SystemExit("caso {}: el LP con series de {} no reproduce el redimensionamiento".format(
                clave, nombre_lp))
    params_ob = json.loads((sol_o / "parameters.json").read_text(encoding="utf-8"))
    desp = {
        "modelo": {k: {"carga": v["dem"], "red": v["red"], "gen": v["gen"], "bat": v["bat"]}
                   for k, v in out_aj.items()},
        "ajuste_energia": {k: {"carga": v["dem_aj"], "red": v["red_aj"], "gen": v["gen_aj"],
                               "bat": v["bat_aj"]} for k, v in out_aj.items()},
        "ajustado": despacho_lp(lp, T),
        "ob": despacho_lp(lp_ob, T),
    }
    infra_s, fijo_s = infraestructura(sol_s, years, True, params)
    infra_o, fijo_o = infraestructura(sol_o, years, False, params_ob)
    lb, lb_fuente = mejor_lb(caso_dir)
    return {
        "clave": clave, "nombre": nombre, "titulo": CASOS_V4[clave]["titulo"],
        "swap_sol": cfg["sol"], "swap_consumer": cfg["consumer"],
        "ob_sol": "{}:{}/{}".format(OB_REF, ob_cfg["caso"], ob_cfg["sol"]),
        "ob_sol_disco": str(sol_o), "ob_consumer": str(OB_REPO / ob_cfg["caso"] / "consumer.log"),
        "ub": ub, "lb": lb, "lb_fuente": lb_fuente, "ob_ub_fuente": ub_fuente,
        "costos": {"Swap modelo": modelo, "Swap ajuste energía": energia_aj,
                   "Swap ajustado": swap_aj, "OB modelo": ob_modelo, "OB comparable": ob},
        "control_swap": redim["control"], "control_ob": redim_ob["control"],
        "lp": {"swap_modelo": redim["lp_carga_modelo"], "swap_ajustado": redim["lp_carga_real"],
               "ob": redim_ob["lp_carga_ob"]},
        "ajuste": aj["energia_mwh"], "deg": {"Swap": deg_s, "OB": deg_o},
        "infra": {"Swap": infra_s, "OB": infra_o}, "fijo": {"Swap": fijo_s, "OB": fijo_o},
        "despacho": desp, "esc_dt": esc * dt, "dt": dt, "P_pot_aj": lp["P_pot"], "P_pot_ob": lp_ob["P_pot"],
        "tablas": {"Swap": tablas_consumer(REPO_ROOT / cfg["consumer"]),
                   "OB": tablas_consumer(OB_REPO / ob_cfg["caso"] / "consumer.log")},
    }


# ----------------------------------------------------------------- excel --
def hojas(res):
    resumen, costos, infra, anual, ener, deg, cons = [], [], [], [], [], [], []
    for r in res:
        c = r["costos"]
        s_m, s_e, s_a = (c[k]["total_cost"] for k in ("Swap modelo", "Swap ajuste energía", "Swap ajustado"))
        o_m, o_c = c["OB modelo"]["total_cost"], c["OB comparable"]["total_cost"]
        resumen.append({
            "Caso": int(r["clave"]), "Escenario": r["titulo"],
            "Swap UB modelo [USD]": s_m, "Swap LB [USD]": r["lb"],
            "Swap gap": (s_m - r["lb"]) / s_m,
            "Swap ajuste energía [USD]": s_e, "Swap ajustado [USD]": s_a,
            "OB modelo [USD]": o_m, "OB comparable [USD]": o_c, "Fuente UB OB": r["ob_ub_fuente"],
            "Swap vs OB (modelo)": s_m / o_m - 1, "Swap vs OB (ajuste energía)": s_e / o_c - 1,
            "Swap vs OB (ajustado)": s_a / o_c - 1,
            "Control LP swap [USD]": r["control_swap"], "Control LP OB [USD]": r["control_ob"],
            "Solución swap": r["swap_sol"], "LB swap (corrida)": r["lb_fuente"],
            "Solución OB": r["ob_sol"],
        })
        for variante, fila in c.items():
            costos.append({"Caso": int(r["clave"]), "Escenario": r["titulo"], "Variante": variante,
                           **{ETIQUETAS[k]: fila[k] for k in ETIQUETAS}})
        for tec in ("Swap", "OB"):
            filas, fijo = r["infra"][tec], r["fijo"][tec]
            serie = lambda col: {f["Año"]: f[col] for f in filas}
            fila = {"Caso": int(r["clave"]), "Escenario": r["titulo"],
                    "Tecnología": "Battery swapping" if tec == "Swap" else "On-board",
                    "Estaciones": evolucion(serie("Estaciones")), "Naves": evolucion(serie("Naves")),
                    "Cargadores": evolucion(serie("Cargadores")),
                    "Baterías pool": evolucion(serie("Baterías pool")),
                    "Flota de baterías": evolucion(serie("Flota de baterías")),
                    "Módulos subestación": fijo["Módulos subestación"],
                    "Subestación [kW]": fijo["Módulos subestación"] * P_SSEE_STEP,
                    "P_pot máx [kW]": max(serie("P_pot [kW]").values())}
            for g, kw in sorted(fijo["G_kw"].items()):
                fila["{} [kW]".format(g)] = kw
            fila["BESS [unidades]"] = fijo["H"]
            if tec == "Swap":
                la = r["lp"]["swap_ajustado"]
                for g, kw in sorted(la["G_kw"].items()):
                    fila["{} ajustado [kW]".format(g)] = kw
                fila["BESS ajustado [unidades]"] = la["H"]
                fila["P_pot máx ajustado [kW]"] = max(r["P_pot_aj"].values())
            infra.append(fila)
            for f in filas:
                anual.append({"Caso": int(r["clave"]), "Tecnología": fila["Tecnología"], **f})
        for variante, clave_d in (("Swap modelo", "modelo"), ("Swap ajuste energía", "ajuste_energia"),
                                  ("Swap ajustado", "ajustado"), ("OB", "ob")):
            tot, _ = energia(r["despacho"][clave_d], r["esc_dt"])
            ener.append({"Caso": int(r["clave"]), "Escenario": r["titulo"], "Variante": variante, **tot})
        for tec, filas in r["deg"].items():
            for anio, b_ini, d_fin, rep in filas:
                deg.append({"Caso": int(r["clave"]),
                            "Tecnología": "Battery swapping" if tec == "Swap" else "On-board",
                            "Año": anio, "Capacidad inicio [kWh]": b_ini, "Capacidad fin [kWh]": d_fin,
                            "Reemplazo": "SI" if rep else "NO"})
        for tec, tablas in r["tablas"].items():
            for titulo, enc, filas in tablas:
                if titulo == "INPUTS" or titulo.startswith("DETALLE"):
                    continue
                for f in filas:
                    for col, v in zip(enc[1:], f[1:]):
                        cons.append({"Caso": int(r["clave"]),
                                     "Tecnología": "Battery swapping" if tec == "Swap" else "On-board",
                                     "Tabla": titulo, "Fila": f[0], "Columna": col,
                                     "Valor": numero(v), "Texto": v})
    cons = pd.DataFrame(cons)[["Caso", "Tecnología", "Tabla", "Fila", "Columna", "Valor", "Texto"]]
    out = {"Resumen": pd.DataFrame(resumen), "Costos": pd.DataFrame(costos),
           "Infraestructura": pd.DataFrame(infra), "Infraestructura anual": pd.DataFrame(anual),
           "Energía": pd.DataFrame(ener), "Degradación": pd.DataFrame(deg),
           "Consumer": cons}
    if set(BASE_NORTE) <= {r["clave"] for r in res}:
        out["Tabla base norte"] = tabla_infraestructura(res)
        out["Costos base norte"] = costos_escenarios(res)
    if set(ESCALADA) <= {r["clave"] for r in res}:
        out["Tabla escalada norte"] = tabla_infraestructura(res, ESCALADA)
        out["Costos escalada norte"] = costos_escenarios(res, ESCALADA)
        if {"1", "4"} <= {r["clave"] for r in res}:
            por_clave = {r["clave"]: r for r in res}
            out["Efecto producción"] = tabla_produccion(por_clave["1"], por_clave["4"])
    return out


def _recursos(k):
    """'RED_GEN_BESS' -> {'RED', 'GEN', 'BESS'} del escenario k."""
    return set(FINALES[k][0].split("/")[-1].split("_"))


TECNOLOGIAS = (("Battery swapping", "Swap"), ("Carga on-board", "OB"))


def tabla_infraestructura(res, claves=None):
    """Infraestructura e indicadores de operacion de base norte por escenario y
    tecnologia (tabla tab:resultados-suministro-energia). Generacion, BESS y
    energia son las del LP de redimensionamiento (swap ajustado y OB comparable),
    las mismas de los costos; cargadores y baterias extra, el maximo del horizonte."""
    por_clave = {r["clave"]: r for r in res}
    filas = []
    for k in claves or BASE_NORTE:
        r = por_clave[k]
        p = json.loads((REPO_ROOT / r["swap_sol"] / "parameters.json").read_text(encoding="utf-8"))
        gen = "GEN" in _recursos(k)
        h = (p.get("storage_set") or [None])[0] if "BESS" in _recursos(k) else None
        for tec, lado in TECNOLOGIAS:
            infra = r["infra"][lado]
            lp = r["lp"]["swap_ajustado" if lado == "Swap" else "ob"]
            tot, _ = energia(r["despacho"]["ajustado" if lado == "Swap" else "ob"], r["esc_dt"])
            filas.append({
                "Escenario operacional": "{} {}".format(tec, ESCENARIOS[k][0]),
                "Producción": produccion(k),
                "Tasa de carga": tasa(k),
                "Potencia por cargador [kW]": float(p["p_charger"]),
                "Zona": zona(k),
                "Cantidad de cargadores": max(f["Cargadores"] for f in infra),
                "Cantidad de baterías extra": max(f["Baterías pool"] for f in infra) if lado == "Swap" else None,
                "Potencia subestación [kW]": r["fijo"][lado]["Módulos subestación"] * P_SSEE_STEP,
                "Generación FV instalada [kW]": lp["G_kw"].get("Solar_PV", 0.0) if gen else None,
                "Generación eólica instalada [kW]": lp["G_kw"].get("Wind", 0.0) if gen else None,
                "BESS [kWh]": lp["H"] * float(_u(p["a_max_h"])[h]) if h else None,
                "BESS [kW]": lp["H"] * float(_u(p["p_max_h"])[h]) if h else None,
                "Energía desde red total [MWh]": tot["Red [MWh]"],
                "Penetración renovable": tot["Penetración renovable"] if gen else None,
                "Reemplazos de flota de baterías": sum(1 for fila in r["deg"][lado] if fila[3]),
            })
    return _sin_columnas_fijas(pd.DataFrame(filas), claves)


def _sin_columnas_fijas(df, claves):
    """Tasa y zona solo van en las sensibilidades que las cambian."""
    claves = claves or BASE_NORTE
    if len({tasa(k) for k in claves}) == 1:
        df = df.drop(columns=["Tasa de carga", "Potencia por cargador [kW]"])
    if len({zona(k) for k in claves}) == 1:
        df = df.drop(columns=["Zona"])
    return df


def costos_escenarios(res, claves=None):
    """Costo total del horizonte de base norte por escenario, tecnologia y
    componente (las categorias de costo_total_base_norte.png), en USD VP. None
    donde el componente no existe en el escenario o la tecnologia."""
    por_clave = {r["clave"]: r for r in res}
    no_aplica = {"Baterías (pool de swap)": lambda k, lado: lado == "OB",
                 "Generación renovable": lambda k, lado: "GEN" not in _recursos(k),
                 "BESS": lambda k, lado: "BESS" not in _recursos(k)}
    filas = []
    for k in claves or BASE_NORTE:
        for tec, lado in TECNOLOGIAS:
            c = por_clave[k]["costos"]["Swap ajustado" if lado == "Swap" else "OB comparable"]
            fila = {"Escenario operacional": "{} {}".format(tec, ESCENARIOS[k][0]), "Tasa de carga": tasa(k),
                    "Potencia por cargador [kW]": potencia_cargador(por_clave[k]), "Zona": zona(k)}
            for grupo, _, etiqueta in STACK_8_CASOS:
                na = no_aplica.get(etiqueta, lambda k, lado: False)(k, lado)
                fila[etiqueta + " [USD]"] = None if na else sum(c[x] for x in grupo)
            fila["Costo total [USD]"] = c["total_cost"]
            filas.append(fila)
    return _sin_columnas_fijas(pd.DataFrame(filas), claves)


ENCABEZADO_TEX = r"""\begin{table}[htbp]
\centering
\caption{<<CAPTION>>}
\label{<<LABEL>>}
\scriptsize
\setlength{\tabcolsep}{2pt}
\renewcommand{\arraystretch}{1.3}
\newcommand{\cab}[1]{\hspace{0pt}\textbf{#1}}
\begin{tabular}{|>{\raggedright\arraybackslash}m{1.8cm}|*{<<N>>}{>{\centering\arraybackslash}m{\dimexpr(\linewidth-1.8cm-<<SEP>>\tabcolsep-<<REG>>\arrayrulewidth)/<<N>>\relax}|}}
\hline
\cab{Escenario operacional} &
<<CABECERAS>> \\
\hline
<<FILAS>>
\end{tabular}
\end{table}
"""
NOMBRES_TEX = {"solo red": "solo red", "red + generación": "red y generación",
               "red + generación + BESS": "red, generación y almacenamiento"}


def _num(v, dec=0):
    return "--" if v is None or pd.isna(v) else \
        "{:,.{}f}".format(v, dec).replace(",", "X").replace(".", ",").replace("X", ".")


def _tabla_tex(path, caption, label, cabeceras, df, celdas, con_produccion=False, con_tasa=False,
               con_zona=False):
    """Tabla LaTeX con el formato de la tesis: una fila por escenario y tecnologia;
    celdas(fila) -> textos de las columnas de valores. con_produccion agrega el
    nivel de produccion al nombre de cada fila; con_tasa lo cambia por la tasa de
    carga y la potencia del cargador (todas las filas del mismo escenario);
    con_zona agrega la zona a los escenarios con generacion."""
    lineas = []
    for f in df.to_dict("records"):
        esc = f["Escenario operacional"]
        tec = r"\textit{Battery swapping}" if esc.startswith("Battery") else r"Carga \textit{on-board}"
        esc = esc.replace("Battery swapping ", "").replace("Carga on-board ", "")
        if con_tasa:
            nombre = "{} ({} kW)".format(f["Tasa de carga"], _num(f["Potencia por cargador [kW]"]))
        else:
            nombre = NOMBRES_TEX[esc] + (", producción " + f["Producción"] if con_produccion else "")
            if con_zona:
                nombre += " (norte y centro)" if esc == "solo red" else ", " + f["Zona"]
        lineas.append(r"{} {} & {} \\ \hline".format(tec, nombre, " & ".join(celdas(f))))
    n = len(cabeceras)
    texto = (ENCABEZADO_TEX.replace("<<CAPTION>>", caption).replace("<<LABEL>>", label)
             .replace("<<N>>", str(n)).replace("<<SEP>>", str(2 * (n + 1))).replace("<<REG>>", str(n + 2))
             .replace("<<CABECERAS>>", " &\n".join(r"\cab{" + c + "}" for c in cabeceras))
             .replace("<<FILAS>>", "\n".join(lineas)))
    if con_tasa:
        texto = texto.replace(r"\cab{Escenario operacional}", r"\cab{Tecnología y tasa de carga}")
    path.write_text(texto, encoding="utf-8")


def escribir_tabla_tex(df, path, caso="base norte", caption=None, label=None, con_tasa=False, con_zona=False):
    def celdas(f):
        fv = f["Generación FV instalada [kW]"]
        gen = "--" if fv is None or pd.isna(fv) else \
            r"FV: {}\newline Eól.: {}".format(_num(fv), _num(f["Generación eólica instalada [kW]"]))
        bess = "--" if f["BESS [kWh]"] is None or pd.isna(f["BESS [kWh]"]) else \
            "{} / {}".format(_num(f["BESS [kWh]"]), _num(f["BESS [kW]"]))
        pen = f["Penetración renovable"]
        return [_num(f["Cantidad de cargadores"]), _num(f["Cantidad de baterías extra"]),
                _num(f["Potencia subestación [kW]"]), gen, bess, _num(f["Energía desde red total [MWh]"]),
                _num(None if pen is None or pd.isna(pen) else 100 * pen, 1),
                _num(f["Reemplazos de flota de baterías"])]
    _tabla_tex(path, caption or "Dimensionamiento de infraestructura e indicadores de operación por "
               "escenario, caso {}.".format(caso), label or "tab:resultados-suministro-energia" + (
                   "" if caso == "base norte" else "-" + caso.replace(" ", "-")),
               ["Cantidad de cargadores", "Cantidad de baterías extra", r"Potencia subes\-tación [kW]",
                r"Genera\-ción instalada [kW]", "BESS [kWh / kW]", "Energía desde red total [MWh]",
                r"Penetra\-ción renovable [\%]", r"Cantidad de reempla\-zos de flota de baterías"],
               df, celdas, con_produccion=caption is not None and not (con_tasa or con_zona), con_tasa=con_tasa,
               con_zona=con_zona)


def escribir_costos_tex(df, path, caso="base norte", caption=None, label=None, con_tasa=False,
                        con_zona=False):
    cols = [c for c in df.columns if c.endswith(" [USD]")]
    cab = {"Estaciones y subestación": r"Estaciones y subes\-tación", "Cargadores": r"Carga\-dores",
           "Baterías (pool de swap)": r"Baterías \textit{pool} de \textit{swap}",
           "Generación renovable": r"Genera\-ción renovable", "BESS": "BESS",
           "Potencia punta": "Potencia punta", "Energía de red": "Energía de red",
           "Reemplazo de baterías": r"Reempla\-zo de baterías", "Costo total": "Costo total"}
    _tabla_tex(path, caption or "Costo total del horizonte por escenario y componente, caso {} "
               "[kUSD, valor presente de 10 años].".format(caso), label or "tab:costos-" + caso.replace(" ", "-"),
               [cab[c[:-len(" [USD]")]] for c in cols], df,
               lambda f: [_num(None if f[c] is None or pd.isna(f[c]) else f[c] / 1000.0) for c in cols],
               con_tasa=con_tasa, con_zona=con_zona)


NOTAS = [
    ("Qué es", "Resultados finales v4 de Mina_modelo, battery swapping (BS) contra carga on-board (OB), "
               "8 casos. Generado por cost_comparisons/resultados_finales.py."),
    ("Swap", "Solución final del L-shaped de cada caso (menor UB; carpeta en la hoja Resumen). LB: la "
             "mayor de las corridas lshaped_A2/A3/A4 del caso."),
    ("Swap modelo", "Costo de la solución tal como la pagó el modelo (= UB, = COSTO TOTAL del consumer)."),
    ("Swap ajuste energía", "Ajuste 1 (ajuste_energia_swap.py): el modelo cobra cada recarga como el "
                            "bloque completo bmin → 100 %; se cambia por la energía real de cada swap."),
    ("Swap ajustado", "Ajuste 2 (redimensionar_gen_bess.py): con la carga real fija, un LP con las "
                      "ecuaciones del modelo vuelve a elegir solar, eólica, BESS, red y punta. Es el "
                      "costo comparable de BS (POST AJUSTE)."),
    ("OB comparable", "Mismo LP con la carga de la solución OB: reproduce lo que pagó OB o lo mejora "
                      "(Control LP OB = modelo − LP)."),
    ("Controles", "El script aborta si: los términos no suman el COSTO TOTAL del consumer, el COSTO "
                  "TOTAL no es la UB, la inversión abierta no reproduce la del consumer, o el LP con la "
                  "carga de la propia solución paga más que el modelo."),
    ("Energía", "MWh del horizonte (días muestreados × factor de escala). Swap modelo: despacho de la "
                "solución; ajuste energía: el mismo despacho sin el sobrante de cada bloque; ajustado: "
                "despacho del LP redimensionado; OB: despacho del mismo LP con la carga OB (mismo "
                "costo que la solución OB). En los dos LP, entre los despachos de igual costo, el de "
                "menor energía por el BESS: el modelo no cobra el ciclado (el ciclo carga-descarga "
                "rinde 1 con A -= P_bat/eta·dt) y la solución puede alternar carga y descarga."),
    ("Consumer", "Todas las tablas que imprime consumer.py para cada solución (sin INPUTS ni el detalle "
                 "por swap), en formato largo. Las de OB vienen del consumer de Elmo_ob."),
    ("Infraestructura", "Evolución 'a → b (año y)'. OB: una nave por cargador; la flota de baterías de "
                        "OB es una por LHD. Subestación: módulos de 500 kW."),
    ("Descuento", "Valores presentes con year_position 1-indexada, df(y) = 1/(1+r)^y."),
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
                es_pct = col in ("Swap gap", "Penetración renovable") or col.startswith("Swap vs OB")
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
    variantes = ["Swap ajustado", "OB comparable"]
    filas = [r["costos"][v] for v in variantes]
    xs = np.arange(len(filas))
    tot = np.array([f["total_cost"] for f in filas]) / 1000.0
    ymax = tot.max() * 1.12
    fig, ax = plt.subplots(figsize=(7, 5.2))
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
    ax.set_xticklabels(["BS ajustado", "On-board"])
    ax.set_ylim(0, ymax)
    ax.set_ylabel("Costo total [kUSD, VP 10 años]")
    ax.set_title("Caso {}: {}\nBS ajustado vs on-board: {:+.2%}".format(
        r["clave"], r["titulo"], tot[0] / tot[1] - 1), fontsize=11)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    h, l = ax.get_legend_handles_labels()
    ax.legend(h[::-1], l[::-1], loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_infra(r, out_png):
    cols = ["Naves", "Cargadores", "Flota de baterías", "P_pot [kW]"]
    fig, axes = plt.subplots(1, 4, figsize=(16, 3.8))
    # Escalon desde el inicio de cada año (lo instalado en el año y rige todo el año y).
    escalon = lambda ys, vs: (list(ys) + [ys[-1] + 1], list(vs) + [vs[-1]])
    for ax, col in zip(axes, cols):
        for tec, color, et, dz in (("Swap", COLOR_SWAP, "BS ajustado", 0.0),
                                   ("OB", COLOR_OB, "On-board", 0.04)):
            # Swap: la punta la vuelve a elegir el LP del ajuste; lo demas no cambia.
            serie = (r["P_pot_aj"] if tec == "Swap" and col == "P_pot [kW]"
                     else {x["Año"]: x[col] for x in r["infra"][tec]})
            ys = sorted(serie)
            xs, vs = escalon(ys, [serie[y] + dz * (col != "P_pot [kW]") for y in ys])
            ax.step(xs, vs, where="post", color=color, linewidth=2, label=et)
        ax.set_title(col)
        ax.set_xlabel("Año (inicio)")
        ax.set_xticks(range(1, 12))
        ax.set_xlim(1, 11)
        if col != "P_pot [kW]":
            ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
            ax.set_ylim(0, None)
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.suptitle("Caso {}: infraestructura por año — {}".format(r["clave"], r["titulo"]), fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


LADOS_DEGRADACION = (("Swap", "Battery swapping"), ("OB", "On-board"))


def _panel_degradacion(ax, deg, titulo, b_max):
    x = np.arange(len(deg))
    ax.bar(x, [d[1] for d in deg], 0.6, color=[COLOR_OB if d[3] else COLOR_SWAP for d in deg],
           label="Capacidad al inicio")
    for xi, d in zip(x, deg):   # el año de reemplazo se rotula en la barra, no en la leyenda;
        if d[3]:                 # vertical y dentro, para que no choquen reemplazos seguidos
            ax.text(xi, d[1] / 2, "reemplazo", ha="center", va="center", rotation=90, fontsize=9,
                    color="white", fontweight="bold")
    ax.plot(x, [d[2] for d in deg], "o-", color="#7F7F7F", label="Capacidad al final")
    ax.axhline(b_max, color="#333", linestyle="--", linewidth=1, label="Capacidad nominal")
    ax.axhline(0.8 * b_max, color="#C00000", linestyle="--", linewidth=2.2, zorder=3,
               label="Piso 80 %")
    ax.set_xticks(x)
    ax.set_xticklabels([d[0] for d in deg])
    ax.set_title(titulo)
    ax.set_xlabel("Año")
    ax.set_ylim(0, b_max * 1.1)
    ax.grid(axis="y", alpha=0.3)


def _cerrar_degradacion(fig, axes, titulo, out_png, abajo):
    h = dict(zip(*axes[0].get_legend_handles_labels()[::-1]))
    orden = ["Capacidad al inicio", "Capacidad al final", "Capacidad nominal", "Piso 80 %"]
    fig.legend([h[k] for k in orden], orden, loc="lower center", bbox_to_anchor=(0.5, abajo),
               ncol=len(orden), fontsize=9, frameon=True, edgecolor="gray")
    fig.suptitle(titulo, fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_degradacion(r, out_png, b_max=482.0):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4), sharey=True)
    for ax, (tec, titulo) in zip(axes, LADOS_DEGRADACION):
        _panel_degradacion(ax, r["deg"][tec], titulo, b_max)
    axes[0].set_ylabel("Capacidad por batería [kWh]")
    _cerrar_degradacion(fig, axes, "Degradación de baterías, producción {}".format(produccion(r["clave"])),
                        out_png, -0.08)


def figura_degradacion_produccion(r_base, r_esc, out_png, b_max=482.0):
    """Degradacion con produccion base (arriba) y escalada (abajo), battery swapping a
    la izquierda y on-board a la derecha. Es practicamente igual en todos los
    escenarios de un mismo nivel de produccion (difieren en 2 kWh o menos)."""
    fig, axes = plt.subplots(2, 2, figsize=(13, 7.6), sharey=True)
    for fila, (prod, r) in zip(axes, (("base", r_base), ("escalada", r_esc))):
        for ax, (tec, titulo) in zip(fila, LADOS_DEGRADACION):
            nota = " (cargadores 1C)" if prod == "escalada" and tec == "OB" else ""
            _panel_degradacion(ax, r["deg"][tec], "{}, producción {}{}".format(titulo, prod, nota), b_max)
        fila[0].set_ylabel("Capacidad por batería [kWh]")
    _cerrar_degradacion(fig, axes.ravel(), "Degradación de baterías con producción base y escalada",
                        out_png, -0.04)


def figura_degradacion_tasa(rs, out_png, b_max=482.0):
    """Degradacion por tasa de carga (una columna por caso de rs): battery swapping
    arriba y on-board abajo."""
    fig, axes = plt.subplots(2, len(rs), figsize=(6.2 * len(rs), 7.6), sharey=True)
    for fila, (tec, titulo) in zip(axes, LADOS_DEGRADACION):
        for ax, r in zip(fila, rs):
            _panel_degradacion(ax, r["deg"][tec], "{}, {} ({} kW)".format(
                titulo, tasa(r["clave"]), _num(potencia_cargador(r))), b_max)
        fila[0].set_ylabel("Capacidad por batería [kWh]")
    _cerrar_degradacion(fig, axes.ravel(), "Degradación de baterías por tasa de carga, producción base, "
                        "red + generación + BESS", out_png, -0.04)


ESTILO_DESPACHO = {"font.size": 11, "axes.titlesize": 13, "axes.labelsize": 12,   # el de
                   "xtick.labelsize": 10, "ytick.labelsize": 10, "legend.fontsize": 11}  # la plantilla


ANIO_PICO = 5   # los años 4-7 tienen la meta de produccion maxima; en el 5 ya esta toda la infraestructura
# Dia muestreado de cada estacion: el 15 de enero, abril, julio y octubre (el de
# otoño y el de invierno tienen cargo por potencia).
ESTACIONES = {"verano": 15, "otoño": 105, "invierno": 196, "primavera": 288}
ESTACION_DEL_DIA = {d: e for e, d in ESTACIONES.items()}
# Despachos on-board vs swap ajustado de base norte: (caso, escenario, estacion, archivo).
ESCENARIOS = {"1": ("solo red", "solo_red"), "3": ("red + generación", "red_gen"),   # nombre, archivo
              "2": ("red + generación + BESS", "red_gen_bess"),
              "4": ("solo red", "solo_red"), "5": ("red + generación + BESS", "red_gen_bess"),
              "6": ("red + generación + BESS", "red_gen_bess"),
              "7": ("red + generación + BESS", "red_gen_bess"), "8": ("red + generación + BESS", "red_gen_bess")}
ESCALADA = ("4", "5")   # produccion escalada norte (swap 0,5C, OB 1C): solo red, red + generacion + BESS
TASAS = ("2", "7", "8")   # sensibilidad a la tasa de carga: base norte red + gen + BESS a 0,5C, 1C y 2C
# Sensibilidad a la ubicacion, base 0,5C: solo red (norte; centro solo cambia el perfil de
# generacion alpha_g, asi que sin generacion es el mismo caso) y red + gen + BESS norte y centro.
UBICACION = ("1", "2", "6")


def zona(k):
    """'.../zona_centro/...' -> 'centro'."""
    return FINALES[k][0].split("/")[1].replace("zona_", "")


def dif_bs_ob(r):
    """'BS vs OB: +1,5 %' del costo total ajustado."""
    v = 100 * (r["costos"]["Swap ajustado"]["total_cost"] / r["costos"]["OB comparable"]["total_cost"] - 1)
    return "BS vs OB: {}{} %".format("+" if v >= 0 else "", _num(v, 1))


def tasa(k):
    """'.../05C/...' -> '0,5C'."""
    c = FINALES[k][0].split("/")[2]
    return "0,5C" if c == "05C" else c


def potencia_cargador(r):
    """Potencia por cargador [kW] (p_charger, la misma en swap y on-board)."""
    p = json.loads((REPO_ROOT / r["swap_sol"] / "parameters.json").read_text(encoding="utf-8"))
    return float(p["p_charger"])


def produccion(k):
    return "escalada" if FINALES[k][0].startswith(E) else "base"
# Despachos on-board vs swap ajustado de base norte: (caso, estacion).
FIGURAS_DESPACHO = [("1", "invierno"), ("2", "invierno"), ("2", "verano"), ("3", "otoño")]
ORDEN_DESPACHO = ["Red", "Generación renovable", "BESS descargando", "Demanda total"]
LHD_SOC = "LH518B_1"   # LHD representativo de los graficos de SOC


def figura_despacho(r, out_png, escenario, estacion, anio=ANIO_PICO):
    """On-board y battery swapping (ajustado) lado a lado en el dia muestreado de
    una estacion del año de mayor produccion, con el formato de
    plot_despacho_swap_vs_ob_v4.py."""
    _figura_despacho([(r, None)], out_png, "Despacho de potencia por escenario operacional: {}\naño {}, {}".format(
        escenario, anio, estacion), estacion, anio)


def figura_despacho_ubicacion(filas, out_png, estacion, anio=ANIO_PICO):
    """Despacho de on-board y battery swapping por zona: filas = [(r, zona)]."""
    _figura_despacho(filas, out_png, "Despacho de potencia por ubicación, producción base, red + generación + "
                     "BESS\naño {}, {}".format(anio, estacion), estacion, anio)


def _figura_despacho(filas, out_png, titulo, estacion, anio):
    """Una fila por (r, rotulo) con on-board y battery swapping lado a lado; el
    rotulo (si hay) va en el titulo de cada panel."""
    d = ESTACIONES[estacion]
    ts = np.arange(1, len(filas[0][0]["despacho"]["ajustado"][(anio, d)]["carga"]) + 1, dtype=float)
    with plt.rc_context(ESTILO_DESPACHO):
        fig, axes = plt.subplots(len(filas), 2, figsize=(16.5, 5.4 * len(filas)), sharey=True, squeeze=False)
        marcas, handles = [], {}
        hay = {"Generación renovable": False, "BESS descargando": False}
        for fila, (r, rotulo) in zip(axes, filas):
            lados = (("ob", "On-board", r["P_pot_ob"][anio]), ("ajustado", "Battery swapping", r["P_pot_aj"][anio]))
            for ax, (k, lado, p_pot) in zip(fila, lados):
                v = r["despacho"][k][(anio, d)]
                marca, _ = dibujar_despacho(ax, ts, v["red"], v["gen"], v["bat"], p_pot, anio, d, lado,
                                            r["dt"], fuentes_completas=True, etiqueta_consumo="Demanda total")
                if rotulo:
                    ax.set_title("{}, {}".format(lado, rotulo))
                marcas.append((ax, marca))
                hay["Generación renovable"] |= bool(np.any(v["gen"] > 1e-6))
                hay["BESS descargando"] |= bool(np.any(v["bat"] > 1e-6))
                handles.update(zip(*ax.get_legend_handles_labels()[::-1]))
            fila[0].set_ylabel("Potencia [kW]")
        for ax in axes[-1]:
            ax.set_xlabel("Hora")
        y0, y1 = axes[0][0].get_ylim()
        axes[0][0].set_ylim(y0, y1 * 1.14)
        for ax, m in marcas:
            if m is not None:
                ax.text(m, ax.get_ylim()[1] * 0.985, "cargo por potencia\n18:00–22:00",
                        ha="center", va="top", fontsize=10, color="#B00020", fontweight="bold")
        etiquetas = [e for e in ORDEN_DESPACHO if e in handles and hay.get(e, True)]
        fig.legend([handles[e] for e in etiquetas], etiquetas, loc="lower center",
                   bbox_to_anchor=(0.5, -0.06 / len(filas)), ncol=len(etiquetas), frameon=True, framealpha=0.95,
                   edgecolor="gray")
        fig.suptitle(titulo, fontsize=15)
        fig.tight_layout(rect=[0, 0.02 / len(filas), 1, 1 - 0.05 / len(filas)])
        fig.savefig(out_png, dpi=150, bbox_inches="tight")
        plt.close(fig)


def figura_potencia_peak(rs, out_png):
    """Demanda peak de las estaciones por año (maximo de la carga en los dias
    muestreados), on-board y battery swapping (ajustado), un panel por escenario
    de base norte; formato de plot_peak_power.py."""
    fig, axes = plt.subplots(1, len(rs), figsize=(6.2 * len(rs), 5.6), sharey=True)
    tope = 0.0
    for ax, r in zip(axes, rs):
        for k, color, etiqueta, s, dy in (("ob", "steelblue", "On-board", 160, 12),
                                          ("ajustado", "darkorange", "Battery swapping", 70, -18)):
            D = r["despacho"][k]
            ys = sorted({y for y, _ in D})
            kw = [max(float(np.max(D[(y, d)]["carga"])) for d in ESTACIONES.values()) for y in ys]
            tope = max(tope, max(kw))
            ax.plot(ys, kw, color=color, linewidth=2, zorder=2)
            ax.scatter(ys, kw, color=color, s=s, zorder=3, label=etiqueta)
            for xi, v in zip(ys, kw):
                ax.annotate(miles(v), (xi, v), textcoords="offset points", xytext=(0, dy), ha="center",
                            fontsize=9, fontweight="bold", color=color)
        nombre = ESCENARIOS[r["clave"]][0]
        ax.set_title(nombre[0].upper() + nombre[1:], fontsize=13)
        ax.set_xlabel("Año")
        ax.set_xticks(ys)
        ax.set_xlim(ys[0] - 0.5, ys[-1] + 0.5)
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("Potencia peak [kW]")
    axes[0].set_ylim(0, tope * 1.2)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, -0.06), ncol=len(l), frameon=True,
               framealpha=0.95, edgecolor="gray")
    fig.suptitle("Potencia peak de operación por año", fontsize=15)
    fig.tight_layout(rect=[0, 0.02, 1, 0.97])
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def peak_anual(D):
    """{año: demanda maxima de las estaciones en los dias muestreados [kW]}."""
    return {y: max(float(np.max(D[(y, d)]["carga"])) for d in ESTACIONES.values()) for y in sorted({y for y, _ in D})}


def figura_peak_produccion(r_base, r_esc, out_png):
    """Potencia peak de operacion por año con produccion base y escalada, un
    panel por tecnologia; formato de plot_peak_power.py."""
    _figura_peak(((r_base, "#4C72B0", "Producción base", 160, -18),
                  (r_esc, "#C44E52", "Producción escalada", 70, 12)),
                 {"ajustado": " (0,5C)", "ob": " (base 0,5C, escalada 1C)"},
                 "Potencia peak de operación por año y nivel de producción, escenario solo red", out_png)


def figura_peak_tasa(rs, out_png):
    """Potencia peak de operacion por año, un panel por tasa de carga, battery swapping
    y on-board en escalon con la capacidad de la subestacion de referencia. El de
    swap es el del modelo: a 1C y 2C el ajuste de energia alarga las cargas con
    deficit (baterias casi nuevas) mas alla del bloque, se monta con la siguiente
    y pasa la potencia del unico cargador; en el modelo el peak es cargadores x
    p_charger. A 0,5C los dos coinciden (sin deficit)."""
    # Swap ancho y translucido debajo y on-board fino encima: se ven los dos donde coinciden.
    lados = (("modelo", "Swap", "darkorange", "Battery swapping", 10, "bottom", 6.0, 0.55),
             ("ob", "OB", "steelblue", "On-board", -12, "top", 2.4, 1.0))
    fig, axes = plt.subplots(1, len(rs), figsize=(6.2 * len(rs), 5.6), sharey=True)
    tope = 0.0
    for ax, r in zip(axes, rs):
        for lado, tec, color, etiqueta, dy, va, lw, alpha in lados:
            pk = peak_anual(r["despacho"][lado])
            ys, kw = list(pk), [round(v) for v in pk.values()]
            ssee = r["fijo"][tec]["Módulos subestación"] * P_SSEE_STEP
            tope = max(tope, max(kw), ssee)
            ax.stairs(kw, [y - 0.5 for y in ys] + [ys[-1] + 0.5], color=color, linewidth=lw, alpha=alpha,
                      baseline=None, label=etiqueta)
            ax.axhline(ssee, color=color, linestyle=":", linewidth=1.8, label="Subestación " + etiqueta[0].lower() + etiqueta[1:])
            n_carg = {f["Año"]: int(f["Cargadores"]) for f in r["infra"][tec]}
            for i, (y, v) in enumerate(zip(ys, kw)):   # rotulo solo donde cambia
                if i == 0 or v != kw[i - 1]:
                    ax.annotate("{} kW ({} carg.)".format(miles(v), n_carg[y]), (y - 0.4, v),
                                textcoords="offset points", xytext=(0, dy), ha="left", va=va,
                                fontsize=10, fontweight="bold", color=color)
        ax.set_title("{} ({} kW por cargador)".format(tasa(r["clave"]), _num(potencia_cargador(r))), fontsize=13)
        ax.set_xlabel("Año")
        ax.set_xticks(range(1, 11))
        ax.set_xlim(0.5, 10.5)
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("Potencia peak [kW]")
    axes[0].set_ylim(0, tope * 1.18)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, -0.07), ncol=4, frameon=True, framealpha=0.95,
               edgecolor="gray")
    fig.suptitle("Potencia peak de operación por año y tasa de carga, producción base, red + generación + BESS",
                 fontsize=15)
    fig.tight_layout(rect=[0, 0.02, 1, 0.97])
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _figura_peak(series, notas, titulo_fig, out_png):
    """series: [(r, color, etiqueta, tamaño del marcador, desplazamiento del rotulo)];
    notas: {lado: texto tras el titulo del panel}."""
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.6), sharey=True)
    tope = 0.0
    for ax, (lado, titulo) in zip(axes, (("ajustado", "Battery swapping"), ("ob", "On-board"))):
        for r, color, etiqueta, s, dy in series:
            pk = peak_anual(r["despacho"][lado])
            ys, kw = list(pk), list(pk.values())
            tope = max(tope, max(kw))
            ax.plot(ys, kw, color=color, linewidth=2, zorder=2)
            ax.scatter(ys, kw, color=color, s=s, zorder=3, label=etiqueta)
            for xi, v in zip(ys, kw):
                ax.annotate(miles(v), (xi, v), textcoords="offset points", xytext=(0, dy), ha="center",
                            fontsize=9, fontweight="bold", color=color)
        ax.set_title(titulo + notas[lado], fontsize=13)
        ax.set_xlabel("Año")
        ax.set_xticks(ys)
        ax.set_xlim(ys[0] - 0.5, ys[-1] + 0.5)
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("Potencia peak [kW]")
    axes[0].set_ylim(0, tope * 1.2)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, -0.06), ncol=len(l), frameon=True,
               framealpha=0.95, edgecolor="gray")
    fig.suptitle(titulo_fig, fontsize=15)
    fig.tight_layout(rect=[0, 0.02, 1, 0.97])
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def tabla_produccion(r_base, r_esc):
    """Efecto de pasar de produccion base a escalada en la infraestructura de
    carga, escenario solo red: una fila por tecnologia y produccion."""
    filas = []
    for tec, lado, desp, sol in (("Battery swapping", "Swap", "ajustado", "swap_sol"),
                                 ("Carga on-board", "OB", "ob", "ob_sol_disco")):
        for prod, r in (("base", r_base), ("escalada", r_esc)):
            p = json.loads((REPO_ROOT / r[sol] / "parameters.json").read_text(encoding="utf-8"))
            infra = r["infra"][lado]
            ch = {f["Año"]: f["Cargadores"] for f in infra}
            tot, _ = energia(r["despacho"][desp], r["esc_dt"])
            filas.append({
                "Escenario": "{} producción {}".format(tec, prod),
                "Cantidad de cargadores": max(ch.values()),
                "Año del último cargador": min(y for y, n in ch.items() if n == max(ch.values())),
                "Potencia por cargador [kW]": float(p["p_charger"]),
                "Cantidad de baterías extra": max(f["Baterías pool"] for f in infra) if lado == "Swap" else None,
                "Potencia subestación [kW]": r["fijo"][lado]["Módulos subestación"] * P_SSEE_STEP,
                "Potencia peak de operación [kW]": max(peak_anual(r["despacho"][desp]).values()),
                "Potencia contratada en punta máx. [kW]": max((r["P_pot_aj"] if lado == "Swap" else r["P_pot_ob"]).values()),
                "Energía demandada total [MWh]": tot["Carga [MWh]"],
                "Reemplazos de flota de baterías": sum(1 for f in r["deg"][lado] if f[3]),
                "Costo total [USD]": r["costos"]["Swap ajustado" if lado == "Swap" else "OB comparable"]["total_cost"],
            })
    return pd.DataFrame(filas)


def escribir_produccion_tex(df, path):
    lineas = []
    for f in df.to_dict("records"):
        tec = r"\textit{Battery swapping}" if f["Escenario"].startswith("Battery") else r"Carga \textit{on-board}"
        celdas = ["{} (año {})".format(_num(f["Cantidad de cargadores"]), f["Año del último cargador"]),
                  _num(f["Potencia por cargador [kW]"]), _num(f["Cantidad de baterías extra"]),
                  _num(f["Potencia subestación [kW]"]), _num(f["Potencia peak de operación [kW]"]),
                  _num(f["Potencia contratada en punta máx. [kW]"]), _num(f["Energía demandada total [MWh]"]),
                  _num(f["Reemplazos de flota de baterías"]), _num(f["Costo total [USD]"] / 1000.0)]
        lineas.append(r"{} producción {} & {} \\ \hline".format(tec, f["Escenario"].split()[-1], " & ".join(celdas)))
    cab = ["Cantidad de cargadores", r"Potencia por carga\-dor [kW]", "Cantidad de baterías extra",
           r"Potencia subes\-tación [kW]", "Potencia peak de operación [kW]",
           r"Potencia contratada en punta máx. [kW]", "Energía demandada total [MWh]",
           r"Cantidad de reempla\-zos de flota de baterías", "Costo total [kUSD]"]
    n = len(cab)
    texto = (ENCABEZADO_TEX.replace("<<CAPTION>>", "Efecto de la producción escalada en la infraestructura "
                                    "de carga, escenario solo red, zona norte.")
             .replace("<<LABEL>>", "tab:efecto-produccion").replace("<<N>>", str(n))
             .replace("<<SEP>>", str(2 * (n + 1))).replace("<<REG>>", str(n + 2))
             .replace(r"\cab{Escenario operacional}", r"\cab{Tecnología y producción}")
             .replace("<<CABECERAS>>", " &\n".join(r"\cab{" + c + "}" for c in cab))
             .replace("<<FILAS>>", "\n".join(lineas)))
    path.write_text(texto, encoding="utf-8")


# Ventanas DET que detienen al LHD (sets de parameters.json): (set, etiqueta, color).
# La mantencion no va: con --free_maintenance (casos finales) es un intervalo libre.
VENTANAS_DET = [("time_intervals_between_shifts_det_set", "Cambio de turno", "#C9B6E4"),
                ("time_intervals_meal_det_set", "Colación", "#CFE6C3"),   # no gris: se confunde con inactivo
                ("time_intervals_road_clearing_det_set", "Despeje de calles", "#DCEBF7")]


def bloques(ts):
    """Intervalos t -> bloques contiguos [(t_inicio, t_fin)]."""
    out = []
    for t in sorted(ts):
        if out and t == out[-1][1] + 1:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def _ventanas(ax, params, dt, d, T, det=True):
    """Fondo de ax: ventanas DET que detienen al LHD (si det) y ventana de cargo por
    potencia achurada, sobre [(t-1) dt, t dt]. Devuelve (etiquetas DET dibujadas, hay cargo)."""
    vistas = []
    for clave_set, etiqueta, c in VENTANAS_DET if det else []:
        for a, b in bloques(int(float(t)) for t in params.get(clave_set) or []):
            ax.axvspan((a - 1) * dt, b * dt, color=c, alpha=0.8, zorder=0, linewidth=0)
            if etiqueta not in vistas:
                vistas.append(etiqueta)
    punta = DIAS_CON_CARGO[0] <= d <= DIAS_CON_CARGO[1]
    if punta:
        for a, b in bloques(intervalos_punta(T, dt)):
            ax.axvspan((a - 1) * dt, b * dt, facecolor="#B00020", alpha=0.08, zorder=1, linewidth=0)
            ax.axvspan((a - 1) * dt, b * dt, facecolor="none", edgecolor="#B00020", hatch="//",
                       alpha=0.5, zorder=1, linewidth=0)
    return vistas, punta


def _leyenda_ventanas(vistas, punta):
    h = [matplotlib.patches.Patch(color=c, alpha=0.8, label=et) for _, et, c in VENTANAS_DET if et in vistas]
    if punta:
        h.append(matplotlib.patches.Patch(facecolor="#F6E1E4", edgecolor="#B00020", hatch="//",
                                          label="Cargo por potencia"))
    return h


def _eje_hora(ax, start_hour, paso=2):
    """Eje x de 24 h con la hora de reloj desde base_hour, cada paso h."""
    ticks = np.arange(0, 25, paso)
    ax.set_xticks(ticks)
    ax.set_xticklabels(["{:02d}:{:02d}".format(int((start_hour + h) % 24),
                                                int(round(((start_hour + h) % 1) * 60))) for h in ticks],
                       fontsize=13)
    ax.set_xlim(0, 24)


def _lados(r):
    """(titulo, solucion, json_plotter.py, variable de carga) de on-board y battery swapping."""
    return (("On-board", Path(r["ob_sol_disco"]), OB_REPO / "json_plotter.py", "P"),
            ("Battery swapping", REPO_ROOT / r["swap_sol"], REPO_ROOT / "json_plotter.py", "Zswap"))


def _orden_lhd(nombre):
    """'LH518B_10' -> 10, para que LHD 2 vaya antes que LHD 10."""
    return int(re.search(r"(\d+)\s*$", str(nombre)).group(1))


def estados_lhd(p, anio, d, carga):
    """{lhd: {t: estado}} de t = 1..T con la regla de plot_lhd_soc_vs_price_and_states
    de json_plotter.py: "asignado" a un punto de extraccion (Y) antes que "carga"
    (P > 1 kW en on-board, Z_swap en swap), y si no "inactivo"."""
    df_c = p.df_P if carga == "P" else p.df_Zswap
    out = {}
    for lhd in sorted(p.df_B["lhd"].unique(), key=_orden_lhd):
        sel = lambda df: (lambda x: x[x["lhd"] == lhd])(p._filter_yd(df, anio, d))
        asig = set(sel(p.df_Y).query("value >= 0.5")["interval"])
        cargando = set(sel(df_c).query("value > 1" if carga == "P" else "value >= 0.5")["interval"])
        out[lhd] = {t: "asignado" if t in asig else ("carga" if t in cargando else "inactivo")
                    for t in p.intervals[1:]}
    return out


def tramos(serie):
    """{t: estado} -> tramos contiguos [(t_inicio, t_fin, estado)]."""
    out = []
    for t in sorted(serie):
        if out and out[-1][2] == serie[t] and t == out[-1][1] + 1:
            out[-1][1] = t
        else:
            out.append([t, t, serie[t]])
    return out


COLOR_ESTADO = {"asignado": "#F0BF57", "carga": "#3366CC", "inactivo": "#B0B0B0"}   # los de figura_soc
ETIQUETA_SOC = {"asignado": "En tránsito", "carga": "Carga / swap", "inactivo": "Inactivo"}


def figura_soc(r, out_png, escenario, estacion, anio=ANIO_PICO, lhd=LHD_SOC, prod="base"):
    """SOC y estados de un LHD, on-board arriba y battery swapping abajo, con el
    formato de plot_lhd_soc_vs_price_and_states de json_plotter.py, sin el precio
    y con las ventanas de detencion y de cargo por potencia. El SOC va respecto
    de la capacidad de la bateria en el año (b_bar) y el eje en hora de reloj
    desde base_hour (la plantilla fija las 09:00)."""
    d = ESTACIONES[estacion]
    color = {"soc": "#f78c11", **{ETIQUETA_SOC[e]: c for e, c in COLOR_ESTADO.items()}}
    fig = plt.figure(figsize=(18, 10))
    gs = gridspec.GridSpec(5, 1, height_ratios=[4.2, 1.0, 1.1, 4.2, 1.0], hspace=0.08)
    punta, vistas = False, []
    lados = _lados(r)
    for i, (titulo, sol, plotter_py, carga) in enumerate(lados):
        p = cargar_plotter(plotter_py, sol)
        params = json.loads((sol / "parameters.json").read_text(encoding="utf-8"))
        dt = float(p.delta_t)
        ax = fig.add_subplot(gs[3 * i])
        ax_t = fig.add_subplot(gs[3 * i + 1], sharex=ax)
        sel = lambda df: (lambda x: x[x["lhd"] == lhd])(p._filter_yd(df, anio, d))
        x = np.array(p.intervals, dtype=float) * dt          # B[t]: fin del intervalo t (t = 0: inicio)
        soc = sel(p.df_B).groupby("interval")["value"].mean().reindex(p.intervals).ffill().bfill()
        cap = float(p.df_b_bar.set_index("year")["b_bar"][anio])
        ax.plot(x, soc.to_numpy() / cap * 100.0, color=color["soc"], lw=3, zorder=3)
        v, punta = _ventanas(ax, params, dt, d, max(p.intervals))
        vistas += [e for e in v if e not in vistas]
        for t, e in estados_lhd(p, anio, d, carga)[lhd].items():
            c = color[ETIQUETA_SOC[e]]
            ax_t.barh(0.0, width=dt, left=(t - 1) * dt, height=0.56, color=c, edgecolor=c)
        ax.set_title(titulo, fontsize=16)
        ax.set_ylabel("Estado de carga [%]", fontsize=15)
        ax.set_ylim(-5, 105)
        ax.set_yticks([0, 20, 40, 60, 80, 100])
        ax_t.set_yticks([])
        ax_t.set_ylabel("Estado", fontsize=14)
        _eje_hora(ax_t, p.start_hour)
        ax.set_xlim(0, 24)
        ax.tick_params(axis="x", labelbottom=False)
        ax.tick_params(axis="y", labelsize=13)
        for a in (ax, ax_t):
            a.grid(False)
        ax.grid(axis="x", linestyle="--", alpha=0.5)
        if i == len(lados) - 1:
            ax_t.set_xlabel("Hora", fontsize=15)
    handles = [plt.Line2D([0], [0], color=color["soc"], lw=4, label="Estado de carga")]
    handles += [plt.Line2D([0], [0], color=color[e], lw=8, label=e)
                for e in ("Carga / swap", "Inactivo", "En tránsito")]
    handles += _leyenda_ventanas(vistas, punta)
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.0), ncol=(len(handles) + 1) // 2,
               fontsize=13, frameon=True, framealpha=0.95, edgecolor="gray")
    fig.suptitle("Estado de carga LHD {}, escenario {}{}\naño {}, {}".format(
        lhd, escenario, "" if prod == "base" else ", producción " + prod, anio, estacion),
                 fontsize=17)
    fig.subplots_adjust(left=0.06, right=0.97, top=0.89, bottom=0.15)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


ETIQUETA_ESTADO = {"asignado": "Asignado a extracción", "carga": "Carga (on-board) / swap (battery swapping)",
                   "inactivo": "Inactivo"}


def _panel_estado(ax, titulo, sol, plotter_py, carga, d, anio, paso=2):
    """Una fila por LHD con sus tramos de estado sobre las ventanas de fondo.
    Devuelve lo de _ventanas."""
    p = cargar_plotter(plotter_py, sol)
    params = json.loads((sol / "parameters.json").read_text(encoding="utf-8"))
    dt = float(p.delta_t)
    vistas, punta = _ventanas(ax, params, dt, d, max(p.intervals))
    est = estados_lhd(p, anio, d, carga)
    for fila, (lhd, serie) in enumerate(est.items()):
        for a, b, e in tramos(serie):
            ax.barh(fila, (b - a + 1) * dt, left=(a - 1) * dt, height=0.62, color=COLOR_ESTADO[e],
                    linewidth=0, zorder=3)
    ax.set_yticks(range(len(est)))
    ax.set_yticklabels(["LHD {}".format(_orden_lhd(l)) for l in est], fontsize=14)
    ax.set_ylim(len(est) - 0.5, -0.5)
    ax.set_title(titulo, fontsize=16)
    ax.grid(False)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    _eje_hora(ax, p.start_hour, paso)
    return vistas, punta


def _cerrar_estado(fig, axs, vistas, punta, titulo, out_png, **ajuste):
    for ax in axs:   # rotulos solo en la fila de abajo y en la columna de la izquierda
        ax.label_outer()
        if ax.get_subplotspec().is_last_row():
            ax.set_xlabel("Hora", fontsize=15)
    handles = [matplotlib.patches.Patch(color=COLOR_ESTADO[e], label=ETIQUETA_ESTADO[e]) for e in COLOR_ESTADO]
    handles += _leyenda_ventanas(vistas, punta)
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.0), ncol=(len(handles) + 1) // 2,
               fontsize=13, frameon=True, framealpha=0.95, edgecolor="gray")
    fig.suptitle(titulo, fontsize=17)
    fig.subplots_adjust(left=0.06, right=0.97, **ajuste)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_estado_flota(r, out_png, escenario, estacion, anio=ANIO_PICO):
    """Estado de cada LHD de la flota en el dia, on-board arriba y battery swapping
    abajo (asignado a extraccion, carga o swap, inactivo), con las ventanas de
    detencion y la de cargo por potencia de fondo."""
    d = ESTACIONES[estacion]
    fig, axs = plt.subplots(2, 1, figsize=(18, 8.8), sharex=True)
    vistas, punta = [], False
    for ax, (titulo, sol, plotter_py, carga) in zip(axs, _lados(r)):
        v, punta = _panel_estado(ax, titulo, sol, plotter_py, carga, d, anio)
        vistas += [e for e in v if e not in vistas]
    _cerrar_estado(fig, axs, vistas, punta, "Estado de operación de la flota LHD, escenario {}\naño {}, {}".format(
        escenario, anio, estacion), out_png, top=0.88, bottom=0.17, hspace=0.25)


def figura_estado_tasa(rs, out_png, estacion, anio=ANIO_PICO):
    """Estado de la flota LHD por tasa de carga: una fila por caso de rs, on-board a
    la izquierda y battery swapping a la derecha (paneles del ancho de los de
    figura_estado_produccion; en 2 x 3 quedan muy angostos)."""
    d = ESTACIONES[estacion]
    fig, axs = plt.subplots(len(rs), 2, figsize=(24, 4.0 * len(rs) + 2.5), sharex=True, sharey=True)
    vistas, punta = [], False
    for fila, r in zip(axs, rs):
        for ax, (titulo, sol, plotter_py, carga) in zip(fila, _lados(r)):
            v, punta = _panel_estado(ax, "{}, {} ({} kW)".format(titulo, tasa(r["clave"]),
                                                                 _num(potencia_cargador(r))),
                                     sol, plotter_py, carga, d, anio, paso=4)
            vistas += [e for e in v if e not in vistas]
    _cerrar_estado(fig, axs.ravel(), vistas, punta,
                   "Estado de operación de la flota LHD por tasa de carga, producción base, red + generación + "
                   "BESS\naño {}, {}".format(anio, estacion), out_png,
                   top=0.91, bottom=0.12, hspace=0.3, wspace=0.06)


def figura_estado_produccion(r_base, r_esc, out_png, escenario, estacion, anio=ANIO_PICO):
    """Estado de la flota LHD con produccion base (izquierda) y escalada (derecha):
    on-board arriba y battery swapping abajo. Con produccion escalada on-board usa
    cargadores de 1C; swap sigue en 0,5C."""
    d = ESTACIONES[estacion]
    fig, axs = plt.subplots(2, 2, figsize=(24, 9.5), sharex=True, sharey=True)
    vistas, punta = [], False
    for i, fila in enumerate(axs):
        for ax, (prod, r) in zip(fila, (("base", r_base), ("escalada", r_esc))):
            titulo, sol, plotter_py, carga = _lados(r)[i]
            nota = " (cargadores 1C)" if prod == "escalada" and i == 0 else ""
            v, punta = _panel_estado(ax, "{}, producción {}{}".format(titulo, prod, nota), sol, plotter_py,
                                     carga, d, anio, paso=4)
            vistas += [e for e in v if e not in vistas]
    _cerrar_estado(fig, axs.ravel(), vistas, punta,
                   "Estado de operación de la flota LHD con producción base y escalada, escenario {}\n"
                   "año {}, {}".format(escenario, anio, estacion), out_png,
                   top=0.87, bottom=0.17, hspace=0.25, wspace=0.06)


SOL_MIN = 0.01   # alpha solar bajo el que no hay sol (los datos dejan colas de 1e-4 al atardecer)

def _alpha_solar(params, anio, d, T):
    """Disponibilidad solar (alpha_g de Solar_PV) del dia por intervalo, o None si no hay solar."""
    if "Solar_PV" not in (params.get("gen_set") or []):
        return None
    return _por_intervalo(_u(params["alpha_g"])["Solar_PV"]["_2"][str(anio)]["_3"][str(d)]["_4"], T)


def figura_baterias_en_carga(r, out_png, escenario, estacion, anio=ANIO_PICO):
    """Baterias cargando a la vez en cada intervalo, on-board arriba (LHD con P > 1 kW:
    la bateria va montada) y battery swapping abajo (Sv de la estacion), con los
    cargadores instalados del año, las horas con sol del dia (alpha solar > SOL_MIN) y la
    ventana de cargo por potencia; el formato de plot_charging_batteries_vs_price de
    json_plotter.py sin el precio. En cada panel, el uso de los cargadores (intervalos
    de carga / cargadores x intervalos del dia) y la parte de la carga hecha con sol."""
    d = ESTACIONES[estacion]
    fig, axs = plt.subplots(2, 1, figsize=(18, 8.8), sharex=True)
    n_max, punta, hay_sol = 0.0, False, False
    for ax, (titulo, sol, plotter_py, carga), lado, col in zip(axs, _lados(r), ("OB", "Swap"),
                                                               (COLOR_OB, COLOR_SWAP)):
        p = cargar_plotter(plotter_py, sol)
        params = json.loads((sol / "parameters.json").read_text(encoding="utf-8"))
        dt, ts = float(p.delta_t), list(p.intervals[1:])
        if carga == "P":
            n = (p._filter_yd(p.df_P, anio, d).query("value > 1").groupby("interval")["lhd"].nunique())
        else:
            n = p._filter_yd(p.df_Sv, anio, d).groupby("interval")["value"].sum()
        n = n.reindex(ts).fillna(0.0).to_numpy(dtype=float)
        n_ch = next(f["Cargadores"] for f in r["infra"][lado] if f["Año"] == anio)
        n_max = max(n_max, n_ch, n.max())
        bordes = np.arange(len(ts) + 1) * dt
        _, punta = _ventanas(ax, params, dt, d, max(p.intervals), det=False)
        alpha = _alpha_solar(params, anio, d, len(ts))
        if alpha is not None:
            hay_sol = True
            for a, b in bloques(t for t, x in zip(ts, alpha) if x > SOL_MIN):
                ax.axvspan((a - 1) * dt, b * dt, ymin=0.94, ymax=1.0, color="#F2C14E", zorder=5, linewidth=0)
        ax.stairs(n, bordes, fill=True, color=col, alpha=0.25, zorder=2)
        ax.stairs(n, bordes, color=col, lw=2.6, zorder=3, baseline=None)
        ax.axhline(n_ch, color="black", ls="--", lw=1.5, zorder=4)
        uso = n.sum() / (n_ch * len(ts))
        texto = "uso de cargadores: {:.0f} %".format(100 * uso)
        if alpha is not None:
            texto += "\ncarga con sol: {:.0f} %".format(100 * n[alpha > SOL_MIN].sum() / n.sum())
        ax.text(24.3, n_ch / 2.0, texto, va="center", ha="left", fontsize=12, clip_on=False)
        ax.set_title(titulo, fontsize=16)
        ax.set_ylabel("Baterías en carga", fontsize=15)
        ax.tick_params(axis="y", labelsize=13)
        ax.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(1))
        ax.grid(False)
        ax.grid(axis="x", linestyle="--", alpha=0.5)
        for lado_eje in ("top", "right"):
            ax.spines[lado_eje].set_visible(False)
        _eje_hora(ax, p.start_hour)
    for ax in axs:
        ax.set_ylim(0, n_max * 1.15)
    axs[0].tick_params(axis="x", labelbottom=False)
    axs[-1].set_xlabel("Hora", fontsize=15)
    handles = [plt.Line2D([0], [0], color=COLOR_OB, lw=4, label="Baterías en carga, on-board"),
               plt.Line2D([0], [0], color=COLOR_SWAP, lw=4, label="Baterías en carga, battery swapping"),
               plt.Line2D([0], [0], color="black", ls="--", lw=1.5, label="Cargadores instalados")]
    if hay_sol:
        handles.append(matplotlib.patches.Patch(color="#F2C14E",
                                                label="Horas con sol"))
    handles += _leyenda_ventanas([], punta)
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.0), ncol=(len(handles) + 1) // 2,
               fontsize=13, frameon=True, framealpha=0.95, edgecolor="gray")
    fig.suptitle("Baterías en carga simultánea, escenario {}\naño {}, {}".format(escenario, anio, estacion),
                 fontsize=17)
    fig.subplots_adjust(left=0.06, right=0.88, top=0.88, bottom=0.17, hspace=0.25)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


# Paleta de plot_battery_circulation (json_plotter.py de la rama battery_swapping): 4 hues
# para los LHD y una rampa neutra descargada -> cargando -> lista para la estacion.
COLOR_LHD = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7"]
COLOR_EN_ESTACION = {"descargada": "#cfcec5", "cargando": "#6b6a63", "lista": "#26251f"}


def _tinta(fondo):
    """Blanco o negro, el de mas contraste sobre fondo (WCAG)."""
    def lum(h):
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    l = lum(fondo)
    return "#ffffff" if 1.05 / (l + 0.05) >= (l + 0.05) / (lum("#141414") + 0.05) else "#141414"


def circulacion_baterias(sol, anio, d, estacion="station_1"):
    """Recorrido de cada bateria fisica de la estacion en el dia. El modelo las cuenta
    (S, X_dch, X_ini, Sv), no las nombra, asi que se les da identidad con FIFO -- el
    LHD se lleva la que lleva mas rato lista y entra al cargador la que lleva mas rato
    descargada -- y se contrasta con S y X_dch en cada intervalo, como
    plot_battery_circulation de la rama battery_swapping. La duracion de cada carga
    sale de Sv[t, a] (cuantas de las que empezaron en a siguen cargando en t), sin
    suponerla. Devuelve None (con aviso) si la reconstruccion no calza; si no
    {"lhds", "baterias", "estado": {bateria: {t: (tipo, lhd)}}, "swaps": {t: [lhd]}}."""
    ruta = lambda n: leer_json(sol, n)["k"][estacion]["y"][str(anio)]["d"][str(d)]["t"]
    serie = lambda n: {int(float(t)): int(round(float(v))) for t, v in ruta(n).items()}
    S, Xd, Xi = serie("S.json"), serie("X_dch.json"), serie("X_ini.json")
    cuenta = {}
    for t, blk in ruta("Sv.json").items():
        for a, v in blk["a"].items():
            if float(v) > 0.5:
                cuenta.setdefault(int(float(a)), {})[int(float(t))] = int(round(float(v)))
    if any(t < a for a, c in cuenta.items() for t in c):
        print("AVISO circulacion {} año {} dia {}: cargas que cruzan el fin del dia (pipeline ciclico); "
              "se omite".format(sol, anio, d), flush=True)
        return None
    # La k-esima bateria que empezo en a termina en el ultimo t con Sv[t, a] >= k.
    fines = {a: [max(t for t, v in c.items() if v >= k) for k in range(1, max(c.values()) + 1)]
             for a, c in cuenta.items()}
    zs = leer_json(sol, "Z_swap.json")["k"][estacion]["i"]
    lhds = sorted(zs, key=_orden_lhd)
    swaps = {}
    for lhd in lhds:
        for t, v in zs[lhd]["y"].get(str(anio), {}).get("d", {}).get(str(d), {}).get("t", {}).items():
            if float(v) >= 0.5:
                swaps.setdefault(int(float(t)), []).append(lhd)
    t0, tf = min(S), max(S)
    baterias = ["B{}".format(n) for n in range(1, len(lhds) + S[t0] + Xd[t0] + 1)]
    montada = dict(zip(lhds, baterias))
    estado = {b: ("lhd", l) for l, b in montada.items()}
    lista, descargada = deque(baterias[len(lhds):len(lhds) + S[t0]]), deque(baterias[len(lhds) + S[t0]:])
    estado.update({b: ("lista", None) for b in lista})
    estado.update({b: ("descargada", None) for b in descargada})
    cargando, hist = [], {b: {} for b in baterias}

    def falla(t, que):
        print("AVISO circulacion {} año {} dia {} t={}: {}; se omite".format(sol, anio, d, t, que), flush=True)

    for t in range(t0, tf + 1):
        for c in [c for c in cargando if c[1] == t - 1]:     # termino al cierre de t - 1: lista en t
            cargando.remove(c)
            lista.append(c[0])
            estado[c[0]] = ("lista", None)
        for lhd in swaps.get(t, []):                          # deja la suya y se lleva la mas antigua lista
            descargada.append(montada[lhd])
            estado[montada[lhd]] = ("descargada", None)
            if not lista:
                return falla(t, "{} hace swap sin bateria lista".format(lhd))
            montada[lhd] = lista.popleft()
            estado[montada[lhd]] = ("lhd", lhd)
        if t == t0 and Xd[t0] != len(descargada):             # X_dch[t0]: inventario al inicio del dia
            return falla(t, "X_dch no calza")
        if len(fines.get(t, [])) != Xi.get(t, 0):
            return falla(t, "Sv no calza con X_ini")
        for fin in fines.get(t, []):
            if not descargada:
                return falla(t, "empieza una carga sin bateria descargada")
            b = descargada.popleft()
            cargando.append((b, fin))
            estado[b] = ("cargando", None)
        if S[t] != len(lista) or (t != t0 and Xd[t] != len(descargada)):
            return falla(t, "la reconstruccion no calza con S / X_dch")
        for b in baterias:
            hist[b][t] = estado[b]
    return {"lhds": lhds, "baterias": baterias, "estado": hist, "swaps": swaps}


def figura_circulacion(r, out_png, escenario, estacion, anio=ANIO_PICO):
    """Gantt de circulacion de las baterias de la estacion de swap con el estilo de
    plot_battery_circulation (rama battery_swapping): una fila por bateria fisica (una
    montada en cada LHD y las extra del pool) coloreada por donde esta, guias en cada
    swap y la ventana de cargo por potencia achurada."""
    d = ESTACIONES[estacion]
    rec = circulacion_baterias(REPO_ROOT / r["swap_sol"], anio, d)
    if rec is None:
        return
    filas = rec["baterias"]
    fig = plt.figure(figsize=(18, max(5.8, 3.3 + 0.62 * len(filas))))
    gs = gridspec.GridSpec(2, 1, height_ratios=[0.75, 0.62 * len(filas)], hspace=0.08)
    ax_ley = fig.add_subplot(gs[0])
    ax = fig.add_subplot(gs[1])
    punta = _panel_circulacion(ax, r, rec, d)
    ax.set_xlabel("Hora", fontsize=16)
    _leyenda_circulacion(ax_ley, rec["lhds"], punta)
    fig.suptitle("Circulación de baterías en la estación de swap, escenario {}\naño {}, {}".format(
        escenario, anio, estacion), fontsize=17)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.88, bottom=0.10)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_circulacion_tasa(rs, out_png, estacion, anio=ANIO_PICO):
    """Circulacion de baterias de la estacion de swap por tasa de carga, un panel
    por caso de rs (cada uno con su flota de baterias)."""
    d = ESTACIONES[estacion]
    recs = [circulacion_baterias(REPO_ROOT / r["swap_sol"], anio, d) for r in rs]
    if any(rec is None for rec in recs):
        return
    n = [len(rec["baterias"]) for rec in recs]
    fig = plt.figure(figsize=(18, 2.2 + 0.62 * sum(n) + 1.0 * len(rs)))
    gs = gridspec.GridSpec(1 + len(rs), 1, height_ratios=[1.2] + [0.62 * k for k in n], hspace=0.35)
    ax_ley = fig.add_subplot(gs[0])
    punta = False
    for i, (r, rec) in enumerate(zip(rs, recs)):
        ax = fig.add_subplot(gs[1 + i])
        punta = _panel_circulacion(ax, r, rec, d) or punta
        ax.set_title("{} ({} kW)".format(tasa(r["clave"]), _num(potencia_cargador(r))), fontsize=16)
        if i == len(rs) - 1:
            ax.set_xlabel("Hora", fontsize=16)
    _leyenda_circulacion(ax_ley, recs[0]["lhds"], punta)
    fig.suptitle("Circulación de baterías en la estación de swap por tasa de carga, producción base, "
                 "red + generación + BESS\naño {}, {}".format(anio, estacion), fontsize=17)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.93, bottom=0.05)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _panel_circulacion(ax, r, rec, d):
    """Una fila por bateria de rec sobre la ventana de punta; devuelve si hay cargo."""
    sol = REPO_ROOT / r["swap_sol"]
    p = cargar_plotter(REPO_ROOT / "json_plotter.py", sol)
    params = json.loads((sol / "parameters.json").read_text(encoding="utf-8"))
    dt = float(p.delta_t)
    color_lhd = {l: COLOR_LHD[n % len(COLOR_LHD)] for n, l in enumerate(rec["lhds"])}
    filas = rec["baterias"]
    _, punta = _ventanas(ax, params, dt, d, max(p.intervals), det=False)
    for t in sorted(rec["swaps"]):
        ax.axvline((t - 1) * dt, color="#a6a59c", lw=0.8, ls=":", alpha=0.75, zorder=1)
    for fila, b in enumerate(filas):
        for a, z, (tipo, lhd) in tramos(rec["estado"][b]):
            x0, ancho = (a - 1) * dt, (z - a + 1) * dt
            fondo = color_lhd[lhd] if tipo == "lhd" else COLOR_EN_ESTACION[tipo]
            ax.barh(fila, ancho, left=x0, height=0.62, color=fondo, edgecolor="#ffffff", linewidth=1.4,
                    hatch="///" if tipo == "cargando" else None, zorder=3)
            if tipo == "lhd" and ancho >= 1.1:
                ax.text(x0 + ancho / 2.0, fila, "LHD {}".format(_orden_lhd(lhd)), ha="center", va="center",
                        fontsize=12.5, fontweight="bold", color=_tinta(fondo), zorder=4)
    ax.set_yticks(range(len(filas)))
    ax.set_yticklabels(["Batería {}".format(n) for n in range(1, len(filas) + 1)], fontsize=14)
    ax.set_ylim(len(filas) - 0.5, -0.5)
    ax.set_ylabel("Flota de baterías", fontsize=16)
    ax.grid(False)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    _eje_hora(ax, p.start_hour)
    return punta


def _leyenda_circulacion(ax_ley, lhds, punta):
    ax_ley.axis("off")
    fila1 = [plt.Line2D([0], [0], color=COLOR_LHD[n % len(COLOR_LHD)], lw=9, label="LHD {}".format(_orden_lhd(l)))
             for n, l in enumerate(lhds)]
    fila2 = [matplotlib.patches.Patch(facecolor=COLOR_EN_ESTACION["descargada"], edgecolor="#a6a59c",
                                      label="Descargada"),
             matplotlib.patches.Patch(facecolor=COLOR_EN_ESTACION["cargando"], edgecolor="#ffffff",
                                      hatch="///", label="Cargando"),
             matplotlib.patches.Patch(facecolor=COLOR_EN_ESTACION["lista"], edgecolor="#ffffff",
                                      label="Cargada, lista")]
    ley1 = ax_ley.legend(handles=fila1, loc="center left", bbox_to_anchor=(0.0, 0.5), ncols=len(fila1),
                         frameon=True, fontsize=13.5, title="Montada en", title_fontsize=14, framealpha=0.6)
    ax_ley.legend(handles=fila2 + _leyenda_ventanas([], punta), loc="center right",
                  bbox_to_anchor=(1.0, 0.5), ncols=len(fila2) + int(punta), frameon=True, fontsize=13.5,
                  title="En la estación de swap", title_fontsize=14, framealpha=0.6)
    ax_ley.add_artist(ley1)


# Nivel de potencia de un LHD conectado on-board: (clave, etiqueta, hatch, opacidad del relleno).
NIVELES_CARGA = (("plena", "Potencia máxima", None, 1.0), ("parcial", "Potencia parcial", "...", 0.4),
                 ("sin", "Conectado sin potencia", "///", 0.0))


def cargadores_ob(sol, anio, d, estacion="station_1"):
    """Ocupacion de los cargadores on-board en el dia. El modelo cuenta los LHD
    conectados (sum Z_charge <= N_chargers) sin decir en que cargador: cada sesion
    (tramo contiguo de Z_charge = 1 de un LHD) va al primer cargador libre, por orden de
    inicio, lo que nunca usa mas cargadores que el maximo de LHD conectados a la vez.
    Devuelve None (con aviso) si eso pasa de N_chargers; si no {"n", "lhds",
    "sesiones": [(cargador, lhd, t_inicio, t_fin)], "P": {lhd: {t: kW}}, "p_max"}, con
    p_max la del cargador limitada por la subestacion."""
    params = leer_json(sol, "parameters.json")
    serie = lambda data, lhd: (data[lhd]["y"].get(str(anio), {}).get("d", {})
                               .get(str(d), {}).get("t", {}))
    zc = leer_json(sol, "Z_charge.json")["k"][estacion]["i"]
    pw = leer_json(sol, "P.json")["k"][estacion]["i"]
    n = int(round(float(leer_json(sol, "N_chargers.json")["k"][estacion]["y"][str(anio)])))
    lhds = sorted(zc, key=_orden_lhd)
    sesiones = sorted((a, b, lhd) for lhd in lhds
                      for a, b in bloques(int(float(t)) for t, v in serie(zc, lhd).items() if float(v) >= 0.5))
    fin, asignadas = [], []
    for a, b, lhd in sesiones:
        c = next((i for i, f in enumerate(fin) if f < a), len(fin))
        fin[c:c + 1] = [b]
        asignadas.append((c, lhd, a, b))
    if len(fin) > n:
        print("AVISO cargadores OB {} año {} dia {}: {} LHD conectados a la vez con {} cargadores; "
              "se omite".format(sol, anio, d, len(fin), n), flush=True)
        return None
    p_max = min(float(params["p_charger"]), float(leer_json(sol, "P_max_k.json")["k"][estacion]))
    return {"n": n, "lhds": lhds, "sesiones": asignadas, "p_max": p_max, "p_charger": float(params["p_charger"]),
            "P": {lhd: {int(float(t)): float(v) for t, v in serie(pw, lhd).items()} for lhd in lhds}}


def _nivel_carga(p, p_max):
    return "sin" if p <= 1.0 else ("plena" if p >= 0.99 * p_max else "parcial")


def _panel_cargadores_ob(ax, sol, rec, d):
    """Una fila por cargador on-board instalado, coloreada por el LHD conectado y su nivel
    de potencia, sobre las detenciones operacionales. Devuelve (vistas, punta, texto con
    la ocupacion y la energia respecto de la capacidad del cargador)."""
    params = leer_json(sol, "parameters.json")
    dt, T = float(params["delta_t"]), int(params["t_fin"])
    vistas, punta = _ventanas(ax, params, dt, d, T)
    color_lhd = {l: COLOR_LHD[n % len(COLOR_LHD)] for n, l in enumerate(rec["lhds"])}
    estilo = {k: (h, alfa) for k, _, h, alfa in NIVELES_CARGA}
    for c, lhd, a, b in rec["sesiones"]:
        col = color_lhd[lhd]
        nivel = {t: _nivel_carga(rec["P"][lhd].get(t, 0.0), rec["p_max"]) for t in range(a, b + 1)}
        for x, z, k in tramos(nivel):
            h, alfa = estilo[k]
            ax.barh(c, (z - x + 1) * dt, left=(x - 1) * dt, height=0.62, hatch=h, linewidth=0,
                    facecolor=matplotlib.colors.to_rgba(col, alfa) if alfa else "#ffffff", edgecolor=col,
                    zorder=3)
        ax.barh(c, (b - a + 1) * dt, left=(a - 1) * dt, height=0.62, fill=False, edgecolor="#ffffff",
                linewidth=1.4, zorder=4)
        if (b - a + 1) * dt >= 0.9:
            ax.text((a - 1 + (b - a + 1) / 2.0) * dt, c, "LHD {}".format(_orden_lhd(lhd)), ha="center",
                    va="center", fontsize=12.5, fontweight="bold", color="#141414", zorder=5,
                    path_effects=[pe.withStroke(linewidth=3, foreground="#ffffff")])
    ax.set_yticks(range(rec["n"]))
    ax.set_yticklabels(["Cargador {}".format(c + 1) for c in range(rec["n"])], fontsize=14)
    ax.set_ylim(rec["n"] - 0.5, -0.5)
    ax.grid(False)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    _eje_hora(ax, cargar_plotter(OB_REPO / "json_plotter.py", sol).start_hour)
    conectado = sum(b - a + 1 for _, _, a, b in rec["sesiones"]) / float(rec["n"] * T)
    energia = sum(sum(s.values()) for s in rec["P"].values()) / (rec["n"] * rec["p_charger"] * T)
    return vistas, punta, "conectado {} % del día, energía {} % de la capacidad".format(
        _num(100 * conectado), _num(100 * energia))


def _leyenda_cargadores(ax_ley, lhds, vistas, punta):
    ax_ley.axis("off")
    fila1 = [plt.Line2D([0], [0], color=COLOR_LHD[n % len(COLOR_LHD)], lw=9, label="LHD {}".format(_orden_lhd(l)))
             for n, l in enumerate(lhds)]
    gris = "#6b6a63"
    fila2 = [matplotlib.patches.Patch(facecolor=matplotlib.colors.to_rgba(gris, alfa) if alfa else "#ffffff",
                                      edgecolor=gris, hatch=h, label=et) for _, et, h, alfa in NIVELES_CARGA]
    ley1 = ax_ley.legend(handles=fila1, loc="center left", bbox_to_anchor=(0.0, 0.5), ncols=2,
                         frameon=True, fontsize=13.5, title="Conectado", title_fontsize=14, framealpha=0.6)
    extra = _leyenda_ventanas(vistas, punta)
    ax_ley.legend(handles=fila2 + extra, loc="center right", bbox_to_anchor=(1.0, 0.5),
                  ncols=(len(fila2) + len(extra) + 1) // 2, frameon=True, fontsize=13.5,
                  title="Nivel de potencia y detenciones operacionales", title_fontsize=14, framealpha=0.6)
    ax_ley.add_artist(ley1)


def figura_cargadores_ob(r, out_png, escenario, estacion, anio=ANIO_PICO):
    """Gantt de los cargadores on-board, el equivalente de figura_circulacion: una fila
    por cargador con el LHD conectado y su nivel de potencia."""
    d = ESTACIONES[estacion]
    sol = Path(r["ob_sol_disco"])
    rec = cargadores_ob(sol, anio, d)
    if rec is None:
        return
    fig = plt.figure(figsize=(18, 3.8 + 0.62 * rec["n"]))
    gs = gridspec.GridSpec(2, 1, height_ratios=[1.0, 0.62 * rec["n"] + 0.4], hspace=0.25)
    ax_ley, ax = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    vistas, punta, texto = _panel_cargadores_ob(ax, sol, rec, d)
    ax.set_title("{} cargador{} de {} kW: {}".format(rec["n"], "es" if rec["n"] > 1 else "",
                                                    _num(rec["p_charger"]), texto), fontsize=15)
    ax.set_xlabel("Hora", fontsize=16)
    _leyenda_cargadores(ax_ley, rec["lhds"], vistas, punta)
    fig.suptitle("Utilización de los cargadores on-board, escenario {}\naño {}, {}".format(
        escenario, anio, estacion), fontsize=17)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.86, bottom=0.10)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figura_cargadores_ob_tasa(rs, out_png, estacion, anio=ANIO_PICO):
    """Cargadores on-board por tasa de carga, un panel por caso de rs."""
    d = ESTACIONES[estacion]
    recs = [cargadores_ob(Path(r["ob_sol_disco"]), anio, d) for r in rs]
    if any(rec is None for rec in recs):
        return
    n = [rec["n"] for rec in recs]
    fig = plt.figure(figsize=(18, 2.6 + 0.62 * sum(n) + 1.3 * len(rs)))
    gs = gridspec.GridSpec(1 + len(rs), 1, height_ratios=[1.1] + [0.62 * k + 0.4 for k in n], hspace=0.6)
    ax_ley = fig.add_subplot(gs[0])
    vistas, punta = [], False
    for i, (r, rec) in enumerate(zip(rs, recs)):
        ax = fig.add_subplot(gs[1 + i])
        v, p, texto = _panel_cargadores_ob(ax, Path(r["ob_sol_disco"]), rec, d)
        vistas += [e for e in v if e not in vistas]
        punta = punta or p
        limite = "" if rec["p_max"] >= rec["p_charger"] else ", limitado a {} kW por la subestación".format(
            _num(rec["p_max"]))
        ax.set_title("{} ({} cargador{} de {} kW{}): {}".format(
            tasa(r["clave"]), rec["n"], "es" if rec["n"] > 1 else "", _num(rec["p_charger"]), limite, texto),
            fontsize=15)
        if i == len(rs) - 1:
            ax.set_xlabel("Hora", fontsize=16)
    _leyenda_cargadores(ax_ley, recs[0]["lhds"], vistas, punta)
    fig.suptitle("Utilización de los cargadores on-board por tasa de carga, producción base, "
                 "red + generación + BESS\naño {}, {}".format(anio, estacion), fontsize=17)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.92, bottom=0.05)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def figuras_ajustado(r, carpeta):
    """Solo BS ajustado: despacho de cada año y dia muestreado con el formato de
    plot_despacho_swap_vs_ob_v4.py (fuentes completas, demanda total con la carga
    del BESS, ventana de punta) y energia por año."""
    desp = r["despacho"]["ajustado"]
    ts = np.arange(1, len(next(iter(desp.values()))["carga"]) + 1, dtype=float)
    for (y, d), v in sorted(desp.items()):
        if not np.allclose(v["red"] + v["gen"] + v["bat"], v["carga"], atol=1e-3):
            raise SystemExit("caso {}: el despacho ajustado no suma la carga (y={}, d={})".format(
                r["clave"], y, d))
        with plt.rc_context(ESTILO_DESPACHO):
            fig, ax = plt.subplots(figsize=(11, 5.4))
            marca, _ = dibujar_despacho(ax, ts, v["red"], v["gen"], v["bat"], r["P_pot_aj"][y], y, d,
                                        "Battery swapping", r["dt"], fuentes_completas=True,
                                        etiqueta_consumo="Demanda total")
            ax.set_xlabel("Hora")
            ax.set_ylabel("Potencia [kW]")
            y0, y1 = ax.get_ylim()
            ax.set_ylim(y0, y1 * 1.14)
            if marca is not None:
                ax.text(marca, ax.get_ylim()[1] * 0.985, "cargo por potencia\n18:00–22:00",
                        ha="center", va="top", fontsize=10, color="#B00020", fontweight="bold")
            # Solo las fuentes que aparecen (el caso solo red no tiene generacion ni BESS).
            hay = {"Generación renovable": np.any(v["gen"] > 1e-6),
                   "BESS descargando": np.any(v["bat"] > 1e-6)}
            h, l = zip(*[(hh, ll) for hh, ll in zip(*ax.get_legend_handles_labels())
                         if hay.get(ll, True)])
            fig.legend(h, l, loc="lower center", bbox_to_anchor=(0.5, -0.06), ncol=len(l),
                       frameon=True, framealpha=0.95, edgecolor="gray")
            fig.suptitle("Despacho de potencia — Caso {}: {}\naño {} del horizonte, {}, "
                         "Mina_modelo v4".format(r["clave"], r["titulo"], y, ESTACION_DEL_DIA[d]),
                         fontsize=14)
            fig.tight_layout(rect=[0, 0.02, 1, 0.95])
            fig.savefig(carpeta / "despacho_Y{}_d{}.png".format(y, d), dpi=150, bbox_inches="tight")
            plt.close(fig)
    # Energia por año
    _, anual = energia(desp, r["esc_dt"])
    ys = sorted(anual)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for campo, color, et in (("carga", "black", "Consumo de las estaciones"),
                             ("red", "#4C72B0", "Energía comprada a la red"),
                             ("gen", "#F2C14E", "Generación renovable usada")):
        if any(anual[y][campo] > 1e-6 for y in ys):
            ax.plot(ys, [anual[y][campo] for y in ys], color=color, marker="o", markersize=4, label=et)
    ax.set_xlabel("Año")
    ax.set_ylabel("MWh/año")
    ax.set_xticks(range(1, 11))
    ax.set_ylim(0, None)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    ax.set_title("Caso {}: energía por año, BS ajustado — {}".format(r["clave"], r["titulo"]),
                 fontsize=11)
    fig.savefig(carpeta / "energia_anual.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def etiqueta_corta(clave):
    """'Produccion_base/zona_norte/05C/RED_GEN_BESS' -> 'Base, norte\\n0,5C\\nRED+GEN+BESS'."""
    prod, zona, c, rec = FINALES[clave][0].split("/")
    c = "0,5C" if c == "05C" else c
    if prod == E:
        c = "BS {} OB 1C".format(c)
    return "{}, {}\n{}\n{}".format("Escalada" if prod == E else "Base", zona.replace("zona_", ""),
                                   c, rec.replace("_", "+"))


# costo_total_8_casos agrupa mas que STACK_ORDER: estaciones y naves con la
# subestacion, y la O&M de generacion y de BESS con su inversion.
STACK_8_CASOS = [
    (("cost_stations", "cost_bays", "substation_cost"), "#1F4E79", "Estaciones y subestación"),
    (("cost_chargers",), "#2E75B6", "Cargadores"),
    (("cost_batteries",), "#5B9BD5", "Baterías (pool de swap)"),
    (("gen_inv_cost", "gen_op_cost"), "#375623", "Generación renovable"),
    (("bess_inv_cost", "bess_op_cost"), "#A9D18E", "BESS"),
    (("peak_power_cost",), "#BF8F00", "Potencia punta"),
    (("grid_energy_cost",), "#ED7D31", "Energía de red"),
    (("battery_replace_cost",), "#C00000", "Reemplazo de baterías"),
]


BASE_NORTE = ("1", "3", "2")   # base norte 0,5C: red, red + generacion, red + generacion + BESS


def figura_escenarios(res, out_png, titulo, rotulo=etiqueta_corta):
    """BS ajustado y OB lado a lado por escenario, en el orden de res."""
    barras, grupos = [], []
    for g, r in enumerate(res):
        x0 = g * 2.6
        barras += [(x0, "BS", r["costos"]["Swap ajustado"]), (x0 + 1.0, "OB", r["costos"]["OB comparable"])]
        grupos.append((x0 + 0.5, rotulo(r["clave"])))
    _barras_costo(barras, grupos, out_png, titulo, (2 * len(res) + 2, 6))


def figura_costo_produccion(pares, out_png, titulo):
    """Produccion base y escalada lado a lado, agrupadas por escenario y tecnologia.
    pares: [(resultado base, resultado escalada)] del mismo escenario."""
    barras, grupos, x0 = [], [], 0.0
    for r_base, r_esc in pares:
        for tec, costo, nota in (("Battery swapping", "Swap ajustado", ""),
                                 ("On-board", "OB comparable", "\n(1C)")):
            barras += [(x0, "Base", r_base["costos"][costo]),
                       (x0 + 1.0, "Escalada" + nota, r_esc["costos"][costo])]
            grupos.append((x0 + 0.5, "{}\n{}".format(tec, ESCENARIOS[r_base["clave"]][0])))
            x0 += 2.6
        x0 += 0.6   # separa los escenarios
    _barras_costo(barras, grupos, out_png, titulo, (2 * len(pares) * 2 + 2, 6), y_grupo=-0.12)


def _barras_costo(barras, grupos, out_png, titulo, tamano, y_grupo=-0.09):
    """Barras apiladas de costo total por componente. barras: [(x, rotulo, costos)];
    grupos: [(x, rotulo del grupo)] bajo los rotulos de las barras."""
    xs = np.array([b[0] for b in barras])
    tot = np.array([b[2]["total_cost"] for b in barras]) / 1000.0
    ymax = tot.max() * 1.08
    fig, ax = plt.subplots(figsize=tamano)
    bottom = np.zeros(len(barras))
    for claves, color, etiqueta in STACK_8_CASOS:
        vals = np.array([sum(b[2][k] for k in claves) for b in barras]) / 1000.0
        if not np.any(vals > 0):
            continue
        ax.bar(xs, vals, 0.8, bottom=bottom, color=color, label=etiqueta, edgecolor="black", linewidth=0.5)
        bottom += vals
    for xi, t in zip(xs, tot):
        ax.text(xi, t + 0.01 * ymax, miles(t), ha="center", va="bottom", fontsize=8, fontweight="bold")
    for xg, texto in grupos:
        ax.text(xg, y_grupo, texto, transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8)
    ax.set_xticks(xs)
    ax.set_xticklabels([b[1] for b in barras])
    ax.set_ylim(0, ymax)
    ax.set_ylabel("Costo total [kUSD, VP 10 años]")
    ax.set_title(titulo)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    h, l = ax.get_legend_handles_labels()
    ax.legend(h[::-1], l[::-1], loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=9)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------ json_plotter --
def cargar_plotter(plotter_py, sol):
    """JSONPlotter de json_plotter.py (de esta rama o de Elmo_ob) sobre una
    solucion, sin dejar la carpeta <sol>/plots vacia que crea al iniciarse."""
    import os
    nombre = "json_plotter_" + Path(plotter_py).parent.name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(nombre, plotter_py)
    mod = importlib.util.module_from_spec(spec)
    creada = not (Path(sol) / "plots").exists()
    with matplotlib.rc_context():   # json_plotter cambia rcParams al importarse (grilla, estilo)
        spec.loader.exec_module(mod)
        p = mod.JSONPlotter(str(sol))
    if creada:
        try:
            os.rmdir(p.plot_dir)
        except OSError:
            pass
    return p


def correr_json_plotter(plotter_py, sol, destino):
    """json_plotter.py (de esta rama o de Elmo_ob) sobre una solucion; los PNG van
    a destino en vez de <sol>/plots. Cada grafico por separado: el de Elmo_ob no
    atrapa los errores y uno que falla cortaria los demas."""
    import traceback
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    p = cargar_plotter(plotter_py, sol)
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


def trabajos_json_plotter():
    out = []
    for clave, (caso_dir, sub, nombre) in FINALES.items():
        carpeta = OUT / "caso_{}_{}".format(clave, nombre)
        ob = CASOS_V4[clave]["ob"]
        out.append((str(REPO_ROOT / "json_plotter.py"), str(REPO_ROOT / V4 / caso_dir / sub / "solucion"),
                    str(carpeta / "battery_swapping_modelo")))
        out.append((str(OB_REPO / "json_plotter.py"), str(OB_REPO / ob["caso"] / ob["sol"]),
                    str(carpeta / "on_board")))
    return out


LEEME = """# Caso {clave}: {titulo}

| | Costo total [USD, VP 10 años] |
|---|---:|
| **BS ajustado** | **{s_a:,.2f}** |
| **On-board** | **{o_c:,.2f}** |
| BS ajustado vs OB | {dif:+.2%} |

Gap de optimalidad del L-shaped de swap: {gap:.2%} (LB de {lb_fuente}).

## Carpetas

- `comparacion/`: costos, infraestructura por año y degradación, BS ajustado contra OB.
- `battery_swapping_modelo/`: `json_plotter.py` de esta rama sobre la solución del
  L-shaped. **Son gráficos del modelo, antes del ajuste**: cada recarga aparece como el
  bloque completo bmin → 100 % y la generación/BESS son las que eligió el modelo.
- `battery_swapping_ajustado/`: despacho de BS ajustado (energía real de cada swap y
  generación, BESS y red redimensionadas) en cada año y día muestreado;
  `energia_anual.png` con el consumo, la compra a la red y la generación usada por año.
- `on_board/`: `json_plotter.py` de Elmo_ob sobre la solución on-board.

## Fuentes

- Swap: `{swap_sol}`
- On-board: `{ob_sol}`

Generado por `cost_comparisons/resultados_finales.py`; el detalle está en
`../resumen_swap_vs_ob_v4.xlsx`.
"""

LEEME_GENERAL = """# Resultados finales v4: battery swapping vs on-board

Costo total del horizonte (USD, VP 10 años). Swap: solución final del L-shaped,
**después** de los dos ajustes (energía real de cada swap y generación, BESS y red
redimensionadas); on-board: su solución final por el mismo LP.

| Caso | Escenario | BS ajustado | On-board | BS vs OB | Gap BS |
|---:|---|---:|---:|---:|---:|
{filas}

## Contenido

- `resumen_swap_vs_ob_v4.xlsx`: hojas Resumen, Costos (cinco variantes por caso),
  Infraestructura (evolución y capacidades), Infraestructura anual, Energía (MWh del
  horizonte), Degradación, Consumer (todas las tablas de consumer.py de los dos lados) y
  Notas (definiciones y controles).
- `costo_total_8_casos.png`: BS ajustado vs OB, desagregado.
- `costo_total_base_norte.png`: lo mismo para base norte 0,5C (red, red + generación y
  red + generación + BESS).
- `despacho_<escenario>_<estacion>.png`: despacho de on-board y battery swapping
  (ajustado), base norte 0,5C, año 5 (meta de producción máxima): solo red en invierno,
  red + generación + BESS en invierno y en verano, y red + generación en otoño. Días
  muestreados: verano 15 de enero (sin cargo por potencia), otoño 15 de abril e invierno
  15 de julio (con cargo de 18:00 a 22:00).
- `estado_flota_<escenario>_<estacion>.png`, `baterias_en_carga_<escenario>_<estacion>.png` y
  `circulacion_baterias_<escenario>_<estacion>.png`: operación de base norte 0,5C en el año 5,
  invierno (con cargo por potencia) y verano (más horas de sol). Estado de cada LHD (asignado a extracción, carga o swap, inactivo) en on-board y
  battery swapping; baterías cargando a la vez con los cargadores instalados y las
  horas con sol; y recorrido de cada batería de la estación de swap (identidad FIFO
  reconstruida desde S, X_dch, X_ini y Sv, contrastada intervalo a intervalo).
- `cargadores_ob_<escenario>_<estacion>.png`: el equivalente on-board de la circulación de
  baterías, mismos casos, año y días: una fila por cargador instalado con el LHD conectado y su
  nivel de potencia (máxima, parcial o conectado sin potencia), sobre las detenciones
  operacionales. El modelo solo cuenta los LHD conectados (sum Z_charge <= N_chargers): cada
  sesión de carga va al primer cargador libre por orden de inicio, que nunca usa más cargadores
  que los instalados. Título con la ocupación (conectado, % del día) y la energía cargada
  respecto de la capacidad nominal de los cargadores.
- `estado_flota_produccion_<escenario>_<estacion>.png`: estado de cada LHD con producción
  base (izquierda) y escalada (derecha), on-board arriba y battery swapping abajo, solo red y
  red + generación + BESS, año 5, invierno y verano. Con producción escalada on-board usa
  cargadores de 1C y battery swapping sigue en 0,5C.
- `costo_total_tasa_carga.png`, `degradacion_tasa_carga.png`,
  `estado_flota_tasa_carga_<estacion>.png`, `tabla_tasa_carga.tex` y `tabla_costos_tasa_carga.tex`:
  sensibilidad a la tasa de carga (0,5C, 1C y 2C) en producción base, norte, red + generación
  + BESS (casos 2, 7 y 8), battery swapping y on-board. También
  `potencia_peak_tasa_carga.png`, `circulacion_baterias_tasa_carga_<estacion>.png` y
  `cargadores_ob_tasa_carga_<estacion>.png` (un panel por tasa; a 2C el cargador on-board de
  964 kW queda limitado a 500 kW por la subestación). El peak de battery swapping es el del modelo (cargadores × potencia por cargador): el del
  despacho ajustado a 1C y 2C pasa la potencia del único cargador, porque el ajuste alarga las cargas
  con déficit (baterías casi nuevas) más allá del bloque y se montan con la siguiente. A 0,5C son
  iguales.
- `costo_total_ubicacion.png`, `despacho_ubicacion_<estacion>.png`, `tabla_ubicacion.tex` y
  `tabla_costos_ubicacion.tex`: sensibilidad a la ubicación en producción base 0,5C: solo red
  (caso 1; norte y centro solo difieren en el perfil de generación, así que sin generación es el
  mismo caso) y red + generación + BESS en norte (caso 2) y centro (caso 6).
- `costo_total_produccion.png`: costo total con producción base y escalada lado a lado,
  agrupado por escenario (solo red y red + generación + BESS) y tecnología.
- `degradacion_produccion.png`: capacidad por batería año a año con producción base y
  escalada (solo red; los demás escenarios difieren en 2 kWh o menos), battery swapping y
  on-board.
- `caso_<n>_<escenario>/`: `comparacion/` (BS ajustado vs OB), `battery_swapping_modelo/`
  (json_plotter, antes del ajuste), `battery_swapping_ajustado/` (despacho y energía de BS
  ajustado) y `on_board/` (json_plotter de Elmo_ob); ver el LEEME.md de cada caso.

## Regenerar

Cada solución swap necesita `parameters.json` y `consumer.log`, y cada solución OB
(en ../Elmo_ob) `parameters.json`:

    python -u escribir_parameters.py data/Tesis_final/Mina_modelo_v4/<caso> <solucion>
    python consumer.py <solucion> > <solucion>/../consumer.log
    (Elmo_ob) python -u escribir_parameters.py --data_folder \\
        data/Resultados_finales_tesis/Mina_modelo_v4/<caso OB> --free_charging --free_maintenance <solucion OB>
    python cost_comparisons/resultados_finales.py --jobs 16
"""


def escribir_leeme_general(res, path):
    filas = []
    for r in res:
        c = r["costos"]
        s_a, o_c = c["Swap ajustado"]["total_cost"], c["OB comparable"]["total_cost"]
        filas.append("| {} | {} | {:,.0f} | {:,.0f} | {:+.2%} | {:.2%} |".format(
            r["clave"], r["titulo"], s_a, o_c, s_a / o_c - 1, (r["ub"] - r["lb"]) / r["ub"]))
    path.write_text(LEEME_GENERAL.format(filas="\n".join(filas)), encoding="utf-8")


# ------------------------------------------------------------------ main --
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--casos", default=",".join(FINALES))
    ap.add_argument("--sin_json_plotter", action="store_true")
    ap.add_argument("--solo_json_plotter", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    claves = a.casos.split(",")

    if not a.solo_json_plotter:
        res = []
        for clave in claves:
            r = procesar(clave)
            res.append(r)
            carpeta = OUT / "caso_{}_{}".format(clave, r["nombre"])
            (carpeta / "comparacion").mkdir(parents=True, exist_ok=True)
            (carpeta / "battery_swapping_ajustado").mkdir(exist_ok=True)
            figura_costos(r, carpeta / "comparacion" / "costos_swap_vs_ob.png")
            figura_infra(r, carpeta / "comparacion" / "infraestructura_anual.png")
            figura_degradacion(r, carpeta / "comparacion" / "degradacion.png")
            figuras_ajustado(r, carpeta / "battery_swapping_ajustado")
            c = r["costos"]
            s_a, o_c = c["Swap ajustado"]["total_cost"], c["OB comparable"]["total_cost"]
            (carpeta / "LEEME.md").write_text(LEEME.format(
                clave=clave, titulo=r["titulo"], s_a=s_a, o_c=o_c, dif=s_a / o_c - 1,
                lb_fuente=r["lb_fuente"], gap=(r["ub"] - r["lb"]) / r["ub"],
                swap_sol=r["swap_sol"], ob_sol=r["ob_sol"]), encoding="utf-8")
            print("[{}] swap modelo {:,.0f} -> ajustado {:,.0f} | OB {:,.0f} | {:+.2%} | controles "
                  "{:+.2f} / {:+.2f}".format(clave, r["ub"], s_a, o_c, s_a / o_c - 1,
                                             r["control_swap"], r["control_ob"]), flush=True)
        try:
            escribir_excel(res, OUT / "resumen_swap_vs_ob_v4.xlsx")
        except PermissionError:   # abierto en Excel: el resto de las salidas no depende de el
            print("AVISO: resumen_swap_vs_ob_v4.xlsx esta abierto; no se reescribio", flush=True)
        if len(res) == len(FINALES):
            figura_escenarios(res, OUT / "costo_total_8_casos.png",
                              "Costo total del horizonte por escenario")
            por_clave = {r["clave"]: r for r in res}
            figura_escenarios([por_clave[k] for k in BASE_NORTE], OUT / "costo_total_base_norte.png",
                              "Costo total del horizonte por escenario, caso base norte",
                              lambda k: FINALES[k][0].split("/")[-1].replace("_", "+"))
            for k, estacion in FIGURAS_DESPACHO:
                figura_despacho(por_clave[k], OUT / "despacho_{}_{}.png".format(
                    ESCENARIOS[k][1], estacion.replace("ñ", "n")), ESCENARIOS[k][0], estacion)
            figura_potencia_peak([por_clave[k] for k in BASE_NORTE], OUT / "potencia_peak_base_norte.png")
            escribir_tabla_tex(tabla_infraestructura(res), OUT / "tabla_base_norte.tex")
            escribir_costos_tex(costos_escenarios(res), OUT / "tabla_costos_base_norte.tex")
            for k in BASE_NORTE:
                figura_soc(por_clave[k], OUT / "soc_{}_invierno.png".format(ESCENARIOS[k][1]),
                           ESCENARIOS[k][0], "invierno")
                # Operacion de la flota: estados de los LHD, baterias en carga y circulacion (swap).
                # Invierno (con cargo por potencia) y verano (mas sol: donde mas se separa la
                # carga en horas de sol de swap y on-board con generacion).
                for estacion in ("invierno", "verano"):
                    for figura, nombre in ((figura_estado_flota, "estado_flota"),
                                           (figura_baterias_en_carga, "baterias_en_carga"),
                                           (figura_circulacion, "circulacion_baterias"),
                                           (figura_cargadores_ob, "cargadores_ob")):
                        figura(por_clave[k], OUT / "{}_{}_{}.png".format(nombre, ESCENARIOS[k][1], estacion),
                               ESCENARIOS[k][0], estacion)
            # Analisis de produccion: base vs escalada (solo red y red + gen + BESS).
            figura_costo_produccion([(por_clave["1"], por_clave["4"]), (por_clave["2"], por_clave["5"])],
                                    OUT / "costo_total_produccion.png",
                                    "Costo total del horizonte por escenario y nivel de producción")
            figura_degradacion(por_clave["4"], OUT / "degradacion_escalada.png")   # igual en 4 y 5
            figura_degradacion_produccion(por_clave["1"], por_clave["4"], OUT / "degradacion_produccion.png")
            # Sensibilidad a la tasa de carga: base norte, red + gen + BESS, 0,5C, 1C y 2C.
            tasas = [por_clave[k] for k in TASAS]
            figura_escenarios(tasas, OUT / "costo_total_tasa_carga.png",
                              "Costo total del horizonte por tasa de carga, producción base, red + generación + BESS",
                              lambda k: "{} ({} kW)\n{}".format(
                                  tasa(k), _num(potencia_cargador(por_clave[k])), dif_bs_ob(por_clave[k])))
            figura_degradacion_tasa(tasas, OUT / "degradacion_tasa_carga.png")
            for estacion in ("invierno", "verano"):
                figura_estado_tasa(tasas, OUT / "estado_flota_tasa_carga_{}.png".format(estacion), estacion)
                figura_circulacion_tasa(tasas, OUT / "circulacion_baterias_tasa_carga_{}.png".format(estacion),
                                        estacion)
                figura_cargadores_ob_tasa(tasas, OUT / "cargadores_ob_tasa_carga_{}.png".format(estacion), estacion)
            figura_peak_tasa(tasas, OUT / "potencia_peak_tasa_carga.png")
            # Sensibilidad a la ubicacion: base 0,5C, solo red (norte y centro) y red + gen + BESS norte y centro.
            figura_escenarios([por_clave[k] for k in UBICACION], OUT / "costo_total_ubicacion.png",
                              "Costo total del horizonte por ubicación, producción base, 0,5C",
                              lambda k: "{}\n{}\n{}".format(
                                  "Norte y centro" if k == "1" else zona(k).capitalize(), ESCENARIOS[k][0],
                                  dif_bs_ob(por_clave[k])))
            for estacion in ("invierno", "verano"):
                figura_despacho_ubicacion([(por_clave["2"], "norte"), (por_clave["6"], "centro")],
                                          OUT / "despacho_ubicacion_{}.png".format(estacion), estacion)
            escribir_tabla_tex(tabla_infraestructura(res, UBICACION), OUT / "tabla_ubicacion.tex",
                               caption="Dimensionamiento de infraestructura e indicadores de operación por "
                               "ubicación, producción base, tasa de carga 0,5C. Sin generación el caso es el "
                               "mismo en ambas zonas.", label="tab:resultados-ubicacion", con_zona=True)
            escribir_costos_tex(costos_escenarios(res, UBICACION), OUT / "tabla_costos_ubicacion.tex",
                                caption="Costo total del horizonte por ubicación y componente, producción base, "
                                "tasa de carga 0,5C [kUSD, valor presente de 10 años].",
                                label="tab:costos-ubicacion", con_zona=True)
            escribir_tabla_tex(tabla_infraestructura(res, TASAS), OUT / "tabla_tasa_carga.tex",
                               caption="Dimensionamiento de infraestructura e indicadores de operación por "
                               "tasa de carga, producción base, zona norte, escenario red, generación y "
                               "almacenamiento.", label="tab:resultados-tasa-carga", con_tasa=True)
            escribir_costos_tex(costos_escenarios(res, TASAS), OUT / "tabla_costos_tasa_carga.tex",
                                caption="Costo total del horizonte por tasa de carga y componente, producción "
                                "base, zona norte, escenario red, generación y almacenamiento [kUSD, valor "
                                "presente de 10 años].", label="tab:costos-tasa-carga", con_tasa=True)
            for k in ESCALADA:
                figura_soc(por_clave[k], OUT / "soc_escalada_{}_invierno.png".format(ESCENARIOS[k][1]),
                           ESCENARIOS[k][0], "invierno", prod="escalada")
            figura_peak_produccion(por_clave["1"], por_clave["4"], OUT / "potencia_peak_produccion.png")
            for k_base, k_esc in (("1", "4"), ("2", "5")):
                for estacion in ("invierno", "verano"):
                    figura_estado_produccion(por_clave[k_base], por_clave[k_esc],
                                             OUT / "estado_flota_produccion_{}_{}.png".format(ESCENARIOS[k_esc][1],
                                                                                             estacion),
                                             ESCENARIOS[k_esc][0], estacion)
            escribir_produccion_tex(tabla_produccion(por_clave["1"], por_clave["4"]),
                                    OUT / "tabla_efecto_produccion.tex")
            escribir_tabla_tex(tabla_infraestructura(res, ("1", "4", "2", "5")),
                               OUT / "tabla_base_y_escalada_norte.tex",
                               caption="Dimensionamiento de infraestructura e indicadores de operación por "
                               "escenario y nivel de producción, zona norte. Con producción escalada, la "
                               r"carga \textit{on-board} usa cargadores de 1C.",
                               label="tab:resultados-produccion")
            escribir_tabla_tex(tabla_infraestructura(res, ESCALADA), OUT / "tabla_escalada_norte.tex",
                               "escalada norte")
            escribir_costos_tex(costos_escenarios(res, ESCALADA), OUT / "tabla_costos_escalada_norte.tex",
                                "escalada norte")
            escribir_leeme_general(res, OUT / "LEEME.md")

    if not a.sin_json_plotter:
        trabajos = [t for t in trabajos_json_plotter()
                    if any("/caso_{}_".format(c) in t[2] for c in claves)]
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(correr_json_plotter, *t): t for t in trabajos}
            for f in as_completed(futs):
                destino, n, fallas = f.result()
                print("json_plotter -> {} ({} png){}".format(
                    Path(destino).relative_to(OUT), n,
                    "" if not fallas else " | fallaron: " + "; ".join(fallas)), flush=True)


if __name__ == "__main__":
    main()
