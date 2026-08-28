# ============================================================
# Potencia peak por año - Battery-swapping 241kW vs Carga on-board 960kW
#
# Lee bs_241kW_peak_power.json y ob_960kW_peak_power.json (generados por
# extract_peak_power_bs_241kW.py y extract_peak_power_ob_960kW.py) y grafica
# la potencia peak real (max P_red[y,d,t]) de cada año del horizonte.
# ============================================================

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent

# Año 1 del horizonte del modelo corresponde al año calendario 2033.
BASE_CALENDAR_YEAR = 2033


def load_scenario(filename: str) -> dict:
    with open(HERE / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    bs = load_scenario("bs_241kW_peak_power.json")
    ob = load_scenario("ob_960kW_peak_power.json")

    years = sorted(int(y) for y in bs["peak_power_kw_by_year"].keys())
    bs_kw = [bs["peak_power_kw_by_year"][str(y)] for y in years]
    ob_kw = [ob["peak_power_kw_by_year"][str(y)] for y in years]
    calendar_years = [BASE_CALENDAR_YEAR + (y - 1) for y in years]

    plt.rcParams.update(
        {
            "font.size": 12,
            "axes.titlesize": 15,
            "axes.labelsize": 13,
            "legend.fontsize": 11,
        }
    )

    fig, ax = plt.subplots(figsize=(13, 7))

    ax.plot(calendar_years, ob_kw, color="steelblue", linewidth=2, zorder=2)
    ax.plot(calendar_years, bs_kw, color="darkorange", linewidth=2, zorder=2)

    ax.scatter(calendar_years, ob_kw, color="steelblue", s=180, zorder=3, label="Carga on-board 960 kW")
    ax.scatter(calendar_years, bs_kw, color="darkorange", s=180, zorder=3, label="Battery-swapping 241 kW")

    for xi, val in zip(calendar_years, ob_kw):
        ax.annotate(f"{val:,.0f}".replace(",", "."), (xi, val),
                    textcoords="offset points", xytext=(0, 14),
                    ha="center", fontsize=12, fontweight="bold", color="steelblue")
    for xi, val in zip(calendar_years, bs_kw):
        ax.annotate(f"{val:,.0f}".replace(",", "."), (xi, val),
                    textcoords="offset points", xytext=(0, -20),
                    ha="center", fontsize=12, fontweight="bold", color="darkorange")

    ax.set_title("Potencia peak por año, horizonte completo BDR")
    ax.set_xlabel("Año")
    ax.set_ylabel("Potencia peak (kW)")
    ax.set_xticks(calendar_years)
    ax.set_xlim(calendar_years[0] - 0.5, calendar_years[-1] + 0.5)
    ax.set_ylim(0, max(max(bs_kw), max(ob_kw)) * 1.15)

    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)

    ax.legend(loc="upper left", frameon=True, framealpha=0.95, fancybox=True, edgecolor="gray")

    plt.tight_layout()

    out_png = HERE / "potencia_peak_por_anno_bs_vs_ob.png"
    fig.savefig(out_png, dpi=300, bbox_inches="tight")
    print("ok ->", out_png)


if __name__ == "__main__":
    main()
