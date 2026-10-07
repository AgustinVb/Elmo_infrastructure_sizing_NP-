"""Carga de un incumbente_respaldo.pkl de Nested Benders sobre un monolitico.

Portado de battery_swapping_multiaño (diagnostico_gap_dias.cargar_respaldo),
para la metodologia de cierre del gap (mejorar_ub.py).
"""
import pickle

import pyomo.environ as pyo

from src.optimization.decomposition.driver import infer_exogenous_stations


def cargar_respaldo(om, ms, ts, ruta):
    """Carga sobre om.model el incumbente de un incumbente_respaldo.pkl de
    Nested Benders (mas X/Delta_X exogenos). Devuelve el dict del respaldo."""
    # best_full_solution: {año: {variable: {indice: valor}}}, con el
    # incumbente de cada bloque anual (ver driver._guardar_respaldo).
    with open(ruta, "rb") as fh:
        r = pickle.load(fh)
    n_ok = n_falta = 0
    for _y, vars_y in r["best_full_solution"].items():
        for nombre, vals in vars_y.items():
            comp = getattr(om.model, nombre, None)
            # Las escalares (H, y D_prev/alpha/w_deg que solo existen en el
            # bloque) vienen como un float suelto, no como {indice: valor}.
            if not isinstance(vals, dict):
                if comp is not None and not comp.is_indexed():
                    comp.set_value(vals, skip_validation=True)
                    n_ok += 1
                else:
                    n_falta += 1
                continue
            if comp is None:
                n_falta += len(vals)
                continue
            for idx, v in vals.items():
                if idx in comp:
                    vd = comp[idx]
                    # Ruido de IntFeasTol en las enteras (como en
                    # driver.build_report_model).
                    if vd.is_binary() or vd.is_integer():
                        v = int(round(v))
                    vd.set_value(v, skip_validation=True)
                    n_ok += 1
                else:
                    n_falta += 1
    # X y Delta_X son exogenas en el modo descompuesto: no estan en el
    # respaldo y, si quedan en 0, el pulido con las enteras fijas es
    # infactible. Se reconstruyen como en driver.build_report_model.
    exo = infer_exogenous_stations(ms, ts)
    anterior = {k: 0 for k in om.model.stations_set}
    for y in om.model.years:
        for k in om.model.stations_set:
            om.model.X[k, y].value = exo[y][k]
            om.model.Delta_X[k, y].value = max(exo[y][k] - anterior[k], 0)
        anterior = {k: exo[y][k] for k in om.model.stations_set}
    # El pulido fija TODAS las enteras: las que quedan sin valor van a 0.
    sin_valor = {}
    for comp in om.model.component_objects(pyo.Var, active=True):
        for vd in comp.values():
            if (vd.is_binary() or vd.is_integer()) and vd.value is None:
                sin_valor[comp.name] = sin_valor.get(comp.name, 0) + 1
    if sin_valor:
        print(f"[diag] enteras sin valor (iran a 0 en el pulido): {sin_valor}", flush=True)
    print(f"[diag] respaldo {ruta} (iteracion {r['iteracion']}, "
          f"incumbente {r['best_cost']:,.2f}): {n_ok:,} valores cargados, "
          f"{n_falta:,} sin variable en el modelo", flush=True)
    return r
