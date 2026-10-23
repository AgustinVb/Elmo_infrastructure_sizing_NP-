#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Despacho de potencia de un dia representativo en un año en regimen: battery
swapping contra carga on-board, casos finales v4 de Mina_modelo. Una figura por
par (los de comparar_swap_ob_v4.py), con swap y OB lado a lado.

Misma lectura que plot_despacho_det.py (de ahi se importan la hora de reloj y
la ventana de punta por punto medio del intervalo):

    P_red + sum_g P_gen + sum_h P_bat = consumo de las estaciones

con P_bat > 0 descargando; el consumo se reconstruye como esa suma. Atribucion
por orden de merito (supuesto de presentacion, no resultado del modelo): BESS
descargando -> generacion renovable (solar + eolica) -> red. La carga del BESS
va como area bajo el cero. En la ventana de punta de los dias 91-244 se verifica
que la red no supere P_pot[y] (power_peak_limit): si la ventana reconstruida
estuviera corrida, esa verificacion falla.

AÑO EN REGIMEN. Por defecto el año 5: los años 4-7 tienen la meta de
produccion pico (23.500 t/dia, presolve de capacidad del Benders) y en el 5 ya
esta toda la infraestructura de los dos lados. Dia 196 por defecto: invierno,
dentro de los dias con cargo por potencia.

Swap se lee del disco; on-board de git (origin/carga_ob_multiaño), con las mismas
soluciones que comparar_swap_ob_v4.py.

    python cost_comparisons/plot_despacho_swap_vs_ob_v4.py [--anio 5 --dia 196]
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from comparar_swap_ob_v4 import PARES, git_bytes                 # noqa: E402
from plot_despacho_det import (DIAS_CON_CARGO, horas_de_reloj,  # noqa: E402
                               intervalos_punta)

DELTA_T = 8.0 / 60.0      # h; Mina_modelo v4 corre con intervalos de 8 min en las dos ramas


def leer_swap(cfg):
    sol = HERE.parent / cfg["sol"]
    dt = json.loads((sol / "parameters.json").read_text(encoding="utf-8")).get("delta_t")
    if dt is not None and abs(float(dt) - DELTA_T) > 1e-9:
        raise SystemExit("{}: delta_t = {} h, se esperaba {}".format(sol, dt, DELTA_T))
    return {v: json.loads((sol / (v + ".json")).read_text(encoding="utf-8"))
            for v in ("P_red", "P_gen", "P_bat", "P_pot")}


def leer_ob(cfg, ref):
    sol = cfg["caso"] + "/" + cfg["sol"]
    return {v: json.loads(git_bytes(ref, sol + "/" + v + ".json"))
            for v in ("P_red", "P_gen", "P_bat", "P_pot")}


def rama(nodo, *ejes):
    for eje in ejes:
        if not isinstance(nodo, dict) or eje not in nodo:
            return {}
        nodo = nodo[eje]
    return nodo if isinstance(nodo, dict) else {}


def perfil(datos, anio, dia):
    """(t, P_red, P_gen total, P_bat total, P_pot del año) del dia pedido."""
    def serie(data, externo=None):
        bloques = ([rama(sub, "y", str(anio), "d", str(dia), "t")
                    for sub in rama(data, externo).values()] if externo else
                   [rama(data, "y", str(anio), "d", str(dia), "t")])
        acc = {}
        for blk in bloques:
            for k, v in blk.items():
                acc[float(k)] = acc.get(float(k), 0.0) + float(v)
        return acc

    p_red = serie(datos["P_red"])
    if not p_red:
        raise SystemExit("no hay P_red para y={} d={}".format(anio, dia))
    p_gen, p_bat = serie(datos["P_gen"], "g"), serie(datos["P_bat"], "h")
    ts = [t for t in sorted(p_red) if t >= 1.0]
    arr = lambda d: np.array([d.get(t, 0.0) for t in ts])
    p_pot = float(rama(datos["P_pot"], "y").get(str(anio), 0.0))
    return np.array(ts), arr(p_red), arr(p_gen), arr(p_bat), p_pot


def panel(ax, datos, anio, dia, titulo, ajuste=None):
    """ajuste: salida de ajuste_energia_swap.ajustar para swap. Se dibuja el
    despacho con la energia REAL de cada swap (sobrante del bloque fijo quitado
    intervalo a intervalo en orden de merito)."""
    ts, p_red, p_gen, p_bat, p_pot = perfil(datos, anio, dia)
    if ajuste is not None:
        v = ajuste[(anio, dia)]
        if not np.allclose(v["red"], p_red, atol=1e-6):
            raise SystemExit("{}: el ajuste no corresponde a esta solucion".format(titulo))
        p_red, p_gen, p_bat = v["red_aj"], v["gen_aj"], v["bat_aj"]
    x = np.arange(len(ts))
    demanda = p_red + p_gen + p_bat
    bess_des, bess_car = np.maximum(p_bat, 0.0), np.maximum(-p_bat, 0.0)
    a_bess = np.minimum(bess_des, demanda)
    a_gen = np.minimum(p_gen, demanda - a_bess)
    a_red = demanda - a_bess - a_gen
    if np.min(a_red) < -1e-6:
        raise SystemExit("{}: aporte de red negativo ({:.3f} kW) con el orden de merito".format(
            titulo, np.min(a_red)))

    ax.stackplot(x, a_red, a_gen, a_bess, labels=["Red", "Generación renovable", "BESS descargando"],
                 colors=["#4C72B0", "#F2C14E", "#55A868"], edgecolor="none", alpha=0.95)
    ax.plot(x, demanda, color="black", linewidth=1.2, label="Consumo de estaciones")
    if np.any(bess_car > 1e-9):
        ax.fill_between(x, 0.0, -bess_car, color="#C44E52", alpha=0.55,
                        label="BESS cargando (consumo extra)")
    ax.axhline(0, color="black", linewidth=0.8)

    pico_ventana = float("nan")
    marca = None
    if DIAS_CON_CARGO[0] <= dia <= DIAS_CON_CARGO[1]:
        punta = intervalos_punta(ts.max(), DELTA_T)
        dentro = np.array([int(t) in punta for t in ts])
        if dentro.any():
            ax.axvspan(x[dentro][0], x[dentro][-1], color="#B00020", alpha=0.10, zorder=0)
            pico_ventana = float(np.max(a_red[dentro]))
            if pico_ventana > p_pot + 1e-3:
                raise SystemExit("{}: red en la ventana de punta ({:.3f} kW) > P_pot[{}] = {:.3f} kW: "
                                 "la ventana reconstruida no coincide con el modelo".format(
                                     titulo, pico_ventana, anio, p_pot))
            marca = (x[dentro][0] + x[dentro][-1]) / 2

    horas = horas_de_reloj(ts, DELTA_T)
    paso = max(1, len(ts) // 12)
    idx = list(range(0, len(ts), paso))
    ax.set_xticks([x[i] for i in idx])
    ax.set_xticklabels(["{:02d}:{:02d}".format(int(horas[i]), int(round((horas[i] % 1) * 60)))
                        for i in idx], rotation=45, ha="right")
    ax.set_xlim(0, len(ts) - 1)
    ax.set_title(titulo)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    return marca, {"panel": titulo.replace("\n", " "), "pico_red_dia_kw": float(np.max(a_red)),
                   "pico_red_ventana_kw": pico_ventana, "p_pot_kw": p_pot,
                   "energia_dia_kwh": float(np.sum(demanda) * DELTA_T),
                   "energia_red_dia_kwh": float(np.sum(a_red) * DELTA_T)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anio", type=int, default=5)
    ap.add_argument("--dia", type=int, default=196)
    ap.add_argument("--ob_ref", default="origin/carga_ob_multiaño")
    a = ap.parse_args()

    plt.rcParams.update({"font.size": 11, "axes.titlesize": 13, "axes.labelsize": 12,
                         "xtick.labelsize": 10, "ytick.labelsize": 10, "legend.fontsize": 11})
    for clave, par in PARES.items():
        fig, fila = plt.subplots(1, 2, figsize=(16.5, 5.4), sharey=True)
        marcas, resumen = [], []
        from ajuste_energia_swap import ajustar
        aj_swap, _ = ajustar(HERE.parent / par["swap"]["sol"])
        for ax, (lado, datos, aj) in zip(fila, (("Battery swapping", leer_swap(par["swap"]), aj_swap),
                                                ("On-board", leer_ob(par["ob"], a.ob_ref), None))):
            m, r = panel(ax, datos, a.anio, a.dia, lado, aj)
            marcas.append((ax, m))
            resumen.append(r)
            ax.set_xlabel("Hora de reloj")
        fila[0].set_ylabel("Potencia [kW]")

        y0, y1 = fila[0].get_ylim()
        fila[0].set_ylim(y0, y1 * 1.14)
        for ax, m in marcas:
            if m is not None:
                ax.text(m, fila[0].get_ylim()[1] * 0.985, "cargo por potencia\n18:00–22:00",
                        ha="center", va="top", fontsize=10, color="#B00020", fontweight="bold")

        handles, etiquetas = max((ax.get_legend_handles_labels() for ax in fila),
                                 key=lambda he: len(he[1]))
        fig.legend(handles, etiquetas, loc="lower center", bbox_to_anchor=(0.5, -0.06), ncol=5,
                   frameon=True, framealpha=0.95, edgecolor="gray")
        fig.suptitle("Despacho de potencia — {}\naño {} del horizonte (régimen), día {}, "
                     "Mina_modelo v4".format(par["titulo"], a.anio, a.dia), fontsize=15)
        fig.text(0.5, -0.12, "Atribución por orden de mérito (BESS descargando → generación → red): "
                 "el modelo fija la suma de las fuentes, no el reparto.",
                 ha="center", fontsize=10.5, color="#444444")
        fig.tight_layout(rect=[0, 0.02, 1, 0.95])
        out = HERE / "despacho_swap_vs_ob_v4_{}_y{}_d{}.png".format(clave, a.anio, a.dia)
        fig.savefig(out, dpi=170, bbox_inches="tight")
        plt.close(fig)
        print("ok ->", out)
        for r in resumen:
            print("  {:18s} pico red: dia {:6.1f} kW, ventana {:6.1f} kW (P_pot {:6.1f})  "
                  "energia dia {:8.1f} kWh, de red {:8.1f} kWh".format(
                      r["panel"], r["pico_red_dia_kw"], r["pico_red_ventana_kw"], r["p_pot_kw"],
                      r["energia_dia_kwh"], r["energia_red_dia_kwh"]))

if __name__ == "__main__":
    main()
