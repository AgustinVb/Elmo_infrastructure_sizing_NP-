# ============================================================
# Costo total en el horizonte por escenario - Mejor solucion encontrada
# Battery-swapping vs Carga on-board, barras apiladas por componente.
#
# Casos (--caso):
#   dch (default): DCH 11 años, swap 160kW vs on-board 640kW. Lee
#     dch_bs_160kW_costs.json / dch_ob_640kW_costs.json (extract_costs_dch_*).
#   det: DET, swap 241kW vs on-board 960kW. Lee bs_241kW_costs.json /
#     ob_960kW_costs.json (extract_costs_bs_241kW.py / extract_costs_ob_960kW.py).
#     Esos JSON son del modelo de costos anterior (inversion anualizada, sin
#     costo de subestacion) -- se mantienen para poder regenerar la figura.
# ============================================================

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent

BAR_WIDTH = 0.45

# Orden de las barras: on-board primero, swap despues.
CASES = {
    "dch": {
        "files": ["dch_ob_640kW_costs.json", "dch_bs_160kW_costs.json"],
        "labels": ["Carga on-board\n640 kW", "Battery-swapping\n160 kW"],
        "title": "Costo total por tecnología, horizonte completo DCH (11 años)",
        "out_png": "costo_total_horizonte_dch_bs_vs_ob.png",
    },
    "det": {
        "files": ["ob_960kW_costs.json", "bs_241kW_costs.json"],
        "labels": ["Carga on-board\n960 kW", "Battery-swapping\n241 kW"],
        "title": "Costo total por tecnología, horizonte completo BDR",
        "out_png": "costo_total_horizonte_bs_vs_ob.png",
    },
}


def load_scenario(filename: str) -> dict:
    path = HERE / filename
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--caso", choices=sorted(CASES), default="dch",
                    help="par de escenarios a graficar (default: dch)")
    args = ap.parse_args()
    case = CASES[args.caso]

    scenarios = [load_scenario(f) for f in case["files"]]
    group_labels = case["labels"]

    def to_k(key):
        return [d.get(key, 0.0) / 1000.0 for d in scenarios]

    # "Estaciones" incluye el costo de bahías y grúa; "cargadores" es solo el cargador.
    costo_estaciones_k  = [d.get("cost_stations", 0.0) / 1000.0 + d.get("cost_bays", 0.0) / 1000.0 for d in scenarios]
    costo_cargadores_k  = to_k("cost_chargers")
    costo_baterias_k    = to_k("cost_batteries")
    # Inversion en potencia de subestacion: la cobra el modelo de costos
    # vigente aparte de calculate_investment_cost(). Los JSON del caso det son
    # anteriores a ese cambio y no la traen -> queda en 0 y no se dibuja.
    costo_subestacion_k = to_k("substation_cost")
    costo_potencia_k    = to_k("peak_power_cost")
    costo_recarga_k     = to_k("grid_energy_cost")
    costo_reemplazo_k   = to_k("battery_replace_cost")

    costo_total_k = [
        costo_estaciones_k[i] + costo_cargadores_k[i] + costo_baterias_k[i] + costo_subestacion_k[i]
        + costo_potencia_k[i] + costo_recarga_k[i] + costo_reemplazo_k[i]
        for i in range(len(scenarios))
    ]

    STACK_ORDER = [
        ("estaciones",  costo_estaciones_k,  "tab:blue",   "Costo estaciones"),
        ("cargadores",  costo_cargadores_k,  "tab:orange", "Costo cargadores"),
        ("baterias",    costo_baterias_k,    "tab:green",  "Costo baterías (inversión pool swap)"),
        ("subestacion", costo_subestacion_k, "tab:purple", "Costo subestación"),
        ("potencia",    costo_potencia_k,    "tab:cyan",   "Costo por potencia"),
        ("recarga",     costo_recarga_k,     "tab:pink",   "Costo de recarga (energía red)"),
        ("reemplazo",   costo_reemplazo_k,   "tab:red",    "Costo reemplazo de baterías"),
    ]

    plt.rcParams.update(
        {
            "font.size": 12,
            "axes.titlesize": 15,
            "axes.labelsize": 13,
            "legend.fontsize": 10,
        }
    )

    fig, ax = plt.subplots(figsize=(13, 8))
    x = np.arange(len(group_labels))

    def format_thousands(value):
        return "{:,.0f}".format(value).replace(",", ".")

    bottom = np.zeros(len(group_labels))
    bar_objects = []

    for key, values, color, label in STACK_ORDER:
        values = np.array(values, dtype=float)
        bars = ax.bar(x, values, BAR_WIDTH, bottom=bottom, color=color, label=label,
                      edgecolor="black", linewidth=0.6)
        bar_objects.append((key, bars, values, bottom.copy()))
        bottom += values

    for key, bars, values, bots in bar_objects:
        for rect, val, bot in zip(bars, values, bots):
            if val >= 60:
                ax.text(
                    rect.get_x() + rect.get_width() / 2,
                    bot + val / 2,
                    format_thousands(val),
                    ha="center",
                    va="center",
                    fontsize=9,
                    color="black",
                    fontweight="bold",
                )

    for xi, tot in zip(x, costo_total_k):
        ax.text(
            xi, tot + 60, format_thousands(tot),
            ha="center", va="bottom", fontsize=12, fontweight="bold",
        )

    def draw_bracket(ax, x_bar, half_width, y0, y1, label, gap=0.03, tick_len=0.06, color="black"):
        x_line = x_bar - half_width - gap
        x_tick = x_line - tick_len
        ax.plot([x_tick, x_line, x_line, x_tick], [y1, y1, y0, y0],
                 color=color, linewidth=1.3, clip_on=False)
        ax.text(
            x_tick - 0.02, (y0 + y1) / 2, label,
            ha="right", va="center", rotation=90, fontsize=10, color=color,
        )

    inversion_top   = (np.array(costo_estaciones_k) + np.array(costo_cargadores_k)
                       + np.array(costo_baterias_k) + np.array(costo_subestacion_k))
    operacion_bottom = inversion_top
    operacion_top    = operacion_bottom + np.array(costo_potencia_k) + np.array(costo_recarga_k)
    reemplazo_bottom = operacion_top
    reemplazo_top    = np.array(costo_total_k)

    ymax = max(costo_total_k) * 1.18

    # Un tramo vacío (p.ej. swap sin reemplazos: R=0 todo el horizonte) deja un
    # corchete de altura cero cuya etiqueta se monta sobre la del tramo de
    # abajo -- esos no se dibujan.
    min_span = 0.02 * ymax

    for xi, inv_top, o_bot, o_top, r_bot, r_top in zip(
        x, inversion_top, operacion_bottom, operacion_top, reemplazo_bottom, reemplazo_top
    ):
        for y0, y1, label in (
            (0.0, inv_top, "Inversión"),
            (o_bot, o_top, "Operación"),
            (r_bot, r_top, "Reemplazo"),
        ):
            if y1 - y0 < min_span:
                continue
            draw_bracket(ax, xi, BAR_WIDTH / 2, y0, y1, label)

    ax.set_ylim(0, ymax)
    ax.set_xlim(-0.65, len(group_labels) - 1 + 0.5)

    ax.set_title(case["title"])
    ax.set_ylabel("Costo total (kUSD)")
    ax.set_xticks(x)
    ax.set_xticklabels(group_labels)

    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)

    ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2,
        frameon=True, framealpha=0.95, fancybox=True, edgecolor="gray",
    )

    plt.tight_layout()

    out_png = HERE / case["out_png"]
    fig.savefig(out_png, dpi=300, bbox_inches="tight")
    print("ok ->", out_png)
    for label, total in zip(group_labels, costo_total_k):
        print(f"  {label.replace(chr(10), ' '):28s} {total * 1000.0:>15,.2f} USD")


if __name__ == "__main__":
    main()
