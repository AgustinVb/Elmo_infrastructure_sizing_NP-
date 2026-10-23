#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Battery swapping contra carga on-board, casos finales v4 de Mina_modelo: costo
total del horizonte desagregado y degradacion de las baterias por año.

PARES (misma produccion, zona, cargador y recursos), los dos en ZONA CENTRO:
  solo_red  Base, centro, 0,5C, solo red          swap RED          / OB P_red
  gen_bess  Base, centro, 0,5C, red + gen + BESS  swap RED_GEN_BESS / OB P_red_gen_bat

El caso solo red se corrio con la carpeta de zona_norte, pero vale para el
centro: los datos de las dos zonas son identicos salvo GenProfiles (perfiles
solar y eolico; verificado hoja por hoja en Mina_modelo_v4), que sin generacion
no entra en el modelo. Asi los dos pares son el mismo sistema con y sin recursos
propios, y sus costos se pueden comparar en una sola figura.

FUENTES. Swap se lee del disco (esta rama); on-board de git, sin cambiar de rama
(`git show <ref>:<ruta>`, por defecto origin/carga_ob_multiaño: hacer `git fetch`
antes). De cada lado se usa el consumer.log de la solucion final, que trae los
costos en valor presente (tabla COSTOS) y la trayectoria de degradacion (tabla
DEGRADACION). La inversion en estaciones del consumer viene sumada; se abre en
sus componentes:
  swap  con investment_breakdown de extract_costos_det.py sobre los JSON de la
        solucion y su parameters.json (estaciones+naves, cargadores, pool);
  OB    con Delta_X y Delta_N_chargers de la solucion (git) y los costos de
        elmo_data.xlsx (git): estacion c_fixed, una nave c_bays por cargador
        (max_chargers_per_bay = 1) y cargador charger_cost + c_charger_space.

RECONCILIACION OBLIGATORIA, el script aborta si algo no cuadra:
  - la inversion abierta reproduce "Costo inversion estaciones" del consumer;
  - los terminos suman el COSTO TOTAL del consumer;
  - el COSTO TOTAL es el UB de la solucion final (swap: el dado aqui; OB: la
    linea "[cert] UB" del certificado).

Descuento: year_position es 1-INDEXADA, df(y) = 1/(1+r)^y con y = 1..10.

    python cost_comparisons/comparar_swap_ob_v4.py [--ob_ref origin/carga_ob_multiaño]
"""
import argparse
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

from extract_costos_det import investment_breakdown              # noqa: E402

V4 = "output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base"
DATOS_OB = "data/Resultados_finales_tesis/Mina_modelo_v4/Produccion_base"

PARES = {
    "solo_red": {           # carpeta zona_norte: datos identicos a centro sin generacion
        "titulo": "Base, centro, 0,5C, solo red",
        "swap": {"sol": V4 + "/zona_norte/05C/RED/reparar_reemplazo/solucion",
                 "consumer": V4 + "/zona_norte/05C/RED/consumer.log",
                 "ub": 3338990.308513049},
        "ob": {"caso": V4 + "/zona_norte/05C/P_red",
               "sol": "mejorar_ub_1/solucion",
               "datos": DATOS_OB + "/zona_norte/05C/P_red/elmo_data.xlsx"},
    },
    "gen_bess": {
        "titulo": "Base, centro, 0,5C, red + generación + BESS",
        "swap": {"sol": V4 + "/zona_centro/05C/RED_GEN_BESS/mejorar_ub_2/solucion",
                 "consumer": V4 + "/zona_centro/05C/RED_GEN_BESS/consumer.log",
                 "ub": 2786793.9593614265},
        "ob": {"caso": V4 + "/zona_centro/05C/P_red_gen_bat",
               "sol": "mejorar_ub_2/solucion",
               "datos": DATOS_OB + "/zona_centro/05C/P_red_gen_bat/elmo_data.xlsx"},
    },
}

# Los consumer.log redondean a centavos: la suma de ~10 terminos puede correrse
# unos centavos del total impreso.
TOL_USD = 0.10

# Filas de la tabla COSTOS del consumer.log. Los logs de OB vienen con los
# acentos rotos (cp1252 leido como utf-8), por eso los patrones evitan las vocales
# acentuadas.
PATRONES_COSTO = [
    ("grid_energy_cost", r"energ.{1,3}a red P_red"),
    ("peak_power_cost", r"potencia (punta|pico)"),
    ("estaciones", r"inversi.{1,3}n estaciones"),
    ("substation_cost", r"inversi.{1,3}n subestaci"),
    ("gen_inv_cost", r"inversi.{1,3}n generaci"),
    ("gen_op_cost", r"operaci.{1,3}n generaci"),
    ("bess_inv_cost", r"inversi.{1,3}n BESS"),
    ("bess_op_cost", r"operaci.{1,3}n BESS"),
    ("battery_replace_cost", r"reemplazo bater"),
    ("penalty_cost", r"penalidad"),
    ("total_cost", r"COSTO TOTAL"),
]


def git_bytes(ref, ruta):
    try:
        return subprocess.run(["git", "show", "{}:{}".format(ref, ruta)], cwd=REPO_ROOT,
                              check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        raise SystemExit("git show {}:{} fallo: {}".format(
            ref, ruta, exc.stderr.decode(errors="replace").strip()))


def costos_consumer(texto, origen):
    """Tabla COSTOS del consumer.log -> {termino: USD}."""
    bloque = texto[texto.rfind("COSTOS"):]
    out = {}
    for clave, patron in PATRONES_COSTO:
        m = re.search(r"\|\s*Costo[^|]*?" + patron + r"[^|]*\|\s*([-0-9.]+)\s*\|", bloque) \
            if clave != "total_cost" else \
            re.search(r"\|\s*COSTO TOTAL[^|]*\|\s*([-0-9.]+)\s*\|", bloque)
        if not m:
            raise SystemExit("{}: no encuentro '{}' en la tabla COSTOS".format(origen, clave))
        out[clave] = float(m.group(m.lastindex))
    return out


def degradacion_consumer(texto, origen):
    """Tabla DEGRADACION del consumer.log -> filas (año, B inicio, D fin, reemplazo)."""
    i = texto.find("DEGRADACION BATERIA")
    if i < 0:
        raise SystemExit(origen + ": sin tabla DEGRADACION")
    filas = []
    for linea in texto[i:].splitlines()[1:]:
        celdas = [c.strip() for c in linea.strip().strip("|").split("|")]
        if len(celdas) >= 6 and celdas[0].isdigit():
            filas.append((int(celdas[0]), float(celdas[1]), float(celdas[4]),
                          celdas[5].upper().startswith("S")))
        elif filas and not linea.startswith(("|", "+")):
            break
    if not filas:
        raise SystemExit(origen + ": tabla DEGRADACION vacia")
    return filas


def serie(data, *ejes):
    """JSON de salida del modelo -> {año: valor}, bajando por los ejes dados."""
    for eje in ejes:
        data = data.get(eje, {}) if isinstance(data, dict) else {}
    return {int(float(k)): float(v) for k, v in data.items() if not isinstance(v, dict)}


def parametro(hoja, columna):
    """Las hojas de elmo_data traen una fila de unidades bajo el encabezado."""
    return float(hoja[columna].iloc[1])


def lado_swap(cfg):
    texto = (REPO_ROOT / cfg["consumer"]).read_text(encoding="utf-8", errors="replace")
    costos = costos_consumer(texto, cfg["consumer"])
    inv = investment_breakdown(REPO_ROOT / cfg["sol"])
    if abs(sum(inv.values()) - costos["estaciones"]) > TOL_USD:
        raise SystemExit("swap {}: la inversion abierta ({:,.2f}) no reproduce la del "
                         "consumer ({:,.2f})".format(cfg["sol"], sum(inv.values()),
                                                     costos["estaciones"]))
    flota = serie(json.loads((REPO_ROOT / cfg["sol"] / "n_battery_fleet.json").read_text()), "y")
    return costos, inv, degradacion_consumer(texto, cfg["consumer"]), cfg["ub"], flota, \
        energia_justa_swap(REPO_ROOT / cfg["sol"], costos["grid_energy_cost"])


def energia_justa_swap(root, red_modelo):
    """Costo de red de swap con la energia REAL de cada swap: el modelo cobra cada
    recarga como el bloque completo de t_charge intervalos (bmin -> 100 %), pero
    la bateria solo necesita reponer capacidad - B_llegada, dividido por
    eta_charge (convencion de esta rama, consumer.real_grid_energy_cost). Sin
    esto swap aparece consumiendo 16-20 % mas energia que on-board.

    El sobrante se ubica intervalo a intervalo y se descuenta en orden de merito
    (red -> descarga BESS -> generacion), ver ajuste_energia_swap.py. El costo de
    red ajustado es el del modelo por la razon de energias de red descontadas
    (precio plano). Con solo red tiene que coincidir con real_grid_energy_cost
    del consumer: se verifica."""
    import consumer as c
    from ajuste_energia_swap import ajustar, totales
    out, res = ajustar(root)
    tot = totales(out, res)
    red_aj = red_modelo * tot["red_aj_vp"] / tot["red_vp"]
    if tot["gen"] <= 1e-9:
        ref = c.calculate_total_costs(root)["real_grid_energy_cost"]
        if abs(red_aj - ref) > 1e-3 * ref:
            raise SystemExit("{}: el ajuste por intervalo ({:,.2f}) no reproduce "
                             "real_grid_energy_cost ({:,.2f})".format(root, red_aj, ref))
    return {"central": red_aj, "bajo": red_aj, "alto": red_aj,
            "energia_mwh": {k: tot[k] for k in ("dem", "dem_aj", "red", "red_aj", "gen", "gen_aj",
                                                 "des", "des_aj")}}


def lado_ob(cfg, ref):
    texto = git_bytes(ref, cfg["caso"] + "/consumer.log").decode("utf-8", errors="replace")
    costos = costos_consumer(texto, cfg["caso"] + "/consumer.log")
    cert = git_bytes(ref, cfg["caso"] + "/certificado.log").decode("utf-8", errors="replace")
    m = re.findall(r"\[cert\] UB ([0-9,.]+)", cert)
    if not m:
        raise SystemExit(cfg["caso"] + ": certificado.log sin linea '[cert] UB'")
    ub = float(m[-1].replace(",", ""))

    xls = pd.ExcelFile(io.BytesIO(git_bytes(ref, cfg["datos"])))
    st, ch = xls.parse("stations"), xls.parse("chargers")
    r = parametro(xls.parse("BatteryDegradation"), "discount_rate")
    c_fijo, c_nave = parametro(st, "c_fixed"), parametro(st, "c_bays")
    c_espacio, c_cargador = parametro(st, "c_charger_space"), parametro(ch, "charger_cost")
    if parametro(st, "max_chargers_per_bay") != 1:
        raise SystemExit(cfg["datos"] + ": max_chargers_per_bay != 1, no hay una nave por cargador")

    sol = cfg["caso"] + "/" + cfg["sol"]
    dx = serie(json.loads(git_bytes(ref, sol + "/Delta_X.json")), "k", "station_1", "y")
    dc = serie(json.loads(git_bytes(ref, sol + "/Delta_N_chargers.json")), "k", "station_1", "y")
    df = {y: 1.0 / (1.0 + r) ** y for y in range(1, 11)}
    inv = {"cost_stations": sum(c_fijo * v * df[y] for y, v in dx.items()),
           "cost_bays": sum(c_nave * v * df[y] for y, v in dc.items()),
           "cost_chargers": sum((c_cargador + c_espacio) * v * df[y] for y, v in dc.items()),
           "cost_batteries": 0.0}
    if abs(sum(inv.values()) - costos["estaciones"]) > TOL_USD:
        raise SystemExit("OB {}: la inversion abierta ({:,.2f}) no reproduce la del consumer "
                         "({:,.2f})".format(sol, sum(inv.values()), costos["estaciones"]))
    n_lhd = round(costos["battery_replace_cost"] * (1 + r) ** 6 / 299000.0) or None
    return costos, inv, degradacion_consumer(texto, cfg["caso"]), ub, n_lhd


def reconciliar(nombre, costos, inv, ub):
    filas = dict(inv)
    for k in ("substation_cost", "gen_inv_cost", "bess_inv_cost", "peak_power_cost",
              "grid_energy_cost", "gen_op_cost", "bess_op_cost", "battery_replace_cost",
              "penalty_cost"):
        filas[k] = costos[k]
    suma = sum(filas.values())
    if abs(suma - costos["total_cost"]) > TOL_USD:
        raise SystemExit("{}: los terminos suman {:,.2f} y el consumer da {:,.2f}".format(
            nombre, suma, costos["total_cost"]))
    if abs(costos["total_cost"] - ub) > TOL_USD:
        raise SystemExit("{}: el consumer ({:,.2f}) no es el UB de la solucion final "
                         "({:,.2f})".format(nombre, costos["total_cost"], ub))
    filas["total_cost"] = suma
    return filas


# ------------------------------------------------------------------ figuras --
STACK_ORDER = [  # paleta de plot_costo_horizonte_det.py / carga_ob_multiaño
    (("cost_stations", "cost_bays"), "#1F4E79", "Estaciones y naves"),
    (("cost_chargers",), "#2E75B6", "Cargadores"),
    (("cost_batteries",), "#5B9BD5", "Baterías (pool de swap)"),
    (("substation_cost",), "#BDD7EE", "Subestación"),
    (("gen_inv_cost",), "#375623", "Generación renovable"),
    (("bess_inv_cost",), "#A9D18E", "BESS"),
    (("peak_power_cost",), "#BF8F00", "Potencia punta"),
    (("grid_energy_cost",), "#ED7D31", "Energía de red"),
    (("gen_op_cost", "bess_op_cost"), "#FFD966", "O&M generación y BESS"),
    (("battery_replace_cost",), "#C00000", "Reemplazo de baterías"),
]


def miles(v):
    return "{:,.0f}".format(v).replace(",", ".")


def figura_costos(res, out_png):
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 12, "axes.labelsize": 11,
                         "xtick.labelsize": 10, "ytick.labelsize": 10, "legend.fontsize": 9})
    barras = []                                     # (x, etiqueta, filas)
    grupos = []
    for g, (clave, r) in enumerate(res.items()):
        x0 = g * 2.6
        barras += [(x0, "Battery\nswapping", r["swap"]["costos"]),
                   (x0 + 1.0, "On-board", r["ob"]["costos"])]
        grupos.append((x0 + 0.5, PARES[clave]["titulo"]))

    xs = np.array([b[0] for b in barras])
    totales = np.array([b[2]["total_cost"] for b in barras]) / 1000.0
    ymax = totales.max() * 1.10
    # Achatada para una slide 16:9 (con la leyenda a la derecha queda ~2:1).
    fig, ax = plt.subplots(figsize=(3.0 + 3.6 * len(grupos), 5.0))
    bottom = np.zeros(len(barras))
    for claves, color, etiqueta in STACK_ORDER:
        vals = np.array([sum(b[2][k] for k in claves) for b in barras]) / 1000.0
        if not np.any(vals > 0):
            continue
        ax.bar(xs, vals, 0.72, bottom=bottom, color=color, label=etiqueta,
               edgecolor="black", linewidth=0.6)
        for xi, v, b0 in zip(xs, vals, bottom):
            if v >= 0.022 * ymax:
                ax.text(xi, b0 + v / 2, miles(v), ha="center", va="center", fontsize=9,
                        color="white", fontweight="bold",
                        path_effects=[pe.withStroke(linewidth=1.8, foreground="#111111")])
        bottom += vals
    if np.max(np.abs(bottom - totales)) > 1e-6 * totales.max():
        raise SystemExit("el apilado no cuadra con total_cost: falta una categoria en STACK_ORDER")

    for xi, tot in zip(xs, totales):
        ax.text(xi, tot + 0.012 * ymax, miles(tot), ha="center", va="bottom",
                fontsize=11, fontweight="bold")
    for g in range(len(grupos)):
        s, o = totales[2 * g], totales[2 * g + 1]
        ax.annotate("swap {:+.1%} vs on-board".format(s / o - 1),
                    xy=(xs[2 * g] + 0.5, max(s, o) + 0.075 * ymax), ha="center",
                    va="bottom", fontsize=9, color="#333333")

    ax.set_xticks(xs)
    ax.set_xticklabels([b[1] for b in barras])
    for xg, titulo in grupos:
        ax.text(xg, -0.15, titulo, transform=ax.get_xaxis_transform(), ha="center",
                va="top", fontsize=10, fontweight="bold")
    ax.set_ylim(0, ymax * 1.08)
    ax.set_xlim(xs.min() - 0.7, xs.max() + 0.7)
    ax.set_title("Costo total del horizonte (10 años, VP) — battery swapping vs on-board, "
                 "Mina_modelo v4")
    ax.set_ylabel("Costo total [kUSD]")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    # Invertida para que el orden de la leyenda siga el del apilado (arriba = ultimo).
    h, l = ax.get_legend_handles_labels()
    ax.legend(h[::-1], l[::-1], loc="center left", bbox_to_anchor=(1.01, 0.5), ncol=1,
              frameon=True, framealpha=0.95, fancybox=False, edgecolor="gray")
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


COLOR_BARRA, COLOR_REEMPLAZO = "#1F77B4", "#ED7D31"
COLOR_LINEA, COLOR_MARCADOR, COLOR_REF = "#BFBFBF", "#9DB8CC", "#333333"


def figura_degradacion(clave, r, out_png, b_max=482.0, frac_piso=0.8):
    piso = frac_piso * b_max
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.labelsize": 11,
                         "legend.fontsize": 9})
    fig, axes = plt.subplots(1, 2, figsize=(14.0, 4.4), sharey=True)
    flota = r["swap"]["flota"]
    paneles = [
        (axes[0], r["swap"]["deg"], "Battery swapping — flota {}→{} baterías "
         "(4 en LHD + pool)".format(int(min(flota.values())), int(max(flota.values())))),
        (axes[1], r["ob"]["deg"], "On-board — flota {} baterías (una por LHD)".format(
            r["ob"]["n_lhd"] or "?")),
    ]
    for ax, deg, titulo in paneles:
        anios = [f[0] for f in deg]
        x = np.arange(len(anios))
        b = np.array([f[1] for f in deg])
        d = np.array([f[2] for f in deg])
        rep = [f[3] for f in deg]
        ax.bar(x, b, 0.62, color=[COLOR_REEMPLAZO if s else COLOR_BARRA for s in rep], zorder=2)
        ax.plot(x, d, color=COLOR_LINEA, linewidth=1.4, marker="o", markersize=5,
                markerfacecolor=COLOR_MARCADOR, markeredgecolor="#7F7F7F",
                markeredgewidth=0.6, zorder=4)
        ax.axhline(b_max, color=COLOR_REF, linewidth=1.2, linestyle="--", zorder=3)
        ax.axhline(piso, color=COLOR_REF, linewidth=1.2, linestyle=":", zorder=3)
        for i, s in enumerate(rep):
            if s:
                ax.text(x[i], b[i] + 0.012 * b_max, "Reemplazo", ha="center", va="bottom",
                        fontsize=10, color=COLOR_REEMPLAZO, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([str(a) for a in anios])
        ax.set_xlim(-0.7, len(anios) - 0.3)
        ax.set_ylim(0, b_max * 1.10)
        ax.set_title(titulo)
        ax.set_xlabel("Año del horizonte")
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("Capacidad por batería [kWh]")
    fig.suptitle("Degradación de las baterías — " + PARES[clave]["titulo"], fontsize=12, y=1.0)
    handles = [
        Line2D([0], [0], color=COLOR_LINEA, linewidth=1.4, marker="o", markersize=5,
               markerfacecolor=COLOR_MARCADOR, markeredgecolor="#7F7F7F",
               label="Capacidad al final del año"),
        Line2D([0], [0], color=COLOR_REF, linewidth=1.2, linestyle="--",
               label="Nominal ({:.0f} kWh)".format(b_max)),
        Line2D([0], [0], color=COLOR_REF, linewidth=1.2, linestyle=":",
               label="Piso {:.0%} ({:.1f} kWh)".format(frac_piso, piso)),
        Patch(facecolor=COLOR_BARRA, label="Capacidad al inicio del año"),
        Patch(facecolor=COLOR_REEMPLAZO, label="Año con reemplazo"),
    ]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=5,
               frameon=True, framealpha=0.95, fancybox=False, edgecolor="gray")
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ob_ref", default="origin/carga_ob_multiaño")
    a = ap.parse_args()

    res = {}
    for clave, par in PARES.items():
        c_s, inv_s, deg_s, ub_s, flota, aj = lado_swap(par["swap"])
        c_o, inv_o, deg_o, ub_o, n_lhd = lado_ob(par["ob"], a.ob_ref)
        modelo = reconciliar("swap " + clave, c_s, inv_s, ub_s)
        # Lo que se grafica: el mismo plan con la energia real de los swaps.
        justo = dict(modelo)
        justo["grid_energy_cost"] = aj["central"]
        justo["total_cost"] = modelo["total_cost"] - modelo["grid_energy_cost"] + aj["central"]
        res[clave] = {
            "swap": {"costos": justo, "costos_modelo": modelo, "ajuste_energia": aj,
                     "rango_total": [modelo["total_cost"] - modelo["grid_energy_cost"] + aj[k]
                                     for k in ("bajo", "alto")],
                     "deg": deg_s, "flota": flota, "fuente": par["swap"]["sol"]},
            "ob": {"costos": reconciliar("OB " + clave, c_o, inv_o, ub_o), "deg": deg_o,
                   "n_lhd": n_lhd, "fuente": "{}:{}/{}".format(a.ob_ref, par["ob"]["caso"],
                                                              par["ob"]["sol"])},
        }
        s, o = res[clave]["swap"]["costos"]["total_cost"], res[clave]["ob"]["costos"]["total_cost"]
        lo, hi = res[clave]["swap"]["rango_total"]
        print("[{}] reconcilia. swap modelo {:,.2f} -> energia real {:,.2f} (rango {:,.0f}-{:,.0f})  "
              "OB {:,.2f}  ({:+.1%}; modelo {:+.1%})".format(
                  clave, modelo["total_cost"], s, lo, hi, o, s / o - 1, modelo["total_cost"] / o - 1))

    (HERE / "swap_vs_ob_v4.json").write_text(
        json.dumps(res, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    png = HERE / "costo_total_swap_vs_ob_v4.png"
    figura_costos(res, png)
    print("ok ->", png)
    for clave in res:
        png = HERE / "degradacion_swap_vs_ob_v4_{}.png".format(clave)
        figura_degradacion(clave, res[clave], png)
        print("ok ->", png)


if __name__ == "__main__":
    main()
