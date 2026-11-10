"""Recorte de una incumbente holgada (diagnostico_holgada.py): busqueda local que
baja la inversion paso a paso SIN perder factibilidad.

La inversion de cada estacion es una trayectoria por año de X, N_bays,
N_batteries y N_chargers, mas los modulos de subestacion n_ssee_k. Movimientos,
en este orden y por grupos de estaciones (las simetricas juntas, --grupos):

  escalera   antes de todo, diseños uniformes de nivel L (--escalera; el primero
             factible queda como incumbente)
  construir  bahias, baterias y cargadores en 0 antes del primer año con LHD
             activos de la estacion (X no: las naves solo abren el año 1)
  tope       min(trayectoria, c) bajando c de a uno (bahias: tambien baterias y
             cargadores a <= c * por bahia)
  ssee       n_ssee_k - 1
  punta      P_pot desde 0: los dias de punta la suben pagando su costo, la operacion
             decide si carga en punta (con P_pot fija, los dias no lo ven)
  retrasar   una compra un año mas tarde (equipo: bahias, cargadores y baterias juntas;
             o solo baterias)
  macro      (con --por_estacion) el algoritmo de UN AÑO de la rama battery_swapping
             (run_macrobloques_decomposicion.py) dentro del multiaño: Fase 1, cada
             macrobloque (año, dia, estacion) con su infraestructura LIBRE y su costo de
             compra de ese año; agregacion, el maximo por estacion y año entre sus dias
             (acumulado en los años: las compras no se deshacen) y subido lo justo para
             las filas de capacidad; Fase 2, esa inversion evaluada (todas las estaciones
             juntas y despues cada estacion sola, las demas en la incumbente)

--por_estacion: los bloques de dia se parten en macrobloques por estacion y un bloque
de red (BendersDias, Particion); cambiar una estacion solo re-resuelve sus macrobloques
(4 LHD en vez de 12) y cada estacion produce al menos lo que producia en la incumbente.
Despues de cada movimiento se bajan las otras familias lo justo para cumplir
N_bays <= N_chargers <= N_batteries y N_chargers <= por bahia * N_bays.

Cada candidata: el maestro la completa (theta = 0, presupuestos en la energia de
la incumbente, R libre: elige los reemplazos), se vuelven a resolver SOLO los
dias cuyas copias cambian (MIP start = operacion incumbente; P_pot puede subir
como en LShaped._dias_ub), b_bar se corrige con la energia real y el pulido
flexible da el costo. Se acepta si baja la UB; cada mejora se escribe en JSON.

    python -u recortar_holgada.py --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/1C/RED \\
        --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \\
        --solucion <holgada>/solucion --grupos "station_1|station_2,station_3" --horas 12 --out <carpeta>
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from pyomo.environ import value                                          # noqa: E402

from diagnostico_holgada import familia, maximos                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.decomposition.lshaped import LShaped               # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

FAM = ("X", "N_bays", "N_batteries", "N_chargers")
FAM_N = ("N_bays", "N_batteries", "N_chargers")


class Recorte:

    def __init__(self, a):
        self.a = a
        self.t0 = time.time()
        ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                       consumption_model=a.consumption_model, wp2_consumption_json=None,
                       n_years=a.n_years, days_per_year=a.days_per_year)
        _s, self.ms, self.ts = build_mine(ns)
        self.om = OptModel(self.ms, self.ts, os.path.join(a.out, "_monolitico"),
                           init_solution_folder=a.solucion, mccormick_degradation=True,
                           free_charging=True, free_maintenance=True, pause_scheme=a.pause_scheme)
        info, self.por_var = gb.export(self.om.model, os.path.join(a.out, "_mps"), mode="year_day",
                                       split=False)
        self.bd = BendersDias(info, self.om.model, self.por_var, M=1e6, jobs=a.jobs, alpha=0.5,
                              cotas_capacidad={}, out=a.out, cpu=a.cpu, por_estacion=a.por_estacion)
        self.P = P = self.bd.P
        self.ls = LShaped(self.bd, self.om, self.por_var, out=a.out)
        self.tope = maximos(self.om.model, a.p_ssee_max)
        self.years = sorted({y for j in P.maestro_vars for f, _k, y in [familia(P.nombre[j])]
                             if f == "N_bays"})
        self.col, self.col_ssee = {}, {}
        for j in P.maestro_vars:
            f, k, y = familia(P.nombre[j])
            if f in FAM and k is not None:
                self.col[(f, k, y)] = j
            elif f == "n_ssee_k":
                self.col_ssee[k] = j
        self.cols_fijas = list(self.col.values()) + list(self.col_ssee.values())
        self.activos = self._lhd_activos()
        # Incumbente: la solucion JSON cargada en el OptModel, pulida. Sin --solucion
        # se parte de cero (UB infinita) y la primera escalera factible es la incumbente;
        # P_pot arranca en p_peak para que los dias de punta no fallen por la potencia.
        if a.solucion:
            vec0 = np.zeros(P.nv)
            for j, n in enumerate(P.nombre):
                vd = self.por_var.get(n)
                v = value(vd, exception=False) if vd is not None else None
                vec0[j] = 0.0 if v is None else v
            costo, completo = self.ls.pulir_flexible(vec0)
            if costo is None:
                raise SystemExit("la solucion dada no pasa el pulido")
            self.UB, self.vec = costo, completo
            self._starts()
        else:
            costo = float("inf")
            self.UB, self.vec = costo, np.zeros(P.nv)
            p_peak = float(value(self.om.model.p_peak))
            for j in P.maestro_vars:
                if P.nombre[j].startswith("P_pot("):
                    self.vec[j] = p_peak
        self.historia = [{"t": self._t(), "mov": "inicial", "ub": costo}]
        self.log(f"incumbente {costo:,.2f}; años {self.years[0]}-{self.years[-1]}; "
                 f"LHD activos por estacion {self.activos}")

    # ------------------------------------------------------------ util -- #
    def _t(self):
        return time.time() - self.t0

    def log(self, s):
        print(f"[REC] {s} ({self._t():.0f}s)", flush=True)

    def _lhd_activos(self):
        """{estacion: {año: LHD activos}} con StationAssignment y NodeAssignment."""
        import pandas as pd
        out = {k: {} for k in self.tope}
        try:
            ts = os.path.join(self.a.data_folder, "time_series.xlsx")
            sa = pd.read_excel(ts, "StationAssignment")
            na = pd.read_excel(ts, "NodeAssignment")
            cols = [c for c in na.columns if str(c).isdigit()]
            act = na.groupby("elhd_name")[cols].max()
            for _, r in sa.iterrows():
                for c in cols:
                    d = out.setdefault(r["station_name"], {})
                    d[int(c)] = d.get(int(c), 0) + int(act.loc[r["elh_name"], c])
        except Exception as e:                       # noqa: BLE001
            self.log(f"sin LHD activos por estacion ({e}); el movimiento construir no se usa")
        return out

    def _starts(self):
        for s in self.bd.subs:
            s.start = ([round(self.vec[s.loc[k]]) for k in s.ent_loc]
                       if self.UB < float("inf") else None)
        P = self.P
        if P.por_estacion:
            # Cada estacion produce al menos lo que producia en la incumbente: las que
            # no se re-resuelven quedan con su operacion y la suma cumple la meta.
            P.piso = ({k: float(P.energia(self.vec, k)) for k in P.piso_natural}
                      if self.UB < float("inf") else dict(P.piso_natural))

    def trayectorias(self, vec=None):
        v = self.vec if vec is None else vec
        T = {(f, k): [int(round(v[self.col[(f, k, y)]])) for y in self.years]
             for f in FAM for k in self.tope}
        S = {k: int(round(v[j])) for k, j in self.col_ssee.items()}
        return T, S

    def inversion(self, vec):
        """Costo de inversion de vec con los coeficientes del maestro."""
        P = self.P
        return float(sum(P.obj[j] * vec[j] for j in P.maestro_vars))

    # ----------------------------------------------------- evaluacion -- #
    def maestro(self, T, S, vec_energia):
        """Inversion completa del maestro con T y S fijos, la energia de vec_energia en los
        presupuestos y theta = 0 (elige R, Delta_*, totales y b_bar). None si no hay.
        vec_energia None (sin incumbente): presupuestos libres -- quedan en la energia
        minima del presolve (EnergyConsumed tiene cota inferior; en 0 es infactible) -- y
        una segunda pasada con la inversion fija maximiza b_bar."""
        P, M = self.P, self.bd.maestro
        M._entero(True)
        m = M.m
        guard = []

        def fijar(v, val):
            guard.append((v, v.LB, v.UB))
            v.LB = v.UB = val
        for (f, k), tr in T.items():
            for y, val in zip(self.years, tr):
                fijar(M.x[M.pos[self.col[(f, k, y)]]], float(val))
        for k, val in S.items():
            fijar(M.x[M.pos[self.col_ssee[k]]], float(val))
        if vec_energia is not None:
            for kk in P.pres_energia:
                fijar(M.e[kk], -abs(float(P.energia(vec_energia, kk))))
        for v in M.th:
            fijar(v, 0.0)
        m.update()
        m.Params.TimeLimit = 300
        m.optimize()
        x = M.punto()[0] if m.SolCount else None
        if x is not None and vec_energia is None:
            import gurobipy as gp
            for k_ in M.ent:
                fijar(M.x[k_], float(round(x[P.maestro_vars[k_]])))
            obj = m.getObjective()
            cbb = self.ls._cols("b_bar")
            m.setObjective(-gp.quicksum(M.x[M.pos[j]] for j in cbb.values()))
            m.optimize()
            if m.SolCount:
                x = M.punto()[0]
            m.setObjective(obj)
        for v, lo, hi in guard:
            v.LB, v.UB = lo, hi
        m.update()
        return x

    def evaluar(self, T, S, etiqueta, punta=False):
        """Costo de la inversion (T, S) o None; si mejora, la deja como incumbente.
        punta: P_pot parte en 0 y los dias la suben pagando su costo (ls._subir), asi la
        operacion decide si carga en punta; sin punta, P_pot queda en la de la incumbente."""
        a, P, bd, ls = self.a, self.P, self.bd, self.ls
        t0 = time.time()
        x = self.maestro(T, S, self.vec if self.UB < float("inf") else None)
        if x is None:
            self.log(f"   {etiqueta}: el maestro no la completa (degradacion o filas de capacidad)")
            return None
        xx = self.vec.copy()
        xx[P.maestro_vars] = x[P.maestro_vars]
        for j in P.maestro_vars:                       # P_pot de la incumbente (o 0)
            if P.nombre[j].startswith("P_pot("):
                xx[j] = 0.0 if punta else self.vec[j]
        # b_bar no tiene costo en el maestro: queda en cualquier valor factible. Se
        # parte de la capacidad maxima que deja la degradacion con la energia de la
        # incumbente (los dias con b_bar bajo son mas dificiles); el lazo la corrige.
        cbb = ls._cols("b_bar")
        real0 = ls._b_bar_real(xx, self.vec) if self.UB < float("inf") else None
        if real0 is not None:
            for y, r in real0.items():
                xx[cbb[y]] = r - 0.1
        vec = xx.copy()
        hechos = {}
        for vuelta in range(3):
            afect = [b for b, s in enumerate(bd.subs)
                     if b not in hechos or any(abs(xx[j] - hechos[b]["cop"][kk]) > 1e-6
                                               for kk, j in enumerate(s.cop))]
            afect = [b for b in afect
                     if b in hechos or any(abs(xx[j] - self.vec[j]) > 1e-6 for j in bd.subs[b].cop)]
            if not afect:
                break
            if punta:
                res = bd._paralelo(lambda b: bd.subs[b].ub(xx, a.t, a.gap, subir=ls._subir(b)), afect)
                for b, r in zip(afect, res):
                    if r["ok"]:
                        for kk in ls._subir(b):
                            j = bd.subs[b].cop[kk]
                            xx[j] = max(xx[j], r["cop"][kk])
            else:
                res = bd._paralelo(lambda b: bd.subs[b].ub(xx, a.t, a.gap), afect)
            malos = [b for b, r in zip(afect, res) if not r["ok"]]
            if malos:
                otra = bd._paralelo(lambda b: bd.subs[b].ub(xx, 2 * a.t, a.gap, subir=ls._subir(b)), malos)
                for b, r in zip(malos, otra):
                    res[afect.index(b)] = r
                siguen = [b for b in malos if not res[afect.index(b)]["ok"]]
                if siguen:
                    self.log(f"   {etiqueta}: sin operacion en " + ", ".join(P.claves[b] for b in siguen[:6])
                             + f" ({time.time() - t0:.0f}s)")
                    self._starts()
                    return None
                for b in malos:
                    r = res[afect.index(b)]
                    for kk in ls._subir(b):
                        j = bd.subs[b].cop[kk]
                        xx[j] = max(xx[j], r["cop"][kk])
            for b, r in zip(afect, res):
                vec[bd.subs[b].loc] = r["x"]
                hechos[b] = {"cop": [float(xx[j]) for j in bd.subs[b].cop]}
            vec[P.maestro_vars] = xx[P.maestro_vars]
            real = ls._b_bar_real(xx, vec)
            if real is None:
                x2 = self.maestro(T, S, vec)           # reemplazos con la energia real
                if x2 is None:
                    self.log(f"   {etiqueta}: la degradacion no cierra con la energia real")
                    self._starts()
                    return None
                for j in P.maestro_vars:
                    if not P.nombre[j].startswith("P_pot("):
                        xx[j] = x2[j]
                continue
            cortos = {y: r for y, r in real.items() if r < xx[cbb[y]] - 1e-6}
            if not cortos:
                break
            for y, r in cortos.items():
                xx[cbb[y]] = r - 0.1
        vec[P.maestro_vars] = xx[P.maestro_vars]
        costo, completo = ls.pulir_flexible(vec)
        dt = time.time() - t0
        n_af = len(hechos)
        if costo is None:
            self.log(f"   {etiqueta}: pulido infactible ({n_af} dias, {dt:.0f}s)")
            self._starts()
            return None
        mejora = self.UB - costo
        self.log(f"   {etiqueta}: {costo:,.2f} ({'-' if mejora > 0 else '+'}{abs(mejora):,.2f}; "
                 f"{n_af} dias, {dt:.0f}s)")
        if costo < self.UB * (1 - a.tol):          # vale tambien con UB infinita
            self.UB, self.vec = costo, completo
            self._starts()
            self.historia.append({"t": self._t(), "mov": etiqueta, "ub": costo})
            self.escribir()
            return costo
        self._starts()
        return None

    def escribir(self):
        from src.io.printer import Printer
        for j, n in enumerate(self.P.nombre):
            vd = self.por_var.get(n)
            if vd is not None and not vd.fixed:
                vd.set_value(round(self.vec[j]) if not vd.is_continuous() else self.vec[j],
                             skip_validation=True)
        destino = os.path.join(self.a.out, "solucion")
        Printer(self.om, destino, self.ts, self.ms).write_variables_jsons()
        T, S = self.trayectorias()
        with open(os.path.join(self.a.out, "resumen.json"), "w", encoding="utf-8") as f:
            json.dump({"ub": self.UB, "historia": self.historia,
                       "inversion": {f"{fam}({k})": tr for (fam, k), tr in T.items()},
                       "n_ssee_k": S, "args": vars(self.a)}, f, indent=2, default=str)
        self.log(f"*** UB {self.UB:,.2f}; JSON en {destino}")

    # ---------------------------------------------------- movimientos -- #
    def tiempo(self):
        return self._t() < self.a.horas * 3600

    def construir(self, grupo):
        T, S = self.trayectorias()
        primero = min((y for y in self.years if any(self.activos.get(k, {}).get(y, 0) for k in grupo)),
                      default=None)
        if primero is None or primero == self.years[0]:
            return False
        cambio = False
        # X queda: las naves solo abren el año 1 (functions.py, Delta_X = 0 en y > 1);
        # una nave abierta puede tener 0 bahias, baterias y cargadores.
        for f in ("N_bays", "N_batteries", "N_chargers"):
            for k in grupo:
                for i, y in enumerate(self.years):
                    if y < primero and T[(f, k)][i]:
                        T[(f, k)][i] = 0
                        cambio = True
        self.reparar(T)
        return cambio and self.evaluar(T, S, f"construir {'+'.join(grupo)} desde el año {primero}") is not None

    def alza(self, T):
        """Como reparar, pero SUBIENDO: N_chargers >= N_bays, N_batteries >= N_chargers y
        N_bays >= ceil(cargadores y baterias / por bahia), hasta que se cumplan todas."""
        from math import ceil
        for k, tp in self.tope.items():
            por_bahia = tp["N_chargers"] // tp["N_bays"]
            bat_bahia = tp["N_batteries"] // tp["N_bays"]
            for i in range(len(self.years)):
                while True:
                    b, ch, bat = (T[(f, k)][i] for f in ("N_bays", "N_chargers", "N_batteries"))
                    ch2 = max(ch, b)
                    bat2 = max(bat, ch2)
                    b2 = max(b, ceil(ch2 / por_bahia), ceil(bat2 / bat_bahia))
                    if (b2, ch2, bat2) == (b, ch, bat):
                        break
                    T[("N_bays", k)][i], T[("N_chargers", k)][i], T[("N_batteries", k)][i] = b2, ch2, bat2
        return T

    def operar(self):
        """Misma inversion, operacion nueva: todos los dias (macrobloques) MILP desde la
        operacion de la incumbente y el pulido. Con macrobloques cada estacion se
        resuelve sola y suele mejorar la operacion que dejo el dia completo."""
        P, bd, a, ls = self.P, self.bd, self.a, self.ls
        t0 = time.time()
        # Sin MIP start: desde la operacion de la incumbente Gurobi no sale de ella en
        # el tope (1C, 6-nov: sin start bajo 33 mil, con start nada).
        for s_ in bd.subs:
            s_.start = None
        res = bd._paralelo(lambda b: bd.subs[b].ub(self.vec, a.t, a.gap))
        malos = [P.claves[b] for b, r in enumerate(res) if not r["ok"]]
        if malos:
            self.log(f"   operar: sin operacion en {', '.join(malos[:6])} ({time.time() - t0:.0f}s)")
            self._starts()
            return False
        vec = self.vec.copy()
        for b, r in enumerate(res):
            vec[bd.subs[b].loc] = r["x"]
        costo, completo = ls.pulir_flexible(vec)
        if costo is None or costo >= self.UB * (1 - a.tol):
            self.log(f"   operar: {'pulido infactible' if costo is None else f'{costo:,.2f}'}, sin mejora "
                     f"({time.time() - t0:.0f}s)")
            self._starts()
            return False
        self.log(f"   operar: misma inversion, operacion de los {P.nb} bloques: {costo:,.2f} "
                 f"(-{self.UB - costo:,.2f}; {time.time() - t0:.0f}s)")
        self.UB, self.vec = costo, completo
        self._starts()
        self.historia.append({"t": self._t(), "mov": "operar", "ub": costo})
        self.escribir()
        return True

    def macro(self, vueltas=3):
        """Algoritmo de un año por macrobloque (ver el docstring del modulo). La Fase 1
        se repite con el b_bar real del diseño que sale (con menos baterias cada una
        cicla mas y la capacidad del año baja: con la de la incumbente, la Fase 1
        acepta diseños que en la Fase 2 no alcanzan), y la Fase 2 parte de la
        operacion de la Fase 1, factible para la inversion agregada."""
        P, bd, a, ls = self.P, self.bd, self.a, self.ls
        if not P.por_estacion:
            self.log("   macro: requiere --por_estacion")
            return False
        T0, S0 = self.trayectorias()
        delta = {}
        for j in P.maestro_vars:
            f, k, y = familia(P.nombre[j])
            if f.startswith("Delta_") and f[len("Delta_"):] in FAM_N and k is not None:
                delta[(f[len("Delta_"):], k, y)] = float(P.obj[j])
        bloques = [b for b in range(P.nb) if eval(P.claves[b])[2] != P.RED]
        cbb = ls._cols("b_bar")
        bb = {y: float(self.vec[j]) for y, j in cbb.items()}

        def tarea(b):
            s = bd.subs[b]
            costos, lbs, ubs = [], [], []
            for j in s.cop:
                f, k, y = familia(P.nombre[j])
                v = bb[y] if f == "b_bar" else float(self.vec[j])
                if f in FAM_N:
                    costos.append(delta.get((f, k, y), 0.0))
                    lbs.append(0.0)
                    ubs.append(float(self.tope[k][f]))
                elif f == "n_ssee_k":
                    costos.append(float(P.obj[j]))
                    lbs.append(0.0)
                    ubs.append(float(self.tope[k]["n_ssee_k"]))
                else:                                  # X de la incumbente, b_bar de la vuelta
                    costos.append(0.0)
                    lbs.append(v)
                    ubs.append(v)
            return s.fase1(costos, lbs, ubs, a.t, a.gap)

        res, T, S = {}, None, None
        for vuelta in range(1, vueltas + 1):
            t0 = time.time()
            # Solo los macrobloques cuyo b_bar cambio desde la vuelta anterior.
            # (b_bar no tiene columna el primer año: queda fijo en b_max.)
            pend = [b for b in bloques
                    if b not in res or abs(res[b]["bb"] - bb.get(eval(P.claves[b])[0], 0.0)) > 1e-6]
            for b, r in zip(pend, bd._paralelo(tarea, pend)):
                r["bb"] = bb.get(eval(P.claves[b])[0], 0.0)
                res[b] = r
            req, Sreq, fallan = {}, {}, []
            for b in bloques:
                r, s = res[b], bd.subs[b]
                if not r["ok"]:
                    fallan.append(P.claves[b])
                cop = r["cop"] if r["ok"] else [self.vec[j] for j in s.cop]
                for kk, j in enumerate(s.cop):
                    f, k2, y2 = familia(P.nombre[j])
                    if f in FAM_N:
                        d_ = req.setdefault((f, k2), {})
                        d_[y2] = max(d_.get(y2, 0), int(round(cop[kk])))
                    elif f == "n_ssee_k":
                        Sreq[k2] = max(Sreq.get(k2, 0), int(round(cop[kk])))
            T = {key: list(v) for key, v in T0.items()}
            for (f, k), por_y in req.items():
                acum = 0
                for i, y in enumerate(self.years):
                    acum = max(acum, por_y.get(y, 0))
                    T[(f, k)][i] = acum
            self.alza(T)
            S = {k: max(1, Sreq.get(k, S0[k])) for k in S0}
            sol = [res[b] for b in pend if res[b]["ok"]]
            self.log(f"   macro, Fase 1 vuelta {vuelta} ({len(pend)} macrobloques, el mas lento "
                     f"{max((r['t'] for r in sol), default=0):.0f}s, gap max "
                     f"{max((r['gap'] for r in sol), default=0):.1%}, {time.time() - t0:.0f}s)"
                     + (f"; sin operacion (quedan en la incumbente): {', '.join(fallan[:6])}" if fallan else ""))
            for k in sorted(self.tope):
                self.log(f"      {k}: bahias {T[('N_bays', k)]} cargadores {T[('N_chargers', k)]} "
                         f"baterias {T[('N_batteries', k)]} ssee {S[k]}")
            # b_bar real del diseño: maestro con T y S fijos y la capacidad maxima que
            # deja la degradacion con la energia de la incumbente (como evaluar).
            x = self.maestro(T, S, self.vec)
            if x is None:
                self.log("   macro: el maestro no completa el diseño de la Fase 1")
                return False
            xx = self.vec.copy()
            xx[P.maestro_vars] = x[P.maestro_vars]
            real = ls._b_bar_real(xx, self.vec)
            if real is None:
                break
            nuevo = {y: real[y] - 0.1 for y in bb}
            cambio = {y: (bb[y], v) for y, v in nuevo.items() if abs(v - bb[y]) > 0.05}
            if not cambio:
                break
            self.log("      b_bar real del diseño: " + ", ".join(
                f"{y}:{o:.1f}->{n:.1f}" for y, (o, n) in sorted(cambio.items())))
            bb = nuevo

        def con_starts(fn):
            # Fase 2 desde la operacion de la Fase 1 (factible con la inversion agregada,
            # que es >= la de cada dia, y el mismo b_bar).
            for b in bloques:
                if res[b]["ok"]:
                    s = bd.subs[b]
                    s.start = [round(res[b]["x"][kk]) for kk in s.ent_loc]
            return fn()
        mejoro = con_starts(lambda: self.evaluar(T, S, "macro: Fase 2, las estaciones de la Fase 1")) is not None
        for k in sorted(self.tope):
            if not self.tiempo():
                break
            T1, S1 = self.trayectorias()
            for f in FAM_N:
                T1[(f, k)] = list(T[(f, k)])
            S1[k] = S[k]
            if (T1, S1) == self.trayectorias():
                continue
            mejoro = (con_starts(lambda: self.evaluar(T1, S1, f"macro: Fase 2, solo {k} de la Fase 1"))
                      is not None) or mejoro
        return mejoro

    def reparar(self, T):
        """Respeta, bajando, las filas de capacidad de functions.py en cada (k, y):
        N_bays <= N_chargers <= N_batteries, N_chargers <= max_chargers_per_bay * N_bays y
        N_batteries <= max_batteries_per_bay * N_bays."""
        for k, tp in self.tope.items():
            por_bahia = tp["N_chargers"] // tp["N_bays"]
            bat_bahia = tp["N_batteries"] // tp["N_bays"]
            for i in range(len(self.years)):
                bat = T[("N_batteries", k)][i]
                ch = min(T[("N_chargers", k)][i], bat)
                b = min(T[("N_bays", k)][i], ch)
                ch = min(ch, por_bahia * b)
                bat = min(bat, bat_bahia * b)
                T[("N_chargers", k)][i], T[("N_bays", k)][i] = ch, b
                T[("N_batteries", k)][i] = bat
        return T

    def escalera(self, L):
        """Diseño uniforme de nivel L por estacion: L baterias, L cargadores, ceil(L/2)
        bahias y los modulos que pidan L cargadores (<= tope), escalado en la rampa por
        los LHD activos del año (maximo acumulado: las compras no se deshacen) y sin
        equipos antes del primer LHD activo."""
        from math import ceil
        from src.optimization.functions import P_SSEE_STEP
        T, S = self.trayectorias()
        p_ch = float(value(self.om.model.p_charger))
        for k, tp in self.tope.items():
            act = self.activos.get(k, {})
            n_max = max(act.values(), default=0) or 1
            acum = 0
            for i, y in enumerate(self.years):
                acum = max(acum, ceil(L * act.get(y, n_max) / n_max - 1e-9))
                T[("X", k)][i] = 1
                T[("N_batteries", k)][i] = min(acum, tp["N_batteries"])
                T[("N_chargers", k)][i] = min(acum, tp["N_chargers"])
                T[("N_bays", k)][i] = min(ceil(acum / 2), tp["N_bays"])
            S[k] = max(1, min(tp["n_ssee_k"], ceil(L * p_ch / P_SSEE_STEP - 1e-9)))
        return T, S

    def bajar_tope(self, grupo, f):
        mejoro = False
        while self.tiempo():
            T, S = self.trayectorias()
            actual = max(max(T[(f, k)]) for k in grupo)
            if actual <= (1 if f in ("X", "N_bays") else 0):
                break
            c = actual - 1
            for k in grupo:
                T[(f, k)] = [min(v, c) for v in T[(f, k)]]
                if f == "N_bays":
                    for g, por in (("N_batteries", "N_batteries"), ("N_chargers", "N_chargers")):
                        tope_g = self.tope[k][por] // self.tope[k]["N_bays"] * c
                        T[(g, k)] = [min(v, tope_g) for v in T[(g, k)]]
            self.reparar(T)
            if self.evaluar(T, S, f"tope {f} {'+'.join(grupo)} = {c}") is None:
                break
            mejoro = True
        return mejoro

    def punta(self):
        T, S = self.trayectorias()
        return self.evaluar(T, S, "punta: P_pot desde 0, la operacion decide", punta=True) is not None

    def bajar_ssee(self, grupo):
        mejoro = False
        while self.tiempo():
            T, S = self.trayectorias()
            if min(S[k] for k in grupo) <= 1:
                break
            for k in grupo:
                S[k] -= 1
            if self.evaluar(T, S, f"ssee {'+'.join(grupo)} = {S[grupo[0]]}") is None:
                break
            mejoro = True
        return mejoro

    def retrasar(self, grupo, f):
        mejoro = False
        for i in range(len(self.years) - 1):
            if not self.tiempo():
                break
            T, S = self.trayectorias()
            fams = ("N_bays", "N_batteries", "N_chargers") if f == "equipo" else (f,)
            if not any(T[(g, k)][i] > (T[(g, k)][i - 1] if i else 0) for g in fams for k in grupo):
                continue
            for g in fams:
                for k in grupo:
                    T[(g, k)][i] = min(T[(g, k)][i], T[(g, k)][i - 1] if i else 0)
            self.reparar(T)
            if self.evaluar(T, S, f"retrasar {f} {'+'.join(grupo)} del año {self.years[i]}") is not None:
                mejoro = True
        return mejoro

    def correr(self):
        grupos = [g.split(",") for g in self.a.grupos.split("|")]
        movs = set(self.a.movs.split(","))
        for L in (int(x) for x in self.a.escalera.split(",") if x):
            if not self.tiempo():
                break
            T, S = self.escalera(L)
            if self.evaluar(T, S, f"escalera L={L}") is not None:
                break
        if self.UB == float("inf"):
            self.log("FINAL: ningun nivel de la escalera es factible; probar niveles mas altos")
            return
        if "construir" in movs:
            for g in grupos:
                if self.tiempo():
                    self.construir(g)
        if "operar" in movs and self.tiempo() and self.UB < float("inf"):
            self.operar()
        if "macro" in movs and self.tiempo():
            self.macro()
        ronda = 0
        while self.tiempo():
            ronda += 1
            ub0 = self.UB
            self.log(f"ronda {ronda}: UB {self.UB:,.2f}")
            if "punta" in movs and self.tiempo():
                self.punta()
            if "tope" in movs:
                for f in ("N_chargers", "N_batteries", "N_bays"):
                    for g in grupos:
                        if self.tiempo():
                            self.bajar_tope(g, f)
            if "ssee" in movs:
                for g in grupos:
                    if self.tiempo():
                        self.bajar_ssee(g)
            if "retrasar" in movs:
                for f in ("equipo", "N_batteries"):
                    for g in grupos:
                        if self.tiempo():
                            self.retrasar(g, f)
            if self.UB > ub0 * (1 - self.a.tol):
                break
        self.log(f"FINAL: UB {self.UB:,.2f} ({len(self.historia) - 1} mejoras)")
        T, S = self.trayectorias()
        for (f, k), tr in sorted(T.items(), key=lambda z: (z[0][1], z[0][0])):
            self.log(f"   {k:10s} {f:12s} {tr}")
        self.log(f"   n_ssee_k {S}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, default=11)
    ap.add_argument("--days_per_year", type=int, choices=[1, 2, 4], default=2)
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp2")
    ap.add_argument("--pause_scheme", choices=["det", "dch"], default="dch")
    ap.add_argument("--solucion", default=None,
                    help="carpeta con los JSON de la incumbente (sin ella: desde cero, con la escalera)")
    ap.add_argument("--grupos", default="station_1|station_2,station_3",
                    help="estaciones que se mueven juntas (simetricas), separadas por |")
    ap.add_argument("--escalera", default="2,3,4",
                    help="niveles uniformes que se prueban primero (el primero factible queda); vacio: no")
    ap.add_argument("--movs", default="construir,punta,tope,ssee,retrasar",
                    help="movimientos que se usan, por coma")
    ap.add_argument("--t", type=float, default=600.0, help="tope por MILP del dia (s)")
    ap.add_argument("--gap", type=float, default=0.01)
    ap.add_argument("--tol", type=float, default=1e-5, help="mejora relativa minima para aceptar")
    ap.add_argument("--horas", type=float, default=12.0)
    ap.add_argument("--jobs", type=int, default=11)
    ap.add_argument("--cpu", type=int, default=None)
    ap.add_argument("--p_ssee_max", type=float, default=1500.0)
    ap.add_argument("--por_estacion", action="store_true",
                    help="macrobloques por estacion (ver el docstring); habilita el movimiento macro")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    r = Recorte(a)
    if r.UB < float("inf"):
        r.escribir()
    r.correr()
    return 0


if __name__ == "__main__":
    sys.exit(main())
