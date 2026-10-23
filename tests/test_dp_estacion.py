"""Tests de dp_estacion.py (Fase 1 de implementacion_dp_estacion_swap.md).

    .venv_elmo/Scripts/python.exe -m pytest tests/test_dp_estacion.py -q
"""
import itertools
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dp_estacion import (INF, construir_arcos, construir_estados, dp_evaluar,  # noqa: E402
                         transicion)


def _costo_por_bateria(c, NC):
    """costo_arco[t][Q] = Q * c[t] (cada bateria cargando cuesta c[t])."""
    c = np.asarray(c, dtype=float)
    return c[:, None] * np.arange(NC + 1)[None, :]


def test_ejemplo_minimo():
    # N=1, NC=1, L=2, T=4, swap en t=1 (indice 0), costos [5,1,1,5], la bateria
    # empieza y termina cargada: lo optimo es esperar y cargar en t=2 y t=3.
    c = _costo_por_bateria([5, 1, 1, 5], 1)
    res = dp_evaluar([1, 0, 0, 0], N=1, NC=1, L=2, T=4, costo_arco=c, s0=(1, 0))
    assert res["factible"]
    assert res["costo"] == 2
    assert res["X_ini"] == [0, 1, 0, 0]
    assert res["Q"] == [0, 1, 1, 0]


def test_infactible_por_tiempo_de_carga():
    # N=1, L=13: dos swaps separados por menos de 13 intervalos no se pueden servir.
    T = 30
    W = [0] * T
    W[2], W[10] = 1, 1
    c = _costo_por_bateria([1] * T, 1)
    res = dp_evaluar(W, N=1, NC=1, L=13, T=T, costo_arco=c)
    assert not res["factible"]
    # separados por 13 o mas si es factible (la carga de 2..14 deja la bateria en 15)
    W = [0] * T
    W[2], W[15] = 1, 1
    res = dp_evaluar(W, N=1, NC=1, L=13, T=T, costo_arco=c)
    assert res["factible"]


def test_capacidad_de_cargadores():
    # N=2, NC=1: dos swaps simultaneos; las cargas no se pueden solapar.
    T, L = 12, 3
    W = [0] * T
    W[1] = 2
    c = _costo_por_bateria([1] * T, 1)
    res = dp_evaluar(W, N=2, NC=1, L=L, T=T, costo_arco=c)
    assert res["factible"]
    assert max(res["Q"]) <= 1
    assert sum(res["X_ini"]) == 2          # se recargan las dos, una tras otra
    # con NC=2 pueden cargar a la vez
    c2 = _costo_por_bateria([1] * T, 2)
    res2 = dp_evaluar(W, N=2, NC=2, L=L, T=T, costo_arco=c2)
    assert res2["factible"] and res2["costo"] == res["costo"]


def test_ciclicidad_completa():
    # Un swap al final del dia con la bateria que se necesita al inicio: en modo
    # "modelo" (pipeline vacio y nada cargando en tf) es infactible; en modo
    # "completo" la carga cruza la medianoche y termina al inicio del dia.
    T, L = 10, 4
    W = [0] * T
    W[3] = 1        # swap temprano: usa la bateria que se cargo cruzando la medianoche
    W[8] = 1        # swap al final: la descargada solo alcanza a cargar cruzando el dia
    # (carga 8, 9, 0, 1 -> disponible en 2 del dia siguiente, a tiempo para el swap de 3)
    c = _costo_por_bateria([1] * T, 1)
    modelo = dp_evaluar(W, N=1, NC=1, L=L, T=T, costo_arco=c, ciclo="modelo")
    assert not modelo["factible"]
    completo = dp_evaluar(W, N=1, NC=1, L=L, T=T, costo_arco=c, ciclo="completo")
    assert completo["factible"]
    assert completo["Q"][9] == 1 and completo["Q"][0] == 1    # carga en 8..11 = 8, 9, 0, 1


def test_estados_y_arcos():
    estados, idx = construir_estados(N=2, NC=2, L=13)
    assert all(s[0] + sum(s[1:]) <= 2 and sum(s[1:]) <= 2 for s in estados)
    assert len(estados) == len(idx)
    arcos = construir_arcos(estados, N=2, NC=2, L=13, w_max=4)
    for i, j, w, x, Q in arcos:
        s2, Q2 = transicion(estados[i], w, x, 2, 2, 13)
        assert idx[s2] == j and Q == Q2 <= 2


def _fuerza_bruta(W, N, NC, L, T, costo, ciclo):
    estados, _ = construir_estados(N, NC, L)
    vacio = lambda s: all(v == 0 for v in s[1:])
    iniciales = [s for s in estados if vacio(s)] if ciclo == "modelo" else estados
    mejor = INF
    for s_ini in iniciales:
        for xs in itertools.product(range(NC + 1), repeat=T):
            s, total, ok = s_ini, 0.0, True
            for t in range(T):
                tr = transicion(s, W[t], xs[t], N, NC, L)
                if tr is None:
                    ok = False
                    break
                s, Q = tr
                if ciclo == "modelo" and t == T - 1 and Q > 0:
                    ok = False
                    break
                total += costo[t, Q]
            if ok and s == s_ini:
                mejor = min(mejor, total)
    return mejor


def test_contra_fuerza_bruta():
    rnd = random.Random(7)
    for caso in range(40):
        N = rnd.choice([1, 2])
        NC = rnd.choice([1, N])
        L = rnd.choice([1, 2, 3])
        T = rnd.choice([5, 6, 7, 8])
        W = [rnd.choice([0, 0, 0, 1]) if t > 0 else 0 for t in range(T)]
        costo = np.asarray([[q * rnd.uniform(0.5, 5) for q in range(NC + 1)] for _ in range(T)])
        for ciclo in ("modelo", "completo"):
            fb = _fuerza_bruta(W, N, NC, L, T, costo, ciclo)
            res = dp_evaluar(W, N=N, NC=NC, L=L, T=T, costo_arco=costo, ciclo=ciclo)
            if fb == INF:
                assert not res["factible"], (caso, ciclo, W)
            else:
                assert res["factible"], (caso, ciclo, W)
                assert abs(res["costo"] - fb) < 1e-9, (caso, ciclo, res["costo"], fb)
