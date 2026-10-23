#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Despacho de swap corregido intervalo a intervalo por la energia REAL de cada swap.

EL PROBLEMA. El modelo cobra cada recarga como un bloque fijo de t_charge
intervalos a p_charger (charging_duration_rule: Sv[k,y,d,t,a] = X_ini[k,y,d,a]
durante t_charge intervalos desde a), o sea llevar la bateria de bmin a 100 %.
La bateria que llega con un swap solo necesita (capacidad del año - B_llegada) /
eta_charge (consumer.calculate_real_charged_energy_from_swaps, la convencion
justa de esta rama). El resto del bloque es energia que el modelo despacha de mas:
16-20 % en los casos v4.

LA CORRECCION, sin re-optimizar:
  1. Cada swap entrega una bateria descargada; las cargas que empiezan en a
     (X_ini[a] = Sv[k,y,d,a,a]) la toman en orden de llegada (FIFO), en forma
     CICLICA: el inventario del inicio del dia es el del final (CI_X_dch), asi
     que las primeras cargas del dia son de los ultimos swaps. Se resuelve
     recorriendo el dia dos veces y quedandose con la segunda vuelta.
  2. En el bloque de cada bateria se cuentan como necesarios los primeros
     E_real / (p_charger * delta_t) intervalos (el ultimo, fraccionario); el
     resto del bloque es sobrante.
  3. En cada intervalo el sobrante se descuenta en ORDEN DE MERITO: primero la
     red (lo mas caro), despues la descarga del BESS (la energia queda guardada),
     al final la generacion (quedaria vertida). Nadie verteria solar para seguir
     comprando red, asi que es lo que haria un operador con el mismo plan.

VERIFICACION. La energia sobrante total tiene que ser sum(bloques) - sum(real):
con solo red el resultado reproduce exactamente la energia real del consumer.
"""
import json
import math
import sys
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import consumer as c                                             # noqa: E402

DIAS = (15, 105, 196, 288)


def _rama(nodo, *ejes):
    for eje in ejes:
        if not isinstance(nodo, dict) or eje not in nodo:
            return {}
        nodo = nodo[eje]
    return nodo if isinstance(nodo, dict) else {}


def _serie(data, y, d, T, externo=None):
    subs = list(_rama(data, externo).values()) if externo else [data]
    acc = np.zeros(T)
    for s in subs:
        for k, v in _rama(s, "y", str(y), "d", str(d), "t").items():
            t = int(float(k))
            if 1 <= t <= T:
                acc[t - 1] += float(v)
    return acc


def ajustar(root):
    """Devuelve {(y, d): dict(red, gen, bat, dem, red_aj, gen_aj, bat_aj, dem_aj,
    sobrante)} en kW por intervalo, mas un resumen con las verificaciones."""
    root = Path(root)
    params = c.load_json(c.find_json_in_folder(root, "parameters.json"))
    dt = float(params["delta_t"])
    p_ch = float(params["p_charger"])
    t_charge = int(round(float(params["t_charge"])))
    leer = lambda n: json.loads((root / n).read_text(encoding="utf-8"))
    sv, red, gen = leer("Sv.json"), leer("P_red.json"), leer("P_gen.json")
    bat = leer("P_bat.json")

    _, meta, eventos = c.calculate_real_charged_energy_from_swaps(root)
    por_dia = {}
    for ev in eventos:
        clave = (str(ev["station"]), int(float(ev["year"])), int(ev["day"]))
        por_dia.setdefault(clave, []).append(ev)

    T = max(int(float(k)) for y in _rama(red, "y").values() for d in _rama(y, "d").values()
            for k in _rama(d, "t"))
    out, sobrante_total, bloque_total, real_total, sin_pareja = {}, 0.0, 0.0, 0.0, 0
    estaciones = list(_rama(sv, "k"))
    years = sorted(int(y) for y in _rama(red, "y"))
    e_int = p_ch * dt                                   # kWh de red por intervalo de carga
    for y in years:
        for d in DIAS:
            sobr = np.zeros(T)
            for k in estaciones:
                # Inicios de carga: X_ini[a] = Sv[k,y,d,a,a].
                filas = _rama(sv, "k", k, "y", str(y), "d", str(d), "t")
                inicios = []
                for t_txt, sub in filas.items():
                    t = int(float(t_txt))
                    n = float(_rama(sub, "a").get(str(t), _rama(sub, "a").get("{}.0".format(t), 0)))
                    inicios += [t] * int(round(n))
                inicios.sort()
                evs = sorted(por_dia.get((k, y, d), []), key=lambda e: e["t"])
                if len(inicios) != len(evs):
                    raise SystemExit("{} y{} d{}: {} cargas y {} swaps; el inventario no es ciclico".format(
                        k, y, d, len(inicios), len(evs)))
                # FIFO ciclico: dos vueltas al dia, se usa la segunda. En un
                # mismo t las llegadas entran antes que las cargas (pueden
                # empezar a cargar en el mismo t, inventory_discharged_batteries_rule).
                cola, pareja = deque(), {}
                for vuelta in (0, 1):
                    llegadas = deque(evs)
                    for t in range(1, T + 1):
                        while llegadas and llegadas[0]["t"] == t:
                            cola.append(llegadas.popleft())
                        for _ in range(inicios.count(t)):
                            if not cola:
                                if vuelta == 1:
                                    sin_pareja += 1
                                continue
                            ev = cola.popleft()
                            if vuelta == 1:
                                pareja.setdefault(t, []).append(ev)
                for a, lista in pareja.items():
                    for ev in lista:
                        n_nec = float(ev["real_event_grid_kwh"]) / e_int
                        real_total += float(ev["real_event_grid_kwh"])
                        bloque_total += t_charge * e_int
                        for j in range(t_charge):
                            frac_sobra = min(1.0, max(0.0, (j + 1) - n_nec))
                            if frac_sobra <= 0:
                                continue
                            t = a + j
                            if t > T:                       # el bloque cruza el fin del dia
                                t -= T
                            sobr[t - 1] += frac_sobra * p_ch
            r, g = _serie(red, y, d, T), _serie(gen, y, d, T, "g")
            b = _serie(bat, y, d, T, "h")
            dem = r + g + b
            # Orden de merito: red -> descarga BESS -> generacion.
            quita_r = np.minimum(r, sobr)
            resto = sobr - quita_r
            quita_b = np.minimum(np.maximum(b, 0.0), resto)
            resto -= quita_b
            quita_g = np.minimum(g, resto)
            resto -= quita_g
            if np.max(resto) > 1e-6:
                raise SystemExit("y{} d{}: sobrante mayor que el consumo ({:.3f} kW)".format(
                    y, d, float(np.max(resto))))
            sobrante_total += float(np.sum(sobr) * dt)
            out[(y, d)] = {"red": r, "gen": g, "bat": b, "dem": dem, "sobrante": sobr,
                           "red_aj": r - quita_r, "gen_aj": g - quita_g, "bat_aj": b - quita_b,
                           "dem_aj": dem - sobr}
    resumen = {"sobrante_kwh_dias": sobrante_total, "bloques_menos_real_kwh_dias": bloque_total - real_total,
               "swaps_sin_pareja": sin_pareja, "dt": dt,
               "escala": float(params.get("scaling_factor_op_cost", 1.0)),
               "tasa": c._get_discount_rate(params) if c._has_degradation_data(params) else 0.0}
    if sin_pareja or abs(sobrante_total - (bloque_total - real_total)) > 1e-6 * max(1.0, bloque_total):
        raise SystemExit("ajuste inconsistente: {}".format(resumen))
    return out, resumen


def totales(out, resumen):
    """Energias del horizonte [MWh] y factor de descuento ponderado por año para
    pasar de costo de red del modelo a costo de red ajustado (precio plano)."""
    dt, esc, r = resumen["dt"], resumen["escala"], resumen["tasa"]
    tot = {k: 0.0 for k in ("red", "gen", "dem", "red_aj", "gen_aj", "dem_aj", "red_vp", "red_aj_vp",
                            "des", "des_aj")}
    for (y, d), v in out.items():
        f = esc * dt / 1000.0
        df = 1.0 / (1.0 + r) ** y
        for k in ("red", "gen", "dem", "red_aj", "gen_aj", "dem_aj"):
            tot[k] += float(np.sum(v[k])) * f
        tot["des"] += float(np.sum(np.maximum(v["bat"], 0))) * f
        tot["des_aj"] += float(np.sum(np.maximum(v["bat_aj"], 0))) * f
        tot["red_vp"] += float(np.sum(v["red"])) * f * df
        tot["red_aj_vp"] += float(np.sum(v["red_aj"])) * f * df
    return tot


if __name__ == "__main__":
    V = HERE.parent / "output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base"
    for nom, sol in (("solo red", V / "zona_norte/05C/RED/reparar_reemplazo/solucion"),
                     ("gen+BESS", V / "zona_centro/05C/RED_GEN_BESS/mejorar_ub_2/solucion")):
        out, res = ajustar(sol)
        t = totales(out, res)
        print("== swap {}: consumo {:,.0f} -> {:,.0f} MWh | red {:,.0f} -> {:,.0f} MWh "
              "(VP x{:.4f}) | generacion usada {:,.0f} -> {:,.0f} | descarga BESS {:,.0f} -> {:,.0f} | "
              "penetracion {:.1%} -> {:.1%}".format(
                  nom, t["dem"], t["dem_aj"], t["red"], t["red_aj"], t["red_aj_vp"] / t["red_vp"],
                  t["gen"], t["gen_aj"], t["des"], t["des_aj"],
                  1 - t["red"] / t["dem"], 1 - t["red_aj"] / t["dem_aj"]))
