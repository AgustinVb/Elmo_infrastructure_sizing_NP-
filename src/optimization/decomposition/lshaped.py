"""L-shaped entero multicorte en branch-and-cut sobre BendersDias (lshaped_entero_swap.md).

Esqueleto: el maestro y los 40 subproblemas (año, dia) de BendersDias, con sus
presupuestos de energia, enlaces elasticos y la cota global de cada dia
(L_{y,d} = theta.LB de _cota_theta). Lo que se agrega:

  CORTE DE CARGAS en cada dia como funcion de la capacidad: la cota por LHD
    n_d(B) (cota_cargas_lhd) no crece con B. En el rango [lo, hi] de b_bar[y]
    en el maestro (Maestro.rangos, relajacion valida):
      - global, en el dia: sum_{k, t<tf} X_ini >= g_d(b_bar), con g_d la
        envolvente convexa del escalon n_d, escrita con la copia de b_bar
        (una fila por arista) mas la constante n_d(hi);
      - por nivel (cambio A): binarias w_{y,l} en el maestro con los extremos
        c_l en las caidas de n_d de los dias del año; con w_l = 1 el dia
        necesita n_d(c_l) cargas, y el corte LP (y el entero) calculado con
        esa constante se apaga con big-M en los niveles mayores. En las
        soluciones enteras es el escalon exacto, como el certificado con el
        b_bar maximo de cada candidata.

  BRANCH-AND-CUT con callback MIPSOL (Fischetti et al. 2017; Angulo et al.
    2016): en cada solucion entera del maestro,
      1. los 40 LP del dia (cortes de Benders, BendersDias.Subproblema.lp),
         con la envolvente y con el escalon del nivel de la solucion; si
         alguno corta, se agregan con cbLazy y se sigue;
      2. si ninguno corta, el corte ENTERO local de cada dia
         (Subproblema.local: copias enteras fijas, continuas y presupuestos
         libres con costo -pi/-lam, cota dual del MILP). Es el L-shaped entero
         de Laporte y Louveaux con la parte continua tratada por dualidad
         lagrangiana en vez de discretizada: theta_b >= R + pi_C.x_C + lam.e
         - Mb * (distancia de las copias enteras a xhat). La distancia se
         escribe con la codificacion binaria de las copias enteras, declarada
         al inicio (en un callback no se pueden crear variables), como pide la
         Fase 2; las continuas sin discretizar.
      3. R es la MEJOR COTA del MILP: con el esquema de Riley, Daoutidis y
         Zhang (2025, Alg. 3) los MILP se cortan por gap (0,1; 0,01; 0) y
         tiempo (que se duplica si corta por tiempo), y se endurecen solo si
         el corte no separa. Cache por (dia, inversion entera).
    La UB se lleva AFUERA de Gurobi (dias MILP con la inversion fija + pulido,
    BendersDias.evaluar_ub): el incumbente interno del maestro tiene theta en
    cotas inferiores y no es una solucion. Aun si una solucion se acepta sin
    separarla del todo (tope de tiempo), la cota del arbol sigue siendo una LB
    valida: solo poda nodos con cota >= el valor aceptado, y esa cota no supera
    el optimo de su nodo.
"""
import json
import math
import os
import time

import gurobipy as gp
import numpy as np
from gurobipy import GRB

INF = GRB.INFINITY


class LShaped:

    def __init__(self, bd, om, por_var, alfas=(0.1, 0.01, 0.0), t_inicial=120.0,
                 t_max_dia=1920.0, ub_timelimit=120.0, ub_gap=0.01, tol=1e-6, out=None,
                 ub_timelimit_inicial=300.0, pasadas_dp=4, p_ssee_max=1500.0, tol_lp=1e-5):
        self.bd, self.om, self.por_var = bd, om, por_var
        self.P, self.M = bd.P, bd.maestro
        self.alfas, self.t_inicial, self.t_max_dia = list(alfas), t_inicial, t_max_dia
        self.ub_timelimit, self.ub_gap, self.tol = ub_timelimit, ub_gap, tol
        # Violacion minima (absoluta, tol_lp * objetivo del maestro) para que un
        # corte cuente como separador. Gurobi no hace cumplir violaciones chicas
        # en filas con coeficientes grandes (duales de hasta 1e6): en el caso 7
        # acepto una solucion con un corte violado en 0,26 (2e-4 del costo de su
        # dia) y el arbol cerro en la cota LP sin un corte entero.
        self.tol_lp = tol_lp
        self.ub_timelimit_inicial, self.pasadas_dp = ub_timelimit_inicial, pasadas_dp
        self.p_ssee_max = p_ssee_max
        # La UB del callback (BendersDias.evaluar_ub) tambien con el pulido flexible.
        bd.pulir = self.pulir_flexible
        self.out = out
        self.t0 = time.time()
        self.cache_lp = {}          # clave del punto -> resultados LP de los dias
        self.estado = {}            # (b, clave entera) -> {"l", "t", "R", "final"}
        self.n_lp = self.n_niv = self.n_ent = self.n_cb = 0
        self.n_sin_cota = 0         # cortes descartados por un big-M sin cota util
        self.niv, self.n_nivel, self.w_todas = {}, {}, []
        self.t_lp = self.t_milp = 0.0
        self.historia = []
        self.aceptadas = []         # objetivos de las soluciones que el callback no corto
        self.n_silenciosas = 0      # incumbentes de Gurobi que el callback si habia cortado
        self.obj_bst = INF
        self.tiempo_max = INF
        self.lazy = []              # (expr, rhs) de los cortes lazy, para reiniciar el arbol
        self.vistas = {}            # objetivo de cada solucion de MIPSOL -> numero de cb

    def _log(self, s):
        print(f"[LS] {s}", flush=True)

    # ------------------------------------------------ corte de cargas -- #
    @staticmethod
    def _envolvente(n, lo, hi, tol):
        """Vertices de la envolvente convexa inferior de la cota de cargas n(B)
        en [lo, hi]. n es no creciente en B (mas capacidad, menos swaps), asi
        que en cada celda (a, b] con n(a) > n(b), ubicada por biseccion hasta
        b - a <= tol, vale n(B) >= n(b): el punto (a, n(b)) es valido. Si
        n(a) = n(b), n es constante en [a, b]."""
        memo = {}

        def f(c):
            if c not in memo:
                v = n(c)
                memo[c] = 0 if v is None else v
            return memo[c]
        pts = [(lo, f(lo))]

        def bis(a, b):
            if f(a) == f(b):
                return
            if b - a <= tol:
                pts.append((a, f(b)))
                return
            c = 0.5 * (a + b)
            bis(a, c)
            bis(c, b)
        bis(lo, hi)
        caidas = sorted(a for a, _ in pts[1:])
        pts.append((hi, f(hi)))
        # Cadena monotona inferior (Andrew), con los puntos ordenados por B.
        casco = []
        for p in sorted(pts):
            while len(casco) >= 2:
                (x1, y1), (x2, y2) = casco[-2], casco[-1]
                if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) <= 0:
                    casco.pop()
                else:
                    break
            casco.append(p)
        return casco, len(memo), caidas

    def agregar_corte_cargas(self, tol=0.05):
        """Corte de cargas en cada dia como funcion de la capacidad b_bar[y]
        (cambio A): sum_{k, t<tf} X_ini >= g(b_bar), con g la envolvente
        convexa de la cota por LHD n(B) en el rango [lo, hi] de b_bar[y] en el
        maestro, escrita con la copia de b_bar del dia (una fila por arista, mas
        la constante n(hi)). Es la relajacion LP de un corte por nivel de B_y,
        sin binarias ni copias nuevas: los duales llevan al maestro que menos
        capacidad exige mas cargas, como el certificado con cada candidata."""
        from pyomo.environ import value
        from cota_cargas_lhd import evaluador_dia
        from src.optimization.functions import intervalos_corte_cargas
        P, M, bd, om = self.P, self.M, self.bd, self.om
        t0 = time.time()
        cols_bb = {int(P.nombre[j][len("b_bar("):-1]): j for j in P.maestro_vars
                   if P.nombre[j].startswith("b_bar(")}
        cap_pool = value(om.model.b_max_pool)
        rg = M.rangos(list(cols_bb.values()))
        M._entero(True)
        self.cap = {y: (min(rg[j][1], cap_pool) if np.isfinite(rg[j][1]) else cap_pool)
                    for y, j in cols_bb.items()}
        self.cap_lo = {y: rg[j][0] for y, j in cols_bb.items()}
        t_corte = set(intervalos_corte_cargas(sorted(om.model.time_intervals_set)))
        self.n_corte = {}
        self.nfun, self.kbb, self.fila_cte, self.caidas, self.filas_env = {}, {}, {}, {}, {}
        n_mip = n_filas = 0
        for b, s in enumerate(bd.subs):
            y, d = eval(P.claves[b])
            n = evaluador_dia(om, y, d)
            hi = self.cap.get(y, cap_pool)
            lo = self.cap_lo.get(y, -INF)
            kb = [k for k, j in enumerate(s.cop) if P.nombre[j] == f"b_bar({y})"]
            kx = [k for k, j in enumerate(s.loc)
                  if (vd := self.por_var.get(P.nombre[j])) is not None
                  and vd.parent_component().name == "X_ini" and vd.index()[-1] in t_corte]
            cargas = gp.quicksum(s.vloc[k] for k in kx)
            if kb and np.isfinite(lo) and 0 < lo < hi:
                casco, evals, caidas = self._envolvente(n, lo, hi, tol)
                n_mip += evals
                self.nfun[b], self.kbb[b] = n, kb[0]
                self.caidas.setdefault(y, set()).update(caidas)
                bb = s.vcop[kb[0]]
                # Arista (c1, n1)-(c2, n2), pendiente sg <= 0: cargas >= n1 + sg (b_bar - c1).
                for (c1, n1), (c2, n2) in zip(casco, casco[1:]):
                    sg = (n2 - n1) / (c2 - c1)
                    if sg < 0:
                        self.filas_env.setdefault(b, []).append(
                            s.m.addLConstr(cargas - sg * bb, GRB.GREATER_EQUAL, float(n1 - sg * c1)))
                        n_filas += 1
            else:
                v = n(hi)
                n_mip += 1
                casco = [(hi, 0 if v is None else v)]
            self.fila_cte[b] = s.m.addLConstr(cargas, GRB.GREATER_EQUAL, float(casco[-1][1]))
            n_filas += 1
            s.m.update()
            self.n_corte[P.claves[b]] = casco
        self._log(f"corte de cargas en los dias, funcion de b_bar ({n_mip} MIP de la cota, "
                  f"{n_filas} filas, {time.time() - t0:.0f}s); rango de b_bar por año "
                  + ", ".join(f"{y}:[{self.cap_lo[y]:.1f},{c:.1f}]" for y, c in sorted(self.cap.items())))
        self._log("   n_corte en el rango (dia 15): " + ", ".join(
            f"{eval(k)[0]}:{c[0][1]}->{c[-1][1]} ({len(c) - 1} aristas)"
            for k, c in self.n_corte.items() if eval(k)[1] == 15))

    # ------------------------------------------------ UB inicial (DP) -- #
    def _cols(self, prefijo):
        """{año: indice} de las columnas del maestro 'prefijo(...,año)'."""
        P = self.P
        out = {}
        for j in P.maestro_vars:
            n = P.nombre[j]
            if n.startswith(prefijo + "("):
                # El MPS escribe los indices separados por '_': N_batteries(station_1_3).
                out[int(n[len(prefijo) + 1:-1].replace(",", "_").split("_")[-1])] = j
        return out

    def _energia_por_año(self, vec=None, ehat=None):
        """{año: kWh} de los dias representativos: suma de los presupuestos del
        maestro (ehat) o de la energia de una solucion completa (vec)."""
        from src.optimization.functions import ESCALA_UNIDADES
        P = self.P
        E = {}
        for k, (_i, b) in enumerate(P.presupuestos):
            y = eval(P.claves[b])[0]
            v = P.energia(vec, k) if vec is not None else float(ehat[k])
            # La fila es EnergyConsumed_y - sum_d energia_d = 0: la parte del
            # dia entra con signo negativo.
            E[y] = E.get(y, 0.0) + abs(v) * ESCALA_UNIDADES["EnergyConsumed"]
        return E

    AUX_DEGRADACION = ("u_nf(", "z_bb(")

    def pulir_flexible(self, vec):
        """Como BendersDias.pulir, pero sin fijar las binarias auxiliares de la
        degradacion (valor de la flota u_nf y tramo del McCormick z_bb): vienen
        del maestro, calculadas con la energia de sus PRESUPUESTOS, y los dias
        MILP gastan otra; fijarlas deja la cadena de degradacion infactible.
        Queda un MILP con ~170 binarias."""
        P = self.P
        F = P.F
        vs = F.getVars()
        if not hasattr(self, "_ent_fijas"):
            self._ent_fijas = [j for j, t in enumerate(P.vtype)
                               if t != "C" and not P.nombre[j].startswith(self.AUX_DEGRADACION)]
            self._ent_aux = [j for j, t in enumerate(P.vtype)
                             if t != "C" and P.nombre[j].startswith(self.AUX_DEGRADACION)]
        vals = [float(round(vec[j])) for j in self._ent_fijas]
        F.setAttr("LB", [vs[j] for j in self._ent_fijas], vals)
        F.setAttr("UB", [vs[j] for j in self._ent_fijas], vals)
        F.setAttr("LB", [vs[j] for j in self._ent_aux], [P.lb[j] for j in self._ent_aux])
        F.setAttr("UB", [vs[j] for j in self._ent_aux], [P.ub[j] for j in self._ent_aux])
        F.Params.TimeLimit = 900
        F.Params.MIPGap = 1e-6
        F.optimize()
        if F.SolCount == 0:
            return None, None
        return F.ObjVal, np.array(F.getAttr("X", vs))

    def _preparar_capacidad(self):
        """Copia del maestro (filas de inversion y degradacion; los cortes no
        importan con theta libre) para calcular la capacidad real de una
        operacion desde el callback: dentro del callback no se puede volver a
        optimizar el maestro."""
        if getattr(self, "_mcap", None) is not None:
            return
        M = self.M
        M._entero(True)
        M.m.update()
        mc = M.m.copy()
        mc.Params.OutputFlag = 0
        mc.Params.Threads = 4
        mc.Params.TimeLimit = 60
        vs = mc.getVars()
        self._mcap = mc
        self._mcap_x = {j: vs[M.x[M.pos[j]].index] for j in self.P.maestro_vars}
        self._mcap_e = [vs[v.index] for v in M.e]
        for t in M.th:
            vt = vs[t.index]
            vt.LB, vt.UB = -INF, INF
        cbb = self._cols("b_bar")
        mc.setObjective(gp.quicksum(self._mcap_x[j] for j in cbb.values()), GRB.MAXIMIZE)
        mc.update()

    def _b_bar_real(self, x, vec):
        """{año: b_bar} con la inversion de x fija y los presupuestos en la
        energia REAL de los dias (vec): la trayectoria de capacidad maxima
        (conjunta) que permite la degradacion. None si no hay."""
        P = self.P
        self._preparar_capacidad()
        mc = self._mcap
        guard = []
        for j, v in self._mcap_x.items():
            if P.vtype[j] != "C" and not P.nombre[j].startswith(self.AUX_DEGRADACION):
                guard.append((v, v.LB, v.UB))
                v.LB = v.UB = float(round(x[j]))
        for k, v in enumerate(self._mcap_e):
            # Mismo signo que el presupuesto del maestro (la fila resta la
            # energia del dia; los presupuestos salen negativos).
            guard.append((v, v.LB, v.UB))
            v.LB = v.UB = -abs(float(P.energia(vec, k)))
        try:
            mc.optimize()
            if mc.SolCount == 0:
                self._log(f"   capacidad real: maestro sin solucion (status {mc.Status}, {mc.Runtime:.0f}s)")
                return None
            return {y: self._mcap_x[j].X for y, j in self._cols("b_bar").items()}
        finally:
            for v, lo, hi in guard:
                v.LB, v.UB = lo, hi

    SUBIR_UB = ("P_pot(",)

    def _subir(self, b):
        """{posicion en las copias del dia b: costo} de las copias que puede
        subir al evaluar la UB (P_pot, con su costo en el objetivo)."""
        P, s = self.P, self.bd.subs[b]
        return {k: float(P.obj[j]) for k, j in enumerate(s.cop)
                if P.nombre[j].startswith(self.SUBIR_UB)}

    def _dias_ub(self, x, timelimit, gap, vueltas=3, holgura=0.1):
        """Dias MILP con la inversion fija y b_bar corregido por la energia real.

        El maestro declara b_bar con la energia de sus presupuestos, que es
        optimista; con el corte de cargas por capacidad (cambio A) ademas le
        conviene declarar el b_bar maximo. Los dias gastan algo mas, la
        degradacion real deja la bateria hasta ~1 kWh mas chica y la operacion
        armada para el b_bar del maestro no cabe en el pulido (infactible en
        4 s; diagnostico del caso 5). Se repite con b_bar = capacidad real -
        holgura hasta que alcance.

        P_pot tambien viene del maestro (operacion relajada) y en los meses de
        punta (P_red <= P_pot) puede no alcanzar para la operacion entera: el
        dia no tiene solucion con la inversion de la solucion conocida (caso 4,
        dias 105 y 196). Los dias que fallan se re-resuelven con P_pot libre
        hacia arriba, pagando su costo, y el doble de tiempo; P_pot[y] sube al
        maximo que pidan sus dias (el pulido la vuelve a optimizar). Devuelve
        (vec o None, resultados)."""
        P, bd = self.P, self.bd
        cbb = self._cols("b_bar")
        xx = x.copy()
        for vuelta in range(vueltas):
            res = bd._paralelo(lambda b: bd.subs[b].ub(xx, timelimit, gap))
            malos = [b for b, r in enumerate(res) if not r["ok"]]
            if malos:
                self._log("   dias sin operacion con esta inversion: " + ", ".join(
                    f"{P.claves[b]} status {res[b]['status']} {res[b]['t']:.0f}s" for b in malos[:8]))
                otra = bd._paralelo(lambda b: bd.subs[b].ub(xx, 2 * timelimit, gap,
                                                             subir=self._subir(b)), malos)
                for b, r in zip(malos, otra):
                    res[b] = r
                siguen = [b for b in malos if not res[b]["ok"]]
                if siguen:
                    self._log("   tampoco con P_pot libre: " + ", ".join(
                        f"{P.claves[b]} status {res[b]['status']} {res[b]['t']:.0f}s" for b in siguen[:8]))
                    return None, res
                subida = {}
                for b in malos:
                    s = bd.subs[b]
                    for k in self._subir(b):
                        j = s.cop[k]
                        if res[b]["cop"][k] > xx[j] + 1e-6:
                            subida[j] = max(subida.get(j, xx[j]), res[b]["cop"][k])
                self._log("   con P_pot libre: " + ", ".join(
                    f"{P.claves[b]} {res[b]['t']:.0f}s" for b in malos[:8]) + "; " + (", ".join(
                        f"{P.nombre[j]} {xx[j]:.1f}->{v:.1f}" for j, v in sorted(subida.items()))
                        or "P_pot no sube (fallaba por tiempo)"))
                for j, v in subida.items():
                    xx[j] = v
            vec = xx.copy()
            for b, r in enumerate(res):
                vec[bd.subs[b].loc] = r["x"]
            real = self._b_bar_real(xx, vec)
            if real is None:
                self._log("   la energia real de los dias no admite ninguna capacidad en el maestro")
                return vec, res
            cortos = {y: (xx[j], real[y]) for y, j in cbb.items() if real[y] < xx[j] - 1e-6}
            if not cortos:
                return vec, res
            self._log(f"   b_bar del maestro sobre la capacidad real (vuelta {vuelta + 1}): " + ", ".join(
                f"{y}:{bh:.2f}>{br:.2f}" for y, (bh, br) in sorted(cortos.items())))
            for y, (_bh, br) in cortos.items():
                xx[cbb[y]] = br - holgura
        return vec, res

    def evaluar_inversion(self, xhat, timelimit, gap):
        """Dias MILP con la inversion fija (b_bar corregido, _dias_ub) +
        pulido flexible. Devuelve (costo o None, vector de los dias): la
        energia de los dias sirve para la siguiente pasada de la DP aunque el
        pulido falle. Cachea por la parte entera de la inversion."""
        P, bd = self.P, self.bd
        ent_m = [j for j in P.maestro_vars if P.vtype[j] != "C"]
        clave = tuple(int(round(xhat[j])) for j in ent_m)
        x = xhat.copy()
        for j in ent_m:
            x[j] = round(x[j])
        t0 = time.time()
        vec, res = self._dias_ub(x, timelimit, gap)
        if vec is None:
            bd.ub_por_inversion[clave] = None
            return None, None
        costo, completo = self.pulir_flexible(vec)
        self._log(f"   dias MILP (gap max {max(r['gap'] for r in res):.2%}) + pulido en "
                  f"{time.time() - t0:.0f}s -> {'infactible' if costo is None else f'{costo:,.2f}'}")
        bd.ub_por_inversion[clave] = costo
        if costo is not None and costo < bd.UB:
            bd.UB, bd.mejor_vec = costo, completo
            self._log(f"   *** nueva UB {costo:,.2f}")
        return costo, vec

    def _evaluar_plan(self, plan, etiqueta):
        """Fija en el maestro baterias, bahias, cargadores y R del plan
        ({año: (pool, naves, carg, R)}), resuelve el resto con los cortes y
        evalua la inversion con los dias MILP + pulido."""
        M, bd = self.M, self.bd
        fijas = []
        for fam, i in (("N_batteries", 0), ("N_bays", 1), ("N_chargers", 2), ("R", 3)):
            for y, j in self._cols(fam).items():
                if y in plan:
                    v = M.x[M.pos[j]]
                    fijas.append((v, v.LB, v.UB))
                    v.LB = v.UB = float(plan[y][i])
        try:
            M.resolver(entero=True)
            xm, em, _ = M.punto()
        except RuntimeError as exc:
            self._log(f"{etiqueta}: el maestro no admite el plan ({exc})")
            return None
        finally:
            for v, lo, hi in fijas:
                v.LB, v.UB = lo, hi
        costo, vec = self.evaluar_inversion(xm, self.ub_timelimit_inicial, self.ub_gap)
        self._log(f"{etiqueta}: [{bd.inversion(xm)}] R {[y for y, p in sorted(plan.items()) if p[3]]} "
                  f"-> {'sin solucion' if costo is None else f'{costo:,.2f}'}")
        return costo, vec

    def ub_inicial_dp(self, ms, ts, margen=0.5):
        """§5.1 del documento: la inversion del maestro de la Fase 1 reparada
        con la DP de degradacion (energia de sus presupuestos), evaluada; y una
        segunda pasada con la energia real de esa solucion."""
        from src.optimization.decomposition.degradacion_dp import parametros_desde, reparar_plan
        M = self.M
        M.resolver(entero=True)
        xm, em, _ = M.punto()
        cols = {f: self._cols(f) for f in ("N_batteries", "N_bays", "N_chargers", "R")}
        años = sorted(cols["N_batteries"])
        pool = {y: int(round(xm[cols["N_batteries"][y]])) for y in años}
        naves = {y: int(round(xm[cols["N_bays"][y]])) for y in años}
        carg = {y: int(round(xm[cols["N_chargers"][y]])) for y in años}
        R = {y: int(round(xm[cols["R"][y]])) for y in años if y in cols["R"]}
        P = parametros_desde(ms, ts)
        con_R = any(R.values())
        E = self._energia_por_año(ehat=em)
        self._log(f"UB inicial: inversion del maestro [{self.bd.inversion(xm)}], reemplazo en "
                  f"{[y for y, v in R.items() if v] or 'ningun año'}; energia por año (kWh) "
                  + ", ".join(f"{y}:{E[y]:,.0f}" for y in sorted(E)))
        vistos = []
        for pasada in range(1, self.pasadas_dp + 1):
            res = reparar_plan(P, E, pool, naves, carg, margen=margen, permitir_reemplazo=con_R)
            if res is None:
                self._log(f"UB inicial, pasada {pasada}: la DP no encontro plan")
                return
            _c, plan, D_fin = res
            if plan in vistos:
                self._log(f"UB inicial: la DP con la energia de los dias repite un plan ya evaluado")
                return
            vistos.append(plan)
            _costo, vec = self._evaluar_plan(plan, f"UB inicial, pasada {pasada} (DP, D final {D_fin:.1f})")
            if vec is None:
                return
            # Siguiente pasada con la energia REAL de los dias MILP (aunque el
            # pulido no haya cerrado: si la flota no alcanza, la DP la ajusta).
            E = self._energia_por_año(vec=vec)

    def mejorar_ub(self, timelimits=(600.0, 900.0), gap=1e-4):
        """Paso final (como mejorar_ub.py): con la inversion de la mejor UB fija,
        re-resuelve los 40 dias MILP con mas tiempo y gap chico (la UB del
        branch-and-cut los corta a ub_timelimit y ub_gap) partiendo de su
        operacion, y pule. Si el pulido conjunto falla (la energia nueva cambia
        la degradacion), acepta año por año en orden de mejora. Una pasada por
        tiempo de `timelimits`; la segunda y siguientes solo si la anterior
        mejoro y queda tiempo del tope del branch-and-cut."""
        P, bd = self.P, self.bd
        for pasada, tl in enumerate(timelimits, 1):
            if bd.mejor_vec is None:
                self._log("mejorar UB: no hay solucion")
                return
            if pasada > 1 and self._restante() <= 0:
                self._log(f"mejorar UB: sin tiempo para la pasada {pasada}")
                return
            xB, UB0, t0 = bd.mejor_vec.copy(), bd.UB, time.time()
            for s in bd.subs:
                s.start = [round(xB[s.loc[k]]) for k in s.ent_loc]
            res = bd._paralelo(lambda b: bd.subs[b].ub(xB, tl, gap))
            por_año = {}
            for b, r in enumerate(res):
                if not r["ok"]:
                    continue
                s = bd.subs[b]
                mejora = float(np.dot(P.obj[s.loc], xB[s.loc])) - r["valor"]
                if mejora > 1e-6 * max(1.0, abs(r["valor"])):
                    d = por_año.setdefault(eval(P.claves[b])[0], {"mejora": 0.0, "dias": []})
                    d["mejora"] += mejora
                    d["dias"].append((b, r["x"]))

            def con(vec, y):
                v = vec.copy()
                for b, x in por_año[y]["dias"]:
                    v[bd.subs[b].loc] = x
                return v
            costo, completo, aceptados = None, None, []
            if por_año:
                vec = xB
                for y in por_año:
                    vec = con(vec, y)
                costo, completo = self.pulir_flexible(vec)
                aceptados = sorted(por_año)
                if costo is None or costo >= UB0:
                    vec, costo, completo, aceptados = xB, UB0, xB, []
                    for y in sorted(por_año, key=lambda y: -por_año[y]["mejora"]):
                        c_y, comp_y = self.pulir_flexible(con(vec, y))
                        if c_y is not None and c_y < costo * (1 - 1e-9):
                            vec, costo, completo = con(vec, y), c_y, comp_y
                            aceptados.append(y)
            mejoro = costo is not None and costo < UB0 * (1 - 1e-7)
            self._log(f"mejorar UB, pasada {pasada} ({tl:.0f}s por dia, {time.time() - t0:.0f}s): "
                      f"{len(por_año)} años con dias que mejoran, aceptados {aceptados} -> "
                      + (f"UB {UB0:,.2f} -> {costo:,.2f} ({(UB0 - costo) / UB0:.3%})" if mejoro
                         else "sin mejora"))
            if not mejoro:
                return
            bd.UB, bd.mejor_vec = costo, completo
            self._log(f"   *** nueva UB {costo:,.2f}")

    # ------------------------------------------------ indicadores -- #
    def preparar_maestro(self):
        """Rangos de copias y presupuestos (validos), cota de theta, one-hot de
        las copias enteras y parametros del branch-and-cut."""
        P, M, bd = self.P, self.M, self.bd
        t = time.time()
        copiadas = sorted({j for s in bd.subs for j in s.cop})
        self.rangos = M.rangos(copiadas)
        self._log(f"rangos de {len(copiadas)} copias en {time.time() - t:.0f}s")
        self._acotar_copias()
        self._preparar_capacidad()
        t = time.time()
        self.rpres = {}
        por_bloque = bd._paralelo(lambda b: bd.subs[b].rango_presupuestos())
        for b, rs in enumerate(por_bloque):
            for kk, (lo, hi) in zip(bd.subs[b].pres, rs):
                self.rpres[kk] = (lo, hi)
                M.e[kk].LB, M.e[kk].UB = lo, hi
        self._log(f"rangos de {len(self.rpres)} presupuestos en {time.time() - t:.0f}s")
        # Codificacion BINARIA de las copias enteras: x = lo + sum_i 2^i beta_i.
        # La distancia de Hamming en los bits es >= 1 si x != xhat (el corte de
        # Laporte y Louveaux sobre la codificacion binaria). Un one-hot por
        # valor no sirve: la subestacion no tiene cota en el modelo y su rango
        # es enorme (crearia una binaria por valor).
        ent_cop = sorted({s.cop[k] for s in bd.subs for k in s.cop_ent})
        self.bits = {}
        for j in ent_cop:
            lo, hi = self.rangos[j]
            if not (np.isfinite(lo) and np.isfinite(hi)):
                raise RuntimeError(f"copia entera {P.nombre[j]} sin rango finito ({lo}, {hi}): "
                                   "no se puede escribir la distancia del corte entero")
            lo, hi = int(math.ceil(lo - 1e-6)), int(math.floor(hi + 1e-6))
            nb = max(1, int(math.ceil(math.log2(hi - lo + 1)))) if hi > lo else 0
            xv = M.x[M.pos[j]]
            betas = [M.m.addVar(vtype=GRB.BINARY, name=f"bit_{P.nombre[j]}_{i}") for i in range(nb)]
            M.m.addLConstr(xv - gp.quicksum((2 ** i) * b for i, b in enumerate(betas)),
                           GRB.EQUAL, float(lo))
            self.bits[j] = (lo, hi, betas)
        M.m.update()
        self._log(f"codificacion binaria: {len(self.bits)} copias enteras, "
                  f"{sum(len(b[2]) for b in self.bits.values())} bits; rangos "
                  + ", ".join(f"{P.nombre[j]}:[{lo},{hi}]" for j, (lo, hi, _b) in self.bits.items()))
        self.preparar_niveles()

    def _acotar_copias(self):
        """Cotas de las copias sin cota en el modelo (rango INF en el maestro).

        Subestacion: P_SSEE_STEP * n_ssee_k <= p_ssee_max (1,5 MW
        por defecto, criterio de la usuaria). El modelo no tiene cota
        (NonNegativeIntegers) y el rango del maestro sale enorme (~2^24), lo
        que infla la codificacion binaria y los big-M. El tope no corta el
        optimo: n_ssee_k solo aparece en sum_a Sv * p_charger <= P_SSEE_STEP *
        n_ssee_k y con costo >= 0, y la carga simultanea (sum_a Sv <= N_chargers,
        <= n_peak_max) pide a lo sumo ceil(max N_chargers * p_charger /
        P_SSEE_STEP) modulos: 2 con 4 cargadores de 241 kW."""
        from pyomo.environ import value
        from src.optimization.functions import P_SSEE_STEP
        P, M = self.P, self.M
        U = int(math.floor(self.p_ssee_max / P_SSEE_STEP + 1e-9))
        # P_pot[y] tampoco tiene cota y su rango en el maestro es INF (1e100):
        # con un pi minusculo el big-M del corte entero llega a 1e78 y Gurobi
        # acepta soluciones que violan los cortes (paso en el caso 3). P_pot
        # solo aparece en P_red <= P_pot (meses de punta) y con costo >= 0, y
        # P_red <= p_peak (grid_limit): P_pot <= p_peak por dominancia.
        p_peak = value(self.om.model.p_peak)
        for j in P.maestro_vars:
            n = P.nombre[j]
            if n.startswith("n_ssee_k("):
                tope, txt = float(U), f"{U * P_SSEE_STEP / 1000:g} MW"
            elif n.startswith("P_pot("):
                tope, txt = float(p_peak), f"p_peak, {p_peak:g} kW"
            else:
                continue
            v = M.x[M.pos[j]]
            lo, hi = self.rangos.get(j, (v.LB, v.UB))
            if tope < hi:
                v.UB = tope
                self.rangos[j] = (lo, tope)
                self._log(f"{n} <= {tope:g} ({txt}); el maestro daba {hi:g}")

    def preparar_niveles(self):
        """Niveles de capacidad B_y en el maestro (cambio A): binarias w_{y,l}
        con sum_l w = 1 y sum_l c_{l-1} w_l <= b_bar[y] <= sum_l c_l w_l, con
        los extremos c_l en las caidas de la cota de cargas de los dias del año
        (asi cada nivel tiene el escalon exacto de cada dia). Con w_l = 1, el
        dia d necesita al menos n_d(c_l) cargas (n no crece con B, y
        b_bar <= c_l): el corte LP calculado con esa constante vale en los
        niveles <= l y se apaga con un big-M en los mayores (_expr_nivel)."""
        M = self.M
        cols_bb = self._cols("b_bar")
        self.niv, self.n_nivel = {}, {}
        for y, caidas in sorted(self.caidas.items()):
            lo, hi = self.cap_lo[y], self.cap[y]
            sup = sorted({c for c in caidas if lo < c < hi}) + [hi]
            inf = [lo] + sup[:-1]
            w = [M.m.addVar(vtype=GRB.BINARY, name=f"nivel_B({y},{l})") for l in range(len(sup))]
            M.m.addLConstr(gp.quicksum(w), GRB.EQUAL, 1.0)
            bb = M.x[M.pos[cols_bb[y]]]
            M.m.addLConstr(bb - gp.quicksum(c * v for c, v in zip(sup, w)), GRB.LESS_EQUAL, 0.0)
            M.m.addLConstr(bb - gp.quicksum(c * v for c, v in zip(inf, w)), GRB.GREATER_EQUAL, 0.0)
            self.niv[y] = (sup, inf, w)
        M.m.update()
        for b, n in self.nfun.items():
            y = eval(self.P.claves[b])[0]
            if y in self.niv:
                self.n_nivel[b] = [n(c) or 0 for c in self.niv[y][0]]
        self.w_todas = [v for y in sorted(self.niv) for v in self.niv[y][2]]
        self._log(f"niveles de capacidad: {len(self.w_todas)} binarias; por año " + ", ".join(
            f"{y}:{len(self.niv[y][0])}" for y in sorted(self.niv)))
        for b in sorted(self.n_nivel):
            if eval(self.P.claves[b])[1] == 15:
                y = eval(self.P.claves[b])[0]
                self._log(f"   año {y}, dia 15: " + " ".join(
                    f"<={c:.1f}:{n}" for c, n in zip(self.niv[y][0], self.n_nivel[b])))

    def _niveles_punto(self, ws):
        """{año: nivel} de una solucion del maestro."""
        out, i = {}, 0
        for y in sorted(self.niv):
            L = len(self.niv[y][0])
            out[y] = int(np.argmax(ws[i:i + L]))
            i += L
        return out

    def _nivel_dia(self, b, nivel):
        """Nivel del dia b en la solucion, si su escalon exige mas cargas que
        la constante n(hi) de la envolvente; si no, None."""
        if b not in self.n_nivel:
            return None
        y = eval(self.P.claves[b])[0]
        l = nivel.get(y)
        if l is None or self.n_nivel[b][l] <= self.n_corte[self.P.claves[b]][-1][1]:
            return None
        return l

    def _con_nivel(self, b, l, fn):
        """fn() con la constante del corte de cargas del dia en n_d(c_l)."""
        f = self.fila_cte[b]
        f.RHS = float(self.n_nivel[b][l])
        try:
            return fn()
        finally:
            f.RHS = float(self.n_corte[self.P.claves[b]][-1][1])

    def _lp_dia(self, b, xhat, ehat, nivel):
        """LP del dia con la envolvente y, si corresponde, con el escalon del
        nivel de la solucion: (resultado global, resultado del nivel o None)."""
        s = self.bd.subs[b]
        r = s.lp(xhat, ehat)
        l = self._nivel_dia(b, nivel)
        if l is None or not r["ok"]:
            return r, None
        return r, self._con_nivel(b, l, lambda: s.lp(xhat, ehat))

    # ------------------------------------------------ cortes -- #
    def _expr_lp(self, b, r, xhat, ehat):
        """(intercepto, LinExpr de theta_b - pi.x - lam.e) del corte LP."""
        s, M = self.bd.subs[b], self.M
        ic = r["valor"] - sum(p * xhat[j] for j, p in zip(s.cop, r["pi"])) \
            - sum(l * ehat[k] for k, l in zip(s.pres, r["lam"]))
        expr = gp.LinExpr(M.th[b])
        for j, p in zip(s.cop, r["pi"]):
            if abs(p) > 1e-9:
                expr.addTerms(-p, M.x[M.pos[j]])
        for k, l in zip(s.pres, r["lam"]):
            if abs(l) > 1e-9:
                expr.addTerms(-l, M.e[k])
        return ic, expr

    def _apagar(self, b, l, Mb, expr):
        """Suma Mb * w_{y,l'} (l' > l) a la LinExpr de un corte que vale solo
        en los niveles <= l."""
        y = eval(self.P.claves[b])[0]
        for v in self.niv[y][2][l + 1:]:
            expr.addTerms(Mb, v)

    def _expr_nivel(self, b, r, xhat, ehat, l):
        """Corte LP del dia con el escalon del nivel l: theta_b >= v + pi (x -
        xhat) + lam (e - ehat) - Mb * sum_{l' > l} w_{y,l'}, con Mb tal que el
        corte apagado queda bajo la cota de theta_b en todo el rango."""
        s, M = self.bd.subs[b], self.M
        tope = r["valor"] - M.th[b].LB + 1.0
        for j, p in zip(s.cop, r["pi"]):
            if abs(p) > 1e-9:
                lo, hi = self.rangos[j]
                tope += max(p * (hi - xhat[j]), p * (lo - xhat[j]))
        for kk, lm in zip(s.pres, r["lam"]):
            if abs(lm) > 1e-9:
                lo, hi = self.rpres[kk]
                tope += max(lm * (hi - ehat[kk]), lm * (lo - ehat[kk]))
        if not (math.isfinite(tope) and tope < 1e12):
            self.n_sin_cota += 1
            return None
        ic, expr = self._expr_lp(b, r, xhat, ehat)
        self._apagar(b, l, max(tope, 1.0), expr)
        return expr, ic

    def _expr_local(self, b, R, r, xhat, nivel=None):
        """LinExpr y lado derecho del corte entero local en xhat (si nivel no
        es None, calculado con el escalon de ese nivel y apagado en los mayores)."""
        s, M = self.bd.subs[b], self.M
        pi, lam = r["pi"], r["lam"]

        def extremo(p, lo, hi):
            return 0.0 if p == 0 else (p * hi if p > 0 else p * lo)
        tope = R - M.th[b].LB + 1.0
        tope += sum(extremo(pi[k], *self.rangos[s.cop[k]]) for k in s.cop_cont)
        tope += sum(extremo(l, *self.rpres[kk]) for kk, l in zip(s.pres, lam))
        if not (math.isfinite(tope) and tope < 1e12):
            # Rango sin cota util: un big-M asi rompe la numerica del maestro.
            self.n_sin_cota += 1
            return None
        Mb = max(tope, 1.0)
        expr = gp.LinExpr(M.th[b])
        if nivel is not None:
            self._apagar(b, nivel, Mb, expr)
        for k in s.cop_cont:
            if abs(pi[k]) > 1e-9:
                expr.addTerms(-pi[k], M.x[M.pos[s.cop[k]]])
        for kk, l in zip(s.pres, lam):
            if abs(l) > 1e-9:
                expr.addTerms(-l, M.e[kk])
        cte = float(R)
        for k in s.cop_ent:
            j = s.cop[k]
            lo, hi, betas = self.bits[j]
            v = int(round(xhat[j])) - lo
            if v < 0 or v > hi - lo:
                return None
            # theta + Mb * dist >= R, con dist = sum_{bit 0} beta + sum_{bit 1} (1 - beta).
            for i, b in enumerate(betas):
                if (v >> i) & 1:
                    expr.addTerms(-Mb, b)
                    cte -= Mb
                else:
                    expr.addTerms(Mb, b)
        return expr, cte

    def _clave_ent(self, b, xhat):
        s = self.bd.subs[b]
        return tuple(int(round(xhat[s.cop[k]])) for k in s.cop_ent)

    # ------------------------------------------------ callback -- #
    def _callback(self, model, where):
        if where == GRB.Callback.MIP:
            bnd = model.cbGet(GRB.Callback.MIP_OBJBND)
            if (self.bd.UB < 1e20) and bnd >= self.bd.UB * (1 - self.gap_obj):
                model.terminate()
            if self._restante() <= 0:
                model.terminate()
            self._vigilar_incumbente(model.cbGet(GRB.Callback.MIP_OBJBST))
            return
        if where != GRB.Callback.MIPSOL:
            return
        self.n_cb += 1
        P, M, bd = self.P, self.M, self.bd
        obj = model.cbGet(GRB.Callback.MIPSOL_OBJ)
        self.vistas[round(obj, 2)] = self.n_cb
        if self._restante() <= 0:
            # Sin cortes Gurobi la acepta; la cota del arbol sigue valida.
            self.aceptadas.append(obj)
            model.terminate()
            return
        xs = model.cbGetSolution(M.x)
        es = model.cbGetSolution(M.e)
        ths = model.cbGetSolution(M.th)
        bnd = model.cbGet(GRB.Callback.MIPSOL_OBJBND)
        xhat = np.zeros(P.nv)
        xhat[P.maestro_vars] = xs
        for j in P.maestro_vars:
            if P.vtype[j] != "C":
                xhat[j] = round(xhat[j])
        ehat = np.array(es)
        nivel = self._niveles_punto(model.cbGetSolution(self.w_todas)) if self.w_todas else {}
        lv = {b: self._nivel_dia(b, nivel) for b in range(P.nb)}
        clave = (tuple(np.round(xhat[P.maestro_vars], 6)), tuple(np.round(ehat, 4)),
                 tuple(sorted(nivel.items())))
        # 1) cortes LP: con la envolvente (globales) y con el escalon del nivel
        if clave not in self.cache_lp:
            t = time.time()
            self.cache_lp[clave] = bd._paralelo(lambda b: self._lp_dia(b, xhat, ehat, nivel))
            self.t_lp += time.time() - t
        res = [r for r, _rn in self.cache_lp[clave]]
        resn = [rn if rn is not None and rn["ok"] else None for _r, rn in self.cache_lp[clave]]
        n_lp = n_niv = 0
        tol_abs = self.tol_lp * max(1.0, abs(obj))
        viol = 0.0                  # mayor violacion (absoluta) de los cortes agregados
        for b, r in enumerate(res):
            if not r["ok"]:
                continue
            if r["valor"] > ths[b] + self.tol * max(1.0, abs(r["valor"])):
                ic, expr = self._expr_lp(b, r, xhat, ehat)
                self._lazy(model, expr, ic)
                n_lp += 1
                viol = max(viol, r["valor"] - ths[b])
            rn = resn[b]
            if rn is not None and rn["valor"] > max(ths[b], r["valor"]) + self.tol * max(1.0, abs(rn["valor"])):
                c = self._expr_nivel(b, rn, xhat, ehat, lv[b])
                if c is not None:
                    self._lazy(model, c[0], c[1])
                    n_niv += 1
                    viol = max(viol, rn["valor"] - ths[b])
        self.n_lp += n_lp
        self.n_niv += n_niv
        # Con violacion bajo tol_abs los cortes se agregan igual, pero se sigue
        # con los enteros (ver tol_lp).
        if (n_lp or n_niv) and viol > tol_abs:
            self._registrar("lp", bnd, n_lp + n_niv, 0, xhat, nivel, viol)
            return
        # 2) UB de esta inversion (afuera de Gurobi), si promete
        costo_m = float(np.dot(P.obj[P.maestro_vars], xs) + P.objcon + sum(ths))
        ent_m = [j for j in P.maestro_vars if P.vtype[j] != "C"]
        if costo_m < bd.UB and tuple(int(round(xhat[j])) for j in ent_m) not in bd.ub_por_inversion \
                and self._restante() > self.ub_timelimit:
            self.evaluar_inversion(xhat, self.ub_timelimit, self.ub_gap)
        # 3) cortes enteros con el esquema de Riley et al. Cada corte se guarda
        # con los duales (pi, lam) con que se calculo su R: R depende de ellos,
        # y combinar el R de un lagrangiano con los duales de otro punto da un
        # corte invalido (paso en la primera version: LB sobre una UB conocida).
        K = len(self.alfas)
        pend = [b for b in range(P.nb) if res[b]["ok"]]
        n_ent = 0

        def valor(b, c):
            s = bd.subs[b]
            R, pi, lam = c
            return R + sum(pi[k] * xhat[s.cop[k]] for k in s.cop_cont) \
                + sum(l * ehat[kk] for kk, l in zip(s.pres, lam))

        def separar(b, st):
            """Agrega el corte guardado que mas viola en el punto; True si corta."""
            if not st["cortes"]:
                return False
            c = max(st["cortes"], key=lambda c: valor(b, c))
            v = valor(b, c)
            if v > ths[b] + max(tol_abs, self.tol * max(1.0, abs(v))):
                loc = self._expr_local(b, c[0], {"pi": c[1], "lam": c[2]}, xhat, lv[b])
                if loc is not None:
                    self._lazy(model, loc[0], loc[1])
                    return True
            return False

        # Primero, los cortes ya guardados para esta inversion entera (y este
        # nivel de capacidad: el corte del nivel l solo vale en los niveles <= l).
        sin_cortar = []
        for b in pend:
            st = self.estado.setdefault((b, self._clave_ent(b, xhat), lv[b]),
                                        {"l": 0, "t": self.t_inicial, "cortes": [], "final": False})
            if separar(b, st):
                n_ent += 1
            elif not st["final"]:
                sin_cortar.append(b)
        pend = [] if n_ent else sin_cortar
        while pend:
            if self._restante() <= 0:
                model.terminate()
                break

            def tarea(b):
                l = lv[b]
                st = self.estado[(b, self._clave_ent(b, xhat), l)]
                tl = min(st["t"], max(10.0, self._restante()))
                s = bd.subs[b]
                rc = [self.rangos[s.cop[k]] for k in s.cop_cont]
                base = res[b] if resn[b] is None else resn[b]
                pi, lam = list(base["pi"]), list(base["lam"])
                if l is None:
                    lg = s.local(xhat, pi, lam, rc, tl, self.alfas[st["l"]])
                else:
                    # Con el escalon del nivel: b_bar <= c_l y n_d(c_l) cargas.
                    i = s.cop_cont.index(self.kbb[b])
                    y = eval(P.claves[b])[0]
                    rc[i] = (rc[i][0], min(rc[i][1], self.niv[y][0][l]))
                    lg = self._con_nivel(b, l, lambda: s.local(xhat, pi, lam, rc, tl,
                                                               self.alfas[st["l"]]))
                return b, st, lg, pi, lam
            t = time.time()
            salidas = bd._paralelo(tarea, pend)
            self.t_milp += time.time() - t
            sigue = []
            for b, st, lg, pi, lam in salidas:
                if lg["ok"]:
                    st["cortes"].append((lg["R"], pi, lam))
                elif lg.get("status") in (GRB.INFEASIBLE, GRB.INF_OR_UNBD):
                    # Sin operacion entera con estas enteras: Q = +inf y vale
                    # cualquier R (con duales nulos el corte solo pesa en xhat).
                    s = bd.subs[b]
                    R = self.bd.UB if (self.bd.UB < 1e20) else 1e7
                    st["cortes"].append((R, [0.0] * s.n_cop, [0.0] * len(s.pres)))
                    st["final"] = True
                optimo = lg.get("status") == GRB.OPTIMAL
                if optimo and st["l"] == K - 1:
                    st["final"] = True
                elif not optimo and lg.get("status") == GRB.TIME_LIMIT:
                    st["t"] = 2 * st["t"]
                    st["l"] = min(st["l"] + 1, K - 1)
                    if st["t"] > self.t_max_dia:
                        st["final"] = True          # tope: se acepta con la mejor cota
                elif not optimo:
                    st["l"] = min(st["l"] + 1, K - 1)
                else:
                    st["l"] = min(st["l"] + 1, K - 1)
                if separar(b, st):
                    n_ent += 1
                elif not st["final"]:
                    sigue.append(b)
            if n_ent:
                break
            pend = sigue
        self.n_ent += n_ent
        if not n_ent:
            self.aceptadas.append(obj)
        self._registrar("entero" if n_ent else "acepta", bnd, n_lp + n_niv, n_ent, xhat, nivel, viol)

    def _lazy(self, model, expr, rhs):
        model.cbLazy(expr >= rhs)
        self.lazy.append((expr, float(rhs)))

    def _restante(self):
        return self.tiempo_max - (time.time() - self.t0)

    def _vigilar_incumbente(self, obst):
        """Gurobi rechaza una solucion solo si un corte lazy la viola mas que su
        tolerancia de factibilidad: si los cortes agregados la violan menos, la
        toma igual como incumbente sin avisar (caso 7: 15 cb, todos con cortes
        LP, y el arbol cerro). La cota sigue siendo valida, pero conviene
        saberlo: se avisa cada incumbente nuevo que el callback no acepto."""
        if obst >= 1e20 or obst >= self.obj_bst - 1e-9 * max(1.0, abs(obst)):
            return
        self.obj_bst = obst
        if any(abs(obst - v) <= 1e-7 * max(1.0, abs(v)) for v in self.aceptadas):
            return
        self.n_silenciosas += 1
        self._log(f"ALERTA aceptacion silenciosa ({self.n_silenciosas}): Gurobi tomo como incumbente "
                  f"{obst:,.2f}, una solucion a la que el callback le agrego cortes que no la "
                  f"violaban mas que la tolerancia")

    def _registrar(self, tipo, bnd, n_lp, n_ent, xhat, nivel=None, viol=0.0):
        fila = {"t": time.time() - self.t0, "tipo": tipo, "LB": bnd, "UB": self.bd.UB,
                "cortes_lp": self.n_lp, "cortes_nivel": self.n_niv,
                "cortes_enteros": self.n_ent, "nuevos_lp": n_lp,
                "nuevos_ent": n_ent, "callbacks": self.n_cb, "t_lp": self.t_lp,
                "t_milp": self.t_milp, "viol_lp": viol, "inversion": self.bd.inversion(xhat),
                "nivel_B": {y: round(self.niv[y][0][l], 1) for y, l in (nivel or {}).items()}}
        self.historia.append(fila)
        gap = (self.bd.UB - bnd) / self.bd.UB if (self.bd.UB < 1e20 and abs(bnd) < 1e20) else float("nan")

        def num(v):
            return f"{v:,.2f}" if abs(v) < 1e20 else "-"
        ref = getattr(self, "ub_referencia", None)
        if ref is not None and bnd > ref * (1 + 1e-6):
            self._log(f"ALERTA COTA INVALIDA: LB {bnd:,.2f} supera una solucion factible conocida "
                      f"({ref:,.2f})")
        self._log(f"cb {self.n_cb:>4} {tipo:7s} LB {num(bnd)}  UB {num(self.bd.UB)} ({gap:.3%})  "
                  f"cortes LP {self.n_lp} + nivel {self.n_niv} (+{n_lp}, viol {viol:.2g}) enteros {self.n_ent} (+{n_ent})  "
                  f"LP {self.t_lp:.0f}s MILP {self.t_milp:.0f}s  [{fila['inversion']}]  B<= "
                  + " ".join(f"{v:.0f}" for _y, v in sorted(fila["nivel_B"].items())))
        if self.out:
            with open(os.path.join(self.out, "lshaped.json"), "w", encoding="utf-8") as f:
                json.dump({"historia": self.historia}, f, indent=1)

    # ------------------------------------------------ fase 2 -- #
    def branch_and_cut(self, tiempo_max, gap_obj=1e-4, hilos=None):
        M = self.M
        self.tiempo_max, self.gap_obj = tiempo_max, gap_obj
        M._entero(True)
        m = M.m
        m.Params.LazyConstraints = 1
        m.Params.MIPGap = 0.0
        if (self.bd.UB < 1e20):
            m.Params.Cutoff = self.bd.UB
        if hilos:
            m.Params.Threads = hilos
        m.Params.TimeLimit = max(1.0, tiempo_max - (time.time() - self.t0))
        m.Params.OutputFlag = 1
        if self.out:
            m.Params.LogFile = os.path.join(self.out, "maestro_bc.log")
        # Si el arbol termina con un incumbente que el callback no acepto (Gurobi
        # lo tomo aunque se le agregaron cortes: casos 7 y 8, el arbol cerraba en
        # la cota LP sin cortes enteros), los cortes lazy pasan a restricciones
        # del maestro, que ya no admite esa solucion, y se reoptimiza.
        n_duras = 0
        while True:
            self.obj_bst = INF
            m.optimize(self._callback)
            if not m.SolCount:
                break
            self._vigilar_incumbente(m.ObjVal)
            obj = m.ObjVal
            if m.Status != GRB.OPTIMAL or any(abs(obj - v) <= 1e-7 * max(1.0, abs(v))
                                              for v in self.aceptadas):
                break
            viol = max((rhs - e.getValue() for e, rhs in self.lazy), default=0.0)
            xs = m.getAttr("X", m.getVars())
            vt = m.getAttr("VType", m.getVars())
            frac = max((abs(x - round(x)) for x, t in zip(xs, vt) if t != "C"), default=0.0)
            cb = self.vistas.get(round(obj, 2))
            self._log(f"incumbente final {obj:,.2f} no aceptado por el callback "
                      f"({f'visto en el cb {cb}' if cb else 'no paso por MIPSOL'}): violacion maxima de "
                      f"los {len(self.lazy)} cortes lazy {viol:.3g}, entera mas fraccionaria {frac:.1e}")
            if self._restante() <= 1.0 or viol <= self.tol_lp * max(1.0, abs(obj)):
                break           # sin tiempo, o los cortes no la excluyen
            for e, rhs in self.lazy:
                m.addLConstr(e, GRB.GREATER_EQUAL, rhs)
            n_duras += len(self.lazy)
            self.lazy = []
            m.Params.TimeLimit = max(1.0, self._restante())
            self._log(f"reinicio del arbol con {n_duras} cortes como restricciones del maestro")
        try:
            lb = m.ObjBound
        except gp.GurobiError:
            lb = None
        if m.Status == GRB.INFEASIBLE:
            # Con Cutoff = UB: ninguna inversion baja de la UB, que es optima.
            lb = self.bd.UB
        return lb, m.Status
