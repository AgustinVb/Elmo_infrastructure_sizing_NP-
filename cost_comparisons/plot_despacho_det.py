#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Despacho de potencia de un dia representativo, escenarios DET de Tesis_final:
como se cubre el consumo de las estaciones de carga, apilando red, solar y BESS
descargando. Dos paneles, solo-red contra generacion+BESS, mismo año y dia.

BALANCE DEL MODELO (power_balance, functions.py):

    P_red[y,d,t] + sum_g P_gen[g,y,d,t] + sum_h P_bat[h,y,d,t] = demanda[y,d,t]

con P_bat > 0 DESCARGANDO. La demanda no se lee de Sv (5 indices, enorme): se
reconstruye como la suma del lado izquierdo, que por el balance ES la demanda y
ademas garantiza consistencia con la solucion escrita.

SUPUESTO DE ATRIBUCION. Cuando el BESS carga, las fuentes suman MAS que la
demanda: el modelo fija la SUMA, no la atribucion por fuente. Para que el
apilado sea exactamente el consumo hay que elegir un orden de merito. Aca se usa:

    1) primero el BESS descargando,
    2) despues la solar,
    3) la red cubre el resto.

Es un supuesto de presentacion, no un resultado del modelo. La carga del BESS
se dibuja aparte, como area bajo el cero, para que no se pierda de vista que
esa energia tambien se compro. Hay un assert de que el apilado suma la demanda.

CONVENCIONES
  Año 1 del horizonte = 2035 (misma que json_plotter.py).
  base_hour = 8,5: la hora de reloj es (base_hour + (t-1)*delta_t) mod 24.
  Ventana con cargo por potencia: 18:00-22:00 y SOLO en los dias 91-244. Se
  sombrea, porque si no no se entiende de donde sale el ahorro.

UNIDADES: G_g y H estan en unidades, no en kW; aca no importa porque P_gen y
P_bat ya son potencias en kW.
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import consumer as c

HERE = Path(__file__).resolve().parent
BASE = REPO_ROOT / "output" / "Resultados_finales_tesis" / "DET"

ANIO_BASE = 2035
BASE_HOUR = 8.5
VENTANA_PUNTA = (18.0, 22.0)
DIAS_CON_CARGO = (91, 244)

PANELES = [
    ("p_red", BASE / "P_red" / "241kW", "Solo red"),
    ("gen_bat", BASE / "Gen_Bat" / "241kW", "Generación + BESS"),
]


def perfil_dia(root, anio, dia):
    """Devuelve (t_ordenados, P_red, P_gen_total, P_bat_total) del dia pedido.
    Los JSON son dispersos y anidados con marcadores de nombre de indice; se
    navega por esos marcadores y se toma 0.0 para lo ausente."""

    def rama(data, *ejes):
        """Baja por los marcadores de nombre de indice ('y','d','t','g','h')."""
        nodo = data
        for eje in ejes:
            if not isinstance(nodo, dict):
                return {}
            if eje in nodo:
                nodo = nodo[eje]
            else:
                return {}
        return nodo if isinstance(nodo, dict) else {}

    def serie_t(path, ejes_externos=()):
        """{t_float: valor} sumando sobre el eje externo (g o h) si lo hay."""
        if path is None or not Path(path).exists():
            return {}
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        bloques = []
        if ejes_externos:
            externo = rama(data, *ejes_externos)
            for _nombre, sub in externo.items():
                bloques.append(rama(sub, "y", str(anio), "d", str(dia), "t"))
        else:
            bloques.append(rama(data, "y", str(anio), "d", str(dia), "t"))
        acumulado = {}
        for blk in bloques:
            for k, v in blk.items():
                if isinstance(v, dict):
                    continue
                acumulado[float(k)] = acumulado.get(float(k), 0.0) + float(v)
        return acumulado

    p_red = serie_t(root / "P_red.json")
    p_gen = serie_t(root / "P_gen.json", ("g",))
    p_bat = serie_t(root / "P_bat.json", ("h",))

    if not p_red:
        raise SystemExit(
            "{}: no hay P_red para y={} d={}. Dias disponibles en esa corrida: "
            "{}".format(root.name, anio, dia, sorted(dias_disponibles(root))))

    ts = sorted(p_red)
    # t = 0 aparece en algunas variables como condicion inicial; el balance
    # corre sobre los 180 intervalos reales, que arrancan en 1.
    ts = [t for t in ts if t >= 1.0]
    arr = lambda d: np.array([d.get(t, 0.0) for t in ts], dtype=float)
    return np.array(ts), arr(p_red), arr(p_gen), arr(p_bat)


def dias_disponibles(root):
    data = json.loads((root / "P_red.json").read_text(encoding="utf-8"))
    dias = set()
    for _y, sub in data.get("y", {}).items():
        dias.update(int(float(d)) for d in sub.get("d", {}))
    return dias


def horas_de_reloj(ts, delta_t):
    return np.array([(BASE_HOUR + (t - 1.0) * delta_t) % 24.0 for t in ts])


def intervalos_punta(max_t, delta_t, base_hour=BASE_HOUR, ventana=VENTANA_PUNTA):
    """Conjunto de t con cargo por potencia, replicando EXACTAMENTE
    _build_intervals_from_clock_windows() de functions.py.

    El modelo clasifica por el PUNTO MEDIO del intervalo, no por su inicio:
        mid = (t-1)*dt + dt/2,  se incluye t si  a_rel <= mid < b_rel.
    Tomar el inicio corre la ventana un intervalo en cada extremo y mete
    intervalos que el modelo NO cobra: aca eso daba un pico de 238 kW dentro
    de una ventana donde P_pot = 0, o sea una contradiccion con la solucion.
    """
    dt_min = int(round(delta_t * 60))
    base_min = int(round(base_hour * 60))
    a = int(round(ventana[0] * 60))
    b = int(round(ventana[1] * 60))
    if a < base_min:
        a += 24 * 60
    if b < base_min:
        b += 24 * 60
    if b <= a:
        b += 24 * 60
    a_rel, b_rel = a - base_min, b - base_min
    return {t for t in range(1, int(max_t) + 1)
            if a_rel <= (t - 1) * dt_min + dt_min / 2 < b_rel}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--anio", type=int, default=8,
                    help="año del horizonte (1..14). Default 8: el de mayor meta "
                         "de produccion segun el presolve de capacidad.")
    ap.add_argument("--dia", type=int, default=196,
                    help="dia del año. Default 196: cae en la ventana con cargo "
                         "por potencia (91-244) y existe en las dos corridas.")
    args = ap.parse_args()

    params = c.load_json(c.find_json_in_folder(PANELES[1][1], "parameters.json"))
    delta_t = c._as_float(params.get("delta_t", 8.0 / 60.0))

    plt.rcParams.update({
        "font.size": 12, "axes.titlesize": 15, "axes.labelsize": 13,
        "xtick.labelsize": 11, "ytick.labelsize": 11, "legend.fontsize": 11,
    })

    fig, axes = plt.subplots(1, 2, figsize=(16.5, 7.6), sharey=True)
    resumen = []
    # La leyenda va una sola vez y FUERA de los ejes: dentro tapaba barras de
    # red en el panel izquierdo y el rotulo de la ventana de punta en el
    # derecho (los dos paneles tienen datos en las cuatro esquinas).
    marcas = []

    for ax, (clave, root, titulo) in zip(axes, PANELES):
        ts, p_red, p_gen, p_bat = perfil_dia(root, args.anio, args.dia)
        horas = horas_de_reloj(ts, delta_t)
        x = np.arange(len(ts))

        demanda = p_red + p_gen + p_bat
        bess_des = np.maximum(p_bat, 0.0)      # descargando
        bess_car = np.maximum(-p_bat, 0.0)     # cargando (se dibuja bajo cero)

        # Orden de merito (supuesto de presentacion, ver docstring).
        aporte_bess = np.minimum(bess_des, demanda)
        aporte_solar = np.minimum(p_gen, demanda - aporte_bess)
        aporte_red = demanda - aporte_bess - aporte_solar

        desvio = np.max(np.abs(aporte_red + aporte_solar + aporte_bess - demanda))
        assert desvio < 1e-6, (
            "el apilado no suma la demanda en {} (desvio max {:.3e} kW)".format(clave, desvio))
        if np.min(aporte_red) < -1e-6:
            raise SystemExit(
                "{}: el orden de merito dejo aporte de red negativo ({:.3f} kW): "
                "las fuentes priorizadas superan la demanda.".format(clave, np.min(aporte_red)))

        ax.stackplot(x, aporte_red, aporte_solar, aporte_bess,
                     labels=["Red", "Solar", "BESS descargando"],
                     colors=["#4C72B0", "#F2C14E", "#55A868"], edgecolor="none", alpha=0.95)
        ax.plot(x, demanda, color="black", linewidth=1.4, label="Consumo de estaciones")
        if np.any(bess_car > 1e-9):
            ax.fill_between(x, 0.0, -bess_car, color="#C44E52", alpha=0.55,
                            label="BESS cargando (consumo extra)")

        # Ventana con cargo por potencia. Solo aplica en los dias 91-244; si el
        # dia elegido cae fuera, se dice en vez de sombrear algo que no rige.
        rige = DIAS_CON_CARGO[0] <= args.dia <= DIAS_CON_CARGO[1]
        if rige:
            t_punta = intervalos_punta(ts.max(), delta_t)
            dentro = np.array([int(t) in t_punta for t in ts])
            if dentro.any():
                ax.axvspan(x[dentro][0], x[dentro][-1], color="#B00020", alpha=0.10, zorder=0)
                marca_punta = ((x[dentro][0] + x[dentro][-1]) / 2,
                               "cargo por potencia\n18:00–22:00")
            else:
                marca_punta = None
        else:
            marca_punta = None
            ax.set_xlabel("Hora de reloj  ·  día {} fuera de 91-244: sin cargo por "
                          "potencia".format(args.dia))

        # Eje horizontal en hora de reloj: el dia arranca a las 8:30, no a las 0.
        paso = max(1, len(ts) // 12)
        idx = list(range(0, len(ts), paso))
        ax.set_xticks([x[i] for i in idx])
        ax.set_xticklabels(["{:02d}:{:02d}".format(int(horas[i]), int(round((horas[i] % 1) * 60)))
                            for i in idx], rotation=45, ha="right")
        ax.set_xlim(0, len(ts) - 1)
        ax.set_title(titulo)
        ax.set_xlabel("Hora de reloj")
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
        ax.axhline(0, color="black", linewidth=0.8)

        # Pico de red DENTRO de la ventana: es el que fija P_pot y el cargo por
        # potencia, no el pico del dia entero.
        if rige and dentro.any():
            pico_ventana = float(np.max(aporte_red[dentro]))
            # Validacion independiente de la ventana: power_peak_limit impone
            # P_red <= P_pot[y] en TODO intervalo de punta de los dias 91-244.
            # Si la ventana reconstruida estuviera corrida, entrarian
            # intervalos que el modelo no cobra y esto reventaria. Es lo que
            # delato el criterio de inicio en vez de punto medio.
            p_pot = json.loads((root / "P_pot.json").read_text(encoding="utf-8"))
            p_pot_y = float(p_pot.get("y", {}).get(str(args.anio), 0.0))
            if pico_ventana > p_pot_y + 1e-3:
                raise SystemExit(
                    "{}: el pico de red en la ventana ({:.3f} kW) supera P_pot[{}] "
                    "= {:.3f} kW. La ventana de punta reconstruida no coincide con "
                    "time_intervals_peak_set (revisar el criterio de punto medio "
                    "en intervalos_punta).".format(clave, pico_ventana, args.anio, p_pot_y))
        else:
            pico_ventana = float("nan")
        resumen.append((titulo, float(np.max(aporte_red)), pico_ventana,
                        float(np.sum(demanda) * delta_t),
                        float(np.sum(aporte_red) * delta_t)))
        marcas.append((ax, marca_punta))

    # Headroom comun para que el rotulo de la ventana no se monte sobre los
    # datos; sharey=True asi que alcanza con fijarlo en un eje.
    y0, y1 = axes[0].get_ylim()
    axes[0].set_ylim(y0, y1 * 1.16)
    for ax, marca in marcas:
        if marca is not None:
            ax.text(marca[0], axes[0].get_ylim()[1] * 0.985, marca[1],
                    ha="center", va="top", fontsize=10.5, color="#B00020",
                    fontweight="bold")

    axes[0].set_ylabel("Potencia (kW)")
    # Solo-red no dibuja "BESS cargando", asi que la leyenda completa sale del
    # panel que tenga mas series, no del primero.
    handles, etiquetas = max((ax.get_legend_handles_labels() for ax in axes),
                             key=lambda he: len(he[1]))
    fig.legend(handles, etiquetas, loc="lower center", bbox_to_anchor=(0.5, -0.03),
               ncol=5, frameon=True, framealpha=0.95, edgecolor="gray")
    fig.suptitle(
        "Despacho de potencia — DET 241 kW, año {} ({}), día {}".format(
            args.anio, ANIO_BASE + args.anio - 1, args.dia),
        fontsize=17)
    fig.text(0.5, -0.10,
             "Atribución por orden de mérito (BESS descargando → solar → red): el modelo fija la suma "
             "de las fuentes, no el reparto.\n"
             "Los dos escenarios corrieron con distinta cantidad de días representativos; el perfil "
             "intradiario es comparable, los agregados anuales no.",
             ha="center", fontsize=10.5, color="#444444")

    fig.tight_layout(rect=[0, 0.02, 1, 0.95])
    out = HERE / "despacho_det_tesis_y{}_d{}.png".format(args.anio, args.dia)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    print("ok ->", out)
    for titulo, pico_dia, pico_ventana, e_dem, e_red in resumen:
        print("  {:22s} pico de red: dia {:7,.1f} kW / ventana de punta {:7,.1f} kW   "
              "energia dia: demanda {:9,.1f} kWh, red {:9,.1f} kWh".format(
                  titulo, pico_dia, pico_ventana, e_dem, e_red))


if __name__ == "__main__":
    main()
