"""Tests de src/optimization/decomposition/degradacion_dp.py con parametros
sinteticos (no lee datos de ningun caso).

    .venv_elmo/Scripts/python.exe -m pytest tests/test_degradacion_dp.py -q
    .venv_elmo/Scripts/python.exe tests/test_degradacion_dp.py      (sin pytest)
"""
import itertools
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.optimization.decomposition.degradacion_dp import (  # noqa: E402
    DPDegradacion, b_necesaria, reparar_plan)

AÑOS = [1, 2, 3, 4]


def _P(c_repl=50.0):
    """b_max 100, piso 80, 2 LHD, hasta 4 baterias en 2 bahias de 2."""
    return {
        "years": AÑOS, "n_lhd": {y: 2 for y in AÑOS}, "b_max": 100.0, "B_L": 80.0,
        "gamma": 1.0, "sf": 1.0, "c_bat": 10.0, "c_nave": 5.0, "c_carg": 3.0,
        "c_repl": c_repl, "r": 0.0, "desc": {y: 1.0 for y in AÑOS}, "por_nave": 2,
        "max_naves": 2, "p_max": 4, "estaciones": ["s"],
    }


# Energia tal que con la flota minima (2 LHD + 1 bateria) la capacidad cae ~6 kWh
# por año: no llega al año 4 sin comprar o reemplazar.
E = {y: 1800.0 for y in AÑOS}


def _costo_plan(P, plan):
    """Costo de un plan {año: (pool, naves, R)} desde cero, con cargadores = naves."""
    costo, p0, nb0 = 0.0, 0, 0
    for y in AÑOS:
        p, nb, R = plan[y]
        costo += ((p - p0) * P["c_bat"] + (nb - nb0) * (P["c_nave"] + P["c_carg"])
                  + R * P["c_repl"] * (p + P["n_lhd"][y])) * P["desc"][y]
        p0, nb0 = p, nb
    return costo


def _factible(P, plan, piso):
    b = P["b_max"]
    for y in AÑOS:
        p, _nb, R = plan[y]
        if R:
            b = P["b_max"]
        D = b - P["gamma"] * P["sf"] * E[y] / (b * (p + P["n_lhd"][y]))
        if D < piso - 1e-9:
            return False
        b = D
    return True


def test_b_necesaria_es_la_inversa_del_desgaste():
    for thr, c in ((80.0, 500.0), (90.0, 1234.5), (85.3, 1e-3)):
        b = b_necesaria(thr, c)
        assert abs((b - c / b) - thr) < 1e-9


def test_V_coincide_con_enumeracion_exhaustiva():
    """Optimo de la DP desde cero = el mejor de todos los planes enumerados
    (pool no decreciente, naves = las justas, reemplazo en cualquier año > 1)."""
    P = _P()
    dp = DPDegradacion(P, E, pool_min=1)
    costo_dp, plan = dp.V(0, 0, 0, P["b_max"])
    mejor = math.inf
    for pools in itertools.product(range(1, 5), repeat=len(AÑOS)):
        if any(pools[i] > pools[i + 1] for i in range(len(AÑOS) - 1)):
            continue
        for Rs in itertools.product((0, 1), repeat=len(AÑOS) - 1):
            Rs_y = (0,) + Rs              # en el año 1 no se reemplaza
            plan_e = {y: (pools[i], -(-pools[i] // 2), Rs_y[i]) for i, y in enumerate(AÑOS)}
            if _factible(P, plan_e, P["B_L"]):
                mejor = min(mejor, _costo_plan(P, plan_e))
    assert plan is not None
    assert abs(costo_dp - mejor) < 1e-6


def test_V_no_crece_con_la_capacidad():
    P = _P()
    dp = DPDegradacion(P, E, pool_min=1)
    previo = math.inf
    for D in [80 + 0.5 * i for i in range(41)]:
        c, _ = dp.V(1, 1, 1, D)
        assert c <= previo + 1e-9
        previo = c


def test_reparar_solo_agrega_y_deja_margen():
    P = _P(c_repl=1e6)        # reemplazo carisimo: tiene que reparar con baterias
    pool = {y: 1 for y in AÑOS}
    naves = {y: 1 for y in AÑOS}
    carg = {y: 1 for y in AÑOS}
    costo, por_año, D_fin = reparar_plan(P, E, pool, naves, carg, margen=0.5)
    for y in AÑOS:
        p, nb, c, R = por_año[y]
        assert p >= pool[y] and nb >= naves[y] and c >= max(carg[y], nb)
        assert p <= P["por_nave"] * nb
        assert R == 0
    assert D_fin >= P["B_L"] + 0.5 - 1e-9
    assert any(por_año[y][0] > pool[y] for y in AÑOS)


def test_reparar_elige_reemplazo_si_es_barato():
    P = _P(c_repl=0.01)
    pool = {y: 1 for y in AÑOS}
    costo, por_año, D_fin = reparar_plan(P, E, pool, {y: 1 for y in AÑOS},
                                         {y: 1 for y in AÑOS}, margen=0.0)
    assert any(por_año[y][3] for y in AÑOS)
    assert all(por_año[y][0] == 1 for y in AÑOS)


if __name__ == "__main__":
    for nombre, f in list(globals().items()):
        if nombre.startswith("test_") and callable(f):
            f()
            print("ok", nombre)
