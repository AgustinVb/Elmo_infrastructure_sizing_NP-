#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Costo total del horizonte DET (Tesis_final), barras apiladas por componente.

Formato, paleta y tipografia identicos a la figura equivalente de la rama
carga_ob_multiaño, para que las dos ramas se puedan poner lado a lado en la
tesis: corchetes Inversion / Operacion / Reemplazo a la izquierda de cada
barra, etiquetas de valor en blanco con halo oscuro, total en negrita arriba,
leyenda en caja al pie a 3 columnas, ejes con rotulo entre corchetes.

Unica diferencia de contenido con on-board: esta rama es battery swapping, asi
que aparece la categoria "Baterias (pool de swap)", que on-board no tiene
(alli las baterias van en el equipo). Se le asigno el azul intermedio de la
misma familia para no romper la lectura de la paleta.

La columna de generacion+BESS se deja EN BLANCO a proposito
(ESCENARIOS_VISIBLES): esa corrida uso 4 dias representativos por año contra
los 2 de solo-red, con escalado operacional distinto (91,25 contra 182,5 dias
por dia modelado) y un gap de 26,55% contra 10,63%, asi que su barra no es
comparable con la de al lado. El lugar queda reservado para cuando se re-corra
en la misma base. Los datos igual se extraen y reconcilian: lo unico que
cambia es que no se dibuja.
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
BAR_WIDTH = 0.45

ARCHIVOS = ["det_tesis_p_red_costs.json", "det_tesis_gen_bat_costs.json"]
ETIQUETAS = ["Solo compra a la red", "Autogeneración renovable\n+ BESS"]
# Que columnas se DIBUJAN. La que va en False conserva su lugar y su rotulo en
# el eje, pero sale sin barra (ver docstring).
ESCENARIOS_VISIBLES = [True, False]
TITULO = "Costo total del horizonte (14 años) — DET, battery swapping 241 kW"
OUT_PNG = "costo_total_horizonte_det_tesis.png"

# Paleta de carga_ob_multiaño. Infraestructura en azules, recursos propios en
# verdes, cargos de red en naranjas/amarillos, reemplazo en rojo.
# (claves que se suman, color, etiqueta de leyenda, tramo del corchete)
STACK_ORDER = [
    (("cost_stations", "cost_bays"), "#1F4E79", "Estaciones y bahías", "inversion"),
    (("cost_chargers",), "#2E75B6", "Cargadores", "inversion"),
    (("cost_batteries",), "#5B9BD5", "Baterías (pool de swap)", "inversion"),
    (("substation_cost",), "#BDD7EE", "Subestación", "inversion"),
    (("gen_inv_cost",), "#375623", "Generación renovable", "inversion"),
    (("bess_inv_cost",), "#A9D18E", "BESS", "inversion"),
    (("peak_power_cost",), "#BF8F00", "Potencia punta", "operacion"),
    (("grid_energy_cost",), "#ED7D31", "Energía de red", "operacion"),
    (("gen_op_cost", "bess_op_cost"), "#FFD966", "O&M generación y BESS", "operacion"),
    (("battery_replace_cost",), "#C00000", "Reemplazo de baterías", "reemplazo"),
]


def main():
    escenarios = [json.loads((HERE / f).read_text(encoding="utf-8")) for f in ARCHIVOS]

    series = {}
    for claves, _color, etiqueta, _tramo in STACK_ORDER:
        series[etiqueta] = np.array(
            [sum(d.get(k, 0.0) for k in claves) / 1000.0 for d in escenarios])

    total_k = sum(series.values())

    # El apilado tiene que reproducir el total reconciliado, en TODOS los
    # escenarios -- tambien en los que no se dibujan. Si no, falta una
    # categoria en STACK_ORDER y la barra saldria corta sin que se note.
    for d, tot in zip(escenarios, total_k):
        ref = d["total_cost"] / 1000.0
        if abs(tot - ref) > 1e-6 * max(1.0, ref):
            raise SystemExit(
                "{}: el apilado ({:,.3f} kUSD) no cuadra con total_cost "
                "({:,.3f} kUSD) -- falta alguna categoria en STACK_ORDER.".format(
                    d["scenario"], tot, ref))

    plt.rcParams.update({
        "font.size": 10, "axes.titlesize": 12, "axes.labelsize": 11,
        "xtick.labelsize": 10, "ytick.labelsize": 10, "legend.fontsize": 9,
    })

    fig, ax = plt.subplots(figsize=(8.0, 7.2))
    x = np.arange(len(ETIQUETAS))
    visible = np.array(ESCENARIOS_VISIBLES, dtype=bool)

    def miles(v):
        return "{:,.0f}".format(v).replace(",", ".")

    bottom = np.zeros(len(ETIQUETAS))
    dibujadas = []
    for _claves, color, etiqueta, _tramo in STACK_ORDER:
        # Los escenarios ocultos van en cero: conservan su lugar en el eje pero
        # no aportan altura ni entran en la leyenda.
        vals = np.where(visible, series[etiqueta], 0.0)
        if not np.any(vals > 0):
            continue
        bars = ax.bar(x, vals, BAR_WIDTH, bottom=bottom, color=color, label=etiqueta,
                      edgecolor="black", linewidth=0.6)
        dibujadas.append((bars, vals, bottom.copy()))
        bottom = bottom + vals

    ymax = float(total_k[visible].max()) * 1.12

    # Umbral de etiqueta: un segmento mas fino que esto no tiene alto para el
    # texto y el numero terminaria pisando a los vecinos.
    umbral = 0.017 * ymax
    for bars, vals, bots in dibujadas:
        for rect, val, bot in zip(bars, vals, bots):
            if val >= umbral:
                ax.text(rect.get_x() + rect.get_width() / 2, bot + val / 2, miles(val),
                        ha="center", va="center", fontsize=10, color="#FFFFFF",
                        fontweight="bold",
                        path_effects=[pe.withStroke(linewidth=1.8, foreground="#111111")])

    for xi, tot, vis in zip(x, total_k, visible):
        if vis:
            ax.text(xi, tot + 0.015 * ymax, miles(tot),
                    ha="center", va="bottom", fontsize=11, fontweight="bold")

    def corchete(x_bar, half, y0, y1, label, gap=0.03, tick=0.06):
        x_line = x_bar - half - gap
        x_tick = x_line - tick
        ax.plot([x_tick, x_line, x_line, x_tick], [y1, y1, y0, y0],
                color="black", linewidth=1.0, clip_on=False)
        ax.text(x_tick - 0.02, (y0 + y1) / 2, label, ha="right", va="center",
                rotation=90, fontsize=9)

    inv = sum(series[e] for _k, _c, e, t in STACK_ORDER if t == "inversion")
    ope = sum(series[e] for _k, _c, e, t in STACK_ORDER if t == "operacion")

    # Un tramo vacio (p.ej. reemplazo = 0) dejaria un corchete de altura cero
    # cuya etiqueta se monta sobre la del tramo de abajo.
    min_span = 0.02 * ymax

    for xi, vis, i_top, o_top, r_top in zip(x, visible, inv, inv + ope, total_k):
        if not vis:
            continue
        for y0, y1, label in ((0.0, i_top, "Inversión"),
                              (i_top, o_top, "Operación"),
                              (o_top, r_top, "Reemplazo")):
            if y1 - y0 >= min_span:
                corchete(xi, BAR_WIDTH / 2, y0, y1, label)

    ax.set_ylim(0, ymax)
    ax.set_xlim(-0.65, len(ETIQUETAS) - 1 + 0.5)
    ax.set_title(TITULO)
    ax.set_ylabel("Costo total [kUSD]")
    ax.set_xticks(x)
    ax.set_xticklabels(ETIQUETAS)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)

    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=3,
              frameon=True, framealpha=0.95, fancybox=False, edgecolor="gray")

    fig.savefig(HERE / OUT_PNG, dpi=150, bbox_inches="tight")
    print("ok ->", HERE / OUT_PNG)
    for etiqueta, tot, vis in zip(ETIQUETAS, total_k, visible):
        print("  {:34s} {:>15,.2f} USD{}".format(
            etiqueta.replace("\n", " "), tot * 1000.0, "" if vis else "   [no dibujado]"))


if __name__ == "__main__":
    main()
