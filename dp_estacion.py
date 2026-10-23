"""Estacion de battery swapping como programacion dinamica (Fase 1 de
implementacion_dp_estacion_swap.md).

La estacion de una nave (inventario de baterias + cargadores, ecs. 3.60-3.68,
3.13 y 3.71 de la tesis) es un inventario con tiempo de carga fijo L = t_charge
(Schneider, Thonemann y Klabjan 2017). Con los swaps W[t] dados, se resuelve
exacto por DP sobre un grafo de estados chico.

CONVENCION (la misma del codigo, ver notas_convencion_estacion.md):
  Estado al inicio de t: s = (S, r_1, ..., r_{L-1})
    S    baterias cargadas disponibles
    r_n  baterias cargando a las que les faltan exactamente n intervalos,
         contando t (r_n esta en su ultimo intervalo cuando n = 1)
    D = N - S - sum(r)  descargadas
  Orden de eventos en t:
    1. swaps w_t <= S:  S -= w_t, D += w_t (la que llega puede cargarse en t)
    2. inicio de cargas x_t <= D
    3. cargando en t: Q_t = sum(r) + x_t <= N_C  (y p_charger*Q_t <= P_ssee)
    4. transicion: S' = S - w_t + r_1; r'_n = r_{n+1}; r'_{L-1} = x_t
  Una carga iniciada en a ocupa cargador y consume en a, ..., a+L-1 y queda
  disponible en a+L. Con L = 1 el pipeline es vacio y la bateria iniciada en t
  queda disponible en t+1.

BORDE DEL DIA:
  ciclo="modelo"   replica el MILP actual: el dia empieza y termina con el
                   pipeline vacio (r = 0), S al final = S al inicio, y no puede
                   haber baterias cargando en el ultimo intervalo (Q_T = 0: la
                   conservacion S + X_dch + sum Sv = N junto con CI_S, CI_X_dch
                   y CB_general lo implican).
  ciclo="completo" circulacion: el estado al final del dia es el del inicio,
                   incluido el pipeline (una carga puede cruzar la medianoche).

    from dp_estacion import dp_evaluar
    res = dp_evaluar(W, N=2, NC=1, L=6, T=180, costo_arco=c)
"""
import itertools
import math

import numpy as np

INF = float("inf")


def construir_estados(N, NC, L):
    """Enumera los estados (S, r_1..r_{L-1}) con S + sum(r) <= N y sum(r) <= NC.

    Devuelve (lista de estados, dict estado -> id)."""
    estados = []
    for r in itertools.product(range(NC + 1), repeat=max(L - 1, 0)):
        sr = sum(r)
        if sr > NC or sr > N:
            continue
        for S in range(N - sr + 1):
            estados.append((S,) + tuple(r))
    idx = {s: i for i, s in enumerate(estados)}
    return estados, idx


def transicion(s, w, x, N, NC, L):
    """Aplica (w, x) al estado s. Devuelve (s_siguiente, Q) o None si no es factible."""
    S, r = s[0], s[1:]
    if w < 0 or x < 0 or w > S:
        return None
    D = N - S - sum(r) + w                     # descargadas tras los swaps de t
    if x > D:
        return None
    Q = sum(r) + x
    if Q > NC:
        return None
    if L == 1:
        return (S - w + x,), Q
    r1 = r[0] if r else 0
    nuevo_r = tuple(r[1:]) + (x,)
    return (S - w + r1,) + nuevo_r, Q


def construir_arcos(estados, N, NC, L, w_max):
    """Para cada estado y cada (w, x) factible: (id_origen, id_destino, w, x, Q).

    La estructura no depende de t; los filtros por t (ventanas de swap, Q
    maximo por subestacion o pico) se aplican aparte."""
    idx = {s: i for i, s in enumerate(estados)}
    arcos = []
    for i, s in enumerate(estados):
        for w in range(min(s[0], w_max) + 1):
            for x in range(NC + 1):
                tr = transicion(s, w, x, N, NC, L)
                if tr is None:
                    continue
                s2, Q = tr
                arcos.append((i, idx[s2], w, x, Q))
    return arcos


def dp_evaluar(W, N, NC, L, T, costo_arco, permitido_carga=None, q_max=None,
               ciclo="modelo", s0=None):
    """DP exacta de la estacion con los swaps W[t] FIJOS (t = 0..T-1).

    costo_arco[t][Q]: costo del intervalo t con Q baterias cargando (array
      (T, NC+1); np.inf para prohibir).
    permitido_carga[t]: si se pueden iniciar cargas en t (por defecto todos).
    q_max[t]: tope de baterias cargando en t (subestacion/pico), por defecto NC.
    ciclo: "modelo" o "completo" (ver docstring del modulo).
    s0: estado inicial fijo (tupla); si es None se prueban todos los permitidos.

    Devuelve dict con costo, factible, S0, X_ini[t], Q[t] y la trayectoria de
    estados (lista de tuplas, T+1 elementos)."""
    W = [int(round(v)) for v in W]
    if len(W) != T:
        raise ValueError(f"W tiene {len(W)} intervalos y T = {T}")
    costo_arco = np.asarray(costo_arco, dtype=float)
    if costo_arco.shape != (T, NC + 1):
        raise ValueError(f"costo_arco debe ser ({T}, {NC + 1}), es {costo_arco.shape}")
    if permitido_carga is None:
        permitido_carga = [True] * T
    if q_max is None:
        q_max = [NC] * T

    estados, idx = construir_estados(N, NC, L)
    ns = len(estados)
    vacio = lambda s: all(v == 0 for v in s[1:])
    if s0 is not None:
        iniciales = [tuple(s0)]
    elif ciclo == "modelo":
        iniciales = [s for s in estados if vacio(s)]
    else:
        iniciales = list(estados)

    # Transiciones por (estado, x) con el w del intervalo; se reusan para
    # todos los estados iniciales.
    mejor = None
    for s_ini in iniciales:
        fin = idx[s_ini]
        V = np.full((T + 1, ns), INF)
        V[T, fin] = 0.0
        acc = np.full((T, ns), -1, dtype=np.int64)
        nxt = np.full((T, ns), -1, dtype=np.int64)
        for t in range(T - 1, -1, -1):
            w = W[t]
            ultimo = (t == T - 1)
            for i, s in enumerate(estados):
                mejor_v, mejor_x, mejor_j = INF, -1, -1
                for x in range(NC + 1):
                    if x > 0 and not permitido_carga[t]:
                        break
                    tr = transicion(s, w, x, N, NC, L)
                    if tr is None:
                        continue
                    s2, Q = tr
                    if Q > q_max[t]:
                        continue
                    if ciclo == "modelo" and ultimo and Q > 0:
                        continue              # nada cargando en tf (ver docstring)
                    c = costo_arco[t, Q]
                    j = idx[s2]
                    v = c + V[t + 1, j]
                    if v < mejor_v:
                        mejor_v, mejor_x, mejor_j = v, x, j
                V[t, i] = mejor_v
                acc[t, i] = mejor_x
                nxt[t, i] = mejor_j
        v0 = V[0, fin]
        if v0 < INF and (mejor is None or v0 < mejor[0] - 1e-12):
            # reconstruir
            tray, X, Qs = [s_ini], [], []
            i = fin
            for t in range(T):
                x = int(acc[t, i])
                j = int(nxt[t, i])
                X.append(x)
                Qs.append(sum(estados[i][1:]) + x)
                i = j
                tray.append(estados[i])
            mejor = (v0, s_ini, X, Qs, tray)
    if mejor is None:
        return {"factible": False, "costo": INF, "S0": None, "X_ini": None, "Q": None,
                "trayectoria": None, "n_estados": ns}
    v0, s_ini, X, Qs, tray = mejor
    return {"factible": True, "costo": float(v0), "S0": s_ini, "X_ini": X, "Q": Qs,
            "trayectoria": tray, "n_estados": ns}


def costo_energia(c_elec, A, p_charger, delta_t, omega, NC):
    """costo_arco[t][Q] = omega * c_t * delta_t * max(0, p_charger*Q - A_t)
    (modo arc_cost: una nave, sin BESS)."""
    c_elec = np.asarray(c_elec, dtype=float)
    A = np.asarray(A, dtype=float) if A is not None else np.zeros_like(c_elec)
    Q = np.arange(NC + 1)[None, :]
    return omega * c_elec[:, None] * delta_t * np.maximum(0.0, p_charger * Q - A[:, None])
