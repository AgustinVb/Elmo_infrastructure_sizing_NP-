#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Battery swapping contra carga on-board, casos finales v4 de Mina_modelo: costo
total del horizonte desagregado y degradacion de las baterias por año.

CASOS: los 8 de la cola v4 (misma produccion, zona y recursos; los escalados se
comparan contra OB 1C porque OB 0,5C no tiene solucion). Tabla en
swap_vs_ob_v4_casos.csv y todo el detalle en swap_vs_ob_v4.json.

AJUSTES DE SWAP (oficiales, en este orden; el UB y la LB del modelo no cambian):
  1. Energia (ajuste_energia_swap.py): el modelo cobra cada recarga como el
     bloque completo bmin -> 100 %; se cambia por la energia real de cada swap.
  2. Redimensionamiento (redimensionar_gen_bess.py): con esa carga real fija, un
     LP con las ecuaciones del modelo vuelve a elegir solar, eolica, BESS, red y
     potencia de punta. Sin generacion ni BESS da lo mismo que el ajuste 1.
  El costo comparable de swap es el del ajuste 2. Control obligatorio: con la
  carga de la propia solucion, el LP reproduce lo que pago el modelo (swap y OB).

FIGURAS (dos pares del mismo sistema con y sin recursos propios):
  solo_red  Base, norte, 0,5C, solo red           swap RED          / OB P_red
  gen_bess  Base, centro, 0,5C, red + gen + BESS  swap RED_GEN_BESS / OB P_red_gen_bat

Los dos pares se pueden comparar en una sola figura aunque sean de zonas
distintas: los datos de norte y centro son identicos salvo GenProfiles (perfiles
solar y eolico; verificado hoja por hoja en Mina_modelo_v4), que sin generacion
no entra en el modelo. El caso solo red de norte es el mismo problema que el de
centro, asi que los dos pares son el mismo sistema con y sin recursos propios.

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
    linea "[cert] UB" del certificado, o el consumer si el caso no tiene
    certificado: OB 1C base y escalado, marcado en la tabla).
Swap necesita el parameters.json de cada solucion (no se versiona: se regenera
con escribir_parameters.py). El LP de OB usa el de swap: los parametros que
entran (precio, perfiles, costos de G y BESS, punta) son identicos.

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
from redimensionar_gen_bess import (TERMINOS, carga_de_solucion,   # noqa: E402
                                    redimensionar)

V4 = "output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4"
DATOS_OB = "data/Resultados_finales_tesis/Mina_modelo_v4"


def _caso(titulo, swap_dir, swap_sol, ub, ob_caso, ob_sol, consumer="consumer.log"):
    return {"titulo": titulo,
            "swap": {"sol": "{}/{}/{}/solucion".format(V4, swap_dir, swap_sol),
                     "consumer": "{}/{}/{}".format(V4, swap_dir, consumer), "ub": ub},
            "ob": {"caso": "{}/{}".format(V4, ob_caso), "sol": ob_sol,
                   "datos": "{}/{}/elmo_data.xlsx".format(DATOS_OB, ob_caso)}}


B, E = "Produccion_base", "Produccion_escalada20"
# Solucion final de cada caso (cola_swap_v4_algoritmo.md, §1) y su UB.
CASOS = {
    "1": _caso("Base, norte, 0,5C, solo red", B + "/zona_norte/05C/RED/dp_real",
               "mejorar_ub_1", 3294143.9406231567, B + "/zona_norte/05C/P_red",
               "mejorar_ub_1/solucion"),
    "2": _caso("Base, norte, 0,5C, red + generación + BESS",
               B + "/zona_norte/05C/RED_GEN_BESS/dp_it0", "mejorar_ub_2", 2493040.1556020295,
               B + "/zona_norte/05C/P_red_gen_bat", "mejorar_ub_2/solucion"),
    "3": _caso("Base, norte, 0,5C, red + generación", B + "/zona_norte/05C/RED_GEN/dp_real_b",
               "mejorar_ub_1", 2550277.5439307033, B + "/zona_norte/05C/P_red_gen",
               "mejorar_ub_2/solucion"),
    "4": _caso("Escalada, norte, solo red (swap 0,5C, OB 1C)", E + "/zona_norte/05C/RED/dp_real",
               "mejorar_ub_1", 4090093.054588234, E + "/zona_norte/1C/P_red",
               "mejorar_ub_2/solucion"),
    "5": _caso("Escalada, norte, red + generación + BESS (swap 0,5C, OB 1C)",
               E + "/zona_norte/05C/RED_GEN_BESS/milp_sinR/it0_energia_sol", "mejorar_ub_2",
               3098875.18, E + "/zona_norte/1C/P_red_gen_bat", "benders"),
    "6": _caso("Base, centro, 0,5C, red + generación + BESS",
               B + "/zona_centro/05C/RED_GEN_BESS/dp_real_t", "mejorar_ub_1", 2767909.361434111,
               B + "/zona_centro/05C/P_red_gen_bat", "mejorar_ub_2/solucion"),
    "7": _caso("Base, norte, 1C, red + generación + BESS",
               B + "/zona_norte/1C/RED_GEN_BESS/dp_it0", "mejorar_ub_2", 2743895.0411102045,
               B + "/zona_norte/1C/P_red_gen_bat", "mejorar_ub_1/solucion"),
    "8": _caso("Base, norte, 2C, red + generación + BESS",
               B + "/zona_norte/2C/RED_GEN_BESS/dp_it0", "mejorar_ub_3", 4949249.553883356,
               B + "/zona_norte/2C/P_red_gen_bat", "mejorar_ub_2/solucion",
               consumer="consumer_mejorar_ub_3.log"),
}
# Pares de las figuras (y de plot_despacho_swap_vs_ob_v4.py): sin generacion,
# norte y centro son el mismo problema.
PARES = {"solo_red": CASOS["1"], "gen_bess": CASOS["6"]}

# El LP con la carga de la solucion reproduce los terminos del consumer (redondeados
# a centavos); mas que esto es una diferencia de formulacion.
TOL_LP_USD = 1.0

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
    from ajuste_energia_swap import ajustar
    root = REPO_ROOT / cfg["sol"]
    params = json.loads((root / "parameters.json").read_text(encoding="utf-8"))
    ajuste = ajustar(root)
    return costos, inv, degradacion_consumer(texto, cfg["consumer"]), cfg["ub"], flota, \
        energia_justa_swap(root, costos["grid_energy_cost"], ajuste), \
        redimensionado_swap(cfg["sol"], params, costos, ajuste), params


def _dims(params):
    return sorted(int(y) for y in params["years"]), len(params["time_intervals_set"])


def controlar(nombre, costos, lp):
    """El LP con la carga de la propia solucion tiene que reproducir lo que pago el
    modelo en los seis terminos que redimensiona: si da mas, el LP no es el mismo
    problema (aborta); si da menos, la solucion no era optima en su
    dimensionamiento y el LP la mejora (aviso). Devuelve modelo - LP."""
    pagado = sum(costos[k] for k in TERMINOS)
    if lp["suma"] > pagado + TOL_LP_USD:
        raise SystemExit("{}: el LP con la carga de la solucion ({:,.2f}) da mas que el modelo "
                         "({:,.2f}): no es la misma formulacion".format(nombre, lp["suma"], pagado))
    if lp["suma"] < pagado - TOL_LP_USD:
        print("[aviso] {}: el LP con la carga de la solucion mejora el dimensionamiento del "
              "modelo en {:,.2f} USD".format(nombre, pagado - lp["suma"]))
    return pagado - lp["suma"]


def con_lp(filas, lp):
    """filas con los seis terminos del LP en vez de los del modelo."""
    out = dict(filas)
    for k in TERMINOS:
        out[k] = lp[k]
    out["total_cost"] = filas["total_cost"] - sum(filas[k] for k in TERMINOS) + lp["suma"]
    return out


def redimensionado_swap(sol, params, costos, ajuste):
    """Ajuste 2 (redimensionar_gen_bess.py): generacion, BESS, red y punta otra vez
    optimizadas para la carga real de swap (dem_aj del ajuste de energia), con el
    resto de la solucion fijo. Antes, el control con la carga del modelo."""
    years, T = _dims(params)
    root = REPO_ROOT / sol
    leer = lambda n: json.loads((root / n).read_text(encoding="utf-8")) if (root / n).exists() else {}
    lp_modelo = redimensionar(params, carga_de_solucion(leer, years, T))
    lp_real = redimensionar(params, {k: v["dem_aj"] for k, v in ajuste[0].items()})
    return {"lp_carga_modelo": lp_modelo, "lp_carga_real": lp_real,
            "control": controlar("swap " + sol, costos, lp_modelo)}


def redimensionado_ob(cfg, ref, params, costos):
    """Control de on-board: el mismo LP con su carga (de git) y los parametros de
    swap, que en lo que entra al LP son identicos."""
    sol = cfg["caso"] + "/" + cfg["sol"]

    def leer(n):
        r = subprocess.run(["git", "show", "{}:{}/{}".format(ref, sol, n)], cwd=REPO_ROOT,
                           capture_output=True)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    years, T = _dims(params)
    lp = redimensionar(params, carga_de_solucion(leer, years, T))
    return {"lp_carga_ob": lp, "control": controlar("OB " + sol, costos, lp)}


def energia_justa_swap(root, red_modelo, ajuste=None):
    """Costo de red de swap con la energia REAL de cada swap: el modelo cobra cada
    recarga como el bloque completo de t_charge intervalos (bmin -> 100 %), pero
    la bateria solo necesita reponer capacidad - B_llegada, dividido por
    eta_charge (convencion de esta rama, consumer.real_grid_energy_cost). Sin
    esto swap aparece consumiendo 16-20 % mas energia que on-board.

    El sobrante se ubica intervalo a intervalo y se descuenta en orden de merito
    (red -> descarga BESS -> generacion); si un swap pide mas que su bloque (1C y
    2C), el deficit se compra a la red, ver ajuste_energia_swap.py. El costo de
    red ajustado es el del modelo por la razon de energias de red descontadas
    (precio plano). Con solo red tiene que coincidir con real_grid_energy_cost
    del consumer: se verifica. ajuste: resultado de ajustar(root), si ya esta."""
    import consumer as c
    from ajuste_energia_swap import ajustar, totales
    out, res = ajuste or ajustar(root)
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
    tiene_cert = subprocess.run(["git", "cat-file", "-e", "{}:{}/certificado.log".format(
        ref, cfg["caso"])], cwd=REPO_ROOT, capture_output=True).returncode == 0
    if tiene_cert:
        cert = git_bytes(ref, cfg["caso"] + "/certificado.log").decode("utf-8", errors="replace")
        m = re.findall(r"\[cert\] UB ([0-9,.]+)", cert)
        if not m:
            raise SystemExit(cfg["caso"] + ": certificado.log sin linea '[cert] UB'")
        ub, ub_fuente = float(m[-1].replace(",", "")), "certificado"
    else:   # OB 1C base y escalado: sin certificado en carga_ob_multiaño
        ub, ub_fuente = costos["total_cost"], "consumer (sin certificado)"

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
    return costos, inv, degradacion_consumer(texto, cfg["caso"]), ub, n_lhd, ub_fuente


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
                 "Mina_modelo v4\nswap con la energía real de cada swap y generación/BESS "
                 "redimensionadas para ella")
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

    res, filas = {}, []
    for clave, caso in CASOS.items():
        c_s, inv_s, deg_s, ub_s, flota, aj, redim, params = lado_swap(caso["swap"])
        c_o, inv_o, deg_o, ub_o, n_lhd, ub_fuente = lado_ob(caso["ob"], a.ob_ref)
        modelo = reconciliar("swap " + clave, c_s, inv_s, ub_s)
        # Ajuste 1: el mismo plan con la energia real de los swaps.
        energia = dict(modelo)
        energia["grid_energy_cost"] = aj["central"]
        energia["total_cost"] = modelo["total_cost"] - modelo["grid_energy_cost"] + aj["central"]
        # Ajuste 2 (lo que se compara y grafica): generacion, BESS y red
        # redimensionadas para esa carga real. OB pasa por el mismo LP (control).
        ob_modelo = reconciliar("OB " + clave, c_o, inv_o, ub_o)
        redim_ob = redimensionado_ob(caso["ob"], a.ob_ref, params, ob_modelo)
        res[clave] = {
            "titulo": caso["titulo"],
            "swap": {"costos": con_lp(modelo, redim["lp_carga_real"]), "costos_modelo": modelo,
                     "costos_ajuste_energia": energia, "ajuste_energia": aj,
                     "redimensionamiento": redim, "deg": deg_s, "flota": flota,
                     "fuente": caso["swap"]["sol"]},
            "ob": {"costos": con_lp(ob_modelo, redim_ob["lp_carga_ob"]), "costos_modelo": ob_modelo,
                   "redimensionamiento": redim_ob, "ub_fuente": ub_fuente, "deg": deg_o,
                   "n_lhd": n_lhd, "fuente": "{}:{}/{}".format(a.ob_ref, caso["ob"]["caso"],
                                                              caso["ob"]["sol"])},
        }
        s, o = res[clave]["swap"]["costos"]["total_cost"], res[clave]["ob"]["costos"]["total_cost"]
        lr, lm, lo = redim["lp_carga_real"], redim["lp_carga_modelo"], redim_ob["lp_carga_ob"]
        print("[{}] swap modelo {:,.0f} -> energia real {:,.0f} -> redimensionado {:,.0f} | OB {:,.0f}"
              " ({}) | swap vs OB {:+.2%} (solo energia {:+.2%}) | controles {:+.2f} / {:+.2f}".format(
                  clave, modelo["total_cost"], energia["total_cost"], s, o, ub_fuente, s / o - 1,
                  energia["total_cost"] / o - 1, redim["control"], redim_ob["control"]))
        fila = {"caso": clave, "titulo": caso["titulo"], "swap_modelo": modelo["total_cost"],
                "swap_ajuste_energia": energia["total_cost"], "swap_redimensionado": s,
                "ob": o, "ob_ub_fuente": ub_fuente,
                "dif_ajuste_energia_pct": 100 * (energia["total_cost"] / o - 1),
                "dif_redimensionado_pct": 100 * (s / o - 1),
                "control_lp_swap": redim["control"], "control_lp_ob": redim_ob["control"],
                "bess_u_swap_modelo": lm["H"], "bess_u_swap_redim": lr["H"], "bess_u_ob": lo["H"]}
        for g in lr["G_kw"]:
            fila.update({"{}_kw_swap_modelo".format(g): lm["G_kw"][g],
                         "{}_kw_swap_redim".format(g): lr["G_kw"][g],
                         "{}_kw_ob".format(g): lo["G_kw"].get(g, 0.0)})
        filas.append(fila)

    (HERE / "swap_vs_ob_v4.json").write_text(
        json.dumps(res, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    csv = HERE / "swap_vs_ob_v4_casos.csv"
    pd.DataFrame(filas).to_csv(csv, index=False, float_format="%.2f")
    print("ok ->", csv)
    pares = {clave: res[next(k for k, c in CASOS.items() if c is par)] for clave, par in PARES.items()}
    png = HERE / "costo_total_swap_vs_ob_v4.png"
    figura_costos(pares, png)
    print("ok ->", png)
    for clave in pares:
        png = HERE / "degradacion_swap_vs_ob_v4_{}.png".format(clave)
        figura_degradacion(clave, pares[clave], png)
        print("ok ->", png)


if __name__ == "__main__":
    main()
