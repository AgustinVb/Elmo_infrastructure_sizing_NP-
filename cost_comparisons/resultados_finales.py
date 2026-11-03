#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Resultados finales v4 de Mina_modelo: battery swapping contra carga on-board en
los 8 casos, con la solucion final del L-shaped para swap. Todo se escribe en
cost_comparisons/resultados_finales/:

  resumen_swap_vs_ob_v4.xlsx      costos, infraestructura, energia, degradacion
                                  y las tablas del consumer de los dos lados
  costo_total_8_casos.png         swap ajustado contra on-board, desagregado
  caso_<n>_<escenario>/
    comparacion/                  costos (modelo, ajustes, OB), infraestructura
                                  por año y degradacion, swap contra OB
    battery_swapping_modelo/      json_plotter de esta rama sobre la solucion
                                  del L-shaped (lo que decidio el modelo)
    battery_swapping_ajustado/    despacho y energia de swap despues de los dos
                                  ajustes, al lado del despacho del modelo
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
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.ticker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ajuste_energia_swap import DIAS, _serie, ajustar
from comparar_swap_ob_v4 import (CASOS as CASOS_V4, HERE, REPO_ROOT, STACK_ORDER, V4,
                                 con_lp, lado_ob, lado_swap, miles, reconciliar,
                                 redimensionado_ob)
from redimensionar_gen_bess import TERMINOS, redimensionar

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


def despacho_solucion(sol, years, T):
    J = lambda n: leer_json(sol, n)
    red, gen, bat = J("P_red.json"), J("P_gen.json"), J("P_bat.json")
    out = {}
    for y in years:
        for d in DIAS:
            r = _serie(red, y, d, T)
            g = _serie(gen, y, d, T, "g") if gen else np.zeros(T)
            b = _serie(bat, y, d, T, "h") if bat else np.zeros(T)
            out[(y, d)] = {"red": r, "gen": g, "bat": b, "carga": r + g + b}
    return out


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
    # ahora guardando el despacho).
    sol_s = REPO_ROOT / cfg["sol"]
    sol_o = OB_REPO / ob_cfg["caso"] / ob_cfg["sol"]
    years = sorted(int(y) for y in params["years"])
    T = len(params["time_intervals_set"])
    dt, esc = float(params["delta_t"]), float(params["scaling_factor_op_cost"])
    out_aj, _ = ajustar(sol_s)
    lp = redimensionar(params, {k: v["dem_aj"] for k, v in out_aj.items()}, series=True)
    if abs(lp["suma"] - redim["lp_carga_real"]["suma"]) > 1e-3:
        raise SystemExit("caso {}: el LP con series no reproduce el redimensionamiento".format(clave))
    params_ob = json.loads((sol_o / "parameters.json").read_text(encoding="utf-8"))
    desp = {
        "modelo": {k: {"carga": v["dem"], "red": v["red"], "gen": v["gen"], "bat": v["bat"]}
                   for k, v in out_aj.items()},
        "ajuste_energia": {k: {"carga": v["dem_aj"], "red": v["red_aj"], "gen": v["gen_aj"],
                               "bat": v["bat_aj"]} for k, v in out_aj.items()},
        "ajustado": {k: {"carga": v["carga"], "red": v["red"], "gen": sum(v["gen"].values())
                         if v["gen"] else np.zeros(T), "bat": v["bat"]}
                     for k, v in lp["series"].items()},
        "ob": despacho_solucion(sol_o, years, T),
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
        "despacho": desp, "esc_dt": esc * dt, "dt": dt, "P_pot_aj": lp["P_pot"],
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
    return {"Resumen": pd.DataFrame(resumen), "Costos": pd.DataFrame(costos),
            "Infraestructura": pd.DataFrame(infra), "Infraestructura anual": pd.DataFrame(anual),
            "Energía": pd.DataFrame(ener), "Degradación": pd.DataFrame(deg),
            "Consumer": cons}


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
                "despacho del LP redimensionado; OB: despacho de la solución OB."),
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
    variantes = ["Swap modelo", "Swap ajuste energía", "Swap ajustado", "OB comparable"]
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
    ax.set_xticklabels(["BS modelo", "BS ajuste\nenergía", "BS ajustado\n(comparable)", "On-board"])
    ax.set_ylim(0, ymax)
    ax.set_ylabel("Costo total [kUSD, VP 10 años]")
    ax.set_title("Caso {}: {}\nBS ajustado vs on-board: {:+.2%}".format(
        r["clave"], r["titulo"], tot[2] / tot[3] - 1), fontsize=11)
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
        for tec, color, et, dz in (("Swap", COLOR_SWAP, "Battery swapping", 0.0),
                                   ("OB", COLOR_OB, "On-board", 0.04)):
            f = r["infra"][tec]
            xs, vs = escalon([x["Año"] for x in f], [x[col] + dz * (col != "P_pot [kW]") for x in f])
            ax.step(xs, vs, where="post", color=color, linewidth=2, label=et)
        if col == "P_pot [kW]":
            ys = sorted(r["P_pot_aj"])
            xs, vs = escalon(ys, [r["P_pot_aj"][y] for y in ys])
            ax.step(xs, vs, where="post", color=COLOR_SWAP, linestyle="--", label="BS ajustado")
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


def figura_degradacion(r, out_png, b_max=482.0):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4), sharey=True)
    for ax, (tec, titulo) in zip(axes, (("Swap", "Battery swapping"), ("OB", "On-board"))):
        deg = r["deg"][tec]
        x = np.arange(len(deg))
        ax.bar(x, [d[1] for d in deg], 0.6, color=[COLOR_OB if d[3] else COLOR_SWAP for d in deg],
               label="Capacidad al inicio (naranjo: reemplazo)")
        ax.plot(x, [d[2] for d in deg], "o-", color="#7F7F7F", label="Capacidad al final")
        ax.axhline(b_max, color="#333", linestyle="--", linewidth=1)
        ax.axhline(0.8 * b_max, color="#333", linestyle=":", linewidth=1, label="Piso 80 %")
        ax.set_xticks(x)
        ax.set_xticklabels([d[0] for d in deg])
        ax.set_title(titulo)
        ax.set_xlabel("Año")
        ax.set_ylim(0, b_max * 1.1)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("Capacidad por batería [kWh]")
    axes[0].legend(fontsize=8, loc="lower left")
    fig.suptitle("Caso {}: degradación de baterías — {}".format(r["clave"], r["titulo"]), fontsize=11)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def panel_despacho(ax, v, horas, titulo):
    dis, carga_bess = np.maximum(v["bat"], 0), np.minimum(v["bat"], 0)
    capas = [(v["red"], "#ED7D31", "Red"), (v["gen"], "#70AD47", "Generación"),
             (dis, "#5B9BD5", "Descarga BESS")]
    base = np.zeros_like(horas)
    for val, color, et in capas:
        if np.any(val > 1e-6):
            ax.fill_between(horas, base, base + val, step="post", color=color, alpha=0.85, label=et,
                            linewidth=0)
        base = base + val
    if np.any(carga_bess < -1e-6):
        ax.fill_between(horas, carga_bess, 0, step="post", color="#A5A5A5", alpha=0.85,
                        label="Carga BESS", linewidth=0)
    ax.step(horas, v["carga"], where="post", color="black", linewidth=1.2, label="Consumo estación")
    ax.set_title(titulo, fontsize=10)
    ax.set_xlabel("Hora del día")
    ax.set_xlim(0, horas[-1] + horas[1])
    ax.set_xticks(range(0, 25, 4))
    ax.grid(alpha=0.3)


def figuras_ajustado(r, carpeta):
    T = len(next(iter(r["despacho"]["modelo"].values()))["carga"])
    horas = np.arange(T) * r["dt"]
    for (y, d) in sorted(r["despacho"]["modelo"]):
        fig, axes = plt.subplots(1, 3, figsize=(18, 4.2), sharey=True)
        for ax, (k, tit) in zip(axes, (("modelo", "BS modelo"), ("ajustado", "BS ajustado (comparable)"),
                                       ("ob", "On-board"))):
            panel_despacho(ax, r["despacho"][k][(y, d)], horas, tit)
        axes[0].set_ylabel("Potencia [kW]")
        h, l = axes[0].get_legend_handles_labels()
        for ax in axes[1:]:
            for hh, ll in zip(*ax.get_legend_handles_labels()):
                if ll not in l:
                    h.append(hh)
                    l.append(ll)
        fig.legend(h, l, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=len(l), fontsize=9)
        fig.suptitle("Caso {}: despacho año {} día {}".format(r["clave"], y, d), fontsize=11)
        fig.savefig(carpeta / "despacho_Y{}_d{}.png".format(y, d), dpi=130, bbox_inches="tight")
        plt.close(fig)
    # Energia por año
    fig, axes = plt.subplots(1, 2, figsize=(14, 4.2))
    for ax, campo, titulo in ((axes[0], "carga", "Consumo de las estaciones"),
                              (axes[1], "red", "Energía comprada a la red")):
        for k, color, ls, et in (("modelo", COLOR_SWAP, "-", "BS modelo"),
                                 ("ajustado", COLOR_SWAP, "--", "BS ajustado"),
                                 ("ob", COLOR_OB, "-", "On-board")):
            _, anual = energia(r["despacho"][k], r["esc_dt"])
            ys = sorted(anual)
            ax.plot(ys, [anual[y][campo] for y in ys], ls, color=color, marker="o", markersize=4, label=et)
        ax.set_title(titulo)
        ax.set_xlabel("Año")
        ax.set_ylabel("MWh/año")
        ax.set_xticks(range(1, 11))
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=9)
    fig.suptitle("Caso {}: energía por año — {}".format(r["clave"], r["titulo"]), fontsize=11)
    fig.savefig(carpeta / "energia_anual.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def etiqueta_corta(clave):
    """'Produccion_base/zona_norte/05C/RED_GEN_BESS' -> 'Base, norte\\n0,5C\\nRED+GEN+BESS'."""
    prod, zona, c, rec = FINALES[clave][0].split("/")
    c = "0,5C" if c == "05C" else c
    if prod == E:
        c += " (OB 1C)"
    return "{}, {}\n{}\n{}".format("Escalada" if prod == E else "Base", zona.replace("zona_", ""),
                                   c, rec.replace("_", "+"))


def figura_8_casos(res, out_png):
    barras, grupos = [], []
    for g, r in enumerate(res):
        x0 = g * 2.6
        barras += [(x0, "BS", r["costos"]["Swap ajustado"]), (x0 + 1.0, "OB", r["costos"]["OB comparable"])]
        grupos.append((x0 + 0.5, "Caso {}".format(r["clave"]), r))
    xs = np.array([b[0] for b in barras])
    tot = np.array([b[2]["total_cost"] for b in barras]) / 1000.0
    ymax = tot.max() * 1.15
    fig, ax = plt.subplots(figsize=(18, 6))
    bottom = np.zeros(len(barras))
    for claves, color, etiqueta in STACK_ORDER:
        vals = np.array([sum(b[2][k] for k in claves) for b in barras]) / 1000.0
        if not np.any(vals > 0):
            continue
        ax.bar(xs, vals, 0.8, bottom=bottom, color=color, label=etiqueta, edgecolor="black", linewidth=0.5)
        bottom += vals
    for xi, t in zip(xs, tot):
        ax.text(xi, t + 0.01 * ymax, miles(t), ha="center", va="bottom", fontsize=8, fontweight="bold")
    for g, (xg, et, r) in enumerate(grupos):
        s, o = tot[2 * g], tot[2 * g + 1]
        ax.text(xg, max(s, o) + 0.06 * ymax, "{:+.1%}".format(s / o - 1), ha="center", fontsize=9)
        ax.text(xg, -0.09, "{}\n{}".format(et, etiqueta_corta(r["clave"])),
                transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8)
    ax.set_xticks(xs)
    ax.set_xticklabels([b[1] for b in barras])
    ax.set_ylim(0, ymax)
    ax.set_ylabel("Costo total [kUSD, VP 10 años]")
    ax.set_title("Costo total del horizonte: battery swapping ajustado (L-shaped final) vs on-board, "
                 "Mina_modelo v4 (encima: BS vs OB)")
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
| Battery swapping, modelo (UB del L-shaped) | {s_m:,.2f} |
| Battery swapping, ajuste de energía | {s_e:,.2f} |
| **Battery swapping, ajustado (comparable)** | **{s_a:,.2f}** |
| **On-board (comparable)** | **{o_c:,.2f}** |
| BS ajustado vs OB | {dif:+.2%} |

LB del swap: {lb:,.2f} ({lb_fuente}), gap {gap:.2%}.

## Carpetas

- `comparacion/`: costos (modelo, ajustes y OB), infraestructura por año y degradación.
- `battery_swapping_modelo/`: `json_plotter.py` de esta rama sobre la solución del
  L-shaped. **Son gráficos del modelo, antes del ajuste**: cada recarga aparece como el
  bloque completo bmin → 100 % y la generación/BESS son las que eligió el modelo.
- `battery_swapping_ajustado/`: despacho de cada año y día muestreado con BS modelo,
  BS ajustado (energía real de cada swap y generación, BESS y red redimensionadas)
  y on-board; `energia_anual.png` compara consumo y compra a la red por año.
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

| Caso | Escenario | BS modelo (UB) | BS ajustado | On-board | BS vs OB | Gap BS |
|---:|---|---:|---:|---:|---:|---:|
{filas}

## Contenido

- `resumen_swap_vs_ob_v4.xlsx`: hojas Resumen, Costos (cinco variantes por caso),
  Infraestructura (evolución y capacidades), Infraestructura anual, Energía (MWh del
  horizonte), Degradación, Consumer (todas las tablas de consumer.py de los dos lados) y
  Notas (definiciones y controles).
- `costo_total_8_casos.png`: BS ajustado vs OB, desagregado.
- `caso_<n>_<escenario>/`: `comparacion/`, `battery_swapping_modelo/` (json_plotter, antes
  del ajuste), `battery_swapping_ajustado/` y `on_board/` (json_plotter de Elmo_ob); ver
  el LEEME.md de cada caso.

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
        filas.append("| {} | {} | {:,.0f} | {:,.0f} | {:,.0f} | {:+.2%} | {:.2%} |".format(
            r["clave"], r["titulo"], r["ub"], s_a, o_c, s_a / o_c - 1, (r["ub"] - r["lb"]) / r["ub"]))
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
                clave=clave, titulo=r["titulo"], s_m=c["Swap modelo"]["total_cost"],
                s_e=c["Swap ajuste energía"]["total_cost"], s_a=s_a, o_c=o_c, dif=s_a / o_c - 1,
                lb=r["lb"], lb_fuente=r["lb_fuente"],
                gap=(r["ub"] - r["lb"]) / r["ub"], swap_sol=r["swap_sol"], ob_sol=r["ob_sol"]),
                encoding="utf-8")
            print("[{}] swap modelo {:,.0f} -> ajustado {:,.0f} | OB {:,.0f} | {:+.2%} | controles "
                  "{:+.2f} / {:+.2f}".format(clave, r["ub"], s_a, o_c, s_a / o_c - 1,
                                             r["control_swap"], r["control_ob"]), flush=True)
        escribir_excel(res, OUT / "resumen_swap_vs_ob_v4.xlsx")
        if len(res) == len(FINALES):
            figura_8_casos(res, OUT / "costo_total_8_casos.png")
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
