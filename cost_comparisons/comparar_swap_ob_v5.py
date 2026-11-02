#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Battery swapping v5 contra carga on-board: los tres casos base norte 0,5C (RED,
RED_GEN, RED_GEN_BESS) con la operacion de swap de la version 5
(cola_lshaped_v5.sh: swap solo con SOC <= 30 % y duracion de la carga segun la
capacidad del año), solucion final del L-shaped en casos_finales_v5/.

Los mismos dos ajustes y controles que comparar_swap_ob_v4.py (se reusan sus
funciones): energia real de cada swap (ajuste_energia_swap.py, que mide la
duracion de cada carga en Sv) y redimensionamiento de generacion, BESS y red
(redimensionar_gen_bess.py). On-board es el mismo de v4. La fila v4 de cada caso
(swap_vs_ob_v4_casos.csv) se copia al lado para comparar.

Necesita parameters.json y consumer.log en cada solucion v5 (escribir_parameters.py
y consumer.py, con ELMO_SWAP_SOC_MAX=0.30 ELMO_CARGA_CAPACIDAD_REAL=1).

    python cost_comparisons/comparar_swap_ob_v5.py [--ob_ref origin/carga_ob_multiaño]
        [--sub lshaped_r] [--casos 1,2,3]

--sub es la carpeta del L-shaped en cada caso (lshaped_r: tercera vuelta, 1-nov,
con la reparacion de inversiones y el update de cotas).
"""
import argparse
import json

import pandas as pd

from comparar_swap_ob_v4 import (CASOS as CASOS_V4, HERE, con_lp, lado_ob, lado_swap,
                                 reconciliar, redimensionado_ob)

V5 = "output/Resultados_finales_tesis/Mina_modelo/casos_finales_v5/Produccion_base/zona_norte/05C"


NOMBRES = {"1": "RED", "2": "RED_GEN_BESS", "3": "RED_GEN"}


def _caso(clave, sub):
    sol = "{}/{}/{}".format(V5, NOMBRES[clave], sub)
    ub = json.loads((HERE.parent / sol / "resumen.json").read_text())["ub_final"]
    return {"titulo": CASOS_V4[clave]["titulo"], "ob": CASOS_V4[clave]["ob"],
            "swap": {"sol": sol + "/solucion", "consumer": sol + "/consumer.log", "ub": ub}}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ob_ref", default="origin/carga_ob_multiaño")
    ap.add_argument("--sub", default="lshaped")
    ap.add_argument("--casos", default="1,2,3")
    a = ap.parse_args()
    CASOS = {c: _caso(c, a.sub) for c in a.casos.split(",")}

    v4 = pd.read_csv(HERE / "swap_vs_ob_v4_casos.csv", dtype={"caso": str}).set_index("caso")
    res, filas = {}, []
    for clave, caso in CASOS.items():
        c_s, inv_s, deg_s, ub_s, flota, aj, redim, params = lado_swap(caso["swap"])
        c_o, inv_o, deg_o, ub_o, n_lhd, ub_fuente = lado_ob(caso["ob"], a.ob_ref)
        modelo = reconciliar("swap " + clave, c_s, inv_s, ub_s)
        energia = dict(modelo)
        energia["grid_energy_cost"] = aj["central"]
        energia["total_cost"] = modelo["total_cost"] - modelo["grid_energy_cost"] + aj["central"]
        ob_modelo = reconciliar("OB " + clave, c_o, inv_o, ub_o)
        redim_ob = redimensionado_ob(caso["ob"], a.ob_ref, params, ob_modelo)
        swap = con_lp(modelo, redim["lp_carga_real"])
        ob = con_lp(ob_modelo, redim_ob["lp_carga_ob"])
        res[clave] = {"titulo": caso["titulo"],
                      "swap": {"costos": swap, "costos_modelo": modelo,
                               "costos_ajuste_energia": energia, "ajuste_energia": aj,
                               "redimensionamiento": redim, "deg": deg_s, "flota": flota,
                               "fuente": caso["swap"]["sol"]},
                      "ob": {"costos": ob, "costos_modelo": ob_modelo,
                             "redimensionamiento": redim_ob, "ub_fuente": ub_fuente,
                             "deg": deg_o, "n_lhd": n_lhd}}
        s, o = swap["total_cost"], ob["total_cost"]
        lr, lo = redim["lp_carga_real"], redim_ob["lp_carga_ob"]
        e = aj["energia_mwh"]
        print("[{}] swap v5 modelo {:,.0f} -> energia real {:,.0f} -> redimensionado {:,.0f} | OB {:,.0f}"
              " | swap vs OB {:+.2%} (v4 {:+.2f} %) | consumo {:,.0f} -> {:,.0f} MWh | controles "
              "{:+.2f} / {:+.2f}".format(clave, modelo["total_cost"], energia["total_cost"], s, o,
                                         s / o - 1, v4.loc[clave, "dif_redimensionado_pct"],
                                         e["dem"], e["dem_aj"], redim["control"], redim_ob["control"]))
        fila = {"caso": clave, "titulo": caso["titulo"], "swap_modelo": modelo["total_cost"],
                "swap_ajuste_energia": energia["total_cost"], "swap_redimensionado": s, "ob": o,
                "dif_ajuste_energia_pct": 100 * (energia["total_cost"] / o - 1),
                "dif_redimensionado_pct": 100 * (s / o - 1),
                "consumo_modelo_mwh": e["dem"], "consumo_real_mwh": e["dem_aj"],
                "control_lp_swap": redim["control"], "control_lp_ob": redim_ob["control"],
                "bess_u_swap_redim": lr["H"], "bess_u_ob": lo["H"],
                "v4_swap_modelo": v4.loc[clave, "swap_modelo"],
                "v4_swap_redimensionado": v4.loc[clave, "swap_redimensionado"],
                "v4_dif_redimensionado_pct": v4.loc[clave, "dif_redimensionado_pct"]}
        for k in ("cost_stations", "cost_bays", "cost_chargers", "cost_batteries", "substation_cost",
                  "gen_inv_cost", "bess_inv_cost", "peak_power_cost", "grid_energy_cost"):
            fila["swap_" + k], fila["ob_" + k] = swap[k], ob[k]
        for g in lr["G_kw"]:
            fila["{}_kw_swap_redim".format(g)] = lr["G_kw"][g]
            fila["{}_kw_ob".format(g)] = lo["G_kw"].get(g, 0.0)
        filas.append(fila)

    (HERE / "swap_vs_ob_v5.json").write_text(
        json.dumps(res, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    csv = HERE / "swap_vs_ob_v5_casos.csv"
    pd.DataFrame(filas).to_csv(csv, index=False, float_format="%.2f")
    print("ok ->", csv)


if __name__ == "__main__":
    main()
