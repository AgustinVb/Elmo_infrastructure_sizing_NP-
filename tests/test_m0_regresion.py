"""M0 -- el refactor aditivo no cambia el modelo monolitico.

Los ganchos que necesita la descomposicion (years_override, exogenous_stations)
son opcionales y su default tiene que reproducir exactamente el modelo de
siempre. Este test compara, sobre un escenario real:

  1. el conteo de variables y restricciones del modelo armado con los defaults
     contra el armado pasando explicitamente el horizonte completo como
     years_override (que es el camino nuevo);
  2. que el valor de la funcion objetivo evaluada en el mismo punto coincida en
     ambos.

Si alguna de las dos cosas se rompe, el refactor dejo de ser aditivo.

Uso:  python tests/test_m0_regresion.py [carpeta_del_escenario] [n_years]
"""
import os
import sys
from argparse import Namespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import pyomo.environ as pyo

from setup import build_mine
from src.optimization.functions import (
    BoundRules,
    ConstraintRules,
    ObjectiveRules,
    OptParameters,
    OptSets,
)

# data/DET/241kW_2dias no sirve: su hoja de estaciones no trae la columna
# c_inv_ssee que pide OptParameters (datos mas viejos que el codigo).
DEFAULT_SCENARIO = "data/DCH/160kW_2dias/"
FAILS = []


def check(cond, label, detail=""):
    print(("  OK   " if cond else "  FALLA") + f"  {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def build_model(mine_system, time_series, years_override=None):
    """Arma el modelo completo con las mismas clases que usa OptModel."""
    model = pyo.ConcreteModel()
    kwargs = {} if years_override is None else {"years_override": years_override}
    OptSets(mine_system, time_series, **kwargs).build_sets(model)
    OptParameters(mine_system, time_series, **kwargs).build_parameters(model)
    BoundRules(mine_system, time_series, **kwargs).build_all_variables(model)
    ConstraintRules(mine_system, time_series, **kwargs).build_all_constraints(model)
    obj_rules = ObjectiveRules(mine_system, time_series, **kwargs)
    model.obj = pyo.Objective(rule=obj_rules.total_cost, sense=pyo.minimize)
    return model


def size(m):
    nv = sum(len(v) if v.is_indexed() else 1 for v in m.component_objects(pyo.Var, active=True))
    nc = sum(len(c) if c.is_indexed() else 1
             for c in m.component_objects(pyo.Constraint, active=True))
    return nv, nc


def obj_at_ones(m):
    """Objetivo evaluado con todas las variables en 1 (recortado a sus cotas):
    un punto comun y reproducible para comparar las dos expresiones sin resolver
    nada. Se usa 1 y no 0 para que todos los terminos de costo aporten -- en 0 el
    objetivo da 0 en cualquier formulacion y la comparacion no probaria nada."""
    for v in m.component_objects(pyo.Var, active=True):
        for idx in (v if v.is_indexed() else [None]):
            vd = v[idx] if idx is not None else v
            if vd.fixed:
                continue
            val = 1.0
            if vd.lb is not None:
                val = max(val, vd.lb)
            if vd.ub is not None:
                val = min(val, vd.ub)
            vd.set_value(val)
    return pyo.value(m.obj)


def main(scenario, n_years):
    args = Namespace(
        data_folder=scenario, model="elmo_data.xlsx", series="time_series.xlsx",
        consumption_model="wp2", wp2_consumption_json=None, n_years=n_years,
    )
    _series, mine_system, time_series = build_mine(args)
    years = sorted(time_series.years)
    print(f"escenario: {scenario}  ({len(years)} años {years}, "
          f"{len(mine_system.get_system_stations())} naves)")

    print("\n[1] defaults (camino de siempre) vs years_override explicito")
    base = build_model(mine_system, time_series)
    override = build_model(mine_system, time_series, years_override=years)

    nv_b, nc_b = size(base)
    nv_o, nc_o = size(override)
    print(f"       defaults        : {nv_b:,} variables / {nc_b:,} restricciones")
    print(f"       years_override  : {nv_o:,} variables / {nc_o:,} restricciones")
    check(nv_b == nv_o, "mismo numero de variables", f"{nv_b:,} vs {nv_o:,}")

    # years_override marca is_decomposed_block, que saca las 4 acumulaciones
    # stock=stock[y-1]+Delta y b_y_link: esas las arma el bloque anual con la
    # copia local. Es la UNICA diferencia esperada.
    n_stations = len(mine_system.get_system_stations())
    esperadas = 4 * n_stations * len(years) + max(0, len(years) - 1)
    print(f"       diferencia esperada (4 link_*_stock + b_y_link): {esperadas:,}")
    check(nc_b - nc_o == esperadas,
          "la unica diferencia en restricciones son los enlaces interanuales",
          f"{nc_b - nc_o:,} vs {esperadas:,} esperadas")

    print("\n[2] objetivo evaluado en el mismo punto")
    v_b = obj_at_ones(base)
    v_o = obj_at_ones(override)
    print(f"       defaults={v_b:,.6f}   years_override={v_o:,.6f}")
    check(abs(v_b - v_o) <= 1e-9 * max(1.0, abs(v_b)),
          "la expresion de costo es la misma")

    print("\n[3] X exogeno saca Delta_X y su enlace, sin tocar el resto")
    exo = {(k, y): 1 for k in mine_system.get_system_stations() for y in years}
    exogenous = build_model_exogenous(mine_system, time_series, years, exo)
    nv_e, nc_e = size(exogenous)
    print(f"       X exogeno       : {nv_e:,} variables / {nc_e:,} restricciones")
    check(nv_b - nv_e == 2 * n_stations * len(years),
          "desaparecen exactamente X y Delta_X",
          f"{nv_b - nv_e:,} vs {2 * n_stations * len(years):,}")
    check(not hasattr(exogenous, "link_station_stock"),
          "no queda link_station_stock cuando X es exogeno")
    check(isinstance(exogenous.X, pyo.Param), "X quedo como Param")

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLAS: {FAILS}"))
    return 1 if FAILS else 0


def build_model_exogenous(mine_system, time_series, years, exo):
    model = pyo.ConcreteModel()
    kwargs = {"years_override": years, "exogenous_stations": exo}
    OptSets(mine_system, time_series, **kwargs).build_sets(model)
    OptParameters(mine_system, time_series, **kwargs).build_parameters(model)
    BoundRules(mine_system, time_series, **kwargs).build_all_variables(model)
    ConstraintRules(mine_system, time_series, **kwargs).build_all_constraints(model)
    obj_rules = ObjectiveRules(mine_system, time_series, **kwargs)
    model.obj = pyo.Objective(rule=obj_rules.total_cost, sense=pyo.minimize)
    return model


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    ny = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    raise SystemExit(main(folder, ny))
