#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Carga on-board, casos finales v4 de Mina_modelo: impacto de la tasa de carga, de
la ubicacion y de la produccion sobre el costo total del horizonte (10 años, VP).

Cada panel cambia UNA variable, todo con red + generacion + BESS:
  tasa de carga  base, norte: 0,5C (caso 2), 1C (caso 7), 2C (caso 8)
  ubicacion      base, 0,5C:  norte (caso 2) vs centro (caso 6)
  produccion     norte, 1C:   base (caso 7) vs escalada +20 % (caso 5)
                 (1C porque con 0,5C la escalada es infactible en on-board)

FUENTES: git (origin/carga_ob_multiaño, hacer `git fetch` antes), sin cambiar de
rama. Por caso: el consumer.log de la solucion final (costos en VP; la carpeta
de la solucion sale de su fila "Folder"), su Delta_X / Delta_N_chargers y los
costos de su elmo_data.xlsx para abrir la inversion en estaciones+naves y
cargadores (como comparar_swap_ob_v4.lado_ob). El UB sale de resumen.txt de la
cola v4 (certificado o, en los casos 5 y 7, la brecha del Benders).

RECONCILIACION, aborta si no cuadra: la inversion abierta reproduce la del
consumer, los terminos suman el COSTO TOTAL y este es el UB del caso.

    python cost_comparisons/sensibilidad_ob_v4.py [--ob_ref origin/carga_ob_multiaño]
"""
import argparse
import io
import json
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from comparar_swap_ob_v4 import (STACK_ORDER, TOL_USD, costos_consumer,   # noqa: E402
                                 git_bytes, miles, parametro, serie)

V4 = "output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4"
DATOS = "data/Resultados_finales_tesis/Mina_modelo_v4"

CASOS = {
    2: "Produccion_base/zona_norte/05C/P_red_gen_bat",
    7: "Produccion_base/zona_norte/1C/P_red_gen_bat",
    8: "Produccion_base/zona_norte/2C/P_red_gen_bat",
    6: "Produccion_base/zona_centro/05C/P_red_gen_bat",
    5: "Produccion_escalada20/zona_norte/1C/P_red_gen_bat",
}
# (titulo del panel, [(caso, etiqueta de la barra)]); la primera barra es la referencia.
PANELES = [
    ("Tasa de carga\n(base, norte)", [(2, "0,5C\n241 kW"), (7, "1C\n482 kW"), (8, "2C\n964 kW")]),
    ("Ubicación\n(base, 0,5C)", [(2, "Norte"), (6, "Centro")]),
    ("Producción\n(norte, 1C)", [(7, "Base"), (5, "Escalada\n+20 %")]),
]


def ub_de_resumen(ref):
    """{caso: UB} de resumen.txt: 'contra UB x' (certificado) o 'UB x  LB' (Benders)."""
    txt = git_bytes(ref, V4 + "/resumen.txt").decode("utf-8", errors="replace")
    out = {}
    for linea in txt.splitlines():
        m = re.match(r"\s*(\d+)\.\s", linea)
        u = re.search(r"contra UB ([0-9,.]+)", linea) or re.search(r"\bUB ([0-9.]+)\s+LB", linea)
        if m and u:
            out[int(m.group(1))] = float(u.group(1).replace(",", ""))
    return out


def caso(n, ruta, ub, ref):
    texto = git_bytes(ref, "{}/{}/consumer.log".format(V4, ruta)).decode("utf-8", errors="replace")
    costos = costos_consumer(texto, ruta)
    m = re.search(r"\|\s*Folder\s*\|\s*(\S+)\s*\|", texto)
    if not m:
        raise SystemExit(ruta + ": consumer.log sin fila Folder")
    sol = m.group(1).replace("\\", "/")

    xls = pd.ExcelFile(io.BytesIO(git_bytes(ref, "{}/{}/elmo_data.xlsx".format(DATOS, ruta))))
    st, ch = xls.parse("stations"), xls.parse("chargers")
    r = parametro(xls.parse("BatteryDegradation"), "discount_rate")
    if parametro(st, "max_chargers_per_bay") != 1:
        raise SystemExit(ruta + ": max_chargers_per_bay != 1, no hay una nave por cargador")
    dx = serie(json.loads(git_bytes(ref, sol + "/Delta_X.json")), "k", "station_1", "y")
    dc = serie(json.loads(git_bytes(ref, sol + "/Delta_N_chargers.json")), "k", "station_1", "y")
    df = {y: 1.0 / (1.0 + r) ** y for y in range(1, 11)}
    c_car = parametro(ch, "charger_cost") + parametro(st, "c_charger_space")
    filas = {"cost_stations": sum(parametro(st, "c_fixed") * v * df[y] for y, v in dx.items()),
             "cost_bays": sum(parametro(st, "c_bays") * v * df[y] for y, v in dc.items()),
             "cost_chargers": sum(c_car * v * df[y] for y, v in dc.items()),
             "cost_batteries": 0.0}
    if abs(sum(filas.values()) - costos["estaciones"]) > TOL_USD:
        raise SystemExit("caso {}: la inversion abierta ({:,.2f}) no reproduce la del consumer "
                         "({:,.2f})".format(n, sum(filas.values()), costos["estaciones"]))
    for k in ("substation_cost", "gen_inv_cost", "bess_inv_cost", "peak_power_cost",
              "grid_energy_cost", "gen_op_cost", "bess_op_cost", "battery_replace_cost", "penalty_cost"):
        filas[k] = costos[k]
    suma = sum(filas.values())
    if abs(suma - costos["total_cost"]) > TOL_USD or abs(suma - ub) > TOL_USD:
        raise SystemExit("caso {}: terminos {:,.2f}, consumer {:,.2f}, UB {:,.2f}".format(
            n, suma, costos["total_cost"], ub))
    filas["total_cost"] = suma
    return filas


def figura(costos, out_png):
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11.5, "axes.labelsize": 11,
                         "xtick.labelsize": 9.5, "ytick.labelsize": 10, "legend.fontsize": 9})
    anchos = [len(barras) for _t, barras in PANELES]
    fig, axes = plt.subplots(1, len(PANELES), figsize=(15.5, 5.4), sharey=True,
                             gridspec_kw={"width_ratios": anchos, "wspace": 0.06})
    ymax = max(costos[n]["total_cost"] for _t, b in PANELES for n, _e in b) / 1000.0 * 1.12
    for ax, (titulo, barras) in zip(axes, PANELES):
        xs = np.arange(len(barras))
        bottom = np.zeros(len(barras))
        for claves, color, etiqueta in STACK_ORDER:
            vals = np.array([sum(costos[n][k] for k in claves) for n, _e in barras]) / 1000.0
            if not np.any(vals > 0):
                continue
            ax.bar(xs, vals, 0.62, bottom=bottom, color=color, label=etiqueta,
                   edgecolor="black", linewidth=0.6)
            for xi, v, b0 in zip(xs, vals, bottom):
                if v >= 0.035 * ymax:
                    ax.text(xi, b0 + v / 2, miles(v), ha="center", va="center", fontsize=8.5,
                            color="white", fontweight="bold",
                            path_effects=[pe.withStroke(linewidth=1.6, foreground="#111111")])
            bottom += vals
        ref = bottom[0]
        for i, (xi, tot) in enumerate(zip(xs, bottom)):
            txt = miles(tot) if i == 0 else "{}\n({:+.0%})".format(miles(tot), tot / ref - 1)
            ax.text(xi, tot + 0.012 * ymax, txt, ha="center", va="bottom", fontsize=10,
                    fontweight="bold")
        ax.set_xticks(xs)
        ax.set_xticklabels([e for _n, e in barras])
        ax.set_xlim(-0.6, len(barras) - 0.4)
        ax.set_title(titulo)
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    axes[0].set_ylim(0, ymax)
    axes[0].set_ylabel("Costo total [kUSD]")
    # Leyenda con todas las categorias presentes, en el orden del apilado (arriba = ultimo).
    vistos = {}
    for ax in axes:
        for h, l in zip(*ax.get_legend_handles_labels()):
            vistos.setdefault(l, h)
    orden = [e for _c, _col, e in STACK_ORDER if e in vistos][::-1]
    fig.legend([vistos[e] for e in orden], orden, loc="center left", bbox_to_anchor=(0.905, 0.5),
               frameon=True, framealpha=0.95, fancybox=False, edgecolor="gray")
    fig.suptitle("Carga on-board — costo total del horizonte (10 años, VP), red + generación + BESS, "
                 "Mina_modelo v4", fontsize=13, y=1.0)
    fig.subplots_adjust(right=0.89)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ob_ref", default="origin/carga_ob_multiaño")
    a = ap.parse_args()
    ubs = ub_de_resumen(a.ob_ref)
    costos = {}
    for n, ruta in CASOS.items():
        if n not in ubs:
            raise SystemExit("caso {} sin UB en resumen.txt".format(n))
        costos[n] = caso(n, ruta, ubs[n], a.ob_ref)
        print("caso {} ({}): reconcilia, {:,.2f}".format(n, ruta, costos[n]["total_cost"]))
    (HERE / "sensibilidad_ob_v4.json").write_text(
        json.dumps({str(n): {"ruta": CASOS[n], **v} for n, v in costos.items()}, indent=2,
                   ensure_ascii=False), encoding="utf-8")
    out = HERE / "sensibilidad_ob_v4.png"
    figura(costos, out)
    print("ok ->", out)


if __name__ == "__main__":
    main()
