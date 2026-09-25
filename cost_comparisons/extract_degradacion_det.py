#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trayectoria de degradacion del pool de baterias por año, escenarios DET de
Tesis_final.

Lee las variables crudas y VERIFICA que cumplan las ecuaciones de degradacion
de ConstraintRules (functions.py), abortando si no:

    (ec. 4) d_y_fade : D[y] = b_bar[y] - gamma_coef * N_ciclos[y]
    (ec. 2) b_y_link : b_bar[y] <= D[y-1] + replace_capacity_fraction*b_max*R[y]
    (ec. 5)            b_bar[y1] = b_max_pool   (fijado en BoundRules)

b_bar[y] es la capacidad al INICIO del año y; D[y] la capacidad al CIERRE del
mismo año. Sin reemplazo D[y] == b_bar[y+1].

R.json es DISPERSO: trae solo los años con reemplazo. Vacio significa que no
hubo ninguno en el horizonte, no que falte el dato.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import consumer as c

HERE = Path(__file__).resolve().parent
BASE = REPO_ROOT / "output" / "Resultados_finales_tesis" / "DET"

ESCENARIOS = {
    "p_red": {"root": BASE / "P_red" / "241kW", "scenario": "DET_241kW_solo_red"},
    "gen_bat": {"root": BASE / "Gen_Bat" / "241kW", "scenario": "DET_241kW_gen_bess"},
}

# Fraccion de b_max que recupera un reemplazo. En esta rama esta hardcodeada en
# 0.3 y no viaja en parameters.json (misma convencion que extract_degradation_*).
DEFAULT_REPLACE_CAPACITY_FRACTION = 0.3

TOL = 1e-6


def serie_por_anio(path):
    """JSON indexado solo por año (eje exterior 'y' o '_1') -> {int: float}.
    Devuelve {} si el archivo no existe o viene vacio: son variables dispersas."""
    if path is None or not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    bloque = data
    for eje in ("y", "_1"):
        if eje in data and isinstance(data[eje], dict):
            bloque = data[eje]
            break
    salida = {}
    for k, v in bloque.items():
        if isinstance(v, dict):
            continue
        try:
            salida[int(float(k))] = float(v)
        except (TypeError, ValueError):
            continue
    return salida


def main():
    for clave, cfg in ESCENARIOS.items():
        root = cfg["root"]
        params = c.load_json(c.find_json_in_folder(root, "parameters.json"))

        b_max = c._as_float(params.get("b_max_pool", params.get("b_max_fleet", 0.0)))
        min_frac = c._as_float(params.get("min_capacity_fraction", 0.0))
        gamma = c._as_float(params.get("gamma_coef", 0.0))
        repl_frac = c._as_float(params.get("replace_capacity_fraction",
                                           DEFAULT_REPLACE_CAPACITY_FRACTION))

        b_bar = serie_por_anio(root / "b_bar.json")
        d_end = serie_por_anio(root / "D.json")
        n_ciclos = serie_por_anio(root / "N_ciclos.json")
        r_repl = serie_por_anio(root / "R.json")  # disperso: {} = sin reemplazos

        anios = sorted(b_bar)
        if not anios:
            raise SystemExit(clave + ": b_bar.json vacio, no hay trayectoria")

        # (ec. 5) el primer año arranca en la capacidad nominal del pool.
        if abs(b_bar[anios[0]] - b_max) > 1e-3:
            raise SystemExit(
                "{}: b_bar[{}] = {:.4f} no arranca en b_max_pool = {:.4f}".format(
                    clave, anios[0], b_bar[anios[0]], b_max))

        # (ec. 4) el fade del año tiene que ser exactamente gamma * N_ciclos.
        for y in anios:
            esperado = b_bar[y] - gamma * n_ciclos.get(y, 0.0)
            if abs(d_end.get(y, 0.0) - esperado) > 1e-3:
                raise SystemExit(
                    "{}: ec.4 no se cumple en y={}: D={:.6f} contra "
                    "b_bar - gamma*N_ciclos = {:.6f}".format(
                        clave, y, d_end.get(y, 0.0), esperado))

        # (ec. 2) el enlace entre años, con el aporte del reemplazo si lo hubo.
        for prev, y in zip(anios, anios[1:]):
            cota = d_end[prev] + repl_frac * b_max * r_repl.get(y, 0.0)
            if b_bar[y] > cota + 1e-3:
                raise SystemExit(
                    "{}: ec.2 violada en y={}: b_bar={:.6f} > D[{}] + aporte "
                    "reemplazo = {:.6f}".format(clave, y, b_bar[y], prev, cota))

        piso = min_frac * b_max
        resultado = {
            "scenario": cfg["scenario"],
            "root": str(root.relative_to(REPO_ROOT)).replace("\\", "/"),
            "b_max_pool": b_max,
            "min_capacity_fraction": min_frac,
            "piso_kwh": piso,
            "gamma_coef": gamma,
            "replace_capacity_fraction": repl_frac,
            "anios": anios,
            "b_bar": {str(y): b_bar[y] for y in anios},
            "D": {str(y): d_end.get(y, 0.0) for y in anios},
            "N_ciclos": {str(y): n_ciclos.get(y, 0.0) for y in anios},
            "anios_reemplazo": [y for y in anios if r_repl.get(y, 0.0) > 0.5],
            "capacidad_final_kwh": d_end[anios[-1]],
            "retencion_final": d_end[anios[-1]] / b_max if b_max else 0.0,
        }

        out = HERE / ("det_tesis_" + clave + "_degradation.json")
        out.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")
        print("[{}] ec.2/4/5 ok. {:.1f} -> {:.1f} kWh ({:.1%} retenido), piso {:.1f}, "
              "reemplazos: {} -> {}".format(
                  clave, b_max, resultado["capacidad_final_kwh"],
                  resultado["retencion_final"], piso,
                  resultado["anios_reemplazo"] or "ninguno", out.name))


if __name__ == "__main__":
    main()
