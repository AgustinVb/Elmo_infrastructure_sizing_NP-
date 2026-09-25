#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Degradacion del pool de baterias por año, escenarios DET de Tesis_final.

Formato, paleta y tipografia identicos a la figura equivalente de la rama
carga_ob_multiaño, para poder ponerlas lado a lado en la tesis:

  Barras       capacidad al INICIO del año (b_bar). El año con reemplazo va en
               naranjo, con el rotulo "Reemplazo" encima.
  Linea gris   capacidad al CIERRE del mismo año (D).
  Trazo ----   capacidad nominal (b_max_pool).
  Trazo ....   piso de reemplazo (min_capacity_fraction * b_max_pool).
  Leyenda      en caja al pie, una sola fila.

Eje vertical desde 0 (no truncado), igual que en on-board.

Lee los JSON de extract_degradacion_det.py, que ya verifico las ecuaciones de
degradacion del modelo (ec. 2, 4 y 5 de ConstraintRules).

Año 1 del horizonte = 2035 (misma convencion que json_plotter.py).
"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np

HERE = Path(__file__).resolve().parent
ANIO_BASE = 2035

# Paleta de carga_ob_multiaño.
COLOR_BARRA = "#1F77B4"
COLOR_REEMPLAZO = "#ED7D31"
COLOR_LINEA = "#BFBFBF"
COLOR_MARCADOR = "#9DB8CC"
COLOR_REF = "#333333"

ESCENARIOS = {
    "p_red": {"json": "det_tesis_p_red_degradation.json",
              "titulo": "Degradación del pool de swap 241 kW — DET, solo compra a la red",
              "png": "degradacion_det_tesis_p_red.png"},
    "gen_bat": {"json": "det_tesis_gen_bat_degradation.json",
                "titulo": "Degradación del pool de swap 241 kW — DET, con generación y BESS",
                "png": "degradacion_det_tesis_gen_bat.png"},
}


def plot_escenario(clave):
    cfg = ESCENARIOS[clave]
    d = json.loads((HERE / cfg["json"]).read_text(encoding="utf-8"))

    anios = [int(y) for y in d["anios"]]
    etiquetas = [str(ANIO_BASE + y - 1) for y in anios]
    b_bar = np.array([d["b_bar"][str(y)] for y in anios])
    d_end = np.array([d["D"][str(y)] for y in anios])
    b_max = d["b_max_pool"]
    piso = d["piso_kwh"]
    min_frac = d["min_capacity_fraction"]
    reemplazos = set(d["anios_reemplazo"])

    plt.rcParams.update({
        "font.size": 10, "axes.titlesize": 12, "axes.labelsize": 11,
        "xtick.labelsize": 10, "ytick.labelsize": 10, "legend.fontsize": 9,
    })

    fig, ax = plt.subplots(figsize=(12.6, 5.2))
    x = np.arange(len(anios))

    colores = [COLOR_REEMPLAZO if y in reemplazos else COLOR_BARRA for y in anios]
    ax.bar(x, b_bar, 0.62, color=colores, zorder=2)

    ax.plot(x, d_end, color=COLOR_LINEA, linewidth=1.4, marker="o", markersize=5,
            markerfacecolor=COLOR_MARCADOR, markeredgecolor="#7F7F7F",
            markeredgewidth=0.6, zorder=4)

    ax.axhline(b_max, color=COLOR_REF, linewidth=1.2, linestyle="--", zorder=3)
    ax.axhline(piso, color=COLOR_REF, linewidth=1.2, linestyle=":", zorder=3)

    for i, y in enumerate(anios):
        if y in reemplazos:
            ax.text(x[i], b_bar[i] + 0.012 * b_max, "Reemplazo", ha="center", va="bottom",
                    fontsize=10, color=COLOR_REEMPLAZO, fontweight="bold")

    ax.set_ylim(0, b_max * 1.10)
    ax.set_xlim(-0.7, len(anios) - 0.3)
    ax.set_xticks(x)
    ax.set_xticklabels(etiquetas)
    ax.set_title(cfg["titulo"])
    ax.set_ylabel("Capacidad del pool [kWh]")
    ax.set_xlabel("Año")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)

    # Handles armados a mano para fijar el orden de la leyenda igual que en
    # on-board: linea, nominal, piso y al final la barra.
    handles = [
        Line2D([0], [0], color=COLOR_LINEA, linewidth=1.4, marker="o", markersize=5,
               markerfacecolor=COLOR_MARCADOR, markeredgecolor="#7F7F7F",
               label="Capacidad al final del año"),
        Line2D([0], [0], color=COLOR_REF, linewidth=1.2, linestyle="--",
               label="Nominal ({:.0f} kWh)".format(b_max)),
        Line2D([0], [0], color=COLOR_REF, linewidth=1.2, linestyle=":",
               label="Piso {:.0%} ({:.1f} kWh)".format(min_frac, piso)),
        Patch(facecolor=COLOR_BARRA, label="Capacidad al inicio del año"),
    ]
    if reemplazos:
        handles.append(Patch(facecolor=COLOR_REEMPLAZO, label="Año con reemplazo"))

    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.14),
              ncol=len(handles), frameon=True, framealpha=0.95, fancybox=False,
              edgecolor="gray")

    out = HERE / cfg["png"]
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("ok -> {}  ({:.1f} -> {:.1f} kWh, {:.1%} retenido, reemplazos: {})".format(
        out.name, b_max, d["capacidad_final_kwh"], d["retencion_final"],
        sorted(reemplazos) or "ninguno"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--escenario", choices=sorted(ESCENARIOS) + ["todos"], default="todos")
    args = ap.parse_args()
    claves = sorted(ESCENARIOS) if args.escenario == "todos" else [args.escenario]
    for k in claves:
        plot_escenario(k)


if __name__ == "__main__":
    main()
