"""Figuras del indicador de holgura operativa (lee las salidas de indicador_holgura.py
y las soluciones guardadas; no vuelve a optimizar).

  uso_tiempo_lhd.png           cuerpo: uso del tiempo de un LHD tipico en regimen (barras
                               apiladas, 24 h) por escenario y tecnologia, con la holgura
                               del peor LHD y dia marcada con un punto
  cargadores_en_uso_<est>.png  anexo 1: cargadores ocupados por intervalo vs instalados,
                               on-board y estacion de swap, produccion base y escalada,
                               red + generacion + BESS, norte, año 5
  holgura_lhd_dia.png          anexo 2: holgura por LHD y dia representativo (promedio de
                               los años de regimen y el minimo entre ellos), base y escalada

Uso: python scripts/figuras_holgura.py   (despues de scripts/indicador_holgura.py)
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches                      # noqa: E402
import matplotlib.ticker                       # noqa: E402
import matplotlib.pyplot as plt                # noqa: E402
import numpy as np                             # noqa: E402
import pandas as pd                            # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "cost_comparisons"))
from indicador_holgura import OUT, activos, leer, regimen               # noqa: E402
from comparar_swap_ob_v4 import CASOS, V4                               # noqa: E402
from resultados_finales import (ANIO_PICO, ESTACIONES, FINALES, OB_REPO, _eje_hora,  # noqa: E402
                                _leyenda_ventanas, _num, _orden_lhd, _ventanas)

LADOS = (("Swap", "Battery swapping"), ("OB", "On-board"))
DIAS = (("verano", 15), ("otoño", 105), ("invierno", 196), ("primavera", 288))
# Segmentos de la barra de uso del tiempo: (columna, etiqueta, color, hatch).
SEGMENTOS = (("h_op", "Operación (asignado a extracción)", "#F0BF57", None),
             ("h_carga", "Carga (on-board) / intercambio (swap)", "#3366CC", None),
             ("h_carga_detenciones", "Detención operacional, cargando", "#D9D4C7", "///"),
             ("h_det_sin_carga", "Detención operacional, sin cargar", "#D9D4C7", None),
             ("h_holgura", "Holgura (inactivo)", "#B0B0B0", None))
COLOR_LADO = {"Swap": "#1F77B4", "OB": "#ED7D31"}


def soluciones(clave):
    return {"Swap": REPO / V4 / FINALES[clave][0] / FINALES[clave][1] / "solucion",
            "OB": OB_REPO / CASOS[clave]["ob"]["caso"] / CASOS[clave]["ob"]["sol"]}


def _regimen(det):
    """Filas del detalle en los años de regimen de cada caso."""
    anios = {k: regimen(leer(soluciones(str(k))["Swap"], "parameters.json")) for k in det["caso"].unique()}
    return det[[a in anios[c] for c, a in zip(det["caso"], det["anio"])]]


# ------------------------------------------------------------ A: uso del tiempo --
def figura_uso_tiempo(det, res, out_png):
    r = _regimen(det).assign(h_det_sin_carga=lambda x: x["h_detenciones"] - x["h_carga_detenciones"])
    cols = [c for c, *_ in SEGMENTOS]
    prom = (r[cols].mul(r["peso"], axis=0).groupby([r["caso"], r["lado"]]).sum()
            .div(r.groupby(["caso", "lado"])["peso"].sum(), axis=0))
    minimo = r.groupby(["caso", "lado"])["h_holgura"].min()
    filas, y, ticks, grupos = [], 0.0, [], []
    for caso in sorted(res["caso"].unique()):
        y0 = y
        for lado, nombre in LADOS:
            filas.append((y, caso, lado))
            ticks.append((y, nombre))
            y += 1.0
        grupos.append(((y0 + y - 1) / 2.0, CASOS[str(caso)]["titulo"]))
        y += 0.6
    fig, ax = plt.subplots(figsize=(15, 0.62 * y + 2.2))
    for yy, caso, lado in filas:
        izq = 0.0
        for col, _, color, hatch in SEGMENTOS:
            v = prom.loc[(caso, lado), col]
            ax.barh(yy, v, left=izq, height=0.72, color=color, hatch=hatch, edgecolor="#ffffff", linewidth=1.0)
            if col == "h_holgura":
                inicio = izq
            izq += v
        h, hmin = prom.loc[(caso, lado), "h_holgura"], minimo.loc[(caso, lado)]
        ax.plot(inicio + hmin, yy, "o", ms=8, mfc="#141414", mec="#ffffff", mew=1.2, zorder=5)
        ax.text(24.25, yy, "{} %".format(_num(100 * h / 16.8, 1)), va="center", fontsize=12)
    ax.set_yticks([t for t, _ in ticks])
    ax.set_yticklabels([n for _, n in ticks], fontsize=12)
    for (yy, caso, lado), lab in zip(filas, ax.get_yticklabels()):
        lab.set_color(COLOR_LADO[lado])
    for yy, titulo in grupos:
        ax.text(-3.3, yy, titulo, ha="right", va="center", fontsize=12, fontweight="bold", wrap=True)
    ax.set_ylim(y - 0.6 + 0.2, -0.8)
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2))
    ax.set_xlabel("Horas por LHD y día (promedio de la flota en los años de producción máxima)", fontsize=13)
    ax.text(24.25, -0.75, "Holgura\n[% disp.]", fontsize=11, va="bottom")
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    hs = [matplotlib.patches.Patch(facecolor=c, hatch=h, edgecolor="#ffffff" if not h else "#8a8579", label=et)
          for _, et, c, h in SEGMENTOS]
    hs.append(plt.Line2D([0], [0], marker="o", ls="", ms=8, mfc="#141414", mec="#ffffff",
                         label="Fin de la holgura en el peor LHD y día"))
    fig.legend(handles=hs, loc="upper center", ncols=3, fontsize=11.5, frameon=True, bbox_to_anchor=(0.6, 0.0))
    fig.suptitle("Uso del tiempo de los LHD por escenario y tecnología", fontsize=16)
    fig.subplots_adjust(left=0.33, right=0.93, top=0.95, bottom=0.06)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------- anexo 1: cargadores --
def cargadores_en_uso(clave, lado, anio, d):
    """(serie de cargadores ocupados por intervalo, instalados, parametros). On-board:
    LHD conectados (Z_charge); swap: baterias cargando (sum_a Sv[t, a])."""
    sol = soluciones(clave)[lado]
    p = leer(sol, "parameters.json")
    T = int(p["t_fin"])
    n = float(leer(sol, "N_chargers.json")["k"]["station_1"]["y"][str(anio)])
    if lado == "OB":
        zc = activos(leer(sol, "Z_charge.json"), ["k", "*", "i"])
        serie = np.array([sum(t in s.get((anio, d), set()) for s in zc.values()) for t in range(1, T + 1)], float)
    else:
        sv = leer(sol, "Sv.json")["k"]["station_1"]["y"][str(anio)]["d"][str(d)]["t"]
        serie = np.array([sum(float(v) for v in sv[str(t)]["a"].values()) for t in range(1, T + 1)])
    return serie, n, p


def figura_cargadores_en_uso(pares, out_png, estacion, anio=ANIO_PICO):
    """pares = [(clave, rotulo de produccion)]: columnas; filas on-board y swap."""
    d = ESTACIONES[estacion]
    p_swap = leer(soluciones(pares[0][0])["Swap"], "parameters.json")
    fig, axs = plt.subplots(2, len(pares), figsize=(9 * len(pares), 8.4), sharex=True, squeeze=False)
    vistas, punta, resumen = [], False, []
    for j, (clave, rotulo) in enumerate(pares):
        for i, (lado, nombre) in enumerate((("OB", "On-board"), ("Swap", "Battery swapping"))):
            ax = axs[i][j]
            serie, n, p = cargadores_en_uso(clave, lado, anio, d)
            dt, T = float(p["delta_t"]), len(serie)
            v, pu = _ventanas(ax, p, dt, d, T)
            vistas += [e for e in v if e not in vistas]
            punta = punta or pu
            ax.stairs(serie, np.arange(T + 1) * dt, color=COLOR_LADO[lado], lw=2.0, zorder=4,
                      fill=True, alpha=0.35)
            ax.stairs(serie, np.arange(T + 1) * dt, color=COLOR_LADO[lado], lw=2.0, zorder=5)
            ax.axhline(n, color="#141414", ls="--", lw=1.6, zorder=6)
            ax.text(23.9, n + 0.08, "{} instalado{}".format(int(n), "s" if n > 1 else ""), ha="right",
                    va="bottom", fontsize=11.5, zorder=7)
            util = serie.sum() / (n * T)
            tasa = "{}C".format(_num(float(p["p_charger"]) / 482.0, 1).replace(",0", "")) \
                if float(p["p_charger"]) != 241 else "0,5C"
            ax.set_title("{}, producción {} ({}): utilización {} %".format(nombre, rotulo, tasa, _num(100 * util)),
                         fontsize=13.5)
            ax.set_ylim(0, max(2.0, n) + 0.7)
            ax.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(1))
            ax.grid(axis="y", alpha=0.3)
            for lado_s in ("top", "right"):
                ax.spines[lado_s].set_visible(False)
            if j == 0:
                ax.set_ylabel("Cargadores ocupados", fontsize=13)
            if i == 1:
                _eje_hora(ax, float(p_swap["base_hour"]))
                ax.set_xlabel("Hora", fontsize=13)
            resumen.append((clave, lado, util))
    hs = [plt.Line2D([0], [0], color=COLOR_LADO[l], lw=6, alpha=0.6, label=et)
          for l, et in (("OB", "On-board: LHD conectados"), ("Swap", "Swap: baterías cargando"))]
    hs.append(plt.Line2D([0], [0], color="#141414", ls="--", lw=1.6, label="Cargadores instalados"))
    fig.legend(handles=hs + _leyenda_ventanas(vistas, punta), loc="lower center", ncols=4, fontsize=11.5,
               frameon=True, bbox_to_anchor=(0.5, -0.07))
    fig.suptitle("Cargadores en uso por intervalo, red + generación + BESS, zona norte\naño {}, {}".format(
        anio, estacion), fontsize=15)
    fig.subplots_adjust(left=0.06, right=0.98, top=0.88, bottom=0.08, hspace=0.25, wspace=0.1)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return resumen


# ---------------------------------------------------- anexo 2: mapa de calor --
def figura_holgura_lhd_dia(det, pares, out_png):
    """Filas tecnologia, columnas produccion; cada panel LHD x dia representativo."""
    r = _regimen(det)
    fig, axs = plt.subplots(2, len(pares), figsize=(7.2 * len(pares), 7.6), squeeze=False)
    vmax = 30.0
    for j, (clave, rotulo) in enumerate(pares):
        for i, (lado, nombre) in enumerate(LADOS):
            ax = axs[i][j]
            s = r[(r["caso"] == int(clave)) & (r["lado"] == lado)]
            lhds = sorted(s["lhd"].unique(), key=_orden_lhd)
            prom = s.pivot_table(index="lhd", columns="dia", values="pct_holgura", aggfunc="mean").loc[lhds]
            mini = s.pivot_table(index="lhd", columns="dia", values="pct_holgura", aggfunc="min").loc[lhds]
            m = prom[[dd for _, dd in DIAS]].to_numpy()
            im = ax.imshow(m, cmap="Greens", vmin=0, vmax=vmax, aspect="auto")
            for a in range(m.shape[0]):
                for b in range(m.shape[1]):
                    mn = mini.iloc[a][DIAS[b][1]]
                    ax.text(b, a, "{}\n(mín. {})".format(_num(m[a, b], 1), _num(mn, 1)), ha="center", va="center",
                            fontsize=11, color="#ffffff" if m[a, b] > 0.6 * vmax else "#141414",
                            fontweight="bold" if mn < 0.05 else "normal")
            ax.set_xticks(range(len(DIAS)))
            ax.set_xticklabels([e.capitalize() for e, _ in DIAS], fontsize=12)
            ax.set_yticks(range(len(lhds)))
            ax.set_yticklabels(["LHD {}".format(_orden_lhd(l)) for l in lhds], fontsize=12)
            tasa = " (1C)" if lado == "OB" and CASOS[clave]["titulo"].startswith("Escalada") else ""
            ax.set_title("{}, producción {}{}".format(nombre, rotulo, tasa), fontsize=13.5)
    fig.subplots_adjust(hspace=0.32)
    cb = fig.colorbar(im, ax=axs, shrink=0.8, pad=0.02)
    cb.set_label("Holgura [% del tiempo disponible]", fontsize=12)
    fig.suptitle("Holgura operativa por LHD y día representativo, red + generación + BESS, zona norte\n"
                  "promedio de los años de producción máxima y mínimo entre ellos", fontsize=14)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    det = pd.read_csv(OUT / "holgura_por_lhd_dia.csv")
    res = pd.read_csv(OUT / "holgura_operativa.csv")
    figura_uso_tiempo(det, res, OUT / "uso_tiempo_lhd.png")
    pares = [("2", "base"), ("5", "escalada")]
    for estacion in ("invierno", "verano"):
        for clave, lado, util in figura_cargadores_en_uso(pares, OUT / "cargadores_en_uso_{}.png".format(estacion),
                                                          estacion):
            print("[{} {} {}] utilizacion {:.1f} %".format(estacion, clave, lado, 100 * util))
    figura_holgura_lhd_dia(det, pares, OUT / "holgura_lhd_dia.png")
    print("->", OUT)


if __name__ == "__main__":
    main()
