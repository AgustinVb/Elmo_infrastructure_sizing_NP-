"""L-shaped entero multicorte en branch-and-cut sobre BendersDias.

Portado de battery_swapping_multiaño (2026-10, lshaped_entero_swap.md),
adaptado a carga on-board. Esqueleto: el maestro y los subproblemas (año, dia)
de BendersDias, con sus presupuestos de energia (s_def), enlaces elasticos y la
cota global de cada dia (L_{y,d} = theta.LB de _cota_theta).

  BRANCH-AND-CUT con callback MIPSOL (Fischetti et al. 2017; Angulo et al.
    2016): en cada solucion entera del maestro,
      1. los LP del dia (cortes de Benders, BendersDias.Subproblema.lp); si
         alguno corta, se agregan con cbLazy y se sigue;
      2. si ninguno corta, el corte ENTERO local de cada dia
         (Subproblema.local: copias enteras fijas, continuas y presupuestos
         libres con costo -pi/-lam, cota dual del MILP). Es el L-shaped entero
         de Laporte y Louveaux con la parte continua tratada por dualidad
         lagrangiana en vez de discretizada: theta_b >= R + pi_C.x_C + lam.e
         - Mb * (distancia de las copias enteras a xhat). La distancia se
         escribe con la codificacion binaria de las copias enteras, declarada
         al inicio (en un callback no se pueden crear variables); las
         continuas sin discretizar. Un dia sin operacion entera con esas
         enteras (Q = +inf) da un corte que excluye esa combinacion: es lo que
         la cota operacional no ve (en DCH 2C, inversiones sin cargador en el
         año 1 con LP factible y MILP infactible);
      3. R es la MEJOR COTA del MILP: con el esquema de Riley, Daoutidis y
         Zhang (2025, Alg. 3) los MILP se cortan por gap (0,1; 0,01; 0) y
         tiempo (que se duplica si corta por tiempo), y se endurecen solo si
         el corte no separa. Cache por (dia, inversion entera).
    La UB se lleva AFUERA de Gurobi (dias MILP con la inversion fija + pulido
    LP): el incumbente interno del maestro tiene theta en cotas inferiores y no
    es una solucion. Aun si una solucion se acepta sin separarla del todo (tope
    de tiempo), la cota del arbol sigue siendo una LB valida: solo poda nodos
    con cota >= el valor aceptado, y esa cota no supera el optimo de su nodo.

DIFERENCIAS CON SWAP. Alla los dias llevaban el corte de cargas por LHD en
funcion de b_bar (cota_cargas_lhd) con niveles de capacidad binarios en el
maestro, la UB inicial salia de la DP de degradacion de swap y el pulido dejaba
libres las binarias auxiliares del McCormick (u_nf, z_bb). En carga on-board:
  - sin corte de cargas: la operacion es casi exacta en su relajacion
    (diagnostico_gap_dias.py, ver certificar_inversiones.py), asi que tampoco
    hay niveles de B_y;
  - sin DP: la UB inicial es la del incumbente (--solucion / --respaldo) o la
    de las inversiones que evalua el callback;
  - el McCormick de la degradacion es continuo (w_deg): el pulido es el LP de
    BendersDias.pulir con todas las enteras fijas.

CORTE DE PISO (OB, DCH 2C). El corte local deja libres las copias continuas
(b_bar, P_pot, G_g, H) con precio -pi: con b_bar libre un dia del año 7 con 1
cargador por estacion es factible y barato, aunque con el b_bar que la
degradacion deja ese año no hay operacion entera (status 3). La primera corrida
acepto esas inversiones y la LB no paso de 10,27 M. Para los dias sin operacion
con la inversion de prueba se calcula, por copia continua k,

    phi_k(xhat_I) = min { c_k : operacion entera, c_I = xhat_I, c_C en rangos }

(Subproblema.piso, cota dual) y se agrega

    x_k >= phi_k - (phi_k - lo_k) * dist(x_I, xhat_I)

Valido sin supuestos: todo x factible con x_I = xhat_I tiene x_k >= phi_k, y si
x_I != xhat_I la distancia es >= 1 y el lado derecho queda <= lo_k. Para b_bar
es ademas exacto en su eje: la factibilidad del dia es monotona en b_bar (en el
dia solo aparece en bmin*b_bar <= B <= b_bar; B' = B + bmin*(b' - b) es factible
con b' > b y no cambia la carga, la descarga ni el costo).
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
                 tol_lp=1e-5):
        self.bd, self.om, self.por_var = bd, om, por_var
        self.P, self.M = bd.P, bd.maestro
        self.alfas, self.t_inicial, self.t_max_dia = list(alfas), t_inicial, t_max_dia
        self.ub_timelimit, self.ub_gap, self.tol = ub_timelimit, ub_gap, tol
        # Violacion minima (absoluta, tol_lp * objetivo del maestro) para que un
        # corte cuente como separador. Gurobi no hace cumplir violaciones chicas
        # en filas con coeficientes grandes (duales de hasta 1e6): en swap (caso
        # 7) acepto una solucion con un corte violado en 0,26 y el arbol cerro
        # en la cota LP sin un corte entero.
        self.tol_lp = tol_lp
        self.out = out
        self.t0 = time.time()
        self.cache_lp = {}          # clave del punto -> resultados LP de los dias
        self.estado = {}            # (b, clave entera) -> {"l", "t", "cortes", "final"}
        self.n_lp = self.n_ent = self.n_cb = 0
        self.n_sin_cota = 0         # cortes descartados por un big-M sin cota util
        self.t_lp = self.t_milp = 0.0
        self.historia = []
        self.aceptadas = []         # objetivos de las soluciones que el callback no corto
        self.n_silenciosas = 0      # incumbentes de Gurobi que el callback si habia cortado
        self.obj_bst = INF
        self.tiempo_max = INF
        self.lazy = []              # (expr, rhs) de los cortes lazy, para reiniciar el arbol
        self.vistas = {}            # objetivo de cada solucion de MIPSOL -> numero de cb
        self.dias_malos = {}        # clave entera del maestro -> dias sin operacion
        self.pisos = {}             # (b, k, clave entera del dia) -> {"phi", "t", "final"}
        self.n_piso = 0
        self.t_piso = 0.0
        self.tol_piso = 1e-3        # violacion relativa minima (cb 1: 164,4 < 164,4)

    def _log(self, s):
        print(f"[LS] {s}", flush=True)

    # ------------------------------------------------ UB -- #
    def _cols(self, prefijo):
        """{año: indice} de las columnas del maestro 'prefijo(...,año)'."""
        P = self.P
        out = {}
        for j in P.maestro_vars:
            n = P.nombre[j]
            if n.startswith(prefijo + "("):
                # El MPS escribe los indices separados por '_': N_chargers(station_1_3).
                out[int(n[len(prefijo) + 1:-1].replace(",", "_").split("_")[-1])] = j
        return out

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
            if P.vtype[j] != "C":
                guard.append((v, v.LB, v.UB))
                v.LB = v.UB = float(round(x[j]))
        for k, v in enumerate(self._mcap_e):
            # Mismo signo que el presupuesto del maestro (la fila s_def resta
            # la energia del dia; los presupuestos salen negativos).
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
        optimista: los dias MILP gastan algo mas, la degradacion real deja la
        bateria algo mas chica y la operacion armada para el b_bar del maestro
        puede no caber en el pulido (diagnostico del caso 5 de swap). Se repite
        con b_bar = capacidad real - holgura hasta que alcance.

        P_pot tambien viene del maestro (operacion relajada) y en los meses de
        punta (P_red <= P_pot) puede no alcanzar para la operacion entera. Los
        dias que fallan se re-resuelven con P_pot libre hacia arriba, pagando
        su costo, y el doble de tiempo; P_pot[y] sube al maximo que pidan sus
        dias (el pulido la vuelve a optimizar). Devuelve (vec o None,
        resultados)."""
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
                    self._malos = malos
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
        """Dias MILP con la inversion fija (b_bar corregido, _dias_ub) + pulido
        LP. Devuelve (costo o None, vector de los dias). Cachea por la parte
        entera de la inversion."""
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
            self.dias_malos[clave] = self._malos
            return None, None
        costo, completo = bd.pulir(vec)
        self._log(f"   dias MILP (gap max {max(r['gap'] for r in res):.2%}) + pulido en "
                  f"{time.time() - t0:.0f}s -> {'infactible' if costo is None else f'{costo:,.2f}'}")
        bd.ub_por_inversion[clave] = costo
        if costo is not None and costo < bd.UB:
            bd.UB, bd.mejor_vec = costo, completo
            self._log(f"   *** nueva UB {costo:,.2f}")
        return costo, vec

    def mejorar_ub(self, timelimits=(600.0, 900.0), gap=1e-4):
        """Paso final (como mejorar_ub.py): con la inversion de la mejor UB fija,
        re-resuelve los dias MILP con mas tiempo y gap chico (la UB del
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
                costo, completo = bd.pulir(vec)
                aceptados = sorted(por_año)
                if costo is None or costo >= UB0:
                    vec, costo, completo, aceptados = xB, UB0, xB, []
                    for y in sorted(por_año, key=lambda y: -por_año[y]["mejora"]):
                        c_y, comp_y = bd.pulir(con(vec, y))
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

    # ------------------------------------------------ maestro -- #
    def preparar_maestro(self):
        """Rangos de copias y presupuestos (validos), codificacion binaria de
        las copias enteras y copia del maestro para la capacidad real."""
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
        # Laporte y Louveaux sobre la codificacion binaria).
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

    def _acotar_copias(self):
        """Cotas de las copias sin cota en el modelo (rango INF en el maestro).

        Subestacion: n_ssee_k no tiene cota (NonNegativeIntegers) y su rango en
        el maestro sale enorme, lo que infla la codificacion binaria y los
        big-M. Solo aparece en P_max_k = P_SSEE_STEP * n_ssee_k, con costo >= 0,
        y P_max_k solo acota sum_i P[k,i] <= sum_i Z_charge * p_charger <=
        N_chargers[k,y] * p_charger <= max_bays_k[k] * p_charger. Con
        n_ssee_k <= ceil(max_bays_k * p_charger / P_SSEE_STEP) no se corta el
        optimo.

        P_pot[y] tampoco tiene cota y su rango es INF: con un pi minusculo el
        big-M del corte entero llega a 1e78 y Gurobi acepta soluciones que
        violan los cortes (paso en swap, caso 3). P_pot solo aparece en P_red <=
        P_pot (meses de punta) y con costo >= 0, y P_red <= p_peak: P_pot <=
        p_peak por dominancia."""
        from pyomo.environ import value
        from src.optimization.functions import P_SSEE_STEP
        P, M, model = self.P, self.M, self.om.model
        p_charger, p_peak = value(model.p_charger), value(model.p_peak)
        for j in P.maestro_vars:
            n = P.nombre[j]
            if n.startswith("n_ssee_k("):
                k = n[len("n_ssee_k("):-1]
                bahias = value(model.max_bays_k[k])
                tope = float(math.ceil(bahias * p_charger / P_SSEE_STEP - 1e-9))
                txt = f"{bahias:g} bahias x {p_charger:g} kW"
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

    def _expr_local(self, b, R, r, xhat):
        """LinExpr y lado derecho del corte entero local en xhat."""
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
        for k in s.cop_cont:
            if abs(pi[k]) > 1e-9:
                expr.addTerms(-pi[k], M.x[M.pos[s.cop[k]]])
        for kk, l in zip(s.pres, lam):
            if abs(l) > 1e-9:
                expr.addTerms(-l, M.e[kk])
        c0 = self._sumar_distancia(b, xhat, Mb, expr)
        if c0 is None:
            return None
        return expr, float(R) - c0

    def _sumar_distancia(self, b, xhat, Mb, expr):
        """Suma a expr los terminos en beta de Mb * dist(x_I, xhat_I) del dia b
        y devuelve la constante Mb * (bits en 1 de xhat), que pasa al lado
        derecho; None si xhat cae fuera de la codificacion."""
        s = self.bd.subs[b]
        c0 = 0.0
        for k in s.cop_ent:
            j = s.cop[k]
            lo, hi, betas = self.bits[j]
            v = int(round(xhat[j])) - lo
            if v < 0 or v > hi - lo:
                return None
            # dist = sum_{bit 0} beta + sum_{bit 1} (1 - beta).
            for i, beta in enumerate(betas):
                if (v >> i) & 1:
                    expr.addTerms(-Mb, beta)
                    c0 += Mb
                else:
                    expr.addTerms(Mb, beta)
        return c0

    def _cortes_piso(self, model, xhat, malos):
        """Cortes de piso (ver el docstring del modulo) en los dias `malos`,
        sin operacion entera con la inversion de xhat. Un solo intento de
        t_inicial por piso (duplicando el tiempo, en DCH 2C cada callback tomaba
        1,3 h y la cota subia de a poco). Devuelve cuantos cortes agrego."""
        P, bd, M = self.P, self.bd, self.M
        por_dia = {}
        for b in malos:
            s = bd.subs[b]
            ce = self._clave_ent(b, xhat)
            for k in s.cop_cont:
                lo, hi = self.rangos[s.cop[k]]
                if hi - lo > 1e-6 * max(1.0, abs(hi)):
                    st = self.pisos.setdefault((b, k, ce), {"phi": -INF, "t": self.t_inicial,
                                                            "final": False})
                    por_dia.setdefault(b, []).append((k, st))
        n, txt = 0, []

        def separar():
            nonlocal n
            pend = {}
            for b, ks in por_dia.items():
                s = bd.subs[b]
                for k, st in ks:
                    j = s.cop[k]
                    lo = self.rangos[j][0]
                    phi = st["phi"]
                    if phi > xhat[j] + self.tol_piso * max(1.0, abs(phi)):
                        # x_k + (phi - lo) * dist >= phi
                        expr = gp.LinExpr(M.x[M.pos[j]])
                        c0 = self._sumar_distancia(b, xhat, phi - lo, expr)
                        if c0 is not None:
                            self._lazy(model, expr, phi - c0)
                            n += 1
                            txt.append(f"{P.claves[b]} {P.nombre[j]} {xhat[j]:.1f}<{phi:.1f}")
                            continue
                    if not st["final"]:
                        pend.setdefault(b, []).append((k, st))
            return pend

        pend = separar()
        while pend and not n and self._restante() > 0:
            def tarea(b):
                s = bd.subs[b]
                rc = [self.rangos[s.cop[k]] for k in s.cop_cont]
                return [(k, st, s.piso(xhat, k, rc, min(st["t"], max(10.0, self._restante()))))
                        for k, st in pend[b]]
            t = time.time()
            salidas = bd._paralelo(tarea, list(pend))
            self.t_piso += time.time() - t
            for filas in salidas:
                for k, st, r in filas:
                    if r["ok"]:
                        st["phi"] = max(st["phi"], r["phi"])
                    st["final"] = True
            por_dia = pend
            pend = separar()
        if n:
            self._log("   pisos: " + ", ".join(txt[:8]) + (f" (+{len(txt) - 8})" if len(txt) > 8 else ""))
        else:
            self._log("   pisos: ningun piso supera la inversion de prueba (" + ", ".join(
                f"{P.claves[b]} {P.nombre[bd.subs[b].cop[k]]} {xhat[bd.subs[b].cop[k]]:.1f}"
                f">={st['phi']:.1f}" for b, ks in por_dia.items() for k, st in ks[:2]) + ")")
        self.n_piso += n
        return n

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
        clave = (tuple(np.round(xhat[P.maestro_vars], 6)), tuple(np.round(ehat, 4)))
        # 1) cortes LP
        if clave not in self.cache_lp:
            t = time.time()
            self.cache_lp[clave] = bd._paralelo(lambda b: bd.subs[b].lp(xhat, ehat))
            self.t_lp += time.time() - t
        res = self.cache_lp[clave]
        n_lp = 0
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
        self.n_lp += n_lp
        # Con violacion bajo tol_abs los cortes se agregan igual, pero se sigue
        # con los enteros (ver tol_lp).
        if n_lp and viol > tol_abs:
            self._registrar("lp", bnd, n_lp, 0, xhat, viol)
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
        # corte invalido (paso en la primera version de swap: LB sobre una UB
        # conocida).
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
                loc = self._expr_local(b, c[0], {"pi": c[1], "lam": c[2]}, xhat)
                if loc is not None:
                    self._lazy(model, loc[0], loc[1])
                    return True
            return False

        # Primero, los cortes ya guardados para esta inversion entera.
        sin_cortar = []
        for b in pend:
            st = self.estado.setdefault((b, self._clave_ent(b, xhat)),
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
                st = self.estado[(b, self._clave_ent(b, xhat))]
                tl = min(st["t"], max(10.0, self._restante()))
                s = bd.subs[b]
                rc = [self.rangos[s.cop[k]] for k in s.cop_cont]
                pi, lam = list(res[b]["pi"]), list(res[b]["lam"])
                lg = s.local(xhat, pi, lam, rc, tl, self.alfas[st["l"]])
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
            # 4) cortes de piso, solo si los enteros no separan: es el caso en que
            # la primera corrida acepto inversiones sin operacion en el año 7.
            # Antes de los enteros se gastaban horas en pisos de inversiones que
            # el corte entero saca en segundos (0 cargadores el año 1).
            malos = self.dias_malos.get(tuple(int(round(xhat[j])) for j in ent_m))
            if malos and self._cortes_piso(model, xhat, malos):
                self._registrar("piso", bnd, n_lp, 0, xhat, viol)
                return
            self.aceptadas.append(obj)
        self._registrar("entero" if n_ent else "acepta", bnd, n_lp, n_ent, xhat, viol)

    def _lazy(self, model, expr, rhs):
        model.cbLazy(expr >= rhs)
        self.lazy.append((expr, float(rhs)))

    def _restante(self):
        return self.tiempo_max - (time.time() - self.t0)

    def _vigilar_incumbente(self, obst):
        """Gurobi rechaza una solucion solo si un corte lazy la viola mas que su
        tolerancia de factibilidad: si los cortes agregados la violan menos, la
        toma igual como incumbente sin avisar (swap, caso 7). La cota sigue
        siendo valida, pero conviene saberlo: se avisa cada incumbente nuevo que
        el callback no acepto."""
        if obst >= 1e20 or obst >= self.obj_bst - 1e-9 * max(1.0, abs(obst)):
            return
        self.obj_bst = obst
        if any(abs(obst - v) <= 1e-7 * max(1.0, abs(v)) for v in self.aceptadas):
            return
        self.n_silenciosas += 1
        self._log(f"ALERTA aceptacion silenciosa ({self.n_silenciosas}): Gurobi tomo como incumbente "
                  f"{obst:,.2f}, una solucion a la que el callback le agrego cortes que no la "
                  f"violaban mas que la tolerancia")

    def _registrar(self, tipo, bnd, n_lp, n_ent, xhat, viol=0.0):
        fila = {"t": time.time() - self.t0, "tipo": tipo, "LB": bnd, "UB": self.bd.UB,
                "cortes_lp": self.n_lp, "cortes_enteros": self.n_ent, "nuevos_lp": n_lp,
                "nuevos_ent": n_ent, "cortes_piso": self.n_piso, "t_piso": self.t_piso,
                "callbacks": self.n_cb, "t_lp": self.t_lp,
                "t_milp": self.t_milp, "viol_lp": viol, "inversion": self.bd.inversion(xhat)}
        self.historia.append(fila)
        gap = (self.bd.UB - bnd) / self.bd.UB if (self.bd.UB < 1e20 and abs(bnd) < 1e20) else float("nan")

        def num(v):
            return f"{v:,.2f}" if abs(v) < 1e20 else "-"
        ref = getattr(self, "ub_referencia", None)
        if ref is not None and bnd > ref * (1 + 1e-6):
            self._log(f"ALERTA COTA INVALIDA: LB {bnd:,.2f} supera una solucion factible conocida "
                      f"({ref:,.2f})")
        self._log(f"cb {self.n_cb:>4} {tipo:7s} LB {num(bnd)}  UB {num(self.bd.UB)} ({gap:.3%})  "
                  f"cortes LP {self.n_lp} (+{n_lp}, viol {viol:.2g}) enteros {self.n_ent} (+{n_ent}) "
                  f"pisos {self.n_piso}  "
                  f"LP {self.t_lp:.0f}s MILP {self.t_milp:.0f}s  [{fila['inversion']}]")
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
        # lo tomo aunque se le agregaron cortes: swap, casos 7 y 8), los cortes
        # lazy pasan a restricciones del maestro, que ya no admite esa solucion,
        # y se reoptimiza.
        n_duras = 0
        while True:
            self.obj_bst = INF
            m.optimize(lambda mod, where: self._callback(mod, where))  # gurobipy 10 no acepta metodos
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
