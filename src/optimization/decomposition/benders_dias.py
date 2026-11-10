"""Benders de UN nivel por (año, dia), con regularizacion level-set.

Adaptacion de Pecci y Jenkins, "Regularized Benders Decomposition for High
Performance Capacity Expansion Models", IEEE TPWRS 2025, al monolitico de
Mina_modelo. En vez del Benders anidado (año a año, en serie), un maestro con
la PLANIFICACION de todo el horizonte y un subproblema por cada (año, dia), que
se resuelven todos a la vez.

LA PARTICION sale del MPS del monolitico y de la clasificacion year_day de
gcg_block (la misma que uso GCG). Medido a 4 años:

  maestro      78 variables sin indice de dia (44 enteras: X, N_*, Delta_*,
               n_ssee_k, R, flota) y las 109 filas que no tienen dia: stock,
               limites de inversion y la cadena de degradacion.
  subproblema  las ~15.000 variables de un (año, dia) y sus filas. Cada dia
               COPIA 8-10 variables del maestro: X, N_bays, N_chargers,
               N_batteries, n_ssee_k, P_pot, G_g, H, b_bar.
  presupuestos la UNICA fila que cruza dias es energy_consumed_def (una por
               año): EnergyConsumed_y = sum_d energia_d. Como las variables de
               presupuesto del paper (ec. 9-10), se parte en e_{y,d} del
               maestro -- EnergyConsumed_y = sum_d e_{y,d} -- y energia_d = e_{y,d}
               en cada subproblema.

LOS ENLACES son elasticos: copia + s+ - s- = x_maestro, con s penalizada en M.
Asi todo subproblema es factible para cualquier x (sin M, una inversion chica
dejaria sin produccion al dia) y el corte trae pendiente M hacia la region
factible, que hace de corte de factibilidad. Con M mayor que cualquier dual
legitimo, el penalizado tiene el mismo optimo que el original. Es lo que hace
el paper con las holguras de CO2 y de almacenamiento.

TRES ETAPAS (el Algoritmo 3 del paper, mas una tercera propia):

  1  maestro y dias CONTINUOS, cortes LP, regularizacion de punto interior
     (barrier sin crossover sobre el conjunto de nivel). Converge a la
     relajacion lineal; sus cortes valen para el problema entero.
  2  maestro ENTERO, dias LP. Converge a la COTA OPERACIONAL (inversion entera,
     operacion relajada): es la validacion de la particion.
  3  maestro entero, cortes FORTALECIDOS (Zou et al.) de los dias MILP: el
     lagrangiano del dia con las copias libres y el multiplicador LP. Esta es la
     que puede subir la LB por encima de la cota operacional.

  La UB se evalua para cada inversion entera nueva: los dias MILP con la
  inversion fija (en paralelo) y un pulido LP sobre el monolitico con todas las
  enteras fijas, que reconcilia la degradacion con la energia real.

LEVEL-SET (paper, ec. 15). Con L la cota del maestro y U la mejor cota
superior, el proximo punto de prueba es un punto INTERIOR de

    { planificacion factible, cortes, costo aproximado <= L + alpha (U - L) }

en vez del optimo del maestro, que oscila entre extremos. Con enteras, como en
la etapa 2 del paper, las enteras se fijan en la solucion del maestro y solo se
centran las continuas.
"""
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

import gurobipy as gp
import numpy as np
from gurobipy import GRB

from src.optimization.decomposition import gcg_block as gb

INF = GRB.INFINITY


def _gap(m):
    """MIPGap, o 0 si el modelo es un LP (el bloque red de los macrobloques no tiene
    enteras y Gurobi no da MIPGap ni ObjBound de un LP)."""
    return m.MIPGap if m.IsMIP else 0.0


def _cota(m):
    """Mejor cota dual: ObjBound del MIP, o el optimo si es un LP; None si no hay."""
    try:
        return m.ObjBound if m.IsMIP else (m.ObjVal if m.Status == GRB.OPTIMAL else None)
    except gp.GurobiError:
        return None


def _env(threads=0):
    env = gp.Env(empty=True)
    env.setParam("OutputFlag", 0)
    if threads:
        env.setParam("Threads", threads)
    env.start()
    return env


# --------------------------------------------------------------------------
# Particion del monolitico
# --------------------------------------------------------------------------

class Particion:
    """Lee el MPS del monolitico y reparte filas y columnas en maestro y
    bloques (año, dia). El modelo leido (F) queda para el pulido de la UB.

    por_estacion (MACROBLOQUES, como run_macrobloques_decomposicion.py de la rama
    battery_swapping): cada (año, dia) se parte en un bloque por estacion -- su
    inventario, bahias, cargadores, sus LHD (StationAssignment) y sus nodos, que
    no se comparten -- y un bloque "red" con lo que no es de ninguna estacion (red,
    generacion, BESS). Las filas del dia que cruzan bloques:
      ACOPLE_MAESTRO  al maestro, con un presupuesto por bloque: power_balance
                      (potencia de cada estacion en cada intervalo) y
                      daily_extraction (produccion de cada estacion).
      RELAJAR         fuera de la descomposicion (relajacion: la LB sigue valida;
                      el pulido, sobre el monolitico, las exige en la UB):
                      peak_power_swap (sum_k Sv <= n_peak_max, holgada con <= 10
                      cargadores) y min_charges_cut (corte valido del dia entero;
                      LShaped pone uno por estacion).
    Cualquier otra fila que cruce estaciones aborta."""

    ACOPLE_MAESTRO = ("power_balance", "daily_extraction")
    RELAJAR = ("peak_power_swap", "min_charges_cut")
    RED = "red"

    def __init__(self, info, model, por_var, env, por_estacion=False):
        t0 = time.time()
        F = gp.read(info["mps"], env=env)
        F.update()
        self.F = F
        vs, cs = F.getVars(), F.getConstrs()
        self.nv, self.nr = len(vs), len(cs)
        self.nombre = [v.VarName for v in vs]
        self.lb = np.array(F.getAttr("LB", vs))
        self.ub = np.array(F.getAttr("UB", vs))
        self.obj = np.array(F.getAttr("Obj", vs))
        self.vtype = list(F.getAttr("VType", vs))
        self.sense = list(F.getAttr("Sense", cs))
        self.rhs = np.array(F.getAttr("RHS", cs))
        self.cname = [c.ConstrName for c in cs]
        self.objcon = F.ObjCon
        self.filas = []
        for c in cs:
            e = F.getRow(c)
            n = e.size()
            self.filas.append((np.fromiter((e.getVar(k).index for k in range(n)), int, n),
                               np.fromiter((e.getCoeff(k) for k in range(n)), float, n)))

        with open(info["decomp"], encoding="utf-8") as f:
            dec = json.load(f)
        self.claves = dec["meta"]["block_keys"]
        self.nb = len(self.claves)
        blk_de_fila = {}
        for b, filas in dec["blocks"].items():
            for r in filas:
                blk_de_fila[r] = int(b)
        self.row_blk = np.array([blk_de_fila.get(n, -1) for n in self.cname])

        # Columnas: de dia (bloque por su clave (año, dia)) o del maestro.
        pos_d = gb._positions(model, set(model.days))
        pos_y = (gb._positions(model, set(model.years)) if len(model.years) > 1 else {})
        clave_a_b = {k: i for i, k in enumerate(self.claves)}
        self.var_blk = np.full(self.nv, -1)
        for j, n in enumerate(self.nombre):
            vd = por_var.get(n)
            if vd is None:
                continue
            d = gb._key(vd, pos_d)
            if d is None:
                continue
            y = gb._key(vd, pos_y) if pos_y else None
            self.var_blk[j] = clave_a_b[str((y, d))]

        self.por_estacion = por_estacion
        self.n_relajadas, self.punta = {}, {}
        if por_estacion:
            self._partir_estaciones(model, por_var)

        self.maestro_vars = np.where(self.var_blk < 0)[0]
        self.locales = {b: np.where(self.var_blk == b)[0] for b in range(self.nb)}
        self.filas_bloque = {b: np.where(self.row_blk == b)[0] for b in range(self.nb)}
        # row_blk -2: filas RELAJAR, fuera del maestro y de los bloques.
        self.filas_maestro = np.where(self.row_blk == -1)[0]

        # Filas de maestro con variables de dia -> presupuestos (fila, bloque).
        self.presupuestos = []
        self.pres_de_fila = {}
        for i in self.filas_maestro:
            idx, _ = self.filas[i]
            bls = sorted(set(self.var_blk[idx]) - {-1})
            for b in bls:
                self.pres_de_fila.setdefault(i, []).append(len(self.presupuestos))
                self.presupuestos.append((int(i), int(b)))
        self.pres_de_bloque = {b: [k for k, (_, bb) in enumerate(self.presupuestos) if bb == b]
                               for b in range(self.nb)}
        # Tipo de cada presupuesto (familia de su fila): energy_consumed_def es la
        # energia de la degradacion; con por_estacion tambien power_balance y
        # daily_extraction.
        self.pres_tipo = [self.familia_fila(i) for i, _b in self.presupuestos]
        self.pres_energia = [k for k, t in enumerate(self.pres_tipo) if t == "energy_consumed_def"]
        # Piso de produccion en la UB ({presupuesto: valor}): con por_estacion cada
        # estacion produce al menos su parte de la meta del dia (piso_natural); quien
        # evalua puede fijar otro reparto (p. ej. el de la incumbente) en piso.
        self.piso_natural = {}
        if por_estacion:
            self._pisos_produccion(model)
        self.piso = dict(self.piso_natural)

        # Copias: variables del maestro que aparecen en filas de un bloque.
        copias = {b: set() for b in range(self.nb)}
        for b in range(self.nb):
            for i in self.filas_bloque[b]:
                idx, _ = self.filas[i]
                for j in idx[self.var_blk[idx] < 0]:
                    copias[b].add(int(j))
        self.copias = {b: sorted(s) for b, s in copias.items()}

        # Chequeo: una fila de bloque solo tiene variables de dia de SU bloque.
        for b in range(self.nb):
            for i in self.filas_bloque[b]:
                idx, _ = self.filas[i]
                otros = set(self.var_blk[idx]) - {-1, b}
                if otros:
                    raise RuntimeError(f"fila {self.cname[i]} del bloque {b} toca {otros}")
        self.t_lectura = time.time() - t0

    def familia_fila(self, i):
        """'c_e_power_balance(7_15_3)_' -> 'power_balance'."""
        mm = gb.DECORACION.match(self.cname[i])
        return (mm.group("label") if mm else self.cname[i]).split("(")[0]

    def _partir_estaciones(self, model, por_var):
        """Bloques (año, dia) -> (año, dia, estacion) y (año, dia, "red"); ver la clase."""
        estaciones = sorted(model.stations_set)
        est_de_lhd = {i: k for (k, i) in model.ZSWAP_INDEX}
        pos_k = gb._positions(model, set(estaciones))
        pos_i = gb._positions(model, set(model.lhd_set))
        viejas = self.claves
        sub = np.full(self.nv, -1)
        nuevas = {}
        for j, n in enumerate(self.nombre):
            b = self.var_blk[j]
            if b < 0:
                continue
            vd = por_var[n]
            k = gb._key(vd, pos_k)
            if k is None:
                i = gb._key(vd, pos_i)
                if i is not None:
                    k = est_de_lhd.get(i)
                    if k is None:
                        raise RuntimeError(f"{n}: el LHD {i} no tiene estacion (ZSWAP_INDEX)")
            y, d = eval(viejas[b])
            clave = str((y, d, k if k is not None else self.RED))
            sub[j] = nuevas.setdefault(clave, len(nuevas))
        filas = self.row_blk.copy()
        for i in np.where(self.row_blk >= 0)[0]:
            idx, _ = self.filas[i]
            bls = set(sub[idx][sub[idx] >= 0].tolist())
            if len(bls) == 1:
                filas[i] = bls.pop()
                continue
            fam = self.familia_fila(i)
            if not bls or (fam not in self.ACOPLE_MAESTRO and fam not in self.RELAJAR):
                raise RuntimeError(f"fila {self.cname[i]} ({fam}) cruza los bloques {sorted(bls)} "
                                   "y no esta en ACOPLE_MAESTRO ni en RELAJAR")
            filas[i] = -1 if fam in self.ACOPLE_MAESTRO else -2
            if fam in self.RELAJAR:
                self.n_relajadas[fam] = self.n_relajadas.get(fam, 0) + 1
        self.var_blk, self.row_blk = sub, filas
        self.claves = [c for c, _b in sorted(nuevas.items(), key=lambda kv: kv[1])]
        self.nb = len(self.claves)
        # Punta del dia: P_red(y,d,t) <= P_pot(y) (power_peak_limit, en el bloque red).
        self.punta = {}
        for i in np.where(self.row_blk >= 0)[0]:
            if self.familia_fila(i) != "power_peak_limit":
                continue
            idx, _ = self.filas[i]
            red = [int(j) for j in idx if self.nombre[j].startswith("P_red(")]
            pot = [int(j) for j in idx if self.nombre[j].startswith("P_pot(")]
            if len(red) == 1 and len(pot) == 1:
                self.punta[red[0]] = pot[0]

    def _pisos_produccion(self, model):
        """piso_natural: cada estacion produce al menos la meta de SUS nodos (m_j de los
        nodos que visitan sus LHD) como fraccion de la meta del dia; los pisos suman la
        meta, asi que la operacion de las estaciones cumple daily_extraction."""
        from pyomo.environ import value
        est_de_lhd = {i: k for (k, i) in model.ZSWAP_INDEX}
        est_de_nodo = {}
        for idx in model.Y:
            est_de_nodo.setdefault(idx[1], est_de_lhd.get(idx[0]))
        meta, tot = {}, {}
        for y in model.years:
            for j in model.nodes_set:
                mj = float(value(model.m_j[j, y]))
                tot[y] = tot.get(y, 0.0) + mj
                k = est_de_nodo.get(j)
                if k is not None:
                    meta[(y, k)] = meta.get((y, k), 0.0) + mj
        for kk, (i, b) in enumerate(self.presupuestos):
            if self.pres_tipo[kk] != "daily_extraction":
                continue
            if self.sense[i] != GRB.GREATER_EQUAL:
                raise RuntimeError(f"{self.cname[i]}: se esperaba una fila >=")
            y, _d, k = eval(self.claves[b])
            parte = meta.get((y, k), 0.0) / tot[y] if tot.get(y) else 0.0
            self.piso_natural[kk] = float(self.rhs[i]) * parte

    def resumen(self):
        n_ent = sum(1 for j in self.maestro_vars if self.vtype[j] != "C")
        extra = ""
        if self.por_estacion:
            tipos = {}
            for t in self.pres_tipo:
                tipos[t] = tipos.get(t, 0) + 1
            extra = (f"; macrobloques por estacion: presupuestos {tipos}, filas relajadas "
                     f"{self.n_relajadas}, {len(self.punta)} intervalos de punta")
        return (f"{self.nv:,} columnas, {self.nr:,} filas; maestro {len(self.maestro_vars)} "
                f"variables ({n_ent} enteras) y {len(self.filas_maestro)} filas; {self.nb} "
                f"bloques; {len(self.presupuestos)} presupuestos; copias por bloque "
                f"{min(len(c) for c in self.copias.values())}-"
                f"{max(len(c) for c in self.copias.values())}" + extra)

    def energia(self, vec, k):
        """Valor del presupuesto k (la parte del bloque en su fila) en `vec`."""
        i, b = self.presupuestos[k]
        idx, coef = self.filas[i]
        sel = self.var_blk[idx] == b
        return float(np.dot(coef[sel], vec[idx[sel]]))


# --------------------------------------------------------------------------
# Subproblema de un (año, dia)
# --------------------------------------------------------------------------

class Subproblema:
    """El dia b como modelo de gurobipy persistente. Modos:

      lp     todo continuo, enlaces elasticos (s cuesta M): valor y duales
             para el corte LP.
      libre  todo continuo, copias libres: cota inferior de theta_b.
      ub     enteras, copias del maestro FIJAS (s = 0), presupuesto libre: la
             operacion real del dia con esa inversion.
      lagr   enteras, copias libres dentro de sus rangos y con costo -pi: el
             lagrangiano del corte fortalecido.
    """

    def __init__(self, P, b, M, threads):
        self.P, self.b, self.M = P, b, M
        self.env = _env(threads)
        m = gp.Model(env=self.env)
        loc = P.locales[b]
        self.loc = loc
        self.vloc = [m.addVar(lb=P.lb[j], ub=P.ub[j], obj=P.obj[j], vtype=P.vtype[j])
                     for j in loc]
        mapa = dict(zip(loc.tolist(), self.vloc))
        self.cop = P.copias[b]
        self.vcop = [m.addVar(lb=P.lb[j], ub=P.ub[j], vtype=P.vtype[j]) for j in self.cop]
        mapa.update(zip(self.cop, self.vcop))
        for i in P.filas_bloque[b]:
            idx, coef = P.filas[i]
            m.addLConstr(gp.LinExpr(coef.tolist(), [mapa[j] for j in idx]),
                         P.sense[i], float(P.rhs[i]))
        self.pres = P.pres_de_bloque[b]
        self.vpres = []
        for k in self.pres:
            i, _ = P.presupuestos[k]
            idx, coef = P.filas[i]
            sel = P.var_blk[idx] == b
            e = m.addVar(lb=-INF, ub=INF)
            m.addLConstr(gp.LinExpr(coef[sel].tolist(), [mapa[j] for j in idx[sel]]) - e,
                         GRB.EQUAL, 0.0)
            self.vpres.append(e)
        # Enlaces elasticos: copia + s+ - s- = valor del maestro.
        self.enl, self.sp, self.sm = [], [], []
        for v in self.vcop + self.vpres:
            sp_, sm_ = m.addVar(obj=M), m.addVar(obj=M)
            self.enl.append(m.addLConstr(v + sp_ - sm_, GRB.EQUAL, 0.0))
            self.sp.append(sp_)
            self.sm.append(sm_)
        self.slacks = self.sp + self.sm
        self.n_cop = len(self.vcop)
        m.update()
        tipos = [P.vtype[j] for j in loc] + [P.vtype[j] for j in self.cop]
        self.enteras = [v for v, t in zip(self.vloc + self.vcop, tipos) if t != GRB.CONTINUOUS]
        self.vt_orig = [t for t in tipos if t != GRB.CONTINUOUS]
        self.ent_loc = [k for k, j in enumerate(loc) if P.vtype[j] != GRB.CONTINUOUS]
        # Copias enteras (X, N_*, n_ssee_k) y continuas (P_pot, G_g, H, b_bar),
        # por posicion en self.cop.
        self.cop_ent = [k for k, j in enumerate(self.cop) if P.vtype[j] != GRB.CONTINUOUS]
        self.cop_cont = [k for k, j in enumerate(self.cop) if P.vtype[j] == GRB.CONTINUOUS]
        self.m = m
        self._entero = True
        self.start = None           # enteras locales de la ultima operacion factible
        self._costo_propio()

    def _costo_propio(self):
        """Macrobloque de estacion (P.por_estacion): en la UB y en la Fase 1 de un año
        la estacion no ve la red (su potencia es un presupuesto libre), asi que paga
        aparte lo que le toca: la energia de su potencia al precio de P_red y una punta
        propia pk >= su potencia en los intervalos de punta, con el costo de P_pot (la
        suma de las puntas de las estaciones acota la del dia; el pulido la recalcula
        exacta). En lp, lagr y local pk queda libre y sin costo: no cambia nada."""
        P, b = self.P, self.b
        self.obj_base = np.array([float(P.obj[j]) for j in self.loc])
        self.obj_ub = self.obj_base.copy()
        self.pk, self.c_pk = None, 0.0
        if not P.por_estacion:
            return
        pos = {int(j): kk for kk, j in enumerate(self.loc)}
        punta = []
        for k in self.pres:
            if P.pres_tipo[k] != "power_balance":
                continue
            i, _ = P.presupuestos[k]
            idx, coef = P.filas[i]
            red = [(int(j), a) for j, a in zip(idx, coef) if P.nombre[j].startswith("P_red(")]
            if len(red) != 1:
                continue
            j_red, a = red[0]
            sel = P.var_blk[idx] == b
            # Fila: a P_red + ... + (parte de la estacion) = 0 -> potencia = -parte / a.
            dem = [(pos[int(j)], -al / a) for j, al in zip(idx[sel], coef[sel])]
            for p, w in dem:
                self.obj_ub[p] += float(P.obj[j_red]) * w
            if j_red in P.punta:
                punta.append((dem, P.punta[j_red]))
        if punta:
            self.pk = self.m.addVar(lb=0.0)
            self.c_pk = float(P.obj[punta[0][1]])
            for dem, _jp in punta:
                self.m.addLConstr(gp.LinExpr([w for _, w in dem], [self.vloc[p] for p, _ in dem])
                                  - self.pk, GRB.LESS_EQUAL, 0.0)
        self.m.update()

    def _modo_ub(self, si):
        if not self.P.por_estacion:
            return
        self.m.setAttr("Obj", self.vloc, (self.obj_ub if si else self.obj_base).tolist())
        if self.pk is not None:
            self.pk.Obj = self.c_pk if si else 0.0

    def _pisos(self):
        """Presupuestos con piso en la UB (P.piso: produccion de la estacion >= piso):
        en el enlace e + s+ - s- = piso, s+ = 0."""
        P = self.P
        for kk, k in enumerate(self.pres):
            v = P.piso.get(k)
            if v is not None:
                self.enl[self.n_cop + kk].RHS = float(v)
                self.sp[self.n_cop + kk].UB = 0.0

    def _valor(self, x):
        """Costo de la operacion con los coeficientes del modelo (sin el costo propio)."""
        return float(np.dot(self.obj_base, x))

    # ---------------------------------------------------------------- #
    def _entera(self, si):
        if si != self._entero:
            self.m.setAttr("VType", self.enteras,
                           self.vt_orig if si else [GRB.CONTINUOUS] * len(self.enteras))
            self._entero = si

    def _rhs(self, xhat, ehat):
        vals = [float(xhat[j]) for j in self.cop] + [float(ehat[k]) for k in self.pres]
        self.m.setAttr("RHS", self.enl, vals)

    def _enlaces(self, costo_cop, ub_cop, costo_pres, ub_pres):
        n = self.n_cop
        np_ = len(self.pres)
        self.m.setAttr("Obj", self.slacks, ([costo_cop] * n + [costo_pres] * np_) * 2)
        self.m.setAttr("UB", self.slacks, ([ub_cop] * n + [ub_pres] * np_) * 2)

    def _copias(self, costos=None, lbs=None, ubs=None):
        P = self.P
        self.m.setAttr("Obj", self.vcop, costos if costos is not None else [0.0] * self.n_cop)
        self.m.setAttr("LB", self.vcop, lbs if lbs is not None else [P.lb[j] for j in self.cop])
        self.m.setAttr("UB", self.vcop, ubs if ubs is not None else [P.ub[j] for j in self.cop])

    # ---------------------------------------------------------------- #
    def lp(self, xhat, ehat):
        m = self.m
        self._entera(False)
        self._enlaces(self.M, INF, self.M, INF)
        self._copias()
        self._rhs(xhat, ehat)
        m.Params.Method = -1
        m.Params.TimeLimit = INF
        m.optimize()
        if m.Status != GRB.OPTIMAL:
            return {"ok": False, "status": m.Status}
        pi = m.getAttr("Pi", self.enl)
        return {"ok": True, "valor": m.ObjVal, "pi": pi[:self.n_cop],
                "lam": pi[self.n_cop:],
                "holgura": sum(m.getAttr("X", self.slacks))}

    def libre(self):
        m = self.m
        self._entera(False)
        self._enlaces(0.0, INF, 0.0, INF)
        self._copias()
        m.optimize()
        return m.ObjVal if m.Status == GRB.OPTIMAL else None

    def ub(self, xhat, timelimit, gap, mip_focus=1, subir=None):
        """Operacion MILP del dia con la inversion `xhat` fija. `subir` =
        {posicion en self.cop: costo}: copias que pueden quedar SOBRE xhat
        pagando ese costo por unidad (la holgura s- del enlace)."""
        m = self.m
        self._entera(True)
        self._enlaces(self.M, 0.0, 0.0, INF)
        if subir:
            sm = [self.sm[k] for k in subir]
            m.setAttr("UB", sm, [INF] * len(sm))
            m.setAttr("Obj", sm, list(subir.values()))
        self._copias()
        self._rhs(xhat, np.zeros(max(self.pres, default=-1) + 1))
        self._pisos()
        self._modo_ub(True)
        if self.start is not None:
            m.setAttr("Start", [self.vloc[k] for k in self.ent_loc], self.start)
        m.Params.TimeLimit = timelimit
        m.Params.MIPGap = gap
        m.Params.MIPFocus = mip_focus
        t0 = time.time()
        try:
            m.optimize()
        finally:
            m.Params.MIPFocus = 0
            self._modo_ub(False)
        if m.SolCount == 0:
            return {"ok": False, "status": m.Status, "t": time.time() - t0}
        x = m.getAttr("X", self.vloc)
        self.start = [round(x[k]) for k in self.ent_loc]
        valor = self._valor(x) if self.P.por_estacion else m.ObjVal
        return {"ok": True, "valor": valor, "x": x, "t": time.time() - t0,
                "gap": _gap(m), "cop": m.getAttr("X", self.vcop)}

    def fase1(self, costos, lbs, ubs, timelimit, gap):
        """Fase 1 del algoritmo de un año (run_macrobloques_decomposicion.py de la rama
        battery_swapping) en el macrobloque: la infraestructura de sus copias LIBRE en
        [lbs, ubs] con costo `costos` por unidad, la operacion entera con el costo propio
        de la UB y la produccion sobre su piso. Devuelve las copias de la mejor
        operacion encontrada (la infraestructura que pide este dia)."""
        m = self.m
        self._entera(True)
        self._enlaces(0.0, INF, 0.0, INF)
        self._copias(list(costos), list(lbs), list(ubs))
        self._rhs(np.zeros(self.P.nv), np.zeros(max(self.pres, default=-1) + 1))
        self._pisos()
        self._modo_ub(True)
        if self.start is not None:
            m.setAttr("Start", [self.vloc[k] for k in self.ent_loc], self.start)
        m.Params.TimeLimit = timelimit
        m.Params.MIPGap = gap
        t0 = time.time()
        try:
            m.optimize()
        finally:
            self._modo_ub(False)
        if m.SolCount == 0:
            self._copias()
            return {"ok": False, "status": m.Status, "t": time.time() - t0}
        x = m.getAttr("X", self.vloc)
        cop = m.getAttr("X", self.vcop)
        self._copias()
        return {"ok": True, "cop": cop, "x": x, "valor": self._valor(x), "t": time.time() - t0,
                "gap": _gap(m), "status": m.Status}

    def rango_presupuestos(self, entero_tl=None):
        """[min, max] de cada presupuesto (energia del dia) con la inversion
        libre, relajado: cualquier punto factible del original cae adentro.
        Con `entero_tl` el max (la energia MINIMA, el presupuesto es -energia)
        sale ademas del MILP del dia, con su cota (valida aunque corte por
        tiempo): el relajado queda bajo el minimo entero y el maestro declara
        presupuestos que ninguna operacion entera alcanza (version 5)."""
        m = self.m
        self._entera(False)
        self._enlaces(0.0, INF, 0.0, INF)
        self._copias()
        todas = m.getVars()
        objs = m.getAttr("Obj", todas)
        salida = []
        try:
            for kk, e in enumerate(self.vpres):
                iv = self._rango_intervalo(kk)
                if iv is not None:
                    salida.append(iv)
                    continue
                r = []
                for sentido in (GRB.MINIMIZE, GRB.MAXIMIZE):
                    m.setObjective(gp.LinExpr(e), sentido)
                    m.optimize()
                    r.append(m.ObjVal if m.Status == GRB.OPTIMAL
                             else (-INF if sentido == GRB.MINIMIZE else INF))
                salida.append(tuple(r))
            if entero_tl is not None:
                self._entera(True)
                m.Params.TimeLimit = entero_tl
                m.Params.MIPGap = 1e-4
                for i, e in enumerate(self.vpres):
                    m.setObjective(gp.LinExpr(e), GRB.MAXIMIZE)
                    m.optimize()
                    if m.Status in (GRB.OPTIMAL, GRB.TIME_LIMIT):
                        salida[i] = (salida[i][0], min(salida[i][1], m.ObjBound))
        finally:
            m.setAttr("Obj", todas, objs)
            m.ModelSense = GRB.MINIMIZE
            m.Params.TimeLimit = INF
        return salida

    def _rango_intervalo(self, kk, tipos=("power_balance",)):
        """[min, max] del presupuesto kk por aritmetica de intervalos sobre las cotas de
        sus columnas (valido, aunque mas ancho que el LP): para los de potencia
        (power_balance, uno por intervalo: con macrobloques son ~180 por bloque) en vez
        de dos LP. None si alguna cota es infinita o el tipo no esta en `tipos` (None:
        todos)."""
        P, k = self.P, self.pres[kk]
        if tipos is not None and P.pres_tipo[k] not in tipos:
            return None
        i, _ = P.presupuestos[k]
        idx, coef = P.filas[i]
        sel = P.var_blk[idx] == self.b
        lo = hi = 0.0
        for j, a in zip(idx[sel], coef[sel]):
            u, l = a * P.ub[j], a * P.lb[j]
            if not (math.isfinite(u) and math.isfinite(l)) or max(abs(u), abs(l)) >= 1e20:
                return None
            lo += min(u, l)
            hi += max(u, l)
        return lo, hi

    def local(self, xhat, pi, lam, rangos_cont, timelimit, gap, callback=None):
        """Lagrangiano LOCAL: copias ENTERAS fijas en xhat (la inversion de
        prueba), copias continuas y presupuestos libres con costo -pi / -lam.

            R = min { opex - pi_C . c_C - lam . e : operacion entera,
                      c_I = xhat_I }

        Por dualidad lagrangiana sobre las continuas, para TODO x con
        x_I = xhat_I:  Q(x) >= R + pi_C . x_C + lam . e. A diferencia del corte
        fortalecido, el dia no puede comprar capacidad entera a precio LP: el
        corte trae el costo de la operacion ENTERA con esa inversion. Cota dual
        de Gurobi: vale aunque corte por tiempo."""
        m = self.m
        self._entera(True)
        n, npres = self.n_cop, len(self.pres)
        ent = set(self.cop_ent)
        costos = [self.M if k in ent else 0.0 for k in range(n)] + [0.0] * npres
        ubs = [0.0 if k in ent else INF for k in range(n)] + [INF] * npres
        self.m.setAttr("Obj", self.slacks, costos * 2)
        self.m.setAttr("UB", self.slacks, ubs * 2)
        P = self.P
        c_obj = [0.0] * n
        lbs = [P.lb[j] for j in self.cop]
        ubs_c = [P.ub[j] for j in self.cop]
        for k, (lo, hi) in zip(self.cop_cont, rangos_cont):
            c_obj[k] = -pi[k]
            lbs[k], ubs_c[k] = lo, hi
        self._copias(c_obj, lbs, ubs_c)
        m.setAttr("Obj", self.vpres, [-l for l in lam])
        self._rhs(xhat, np.zeros(max(self.pres, default=-1) + 1))
        if self.start is not None:
            m.setAttr("Start", [self.vloc[k] for k in self.ent_loc], self.start)
        m.Params.TimeLimit = timelimit
        m.Params.MIPGap = gap
        t0 = time.time()
        m.optimize(callback)
        cota = _cota(m)
        st = m.Status
        if m.SolCount:
            x = m.getAttr("X", self.vloc)
            self.start = [round(x[k]) for k in self.ent_loc]
        m.setAttr("Obj", self.vpres, [0.0] * npres)
        self._copias()
        if st in (GRB.INF_OR_UNBD, GRB.UNBOUNDED, GRB.INFEASIBLE) or cota is None \
                or not math.isfinite(cota) or abs(cota) >= 1e20:
            return {"ok": False, "status": st, "t": time.time() - t0}
        return {"ok": True, "R": cota, "t": time.time() - t0, "status": st,
                "gap": _gap(m) if m.SolCount else None}

    def lagr(self, pi, lam, rangos, timelimit, gap):
        """R(pi) = min { opex - pi.copia - lam.presupuesto } con la operacion
        entera y las copias libres dentro de `rangos`. Cota dual de Gurobi:
        vale aunque corte por tiempo. None si no hay cota finita."""
        m = self.m
        self._entera(True)
        self._enlaces(0.0, INF, 0.0, INF)
        lbs = [r[0] for r in rangos]
        ubs = [r[1] for r in rangos]
        self._copias([-p for p in pi], lbs, ubs)
        m.setAttr("Obj", self.vpres, [-l for l in lam])
        if self.start is not None:
            m.setAttr("Start", [self.vloc[k] for k in self.ent_loc], self.start)
        m.Params.TimeLimit = timelimit
        m.Params.MIPGap = gap
        t0 = time.time()
        m.optimize()
        cota = _cota(m)
        st = m.Status
        m.setAttr("Obj", self.vpres, [0.0] * len(self.vpres))
        self._copias()
        if st in (GRB.INF_OR_UNBD, GRB.UNBOUNDED, GRB.INFEASIBLE) or cota is None \
                or not math.isfinite(cota) or abs(cota) >= 1e20:
            return {"ok": False, "status": st, "t": time.time() - t0}
        return {"ok": True, "R": cota, "t": time.time() - t0, "status": st}


# --------------------------------------------------------------------------
# Maestro
# --------------------------------------------------------------------------

class Maestro:

    def __init__(self, P, env, cotas_capacidad=None):
        self.P = P
        m = gp.Model(env=env)
        mv = P.maestro_vars
        self.pos = {int(j): k for k, j in enumerate(mv)}
        self.x = [m.addVar(lb=P.lb[j], ub=P.ub[j], vtype=P.vtype[j], name=P.nombre[j])
                  for j in mv]
        for nombre, n_min in (cotas_capacidad or {}).items():
            for k, j in enumerate(mv):
                if P.nombre[j] == f"n_ssee_k({nombre})":
                    self.x[k].LB = max(P.lb[j], n_min)
        self.e = [m.addVar(lb=-INF, ub=INF) for _ in P.presupuestos]
        self.th = [m.addVar(lb=-INF, ub=INF) for _ in range(P.nb)]
        for i in P.filas_maestro:
            idx, coef = P.filas[i]
            expr = gp.LinExpr()
            for j, a in zip(idx, coef):
                if P.var_blk[j] < 0:
                    expr.addTerms(a, self.x[self.pos[int(j)]])
            for k in P.pres_de_fila.get(int(i), []):
                expr.addTerms(1.0, self.e[k])
            m.addLConstr(expr, P.sense[i], float(P.rhs[i]))
        self.obj_expr = gp.LinExpr([float(P.obj[j]) for j in mv], self.x) \
            + gp.quicksum(self.th) + P.objcon
        m.setObjective(self.obj_expr, GRB.MINIMIZE)
        m.Params.MIPGap = 1e-6
        m.update()
        self.m = m
        self.ent = [k for k, j in enumerate(mv) if P.vtype[j] != GRB.CONTINUOUS]
        self.vt_orig = [P.vtype[mv[k]] for k in self.ent]
        # Binarias propias del maestro (LShaped.agregar_corte_produccion): se relajan
        # junto con las enteras del modelo.
        self.extra_ent = []
        self.n_cortes = 0

    def _entero(self, si):
        vs = [self.x[k] for k in self.ent] + self.extra_ent
        self.m.setAttr("VType", vs, (self.vt_orig + [GRB.BINARY] * len(self.extra_ent)) if si
                       else [GRB.CONTINUOUS] * len(vs))

    def punto(self):
        """(xhat sobre TODAS las columnas -- solo las del maestro con valor --,
        ehat, theta)."""
        xh = np.zeros(self.P.nv)
        xh[self.P.maestro_vars] = self.m.getAttr("X", self.x)
        return xh, np.array(self.m.getAttr("X", self.e)), np.array(self.m.getAttr("X", self.th))

    def resolver(self, entero):
        self._entero(entero)
        self.m.optimize()
        if self.m.Status != GRB.OPTIMAL:
            raise RuntimeError(f"maestro sin optimo (status {self.m.Status})")
        return (self.m.ObjBound if entero else self.m.ObjVal)

    def regularizar(self, L, U, alpha, fijar_enteras):
        """Punto interior del conjunto de nivel (barrier sin crossover). Con
        fijar_enteras, las enteras quedan en la solucion actual del maestro."""
        m = self.m
        xs_ent = [round(self.x[k].X) for k in self.ent]
        nivel = m.addLConstr(self.obj_expr, GRB.LESS_EQUAL, L + alpha * (U - L))
        m.setObjective(gp.LinExpr(), GRB.MINIMIZE)
        guard = None
        if fijar_enteras:
            ents = [self.x[k] for k in self.ent]
            guard = (m.getAttr("LB", ents), m.getAttr("UB", ents))
            m.setAttr("LB", ents, xs_ent)
            m.setAttr("UB", ents, xs_ent)
        self._entero(False)
        m.Params.Method = 2
        m.Params.Crossover = 0
        try:
            m.optimize()
            ok = m.Status in (GRB.OPTIMAL, GRB.SUBOPTIMAL)
            pt = self.punto() if ok else None
        finally:
            m.Params.Method = -1
            m.Params.Crossover = -1
            m.remove(nivel)
            m.setObjective(self.obj_expr, GRB.MINIMIZE)
            if guard is not None:
                ents = [self.x[k] for k in self.ent]
                m.setAttr("LB", ents, guard[0])
                m.setAttr("UB", ents, guard[1])
            m.update()
        return pt

    def corte(self, b, intercepto, pi, lam, cop, pres, tol=1e-9):
        """theta_b >= intercepto + pi.x + lam.e"""
        expr = gp.LinExpr(self.th[b])
        for j, p in zip(cop, pi):
            if abs(p) > tol:
                expr.addTerms(-p, self.x[self.pos[j]])
        for k, l in zip(pres, lam):
            if abs(l) > tol:
                expr.addTerms(-l, self.e[k])
        self.m.addLConstr(expr, GRB.GREATER_EQUAL, float(intercepto))
        self.n_cortes += 1

    def corte_local(self, b, R, pi, lam, cop, pres, cop_ent, cop_cont, xhat, Mb):
        """theta_b >= R + pi_C.x_C + lam.e - Mb * ||x_I - xhat_I||_1

        Exacto (hasta la dualidad en las continuas) en la inversion entera
        xhat_I y desactivado en cualquier otra: como las copias enteras son
        enteras, otra inversion esta a distancia >= 1 y con Mb el lado derecho
        cae bajo la cota global de theta_b."""
        m = self.m
        expr = gp.LinExpr(self.th[b])
        for k in cop_cont:
            if abs(pi[k]) > 1e-9:
                expr.addTerms(-pi[k], self.x[self.pos[cop[k]]])
        for kk, l in zip(pres, lam):
            if abs(l) > 1e-9:
                expr.addTerms(-l, self.e[kk])
        for k in cop_ent:
            j = cop[k]
            xv = self.x[self.pos[j]]
            v = float(round(xhat[j]))
            d = m.addVar(lb=0.0)
            m.addLConstr(d - xv, GRB.GREATER_EQUAL, -v)
            m.addLConstr(d + xv, GRB.GREATER_EQUAL, v)
            expr.addTerms(Mb, d)
        m.addLConstr(expr, GRB.GREATER_EQUAL, float(R))
        self.n_cortes += 1

    def rangos(self, cols):
        """[min, max] de cada columna del maestro sobre las filas del maestro
        (relajacion lineal, sin cortes): cotas validas del problema original."""
        m = self.m
        self._entero(False)
        salida = {}
        # theta libre: asi ningun corte restringe x (siempre hay un theta que
        # lo cumple) y el rango sale solo de las filas del maestro.
        guard = [(t.LB, t.UB) for t in self.th]
        for t in self.th:
            t.LB, t.UB = -INF, INF
        try:
            for j in cols:
                v = self.x[self.pos[j]]
                r = []
                for sentido in (GRB.MINIMIZE, GRB.MAXIMIZE):
                    m.setObjective(gp.LinExpr(v), sentido)
                    m.optimize()
                    if m.Status == GRB.OPTIMAL:
                        r.append(m.ObjVal)
                    else:
                        r.append(-INF if sentido == GRB.MINIMIZE else INF)
                salida[j] = (max(r[0], v.LB), min(r[1], v.UB))
        finally:
            for t, (lo, hi) in zip(self.th, guard):
                t.LB, t.UB = lo, hi
            m.setObjective(self.obj_expr, GRB.MINIMIZE)
            m.update()
        return salida


# --------------------------------------------------------------------------
# Algoritmo
# --------------------------------------------------------------------------

class BendersDias:

    def __init__(self, info, model, por_var, M=1e6, jobs=8, alpha=0.5,
                 cotas_capacidad=None, out=None, verbose=True, cpu=None, por_estacion=False,
                 acotar=True):
        self.verbose = verbose
        self.out = out
        cpu = cpu or os.cpu_count() or 8
        self.jobs = jobs
        self.env_F = _env(cpu)
        t0 = time.time()
        self.P = Particion(info, model, por_var, self.env_F, por_estacion=por_estacion)
        self._log(f"particion en {time.time() - t0:.0f}s: {self.P.resumen()}")
        t0 = time.time()
        hilos = max(1, cpu // jobs)
        self.subs = [Subproblema(self.P, b, M, hilos) for b in range(self.P.nb)]
        self.env_M = _env(min(cpu, 16))
        self.maestro = Maestro(self.P, self.env_M, cotas_capacidad)
        self._log(f"{self.P.nb} subproblemas y maestro armados en {time.time() - t0:.0f}s "
                  f"({jobs} en paralelo x {hilos} hilos)")
        self.alpha = alpha
        self.pool = ThreadPoolExecutor(max_workers=jobs)
        self.UB, self.mejor_vec = INF, None
        self.ub_por_inversion = {}
        self.historia = []
        self.t0 = time.time()
        if por_estacion and acotar:
            self.acotar_presupuestos()

    def _log(self, s):
        if self.verbose:
            print(f"[BD] {s}", flush=True)

    def acotar_presupuestos(self):
        """Cotas de los presupuestos del maestro por aritmetica de intervalos (validas).
        Con macrobloques hay ~16 mil (potencia de cada estacion en cada intervalo): libres,
        el maestro de la etapa 1 les da valores enormes y los enlaces elasticos de los
        cortes quedan con holguras de 1e18 (05C, 6-nov)."""
        # Las columnas de carga (Sv) no tienen cota en el modelo: los intervalos solo
        # acotan ~1 %. Rangos LP (Subproblema.rango_presupuestos, validos) en paralelo,
        # guardados para LShaped.preparar_maestro.
        t0 = time.time()
        cache = os.path.join(self.out, "rangos_presupuestos.json") if self.out else None
        por_bloque = None
        if cache and os.path.exists(cache):
            # De una corrida anterior con la misma particion (~40 min de LP en 05C).
            with open(cache, encoding="utf-8") as f:
                guardado = json.load(f)
            if guardado.get("claves") == self.P.claves and guardado.get("n") == len(self.P.presupuestos):
                por_bloque = [[tuple(r) for r in rs] for rs in guardado["por_bloque"]]
                self._log(f"rangos de presupuestos leidos de {cache}")
        if por_bloque is None:
            por_bloque = self._paralelo(lambda b: self.subs[b].rango_presupuestos())
            if cache:
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump({"claves": self.P.claves, "n": len(self.P.presupuestos),
                               "por_bloque": [[list(r) for r in rs] for rs in por_bloque]}, f)
        self.rangos_pres = {}
        for b, rs in enumerate(por_bloque):
            for k, (lo, hi) in zip(self.subs[b].pres, rs):
                self.rangos_pres[k] = (lo, hi)
                self.maestro.e[k].LB, self.maestro.e[k].UB = lo, hi
        self.maestro.m.update()
        finitos = sum(1 for lo, hi in self.rangos_pres.values() if math.isfinite(lo) and math.isfinite(hi))
        self._log(f"presupuestos acotados (rangos LP): {finitos} de {len(self.P.presupuestos)} "
                  f"finitos en {time.time() - t0:.0f}s")

    def _paralelo(self, fn, bloques=None):
        return list(self.pool.map(fn, bloques if bloques is not None else range(self.P.nb)))

    def _guardar(self):
        if not self.out:
            return
        with open(os.path.join(self.out, "historia.json"), "w", encoding="utf-8") as f:
            json.dump({"UB": self.UB, "historia": self.historia}, f, indent=1)

    # ------------------------------------------------------------ UB -- #
    def pulir(self, vec):
        """Monolitico con TODAS las enteras fijas en `vec`: LP de las continuas.
        Devuelve (costo, vector completo) o (None, None)."""
        F = self.P.F
        vs = F.getVars()
        ent = [j for j, t in enumerate(self.P.vtype) if t != "C"]
        vals = [float(round(vec[j])) for j in ent]
        F.setAttr("LB", [vs[j] for j in ent], vals)
        F.setAttr("UB", [vs[j] for j in ent], vals)
        F.Params.TimeLimit = 900
        F.optimize()
        if F.Status != GRB.OPTIMAL:
            return None, None
        return F.ObjVal, np.array(F.getAttr("X", vs))

    def evaluar_ub(self, xhat, timelimit, gap):
        """Dias MILP con la inversion de `xhat` fija + pulido. Cachea por la
        parte entera de la inversion."""
        P = self.P
        ent_m = [j for j in P.maestro_vars if P.vtype[j] != "C"]
        clave = tuple(int(round(xhat[j])) for j in ent_m)
        if clave in self.ub_por_inversion:
            return self.ub_por_inversion[clave], False
        x = xhat.copy()
        for j in ent_m:
            x[j] = round(x[j])
        t0 = time.time()
        res = self._paralelo(lambda b: self.subs[b].ub(x, timelimit, gap))
        malos = [b for b, r in enumerate(res) if not r["ok"]]
        if malos:
            self._log(f"   UB: {len(malos)} dias sin operacion factible con esta inversion "
                      f"({[P.claves[b] for b in malos][:4]})")
            self.ub_por_inversion[clave] = None
            return None, True
        vec = x.copy()
        for b, r in enumerate(res):
            vec[self.subs[b].loc] = r["x"]
        costo, completo = self.pulir(vec)
        t_dias = max(r["t"] for r in res)
        self._log(f"   UB: dias MILP (mas lento {t_dias:.0f}s, gap max "
                  f"{max(r['gap'] for r in res):.2%}) + pulido en {time.time() - t0:.0f}s -> "
                  f"{'infactible' if costo is None else f'{costo:,.2f}'}")
        self.ub_por_inversion[clave] = costo
        if costo is not None and costo < self.UB:
            self.UB, self.mejor_vec = costo, completo
            self._log(f"   *** nueva UB {costo:,.2f}")
        return costo, True

    def cargar_incumbente(self, vec):
        """Solucion conocida (p.ej. la de Benders anidado): UB inicial, punto de
        arranque de los cortes y start de los dias MILP."""
        costo, completo = self.pulir(vec)
        if costo is None:
            raise RuntimeError("el incumbente dado no es factible al pulirlo")
        self.UB, self.mejor_vec = costo, completo
        for s in self.subs:
            s.start = [round(completo[s.loc[k]]) for k in s.ent_loc]
        ent_m = [j for j in self.P.maestro_vars if self.P.vtype[j] != "C"]
        self.ub_por_inversion[tuple(int(round(completo[j])) for j in ent_m)] = costo
        e0 = np.array([self.P.energia(completo, k) for k in range(len(self.P.presupuestos))])
        return costo, completo, e0

    # --------------------------------------------------------- cortes -- #
    def cortes_lp(self, xhat, ehat):
        res = self._paralelo(lambda b: self.subs[b].lp(xhat, ehat))
        for b, r in enumerate(res):
            if not r["ok"]:
                raise RuntimeError(f"LP del dia {self.P.claves[b]} sin optimo ({r['status']})")
            s = self.subs[b]
            intercepto = r["valor"] - sum(p * xhat[j] for j, p in zip(s.cop, r["pi"])) \
                - sum(l * ehat[k] for k, l in zip(s.pres, r["lam"]))
            self.maestro.corte(b, intercepto, r["pi"], r["lam"], s.cop, s.pres)
        return res

    def cortes_fortalecidos(self, xhat, ehat, rangos, timelimit, gap):
        def tarea(b):
            s = self.subs[b]
            r = s.lp(xhat, ehat)
            if not r["ok"]:
                return r, None
            rg = [rangos[j] for j in s.cop]
            return r, s.lagr(r["pi"], r["lam"], rg, timelimit, gap)
        res = self._paralelo(tarea)
        n_f, t_max, subida = 0, 0.0, 0.0
        for b, (r, lg) in enumerate(res):
            s = self.subs[b]
            ic_lp = r["valor"] - sum(p * xhat[j] for j, p in zip(s.cop, r["pi"])) \
                - sum(l * ehat[k] for k, l in zip(s.pres, r["lam"]))
            self.maestro.corte(b, ic_lp, r["pi"], r["lam"], s.cop, s.pres)
            if lg is not None and lg["ok"]:
                t_max = max(t_max, lg["t"])
                if lg["R"] > ic_lp + 1e-6 * max(1.0, abs(ic_lp)):
                    self.maestro.corte(b, lg["R"], r["pi"], r["lam"], s.cop, s.pres)
                    n_f += 1
                    subida += lg["R"] - ic_lp
        return res, n_f, t_max, subida

    def cortes_locales(self, xhat, ehat, rangos, rpres, timelimit, gap):
        """Corte LP + corte lagrangiano LOCAL (ver Subproblema.local y
        Maestro.corte_local) de cada dia en la inversion entera de xhat."""
        P = self.P
        x = xhat.copy()
        for j in P.maestro_vars:
            if P.vtype[j] != "C":
                x[j] = round(x[j])

        def tarea(b):
            s = self.subs[b]
            r = s.lp(x, ehat)
            if not r["ok"]:
                return r, None
            rc = [rangos[s.cop[k]] for k in s.cop_cont]
            return r, s.local(x, r["pi"], r["lam"], rc, timelimit, gap)
        res = self._paralelo(tarea)
        n_loc, t_max, subida = 0, 0.0, 0.0

        def extremo(p, lo, hi):
            if p == 0:
                return 0.0
            return p * hi if p > 0 else p * lo

        for b, (r, lg) in enumerate(res):
            s = self.subs[b]
            ic_lp = r["valor"] - sum(p * x[j] for j, p in zip(s.cop, r["pi"])) \
                - sum(l * ehat[k] for k, l in zip(s.pres, r["lam"]))
            self.maestro.corte(b, ic_lp, r["pi"], r["lam"], s.cop, s.pres)
            if lg is None or not lg["ok"]:
                continue
            t_max = max(t_max, lg["t"])
            pi, lam = r["pi"], r["lam"]
            tope = lg["R"] - self.maestro.th[b].LB + 1.0
            tope += sum(extremo(pi[k], *rangos[s.cop[k]]) for k in s.cop_cont)
            tope += sum(extremo(l, *rpres[kk]) for kk, l in zip(s.pres, lam))
            if not math.isfinite(tope):
                continue
            self.maestro.corte_local(b, lg["R"], pi, lam, s.cop, s.pres, s.cop_ent,
                                     s.cop_cont, x, max(tope, 1.0))
            n_loc += 1
            # Cuanto sube el corte local sobre el LP en el punto de prueba: la
            # ganancia por integralidad de la operacion de ese dia.
            en_x = lg["R"] + sum(pi[k] * x[s.cop[k]] for k in s.cop_cont) \
                + sum(l * ehat[kk] for kk, l in zip(s.pres, lam))
            subida += en_x - r["valor"]
        return res, n_loc, t_max, subida

    def inversion(self, xhat):
        P = self.P
        partes = {}
        for j in P.maestro_vars:
            fam = P.nombre[j].split("(")[0]
            if fam in ("N_bays", "N_chargers", "N_batteries", "n_ssee_k"):
                partes.setdefault(fam, []).append(str(int(round(xhat[j]))))
        return " | ".join(f"{k.replace('N_', '')} {','.join(v)}" for k, v in partes.items())

    # --------------------------------------------------------- etapas -- #
    def _cota_theta(self):
        cotas = self._paralelo(lambda b: self.subs[b].libre())
        for b, c in enumerate(cotas):
            if c is not None:
                self.maestro.th[b].LB = c - 1e-6 * max(1.0, abs(c))
        return cotas

    def _costo_aprox(self, xhat, valores):
        mv = self.P.maestro_vars
        return float(np.dot(self.P.obj[mv], xhat[mv]) + self.P.objcon + sum(valores))

    def correr(self, x0, e0, max_it=(30, 30, 20), tol=(1e-3, 1e-4), ub_timelimit=60,
               ub_gap=0.01, lagr_timelimit=60, lagr_gap=1e-4, tiempo_max=None,
               etapas=(1, 2, 3), ub_en_etapa2=True, corte3="local"):
        M = self.maestro
        cotas = self._cota_theta()
        self._log(f"cota de theta (dias con inversion libre): suma {sum(c for c in cotas if c):,.2f}")
        # Cortes en el punto de arranque (el incumbente). Su costo con la
        # operacion relajada es la primera U de las etapas 1 y 2: es un punto
        # entero y factible del maestro.
        res0 = self.cortes_lp(x0, e0)
        U0_lp = self._costo_aprox(x0, [r["valor"] for r in res0])
        self._log(f"arranque en el incumbente: costo con operacion LP {U0_lp:,.2f}, "
                  f"holgura de enlaces {sum(r['holgura'] for r in res0):.3g}")
        L_final = {}
        vencido = (lambda: tiempo_max is not None and time.time() - self.t0 > tiempo_max)

        # ---------------- Etapa 1: relajacion continua ----------------
        if 1 in etapas:
            U1 = U0_lp
            for it in range(1, max_it[0] + 1):
                t_it = time.time()
                L = M.resolver(entero=False)
                pt = M.regularizar(L, U1, self.alpha, fijar_enteras=False)
                xh, eh, _ = pt if pt is not None else M.punto()
                res = self.cortes_lp(xh, eh)
                U1 = min(U1, self._costo_aprox(xh, [r["valor"] for r in res]))
                gap = (U1 - L) / abs(U1)
                self._registrar(1, it, L, U1, gap, t_it, res)
                if gap <= tol[0] or vencido():
                    break
            L_final[1] = L

        # ---------------- Etapa 2: maestro entero, cortes LP ----------------
        if 2 in etapas:
            U2 = U0_lp
            for it in range(1, max_it[1] + 1):
                t_it = time.time()
                L = M.resolver(entero=True)
                xm, em, _ = M.punto()
                if ub_en_etapa2:
                    self.evaluar_ub(xm, ub_timelimit, ub_gap)
                pt = M.regularizar(L, U2, self.alpha, fijar_enteras=True)
                xh, eh, _ = pt if pt is not None else (xm, em, None)
                res = self.cortes_lp(xh, eh)
                U2 = min(U2, self._costo_aprox(xh, [r["valor"] for r in res]))
                gap = (U2 - L) / abs(U2)
                self._registrar(2, it, L, U2, gap, t_it, res)
                if gap <= tol[1] or vencido():
                    break
            L_final[2] = L

        # ---------------- Etapa 3: cortes de los dias MILP ----------------
        if 3 in etapas and not vencido():
            copiadas = sorted({j for s in self.subs for j in s.cop})
            rangos = M.rangos(copiadas)
            self._log("rangos de las copias (filas del maestro): " + ", ".join(
                f"{self.P.nombre[j]}=[{lo:g},{hi:g}]" for j, (lo, hi) in rangos.items()))
            rpres = {}
            if corte3 == "local":
                por_bloque = self._paralelo(lambda b: self.subs[b].rango_presupuestos())
                for b, rs in enumerate(por_bloque):
                    for kk, (lo, hi) in zip(self.subs[b].pres, rs):
                        rpres[kk] = (lo, hi)
                        # Cota valida: la energia de cualquier dia factible cae
                        # en su rango. Deja acotado lam.e en el corte local.
                        M.e[kk].LB, M.e[kk].UB = lo, hi
                self._log("rango de energia por dia: " + ", ".join(
                    f"[{lo:.3g},{hi:.3g}]" for lo, hi in list(rpres.values())[:4]) + " ...")
            L_prev, quietas = -INF, 0
            for it in range(1, max_it[2] + 1):
                t_it = time.time()
                L = M.resolver(entero=True)
                xm, em, _ = M.punto()
                self.evaluar_ub(xm, ub_timelimit, ub_gap)
                pt = M.regularizar(L, self.UB, self.alpha, fijar_enteras=True)
                xh, eh, _ = pt if pt is not None else (xm, em, None)
                if corte3 == "local":
                    res, n_f, t_lag, subida = self.cortes_locales(
                        xh, eh, rangos, rpres, lagr_timelimit, lagr_gap)
                else:
                    res, n_f, t_lag, subida = self.cortes_fortalecidos(
                        xh, eh, rangos, lagr_timelimit, lagr_gap)
                gap = (self.UB - L) / abs(self.UB)
                self._registrar(3, it, L, self.UB, gap, t_it, [r for r, _ in res],
                                extra={"fortalecidos": n_f, "t_lagr_max": t_lag,
                                       "subida_intercepto": subida,
                                       "inversion": self.inversion(xm)})
                quietas = quietas + 1 if L <= L_prev + 1e-6 * abs(L) else 0
                L_prev = max(L_prev, L)
                if gap <= tol[1] or quietas >= 4 or vencido():
                    break
            L_final[3] = L_prev
        self._guardar()
        return L_final

    def _registrar(self, etapa, it, L, U, gap, t_it, res, extra=None):
        holg = sum(r.get("holgura", 0.0) for r in res)
        fila = {"etapa": etapa, "it": it, "L": L, "U_etapa": U, "gap_etapa": gap,
                "UB": self.UB, "gap_UB": (self.UB - L) / abs(self.UB) if self.UB < 1e20 else None,
                "t_iter": time.time() - t_it, "t_total": time.time() - self.t0,
                "cortes": self.maestro.n_cortes, "holgura_enlaces": holg}
        if extra:
            fila.update(extra)
        self.historia.append(fila)
        ub = f"{self.UB:,.2f} (gap {fila['gap_UB']:.2%})" if fila["gap_UB"] is not None else "-"
        ex = ""
        if extra:
            ex = (f"\n[BD]         cortes MILP {extra['fortalecidos']}/{self.P.nb} "
                  f"(+{extra['subida_intercepto']:,.0f} sobre LP en el punto, "
                  f"MILP max {extra['t_lagr_max']:.0f}s)  inversion [{extra['inversion']}]")
        self._log(f"E{etapa} it {it:>2}  L {L:,.2f}  U_e {U:,.2f} ({gap:.3%})  UB {ub}  "
                  f"holgura {holg:.3g}  {fila['t_iter']:.0f}s (total {fila['t_total'] / 60:.1f} min)"
                  + ex)
        self._guardar()
