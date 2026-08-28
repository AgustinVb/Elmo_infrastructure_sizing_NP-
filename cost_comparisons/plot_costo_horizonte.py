# ============================================================
# Costo total en el horizonte por escenario - Mejor solucion encontrada
# Battery-swapping 241kW vs Carga on-board 960kW
#
# Lee bs_241kW_costs.json y ob_960kW_costs.json (generados por
# extract_costs_bs_241kW.py y extract_costs_ob_960kW.py respectivamente)
# y arma el grafico de barras apiladas.
# ============================================================

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent

BAR_WIDTH = 0.45

DISPLAY_LABEL = {
    "carga_on_board_960kW": "Carga on-board\n960 kW",
    "battery_swapping_241kW": "Battery-swapping\n241 kW",
}

# Orden de las barras en el grafico
SCENARIO_ORDER = ["carga_on_board_960kW", "battery_swapping_241kW"]


def load_scenario(filename: str) -> dict:
    path = HERE / filename
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    data_by_scenario = {
        "carga_on_board_960kW": load_scenario("ob_960kW_costs.json"),
        "battery_swapping_241kW": load_scenario("bs_241kW_costs.json"),
    }

    scenarios = [data_by_scenario[s] for s in SCENARIO_ORDER]
    group_labels = [DISPLAY_LABEL[s] for s in SCENARIO_ORDER]

    def to_k(key):
        return [d.get(key, 0.0) / 1000.0 for d in scenarios]

    # "Estaciones" incluye el costo de bahías y grúa; "cargadores" es solo el cargador.
    costo_estaciones_k = [d.get("cost_stations", 0.0) / 1000.0 + d.get("cost_bays", 0.0) / 1000.0 for d in scenarios]
    costo_cargadores_k = to_k("cost_chargers")
    costo_baterias_k   = to_k("cost_batteries")
    costo_potencia_k   = to_k("peak_power_cost")
    costo_recarga_k    = to_k("grid_energy_cost")
    costo_reemplazo_k  = to_k("battery_replace_cost")

    costo_total_k = [
        costo_estaciones_k[i] + costo_cargadores_k[i] + costo_baterias_k[i]
        + costo_potencia_k[i] + costo_recarga_k[i] + costo_reemplazo_k[i]
        for i in range(len(scenarios))
    ]

    STACK_ORDER = [
        ("estaciones", costo_estaciones_k, "tab:blue",   "Costo estaciones"),
        ("cargadores", costo_cargadores_k, "tab:orange", "Costo cargadores"),
        ("baterias",   costo_baterias_k,   "tab:green",  "Costo baterías (inversión pool swap)"),
        ("potencia",   costo_potencia_k,   "tab:cyan",   "Costo por potencia"),
        ("recarga",    costo_recarga_k,    "tab:pink",   "Costo de recarga (energía red)"),
        ("reemplazo",  costo_reemplazo_k,  "tab:red",    "Costo reemplazo de baterías"),
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

    inversion_top   = np.array(costo_estaciones_k) + np.array(costo_cargadores_k) + np.array(costo_baterias_k)
    operacion_bottom = inversion_top
    operacion_top    = operacion_bottom + np.array(costo_potencia_k) + np.array(costo_recarga_k)
    reemplazo_bottom = operacion_top
    reemplazo_top    = np.array(costo_total_k)

    for xi, inv_top, o_bot, o_top, r_bot, r_top in zip(
        x, inversion_top, operacion_bottom, operacion_top, reemplazo_bottom, reemplazo_top
    ):
        draw_bracket(ax, xi, BAR_WIDTH / 2, 0, inv_top, "Inversión")
        draw_bracket(ax, xi, BAR_WIDTH / 2, o_bot, o_top, "Operación")
        draw_bracket(ax, xi, BAR_WIDTH / 2, r_bot, r_top, "Reemplazo")

    ymax = max(costo_total_k) * 1.18
    ax.set_ylim(0, ymax)
    ax.set_xlim(-0.65, len(group_labels) - 1 + 0.5)

    ax.set_title("Costo total por tecnología, horizonte completo BDR")
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

    out_png = HERE / "costo_total_horizonte_bs_vs_ob.png"
    fig.savefig(out_png, dpi=300, bbox_inches="tight")
    print("ok ->", out_png)


if __name__ == "__main__":
    main()
