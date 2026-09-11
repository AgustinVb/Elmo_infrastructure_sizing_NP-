# ============================================================
# Degradacion de bateria por año - Caso de estudio MCHS
# Un grafico por tecnologia: carga on-board 640kW y battery-swapping 160kW
#
# Mismo diseño que json_plotter.py::plot_battery_degradation
# (BatteryDegradation.png): barras = capacidad al INICIO del año (b_bar[y]),
# lineas de referencia de la capacidad nominal y del piso operable, y los
# años con reemplazo (R[y]=1) pintados en el color de reemplazo con su
# etiqueta.
#
# Diferencias respecto del plotter:
#   - sin los marcadores D (fin de año): solo las barras de b_bar
#   - años calendario partiendo en 2035 (MCHS), no en 2033 (BDR/DET, que es
#     lo que tiene hardcodeado json_plotter.BASE_CALENDAR_YEAR)
#   - titulo con la referencia al caso MCHS y a la potencia del escenario
#   - sin el panel de ciclos equivalentes que trae la version on-board
#   - lee los JSON ya extraidos en vez de la carpeta del run, asi el grafico
#     de on-board se puede generar desde esta rama (ese run vive en
#     carga_ob_multiano)
#
# Lee dch_ob_640kW_degradation.json y dch_bs_160kW_degradation.json (generados por
# extract_degradation_dch_ob_640kW.py y extract_degradation_dch_bs_160kW.py).
#
# Salidas:
#   degradacion_ob_640kW.png
#   degradacion_bs_160kW.png
# ============================================================

import json
from pathlib import Path

import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent

# Año 1 del horizonte del modelo. MCHS parte en 2035 (json_plotter.py usa
# 2033, que es el año base del caso BDR/DET).
BASE_CALENDAR_YEAR = 2035

# Paleta de json_plotter.py::plot_battery_degradation
CAP_COLOR = "#1565C0"       # barras de b_bar
REPLACE_COLOR = "#F57C00"   # años con R[y]=1
REF_COLOR = "#616161"       # lineas de referencia

BAR_WIDTH = 0.6

TECHS = [
    {
        "key": "ob",
        "file": "dch_ob_640kW_degradation.json",
        "short": "on-board",
        "title": "Degradación de batería on-board (fleet-wide) 640 kW — MCHS",
        "out_png": "degradacion_ob_640kW.png",
    },
    {
        "key": "bs",
        "file": "dch_bs_160kW_degradation.json",
        "short": "battery-swapping",
        "title": "Degradación de baterías battery-swapping 160 kW — MCHS",
        "out_png": "degradacion_bs_160kW.png",
    },
]


def load(filename):
    with open(HERE / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def plot_single(tech):
    data = load(tech["file"])

    years = sorted(int(y) for y in data["b_bar_kwh_by_year"])
    labels = [str(BASE_CALENDAR_YEAR + (y - 1)) for y in years]
    b_bar = [data["b_bar_kwh_by_year"][str(y)] for y in years]
    replaced = set(data.get("replacement_years", []))

    b_max = data["b_max_kwh"]
    floor = data["capacity_floor_kwh"]
    min_frac = data["min_capacity_fraction"]

    fig, ax = plt.subplots(figsize=(max(6, 1.2 * len(years) + 2), 5.5))

    bar_colors = [REPLACE_COLOR if y in replaced else CAP_COLOR for y in years]
    ax.bar(labels, b_bar, color=bar_colors, width=BAR_WIDTH, zorder=3,
           label="B (inicio de año)")

    ax.axhline(b_max, color=REF_COLOR, linewidth=1.2, linestyle="--",
               label="b_max (nominal)")
    ax.axhline(floor, color=REF_COLOR, linewidth=1.2, linestyle=":",
               label="Piso ({:.0%}·b_max)".format(min_frac))

    for y, lab, val in zip(years, labels, b_bar):
        if y in replaced:
            ax.text(lab, val, "Reemplazo", ha="center", va="bottom",
                    fontsize=9, color=REPLACE_COLOR, fontweight="bold")

    ax.set_ylabel("Capacidad batería [kWh]", fontsize=11)
    ax.set_xlabel("Año", fontsize=12)
    ax.set_title(tech["title"], fontsize=14)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.legend(loc="lower left", fontsize=9, framealpha=0.85)

    fig.tight_layout()
    out_png = HERE / tech["out_png"]
    fig.savefig(out_png, dpi=300, bbox_inches="tight")
    plt.close(fig)

    print("ok ->", out_png)
    print("   {:<18} {}-{}  |  SOH final {:.1f}%  |  reemplazos: {}".format(
        tech["short"], labels[0], labels[-1],
        100.0 * data["d_end_kwh_by_year"][str(years[-1])] / b_max,
        [BASE_CALENDAR_YEAR + (y - 1) for y in sorted(replaced)] or "ninguno"))


def main():
    for tech in TECHS:
        data = load(tech["file"])
        res = abs(data.get("checks", {}).get("max_abs_residual_d_y_fade_kwh", 0.0))
        if res > 1e-6:
            raise AssertionError(
                "{}: D[y] no cuadra con b_bar[y]-gamma*N_ciclos[y] "
                "(residuo {:.3e} kWh)".format(tech["key"], res)
            )
        plot_single(tech)


if __name__ == "__main__":
    main()
