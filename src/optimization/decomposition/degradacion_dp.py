"""Programacion dinamica de la degradacion del pool de baterias de swap: el
costo minimo en baterias, bahias, cargadores y reemplazos para terminar el
horizonte desde un estado dado (2026-10-26).

ESTADO al final del año y: (p, nb, D) con p = baterias en estacion (pool),
nb = bahias y D = capacidad por bateria (kWh). DECISION en el año y+1: comprar
k baterias (con las bahias que hagan falta para guardarlas y los cargadores que
exijan esas bahias) y reemplazar o no la flota completa (R). Mismas ecuaciones
que el modelo (functions.py):

    flota      n = p + n_active_slhd[y]
    desgaste   D = b - gamma * N_ciclos,  N_ciclos = sf * E[y] / (b * n)
               o sea  D = b - c/b  con  c = gamma * sf * E[y] / n
    herencia   b[y+1] = D[y] sin reemplazo, b_max con reemplazo, b[1] = b_max
    piso       D[y] >= B_L (+ margen) en todo año
    estacion   p <= max_batteries_per_bay * nb,  nb <= N_chargers
    costos     bateria (battery_cost + c_battery_space), bahia (c_bays +
               c_crane), cargador (charger_cost + c_charger_space), reemplazo
               c_bat_replace * n; descontados 1/(1+r)^pos

E[y] (energia consumida en los dias representativos) la fija la produccion:
energia_nominal() la calcula del plan de produccion (visitas = meta / carga por
visita, sin redondear). En el caso 2 v4 queda 0,3-2 % sobre la de las
soluciones en los años de mas carga, o sea un poco conservadora.

V(y, p, nb, D) es escalonada y no creciente en D: cada plan futuro es factible
desde cierta capacidad minima (umbral) y cuesta algo fijo. Por eso la DP guarda,
por estado discreto, el FRENTE de Pareto de pares (umbral, costo) y no necesita
grilla en D. El umbral sale cerrado: para terminar el año con al menos thr,
b - c/b >= thr  <=>  b >= (thr + sqrt(thr^2 + 4c)) / 2.

Solo ve la degradacion: no sabe cuanto ayuda una bateria extra a la operacion.
Por eso, para reparar una trayectoria (reparar_plan), sus baterias, bahias y
cargadores entran como COTAS INFERIORES por año: la reparacion solo agrega o
adelanta compras, nunca quita lo que la trayectoria decidio por la operacion.
"""
import math

from src.optimization.functions import tope_estacion


def parametros_desde(ms, ts, tope=None):
    """Parametros de la DP desde el sistema minero y la serie de tiempo (los
    mismos objetos que recibe OptModel)."""
    bd = ms.battery_degradation
    if bd is None:
        raise ValueError("el caso no tiene hoja BatteryDegradation")
    years = sorted(ts.years)
    swap = set(ms.get_swap_lhds())
    na = ts.mapper["NodeAssignment"]
    n_lhd = {y: len(set(na.loc[na["year"] == y, "elhd_name"]) & swap) for y in years}
    b_max = {round(float(ms.elhd.get_e_max(b)), 6) for b in swap}
    if len(b_max) != 1:
        raise ValueError(f"capacidades nominales distintas en el pool: {b_max}")
    b_max = b_max.pop()
    est = list(ms.get_system_stations())
    st = ms.stations
    # Varias estaciones: se agregan (capacidad sumada, costos minimos), lo que
    # deja a V como cota inferior de la version por estacion.
    por_nave = min(int(st.get_max_batteries_per_bay(k)) for k in est)
    max_naves = sum(int(st.get_max_bays(k)) for k in est)
    p_max = sum(int(st.get_max_bays(k)) * int(st.get_max_batteries_per_bay(k)) for k in est)
    if tope is None:
        tope = tope_estacion()
    if tope is not None:
        p_max = min(p_max, tope * len(est))
    r = float(bd.get_discount_rate())
    return {
        "years": years,
        "n_lhd": n_lhd,
        "b_max": b_max,
        "B_L": float(bd.get_min_capacity_fraction()) * b_max,
        "gamma": float(bd.get_gamma_coef()),
        "sf": float(ts.scaling_factor_op_cost),
        "c_bat": float(ms.chargers.get_battery_cost()) + min(float(st.get_c_battery_space(k)) for k in est),
        "c_nave": min(float(st.get_c_bays(k)) + float(st.get_c_crane(k)) for k in est),
        "c_carg": float(ms.chargers.get_charger_cost()) + min(float(st.get_c_charger_space(k)) for k in est),
        "c_repl": float(bd.get_c_bat_replace()),
        "r": r,
        "desc": {y: 1.0 / (1.0 + r) ** (i + 1) for i, y in enumerate(years)},
        "por_nave": por_nave,
        "max_naves": max_naves,
        "p_max": p_max,
        "estaciones": est,
    }


def energia_nominal(ms, ts):
    """{año: kWh} de energia consumida por los LHD de swap en los dias
    representativos si cada nodo recibe exactamente meta / carga por visita
    (las visitas de ConstraintRules.production sin redondear), con la energia
    por visita de energy_consumed_def. Toma el primer LHD de swap asignado al
    nodo, como production_visit_bounds."""
    swap = set(ms.get_swap_lhds())
    na = ts.mapper["NodeAssignment"]
    n_dias = len(ts.days_within_year)
    out = {}
    for y in sorted(ts.years):
        tot = 0.0
        for j, g in na[na["year"] == y].groupby("node_name"):
            lhds = [i for i in g["elhd_name"] if i in swap]
            if not lhds:
                continue
            i = lhds[0]
            meta = ts.get_extraction_goal(j, y)
            if meta <= 0:
                continue
            por_visita = (ms.elhd.get_load_capacity(i) * ts.get_n_trips(j, i)
                          * ms.elhd.get_filling_factor(i))
            e_visita = (ts.get_energy_consumption(j, i) * ts.get_n_intervals_trip(j, i)
                        * ts.get_n_trips(j, i))
            tot += meta / por_visita * e_visita
        out[y] = tot * n_dias
    return out


def b_necesaria(thr, c):
    """Minima capacidad de inicio de año para terminarlo con al menos thr."""
    return (thr + math.sqrt(thr * thr + 4.0 * c)) / 2.0


def _podar(entradas):
    """Frente de Pareto: menor umbral y menor costo. Ordenado por umbral, cada
    entrada que sobrevive es mas barata que todas las de umbral menor."""
    entradas.sort(key=lambda e: (e[0], e[1]))
    frente, mejor = [], math.inf
    for e in entradas:
        if e[1] < mejor - 1e-6:
            frente.append(e)
            mejor = e[1]
    return frente


class DPDegradacion:
    """pool_lb / naves_lb / carg_lb: {año: minimo} (los de una trayectoria a
    reparar); margen: kWh sobre el piso B_L exigidos en todo año."""

    def __init__(self, P, E, pool_min=1, permitir_reemplazo=True,
                 pool_lb=None, naves_lb=None, carg_lb=None, margen=0.0):
        self.P, self.E = P, E
        self.pool_min = pool_min
        self.permitir_reemplazo = permitir_reemplazo
        self.pool_lb = pool_lb or {}
        self.naves_lb = naves_lb or {}
        self.carg_lb = carg_lb or {}
        self.piso = P["B_L"] + margen
        self.frentes = {}
        self._resolver()

    def _c(self, y, n):
        return self.P["gamma"] * self.P["sf"] * self.E[y] / n

    def _naves_para(self, p):
        return -(-p // self.P["por_nave"])

    def _cargadores(self, y, nb):
        """Cargadores en el año y con nb bahias: los de la trayectoria o, si
        las bahias los superan, uno por bahia (bays_le_chargers)."""
        return max(int(self.carg_lb.get(y, 0)), nb) if y in self.E else 0

    def _resolver(self):
        P = self.P
        years = P["years"]
        Y = years[-1]
        estados = [(p, nb) for p in range(P["p_max"] + 1) for nb in range(P["max_naves"] + 1)]
        # f = 1 si el plan desde aca incluye al menos un reemplazo.
        self.frentes[Y] = {(p, nb, 0): [(self.piso, 0.0, ())] for p, nb in estados}
        previos = [years[0] - 1] + years[:-1]
        for y in reversed(previos):
            yn = y + 1
            sig = self.frentes[yn]
            p_lb = max(self.pool_min, int(self.pool_lb.get(yn, 0)))
            nb_lb = int(self.naves_lb.get(yn, 0))
            acum = {}
            for p, nb in estados:
                carg_prev = self._cargadores(y, nb)
                for k in range(P["p_max"] - p + 1):
                    p2 = p + k
                    if p2 < p_lb:
                        continue
                    nb2 = max(nb, self._naves_para(p2), nb_lb)
                    if nb2 > P["max_naves"]:
                        continue
                    n2 = p2 + P["n_lhd"][yn]
                    if n2 <= 0:
                        continue
                    c = self._c(yn, n2)
                    dcarg = max(0, self._cargadores(yn, nb2) - carg_prev)
                    compra = (k * P["c_bat"] + (nb2 - nb) * P["c_nave"]
                              + dcarg * P["c_carg"]) * P["desc"][yn]
                    opciones_R = (0, 1) if (self.permitir_reemplazo and yn != years[0]) else (0,)
                    for R in opciones_R:
                        for f2 in (0, 1):
                            for thr2, costo2, plan2 in sig.get((p2, nb2, f2), ()):
                                if R:
                                    b = P["b_max"]
                                    if b - c / b < thr2 - 1e-9:
                                        continue
                                    thr = self.piso
                                    costo = costo2 + compra + P["c_repl"] * n2 * P["desc"][yn]
                                else:
                                    b = b_necesaria(thr2, c)
                                    if b > P["b_max"] + 1e-9:
                                        continue
                                    thr = max(b, self.piso)
                                    costo = costo2 + compra
                                paso = (yn, k, nb2 - nb, R)
                                plan = ((paso,) if (k or nb2 > nb or R) else ()) + plan2
                                acum.setdefault((p, nb, max(f2, R)), []).append((thr, costo, plan))
            self.frentes[y] = {s: _podar(v) for s, v in acum.items()}

    def V(self, y, p, nb, D, modo="libre"):
        """(costo, plan) minimo desde el final del año y con (p, nb, D). modo:
        libre / sin (sin reemplazos) / con (al menos un reemplazo)."""
        fs = {"libre": (0, 1), "sin": (0,), "con": (1,)}[modo]
        mejor = (math.inf, None)
        for f in fs:
            for thr, costo, plan in self.frentes[y].get((p, nb, f), ()):
                if thr <= D + 1e-9 and costo < mejor[0]:
                    mejor = (costo, plan)
        return mejor

    def escalones(self, y, p, nb, modo="libre"):
        fs = {"libre": (0, 1), "sin": (0,), "con": (1,)}[modo]
        return _podar([e for f in fs for e in self.frentes[y].get((p, nb, f), ())])

    def por_año(self, plan):
        """{año: (pool, naves, cargadores, R)} de un plan que parte de cero."""
        p = nb = 0
        pasos = {yn: (k, dnb, R) for yn, k, dnb, R in plan}
        out = {}
        for y in self.P["years"]:
            k, dnb, R = pasos.get(y, (0, 0, 0))
            p, nb = p + k, nb + dnb
            out[y] = (p, nb, self._cargadores(y, nb), R)
        return out

    def simular(self, pool, R=None):
        """Trayectoria de capacidad de un plan dado ({año: pool}, {año: R})."""
        P = self.P
        R = R or {}
        b, tray = P["b_max"], {}
        for y in P["years"]:
            if R.get(y):
                b = P["b_max"]
            n = pool[y] + P["n_lhd"][y]
            D = b - self._c(y, n) / b
            tray[y] = (b, D)
            b = D
        return tray


def reparar_plan(P, E, pool, naves, carg, margen=0.5, permitir_reemplazo=True):
    """Ajuste mas barato de un plan de estacion ({año: valor} de baterias,
    bahias y cargadores) para que la flota termine el horizonte con al menos
    B_L + margen, agregando o adelantando compras (nunca quitando) y, si
    conviene, reemplazando la flota. Devuelve (costo_dp, {año: (pool, naves,
    cargadores, R)}, D al final del horizonte) o None si no hay plan."""
    dp = DPDegradacion(P, E, pool_min=0, permitir_reemplazo=permitir_reemplazo,
                       pool_lb=pool, naves_lb=naves, carg_lb=carg, margen=margen)
    y0 = P["years"][0] - 1
    costo, plan = dp.V(y0, 0, 0, P["b_max"])
    if plan is None:
        return None
    por_año = dp.por_año(plan)
    tray = dp.simular({y: v[0] for y, v in por_año.items()},
                      {y: v[3] for y, v in por_año.items()})
    return costo, por_año, tray[P["years"][-1]][1]


def fmt_plan(plan):
    if not plan:
        return "nada"
    partes = []
    for yn, k, dnb, R in plan:
        s = []
        if k:
            s.append(f"+{k} bat")
        if dnb:
            s.append(f"+{dnb} nave")
        if R:
            s.append("REEMPLAZO")
        partes.append(f"año {yn}: " + ", ".join(s))
    return "; ".join(partes)
