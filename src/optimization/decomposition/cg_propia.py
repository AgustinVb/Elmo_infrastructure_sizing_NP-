"""Generacion de columnas propia (Dantzig-Wolfe) sobre el monolitico, con el
pricing en Gurobi y en paralelo, y branch-and-price sobre la inversion.

Por que no GCG: en la raiz de 19 h de year_day el pricing se llevo el 91 % del
tiempo, GCG resuelve los bloques en serie, no puede resolver el maestro con
barrier (SoPlex) y filtra las columnas sin costo reducido negativo, lo que deja
fuera el Column Sharing. Aca:

  ESTRUCTURA  sale del model.mps + decomp.json de gcg_block.export (por defecto
              con las capacidades separadas por bloque, copia <= capacidad). La
              propiedad de cada columna se deduce de las filas: una variable que
              solo aparece en filas de UN bloque es de ese bloque (las variables
              del dia y las copias de capacidad sv_copia); una que aparece en
              filas de VARIOS bloques es de enlace (b_bar, D, R, ...) y cada
              bloque usa su copia, unida a la del maestro con copia - x = 0 (lo
              mismo que hace GCG); las demas son del maestro.
  MAESTRO     LP restringido en gurobipy: variables del maestro, una lambda por
              columna (convexificacion), las filas del maestro, las de enlace y
              una de convexidad por bloque. Artificiales en todas las filas, asi
              arranca factible aun sin columnas; con la caja (ver abajo) sus
              costos son la caja de los duales.
  PRICING     un MILP por bloque, persistente, con el objetivo de costo reducido
              c - pi A. Los bloques en paralelo (ThreadPoolExecutor: Gurobi suelta
              el GIL). Rondas heuristicas (tope corto, gap holgado, varias
              soluciones del pool) para generar columnas y rondas EXACTAS (gap
              absoluto chico) para certificar la cota.
  COTA        lagrangiana, valida para cualquier pi con el signo correcto:
              pi'b + sum_j min_x rc_j x_j + sum_b ObjBound_b, con ObjBound la cota
              dual de Gurobi del pricing (vale aunque se corte por tiempo). Solo
              en las rondas en que se pricearon TODOS los bloques.

MEJORAS (2026-10-13, cada una con su flag en cg_propia.py para medirla aparte):

  1 UB DIRIGIDA POR EL MAESTRO (heuristica_maestro). La UB vieja (maestro entero
    sobre las columnas, ub_columnas) casi nunca respeta el enlace: las columnas
    de dias distintos traen b_bar/D distintas. La nueva toma del maestro las
    capacidades (redondeadas hacia arriba) y el estado de degradacion, resuelve
    cada dia por separado con eso fijo (los dias se separan dadas ~20 variables
    anuales, como en day_blocks) y pule el monolitico con la operacion fija.
    Refina b_bar con el valor pulido y, si el pulido es infactible, rehace los
    dias año por año (respaldo secuencial). Diving: ver 6.
  2 ARRANQUE EN CALIENTE (relajacion_inicial). La relajacion LP del monolitico
    o la operacional (inversion entera, operacion LP; la cota de Nested Benders)
    da (a) una cota inferior valida desde el arranque, (b) duales iniciales para
    el centro de estabilizacion: los de las filas del maestro salen directo y
    los de las filas de enlace copia - x = 0 se reconstruyen de los costos
    reducidos de las copias, y (c) el punto de partida de la heuristica 1, cuyas
    columnas eliminan la fase de artificiales.
  3 CERTIFICACION SEPARADA. La version anterior solo pasaba al pricing exacto si
    una ronda no traia NINGUNA columna con Wentges apagado, y con 5 soluciones
    por bloque casi nunca pasaba (0 de 140 rondas en 4 años): la cota salia de
    MILP cortados a 30 s. Ahora hay rondas exactas cada `exacto_cada` rondas y
    al estancarse el maestro, con gap ABSOLUTO (la perdida total de cota queda
    acotada por tol_gap*|z|), el mispricing se reintenta en la misma ronda con
    los duales del maestro, y una columna cuenta como nueva solo si su costo
    reducido supera rc_min*|z| (las de costo reducido casi nulo alargaban la
    convergencia).
  4 PRICING MAS RAPIDO. Paralelismo automatico (mas bloques a la vez, menos
    hilos cada uno), pool de columnas fuera del maestro que se re-pricea antes
    de llamar a los MILP, pricing parcial (los bloques que no dieron columnas en
    las ultimas rondas descansan hasta la siguiente ronda completa), Column
    Sharing cada `compartir_cada` rondas, y transferencia de columnas entre
    bloques GEMELOS (mismo dia de años con filas identicas salvo el costo, p.ej.
    años con la misma meta): la operacion se re-costea con el descuento del otro
    año sin resolver nada.
  5 ESTABILIZACION POR CAJA sobre los duales (box-step de Marsten, el analogo de
    lo que hizo funcionar a Nested Benders): pi en [pi_c - delta, pi_c + delta],
    implementada con los costos de las artificiales; el centro se mueve en los
    pasos serios (mejora de la cota lagrangiana) y la caja crece si esta activa
    y el pricing ya no da columnas. Combinable con Wentges y barrier.
  6 BRANCH-AND-PRICE sobre las enteras del maestro (capacidades, apertura, R):
    viven en el maestro, asi que ramificar es cambiar cotas del maestro y de las
    copias en el pricing. Primero un diving (fija la entera mas fraccional en su
    redondeo y re-optimiza unas rondas, hasta que la inversion es entera; ahi
    corre la heuristica 1) y despues mejor-primero. Un nodo con la inversion ya
    entera que no se poda no se puede ramificar mas aca: queda como hoja abierta
    y su cota entra en la cota global (que sigue siendo valida).
  Ademas: cotas de las variables del maestro por costo (x_k <= UB/c_k si todos
  los costos son >= 0), que vuelven finitas las de n_ssee_k o P_pot y con eso la
  cota lagrangiana no se cae a -inf.

ESTABILIZACION ANTERIOR (sigue disponible, Flores-Quiroz y Strunz 2021):
  wentges  suavizado de duales: se pricea en pi_sep = a pi_centro + (1-a) pi_RMP,
           con a automatico (Pessoa et al. 2018) y mispricing -> a = 0.
  barrier  el maestro con punto interior, sin crossover: duales centrados.
  COMPARTIR (CG&S): la capacidad de la mejor columna nueva de un bloque se
           prueba en sus "hermanos" (los bloques que separan las MISMAS
           capacidades) con esa capacidad fija, y esas columnas entran al maestro
           aunque no tengan costo reducido negativo.
"""
import ast
import heapq
import itertools
import json
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import gurobipy as gp
import numpy as np
from gurobipy import GRB

INF = GRB.INFINITY


def _env(threads=0):
    env = gp.Env(empty=True)
    env.setParam("OutputFlag", 0)
    if threads:
        env.setParam("Threads", threads)
    env.start()
    return env


def _clave_bloque(clave):
    """'(4, 105)' -> (4, 105); '105' -> (None, 105)."""
    try:
        v = ast.literal_eval(clave)
    except (ValueError, SyntaxError):
        return None, clave
    if isinstance(v, tuple) and len(v) == 2:
        return (None if v[0] is None else int(v[0])), v[1]
    return None, v


class MaestroNoAcotado(Exception):
    pass


# --------------------------------------------------------------------------
# Estructura Dantzig-Wolfe
# --------------------------------------------------------------------------

class EstructuraDW:
    """Bloques, variables del maestro y filas extendidas (maestro + enlace)."""

    def __init__(self, mps, decomp, env, fijar=None, replicar=True, max_replica=8):
        """fijar: {nombre de variable del MPS: valor}. Para RAMIFICAR sobre una
        entera del maestro que el maestro LP relaja mal. El caso que lo motivo:
        el reemplazo de baterias R. Con b_bar <= D_prev + 0,3*b_max*R, el
        maestro sube b_bar pagando un R fraccionario (año 6: R ~ 0,16, maestro
        42,6 mil) y la solucion entera necesita R = 1 (~1 M). Se resuelve una
        rama por valor; la cota del problema es la menor de las ramas."""
        t0 = time.time()
        F = gp.read(mps, env=env)
        F.update()
        vs, cs = F.getVars(), F.getConstrs()
        self.nv, self.nr = len(vs), len(cs)
        self.nombre = [v.VarName for v in vs]
        self.fijadas = {}
        if fijar:
            por_nombre = {n: j for j, n in enumerate(self.nombre)}
            for n, val in fijar.items():
                if n not in por_nombre:
                    raise KeyError(f"fijar: no existe la variable {n!r} en el MPS")
                vs[por_nombre[n]].LB = vs[por_nombre[n]].UB = float(val)
                self.fijadas[n] = float(val)
            F.update()
        self.lb = np.array(F.getAttr("LB", vs))
        self.ub = np.array(F.getAttr("UB", vs))
        self.obj = np.array(F.getAttr("Obj", vs))
        self.vtype = list(F.getAttr("VType", vs))
        sense = list(F.getAttr("Sense", cs))
        rhs = np.array(F.getAttr("RHS", cs))
        cname = [c.ConstrName for c in cs]
        self.objcon = F.ObjCon
        filas = []
        for c in cs:
            e = F.getRow(c)
            n = e.size()
            filas.append((np.fromiter((e.getVar(k).index for k in range(n)), int, n),
                          np.fromiter((e.getCoeff(k) for k in range(n)), float, n)))
        # Se conservan para las heuristicas (pulido) y los duales iniciales.
        self.F = F
        self._filas = filas

        with open(decomp, encoding="utf-8") as f:
            dec = json.load(f)
        self.claves = dec["meta"]["block_keys"]
        self.nb = nb = len(self.claves)
        blk_de_fila = {r: int(b) for b, fl in dec["blocks"].items() for r in fl}
        row_blk = np.array([blk_de_fila.get(n, -1) for n in cname])
        self.filas_bloque = {b: np.where(row_blk == b)[0] for b in range(nb)}
        filas_maestro = np.where(row_blk < 0)[0]
        self.filas_maestro = filas_maestro

        # Propiedad de las columnas, por las filas en que aparecen.
        cnt = np.zeros(self.nv, int)
        dueno = np.full(self.nv, -1)
        vars_de = {}
        for b in range(nb):
            fb = self.filas_bloque[b]
            vb = (np.unique(np.concatenate([filas[i][0] for i in fb])) if len(fb)
                  else np.array([], int))
            vars_de[b] = vb
            cnt[vb] += 1
            dueno[vb] = b
        vars_propias = {b: set(vars_de[b].tolist()) for b in range(nb)}
        self.var_blk = np.where(cnt == 1, dueno, -1)
        self.mvars = np.where(self.var_blk < 0)[0]
        pos_m = {int(j): k for k, j in enumerate(self.mvars)}
        self.pos_m = pos_m
        self.n_enlace = int((cnt >= 2).sum())

        # Replica de filas del maestro en los bloques. Una fila que solo tiene
        # variables del maestro y toca una variable de ENLACE (las que los
        # bloques copian con igualdad, p.ej. b_bar) se agrega tambien a cada
        # bloque que la copia, con copias de sus otras variables (D_prev, R,
        # ...). Es valida (las copias valen lo mismo que el maestro) y aprieta la
        # convexificacion: sin ella el maestro solo controla el PROMEDIO de b_bar
        # entre columnas, y mezcla dias operados con baterias nuevas (b_bar 481)
        # con otros a 387 para cumplir b_bar <= D_prev + 0,3*b_max*R sin pagar R
        # (medido en el año 6, 2026-10-02: maestro casi sin R y UB ~1,06 M).
        # Con la replica, cada columna trae su (b_bar, R) coherente y R es
        # binaria en el pricing.
        self.replicas = {b: [] for b in range(nb)}
        if replicar:
            enlace = set(np.where(cnt >= 2)[0].tolist())
            vset = {b: set(vars_de[b].tolist()) for b in range(nb)}
            for i in filas_maestro:
                idx, _ = filas[i]
                if (self.var_blk[idx] >= 0).any() or len(idx) > max_replica:
                    continue
                lk = [int(j) for j in idx if int(j) in enlace]
                if not lk:
                    continue
                for b in range(nb):
                    if any(j in vset[b] for j in lk):
                        self.replicas[b].append(int(i))
            for b in range(nb):
                extra = set()
                for i in self.replicas[b]:
                    extra.update(int(j) for j in filas[i][0])
                vars_de[b] = np.union1d(vars_de[b], np.array(sorted(extra), int))
        self.n_replicas = sum(len(v) for v in self.replicas.values())

        # Bloques: z = (propias, copias de enlace)
        self.bloques = []
        for b in range(nb):
            loc = np.where(self.var_blk == b)[0]
            cop = np.array(sorted(int(j) for j in vars_de[b] if self.var_blk[j] < 0), int)
            zpos = {int(j): k for k, j in enumerate(loc)}
            cpos = {int(j): len(loc) + k for k, j in enumerate(cop)}
            filas_b = []
            for i in list(self.filas_bloque[b]) + self.replicas[b]:
                idx, coef = filas[i]
                pos = np.array([zpos[j] if j in zpos else cpos[j] for j in idx.tolist()], int)
                filas_b.append((pos, coef, sense[i], float(rhs[i])))
            costo = np.concatenate([self.obj[loc], np.zeros(len(cop))])
            lbz = np.concatenate([self.lb[loc], self.lb[cop]])
            ubz = np.concatenate([self.ub[loc], self.ub[cop]])
            vt = [self.vtype[j] for j in loc] + [self.vtype[j] for j in cop]
            self.bloques.append({"loc": loc, "cop": cop, "zpos": zpos, "cpos": cpos,
                                 "filas": filas_b, "costo": costo, "lb": lbz.copy(),
                                 "ub": ubz.copy(), "lb0": lbz, "ub0": ubz,
                                 "vtype": vt, "partes": [],
                                 "ent": np.array([k for k, t in enumerate(vt) if t != "C"], int)})

        # Filas extendidas: las del maestro y las de enlace (copia - x = 0).
        self.ext = []
        sombra = {b: {} for b in range(nb)}
        for i in filas_maestro:
            idx, coef = filas[i]
            mk = self.var_blk[idx] < 0
            r = {"nombre": cname[i], "sense": sense[i], "rhs": float(rhs[i]), "fila": int(i),
                 "m_pos": np.array([pos_m[int(j)] for j in idx[mk]], int),
                 "m_coef": coef[mk], "solo_maestro": bool(mk.all())}
            n_r = len(self.ext)
            for b in sorted(set(self.var_blk[idx[~mk]].tolist())):
                sel = self.var_blk[idx] == b
                zp = np.array([self.bloques[b]["zpos"][int(j)] for j in idx[sel]], int)
                self.bloques[b]["partes"].append((n_r, zp, coef[sel]))
                # copia de capacidad: sv_enlace = copia_b - x <= 0
                if "sv_enlace" in cname[i] and sel.sum() == 1 and mk.sum() == 1:
                    sombra[b][int(r["m_pos"][0])] = int(zp[0])
            self.ext.append(r)
        self.enlace_de = {b: [] for b in range(nb)}
        for b in range(nb):
            for j, k in self.bloques[b]["cpos"].items():
                n_r = len(self.ext)
                self.ext.append({"nombre": f"enlace_b{b}_{self.nombre[j]}", "sense": "=",
                                 "rhs": 0.0, "m_pos": np.array([pos_m[j]], int),
                                 "m_coef": np.array([-1.0]), "b": b, "j": int(j)})
                self.enlace_de[b].append((n_r, int(j)))
                self.bloques[b]["partes"].append((n_r, np.array([k], int), np.array([1.0])))
        self.n_ext = len(self.ext)

        # Matriz densa del maestro (filas extendidas x variables del maestro)
        self.A_M = np.zeros((self.n_ext, len(self.mvars)))
        for r, f in enumerate(self.ext):
            np.add.at(self.A_M[r], f["m_pos"], f["m_coef"])
        self.rhs_ext = np.array([f["rhs"] for f in self.ext])
        self.sense_ext = [f["sense"] for f in self.ext]
        self.fila_menor = np.array([s == "<" for s in self.sense_ext])
        self.fila_mayor = np.array([s == ">" for s in self.sense_ext])

        # Hermanos (Column Sharing): bloques que separan las mismas capacidades.
        self.sombra = sombra
        self.hermanos = {b: [bb for bb in range(nb) if bb != b and sombra[b]
                             and set(sombra[bb]) == set(sombra[b])] for b in range(nb)}

        # Copias de enlace que aparecen en filas PROPIAS del bloque (no solo en
        # replicas): b_bar en las de estado de carga, X en las de existencia de
        # la estacion. La heuristica 1 fija las continuas (estado) y acota las
        # enteras (capacidad); el resto de las copias queda libre.
        self.propias = {b: np.array(sorted(k for j, k in self.bloques[b]["cpos"].items()
                                           if j in vars_propias[b]), int)
                        for b in range(nb)}
        self.anio = [_clave_bloque(c)[0] for c in self.claves]
        self.dia = [_clave_bloque(c)[1] for c in self.claves]
        # Para las cotas por costo: todo costo >= 0 sobre variables >= 0.
        self.obj_no_negativo = bool(self.obj.min() >= -1e-12 and self.lb.min() >= -1e-12)

        # Posiciones de z afectadas por las cotas del maestro (nodos, cotas por
        # costo): las copias de capacidad (solo cota superior) y las de enlace.
        self.afectadas = {}
        for b in range(nb):
            B = self.bloques[b]
            ks = list(sombra[b].values()) + list(B["cpos"].values())
            self.afectadas[b] = np.array(ks, int)

        # Cotas de las copias. Sin esto el pricing ve las capacidades con cota
        # infinita (N_*, G_g, H no la traen en el MPS: la ponen filas del
        # maestro como N_bays <= max*X o G_g <= g_max), y con los duales de esas
        # filas en 0 -- lo normal al arrancar, el maestro es muy degenerado --
        # devuelve columnas con capacidad ilimitada y gratis (medido a 2 años:
        # 6 de 8 bloques con costo reducido 0 y el maestro clavado en el
        # incumbente). Las cotas implicadas por las filas SOLO del maestro valen
        # para toda solucion factible, y copia <= capacidad las hereda.
        mv = self.mvars
        self.actualizar_cotas_maestro(*self.propagar(self.lb[mv].astype(float).copy(),
                                                     self.ub[mv].astype(float).copy()))
        self.t_lectura = time.time() - t0

    def propagar(self, lb, ub, pasadas=50):
        """Propagacion de cotas (FBBT) sobre las filas que solo tienen
        variables del maestro, desde (lb, ub). Devuelve (lb, ub)."""
        mv = self.mvars
        lb, ub = np.array(lb, float), np.array(ub, float)
        ent = np.array([self.vtype[j] != "C" for j in mv])
        filas = [f for f in self.ext if f.get("solo_maestro")]
        for _ in range(pasadas):
            cambio = False
            for f in filas:
                p, a0 = f["m_pos"], f["m_coef"]
                for s in ((f["sense"],) if f["sense"] != "=" else ("<", ">")):
                    sg = 1.0 if s == "<" else -1.0      # sg*a x <= sg*rhs
                    a, bb = sg * a0, sg * f["rhs"]
                    mins = np.where(a > 0, a * lb[p], a * ub[p])
                    mins = np.where(a == 0, 0.0, mins)
                    inf = np.isinf(mins)
                    n_inf = int(inf.sum())
                    if n_inf > 1:
                        continue
                    tot = float(mins[~inf].sum())
                    for k in range(len(p)):
                        if a[k] == 0 or (n_inf == 1 and not inf[k]):
                            continue
                        resto = tot - (0.0 if inf[k] else mins[k])
                        v = (bb - resto) / a[k]
                        j = p[k]
                        if a[k] > 0:
                            if ent[j]:
                                v = np.floor(v + 1e-6)
                            if v < ub[j] - 1e-9:
                                ub[j], cambio = v, True
                        else:
                            if ent[j]:
                                v = np.ceil(v - 1e-6)
                            if v > lb[j] + 1e-9:
                                lb[j], cambio = v, True
            if not cambio:
                break
        return lb, ub

    def _propagar_cotas(self, pasadas=50):
        mv = self.mvars
        return self.propagar(self.lb[mv], self.ub[mv], pasadas)

    def cotas_bloque(self, b, lb_m, ub_m):
        """Cotas de z del bloque b dadas las cotas (lb_m, ub_m) del maestro."""
        B = self.bloques[b]
        lbz, ubz = B["lb0"].copy(), B["ub0"].copy()
        for jm, k in self.sombra[b].items():          # copia <= capacidad
            ubz[k] = min(ubz[k], ub_m[jm])
        for j, k in B["cpos"].items():                # copia = variable
            jm = self.pos_m[j]
            lbz[k] = max(lbz[k], lb_m[jm])
            ubz[k] = min(ubz[k], ub_m[jm])
        return lbz, ubz

    def actualizar_cotas_maestro(self, lb_m, ub_m):
        self.lb_m, self.ub_m = np.array(lb_m, float), np.array(ub_m, float)
        n_acot = 0
        for b in range(self.nb):
            B = self.bloques[b]
            B["lb"], B["ub"] = self.cotas_bloque(b, self.lb_m, self.ub_m)
            n_acot += sum(1 for k in self.sombra[b].values() if B["ub"][k] < B["ub0"][k])
        self.n_copias_acotadas = n_acot

    def resumen(self):
        tam = [len(B["costo"]) for B in self.bloques]
        return (f"{self.nv:,} columnas y {self.nr:,} filas; {self.nb} bloques de "
                f"{min(tam):,}-{max(tam):,} variables; maestro {len(self.mvars)} variables, "
                f"{self.n_ext} filas extendidas ({self.n_enlace} variables de enlace con "
                f"igualdad, {self.n_replicas} filas del maestro replicadas en bloques); "
                f"capacidades separadas por bloque "
                f"{min(len(s) for s in self.sombra.values())}-"
                f"{max(len(s) for s in self.sombra.values())} ({self.n_copias_acotadas} "
                f"copias con cota finita por propagacion); hermanos por bloque "
                f"{min(len(h) for h in self.hermanos.values())}-"
                f"{max(len(h) for h in self.hermanos.values())}")

    def z_de(self, b, vec):
        B = self.bloques[b]
        return np.concatenate([vec[B["loc"]], vec[B["cop"]]])

    def coefs(self, b, z):
        """[(fila extendida, coeficiente)] de la columna z del bloque b."""
        return [(r, float(np.dot(c, z[p]))) for r, p, c in self.bloques[b]["partes"]]


def gemelos_por_anio(E, model, por_var):
    """{b: [(b2, perm)]}: bloques del MISMO dia de años distintos cuyas filas y
    cotas son identicas salvo el costo (p.ej. años con la misma meta), con
    perm[k] = posicion en b2 de la posicion k de b. Una columna de b es entonces
    una columna valida de b2 (re-costeada con el costo de b2). La identificacion
    se hace sobre el modelo de Pyomo: el año de cada variable se reemplaza por su
    desfase respecto del año del bloque (D(y-1) en el bloque y -> D(-1)) y las
    copias sv_copia por la capacidad que copian."""
    from src.optimization.decomposition.gcg_block import _positions
    anios = set(model.years)
    if len(anios) < 2:
        return {}
    pos_y = _positions(model, anios)
    orig = getattr(model, "_sv_originales", None)

    def clave(j, y):
        vd = por_var.get(E.nombre[j])
        if vd is None:
            return repr(("?", E.nombre[j]))
        comp, tag = vd.parent_component().name, ""
        if comp == "sv_copia" and orig is not None:
            vd = orig[vd.index()]
            comp, tag = vd.parent_component().name, "sv"
        idx = vd.index()
        idx = idx if isinstance(idx, tuple) else (idx,)
        p = pos_y.get(comp)
        if p is not None and p < len(idx) and idx[p] in anios:
            idx = idx[:p] + (("dA", idx[p] - y),) + idx[p + 1:]
        return repr((tag, comp, idx))

    firmas = {}
    for b in range(E.nb):
        y = E.anio[b]
        if y is None:
            continue
        B = E.bloques[b]
        cl = [clave(j, y) for j in B["loc"]] + [clave(j, y) for j in B["cop"]]
        if len(set(cl)) != len(cl):
            continue
        filas = Counter(hash((tuple(sorted(f"{cl[p]}|{c:.9g}" for p, c in zip(pp, cc))),
                              s, round(rhs, 9))) for pp, cc, s, rhs in B["filas"])
        cotas = Counter(hash((cl[k], round(float(B["lb"][k]), 9), round(float(B["ub"][k]), 9),
                              B["vtype"][k])) for k in range(len(cl)))
        firmas[b] = (cl, {c: k for k, c in enumerate(cl)}, filas, cotas)
    gem = {}
    for b, (cl, pos, filas, cotas) in firmas.items():
        for b2, (cl2, pos2, filas2, cotas2) in firmas.items():
            if (b2 == b or E.dia[b2] != E.dia[b] or len(cl2) != len(cl)
                    or pos.keys() != pos2.keys() or filas != filas2 or cotas != cotas2):
                continue
            gem.setdefault(b, []).append((b2, np.array([pos2[c] for c in cl], int)))
    return gem


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

class Pricing:
    """El MILP de un bloque, persistente; cambian el objetivo y las cotas."""

    def __init__(self, E, b, threads):
        B = E.bloques[b]
        self.E, self.b, self.B = E, b, B
        self.env = _env(threads)
        m = gp.Model(env=self.env)
        self.lb_act = np.array(B["lb"], float)
        self.ub_act = np.array(B["ub"], float)
        self.z = m.addVars(len(B["costo"]), lb=self.lb_act.tolist(), ub=self.ub_act.tolist(),
                           vtype=B["vtype"])
        m.update()
        zl = [self.z[k] for k in range(len(B["costo"]))]
        self.zl = zl
        for pos, coef, s, rhs in B["filas"]:
            m.addLConstr(gp.LinExpr(coef.tolist(), [zl[k] for k in pos]), s, rhs)
        m.update()
        self.m = m
        self.start = None

    def poner_cotas(self, lb, ub):
        """Cotas base (las del nodo), persistentes."""
        lb, ub = np.asarray(lb, float), np.asarray(ub, float)
        ch = np.where((lb != self.lb_act) | (ub != self.ub_act))[0]
        if len(ch):
            vs = [self.zl[k] for k in ch]
            self.m.setAttr("LB", vs, lb[ch].tolist())
            self.m.setAttr("UB", vs, ub[ch].tolist())
        self.lb_act, self.ub_act = lb.copy(), ub.copy()

    def objetivo(self, pi):
        c = self.B["costo"].copy()
        if pi is None:
            return c
        for r, p, coef in self.B["partes"]:
            if pi[r] != 0.0:
                np.add.at(c, p, -pi[r] * coef)
        return c

    def resolver(self, c, timelimit, gap, pool, fijar=None, cotas=None, gap_abs=None):
        """min c z sobre el bloque. fijar: {posicion: valor} (Column Sharing).
        cotas: {posicion: (lb, ub)}, intersectadas con las del nodo
        (heuristicas). Las dos son temporales."""
        m, zl = self.m, self.zl
        t0 = time.time()
        m.setAttr("Obj", zl, c.tolist())
        temp = {}
        if cotas:
            for k, (l, u) in cotas.items():
                temp[int(k)] = (max(l, self.lb_act[k]), min(u, self.ub_act[k]))
        if fijar:
            for k, v in fijar.items():
                temp[int(k)] = (float(v), float(v))
        ks = list(temp)
        if ks:
            vs = [zl[k] for k in ks]
            m.setAttr("LB", vs, [temp[k][0] for k in ks])
            m.setAttr("UB", vs, [temp[k][1] for k in ks])
        if self.start is not None and not fijar:
            m.setAttr("Start", zl, self.start.tolist())
        m.Params.TimeLimit = max(0.5, timelimit)
        m.Params.MIPGap = gap
        m.Params.MIPGapAbs = gap_abs if gap_abs is not None else 1e-10
        m.Params.PoolSolutions = max(1, pool)
        try:
            m.optimize()
            st = m.Status
            sols = []
            if m.SolCount > 0:
                for k in range(min(m.SolCount, pool)):
                    m.Params.SolutionNumber = k
                    z = np.array(m.getAttr("Xn", zl))
                    ent = self.B["ent"]
                    z[ent] = np.round(z[ent])
                    sols.append(z)
            if st == GRB.INFEASIBLE:
                bound = INF          # sin puntos: solo queda la columna artificial
            elif st in (GRB.UNBOUNDED, GRB.INF_OR_UNBD):
                bound = -INF
            else:
                try:
                    bound = m.ObjBound
                except gp.GurobiError:   # bloque sin enteras (LP)
                    bound = m.ObjVal if st == GRB.OPTIMAL else -INF
            return {"ok": m.SolCount > 0, "status": st, "val": m.ObjVal if m.SolCount else INF,
                    "bound": bound, "sols": sols, "t": time.time() - t0}
        finally:
            if ks:
                m.setAttr("LB", vs, self.lb_act[ks].tolist())
                m.setAttr("UB", vs, self.ub_act[ks].tolist())


# --------------------------------------------------------------------------
# Maestro restringido
# --------------------------------------------------------------------------

class Maestro:

    def __init__(self, E, env, M_art=1e8):
        self.E = E
        m = gp.Model(env=env)
        mv = E.mvars
        self.x = m.addVars(len(mv), lb=E.lb_m.tolist(), ub=E.ub_m.tolist(),
                           obj=E.obj[mv].tolist())
        m.update()
        self.xl = [self.x[k] for k in range(len(mv))]
        self.M_art = M_art
        self.art = []                # (fila, signo)
        self.art_vars = []
        self.filas = []
        for r, f in enumerate(E.ext):
            expr = gp.LinExpr(f["m_coef"].tolist(), [self.x[k] for k in f["m_pos"]])
            if f["sense"] in ("<", "="):
                a = m.addVar(obj=M_art)
                expr.add(a, -1.0)
                self.art.append((r, -1.0))
                self.art_vars.append(a)
            if f["sense"] in (">", "="):
                a = m.addVar(obj=M_art)
                expr.add(a, 1.0)
                self.art.append((r, 1.0))
                self.art_vars.append(a)
            self.filas.append(m.addLConstr(expr, f["sense"], f["rhs"]))
        self.n_art_filas = len(self.art)
        self.conv = []
        for b in range(E.nb):
            a = m.addVar(obj=M_art)
            self.art_vars.append(a)
            self.conv.append(m.addLConstr(a, GRB.EQUAL, 1.0))
        m.update()
        self.m = m
        self.lams = {b: [] for b in range(E.nb)}
        self.pool = {b: [] for b in range(E.nb)}
        self.vistas = {b: set() for b in range(E.nb)}
        self.n_cols = 0
        self.box = False

    @staticmethod
    def _clave(z):
        return np.round(z, 6).tobytes()

    def costo_cf(self, b, z):
        costo = float(np.dot(self.E.bloques[b]["costo"], z))
        cf = [(r, v) for r, v in self.E.coefs(b, z) if v != 0.0]
        return costo, cf

    def _agregar_col(self, b, z, costo, cf):
        col = gp.Column([v for _, v in cf] + [1.0],
                        [self.filas[r] for r, _ in cf] + [self.conv[b]])
        self.lams[b].append((self.m.addVar(obj=costo, column=col), z, costo, cf))
        self.n_cols += 1

    def agregar(self, b, z, costo=None, cf=None):
        """Agrega la columna z del bloque b si no estaba (ni en el maestro ni en
        el pool). Devuelve True si entro."""
        clave = self._clave(z)
        if clave in self.vistas[b]:
            return False
        self.vistas[b].add(clave)
        if costo is None:
            costo, cf = self.costo_cf(b, z)
        self._agregar_col(b, z, costo, cf)
        return True

    def agregar_pool(self, b, z, costo, cf):
        """Columna que hoy no mejora: queda fuera del maestro hasta que el
        re-pricing la encuentre con costo reducido negativo."""
        clave = self._clave(z)
        if clave in self.vistas[b]:
            return False
        self.vistas[b].add(clave)
        self.pool[b].append((z, costo, cf))
        return True

    def reprecio(self, b, pi, sigma_b, thr, compatible=None):
        """Pasa al maestro las columnas del pool de b con costo reducido < -thr."""
        if not self.pool[b]:
            return 0
        quedan, n = [], 0
        for z, costo, cf in self.pool[b]:
            if compatible is not None and not compatible(b, z):
                quedan.append((z, costo, cf))
                continue
            rc = costo - sum(pi[r] * v for r, v in cf) - sigma_b
            if rc < -thr:
                self._agregar_col(b, z, costo, cf)
                n += 1
            else:
                quedan.append((z, costo, cf))
        self.pool[b] = quedan
        return n

    def poner_box(self, centro, delta):
        """Caja de los duales de las filas extendidas: pi en [centro - delta,
        centro + delta], via los costos de las artificiales (una artificial con
        coeficiente -1 y costo c impone pi >= -c; con +1, pi <= c)."""
        costos = [-(centro[r] - delta[r]) if s < 0 else centro[r] + delta[r]
                  for r, s in self.art]
        self.m.setAttr("Obj", self.art_vars[:self.n_art_filas], costos)
        self.box = True

    def quitar_box(self):
        if self.box:
            self.m.setAttr("Obj", self.art_vars[:self.n_art_filas],
                           [self.M_art] * self.n_art_filas)
        self.box = False

    def poner_cotas_x(self, lb, ub):
        self.m.setAttr("LB", self.xl, list(map(float, lb)))
        self.m.setAttr("UB", self.xl, list(map(float, ub)))

    def filtrar(self, b, compatible):
        """lambda <= 0 para las columnas de b incompatibles con el nodo."""
        lams = self.lams[b]
        if lams:
            self.m.setAttr("UB", [l[0] for l in lams],
                           [INF if compatible(b, l[1]) else 0.0 for l in lams])

    def resolver(self, barrier=False):
        m = self.m
        m.Params.Method = 2 if barrier else 1
        m.Params.Crossover = 0 if barrier else -1
        m.optimize()
        if m.Status in (GRB.UNBOUNDED, GRB.INF_OR_UNBD):
            raise MaestroNoAcotado()
        if m.Status != GRB.OPTIMAL:
            raise RuntimeError(f"maestro restringido sin optimo (status {m.Status})")
        pi = np.array(m.getAttr("Pi", self.filas))
        sigma = np.array(m.getAttr("Pi", self.conv))
        xm = np.array(m.getAttr("X", self.xl))
        av = np.array(m.getAttr("X", self.art_vars))
        return (m.ObjVal + self.E.objcon, pi, sigma, xm, float(av.sum()),
                float(av[:self.n_art_filas].sum()))


# --------------------------------------------------------------------------
# Generacion de columnas
# --------------------------------------------------------------------------

class GeneracionColumnas:

    def __init__(self, E, jobs=None, hilos=None, out=None, verbose=True, M_art=1e7):
        self.E = E
        cpu = os.cpu_count() or 8
        # Mejora 4: por defecto mas bloques a la vez con menos hilos cada uno
        # (un MILP de ~15 mil variables no aprovecha 8 hilos; con 4 a la vez una
        # ronda de 16 bloques eran 4 tandas en serie).
        if hilos is None:
            hilos = 4 if jobs is None else max(1, cpu // jobs)
        if jobs is None:
            jobs = max(1, min(E.nb, cpu // hilos))
        self.jobs, self.hilos = jobs, hilos
        t0 = time.time()
        self.pricing = [Pricing(E, b, hilos) for b in range(E.nb)]
        self.maestro = Maestro(E, _env(min(cpu, 8)), M_art=M_art)
        self.pool = ThreadPoolExecutor(max_workers=jobs)
        self.out, self.verbose = out, verbose
        self.historia = []
        self.trayectoria = []
        self.UB, self.mejor_vec, self.origen_ub = INF, None, None
        self.LB_ext, self.pi0, self.xm0 = -INF, None, None
        self.LB_global = -INF
        self.gemelos = {}
        self.lb_nodo, self.ub_nodo = E.lb_m.copy(), E.ub_m.copy()
        self.cotas_nodo = {}
        self._cz = {b: (E.bloques[b]["lb"].copy(), E.bloques[b]["ub"].copy())
                    for b in range(E.nb)}
        self._F_vars = E.F.getVars()
        self.t_inicio = time.time()
        self._log(f"{E.nb} pricing y maestro armados en {time.time() - t0:.0f}s "
                  f"({jobs} bloques a la vez x {hilos} hilos)")

    def _log(self, s):
        if self.verbose:
            print(f"[CG] {s}", flush=True)

    def _t(self):
        return time.time() - self.t_inicio

    def _punto(self, fase, LB=None):
        """Registra la trayectoria (tiempo desde el arranque, UB, LB global)."""
        if LB is not None and LB > self.LB_global:
            self.LB_global = LB
        self.trayectoria.append({"t": round(self._t(), 1), "UB": self.UB,
                                 "LB": self.LB_global, "fase": fase})

    def inicializar(self, vec):
        """Columnas iniciales desde una solucion completa (vector del MPS)."""
        for b in range(self.E.nb):
            z = self.E.z_de(b, vec)
            z[self.E.bloques[b]["ent"]] = np.round(z[self.E.bloques[b]["ent"]])
            if self._compatible(b, z):
                self.maestro.agregar(b, z)
            self.pricing[b].start = z

    # ------------------------------------------------------------------ #
    # Mejora 2: relajacion inicial (cota, duales, punto de partida)
    # ------------------------------------------------------------------ #
    def relajacion_inicial(self, modo="operacional", timelimit=600.0, gap=1e-3):
        """Los duales iniciales salen SIEMPRE de la relajacion LP completa: es
        dual-factible con la inversion libre (los duales de copia <= capacidad
        reflejan el costo de invertir). Los del LP con la inversion fija no: ahi
        esas filas tienen dual ~0, la caja hace barato violar la capacidad y el
        maestro estabilizado se va a ~8 % del costo (medido a 1 año).
        modo "operacional" agrega la relajacion operacional (inversion entera,
        operacion LP: la cota de Nested Benders), que da una cota mas fuerte y un
        punto de partida con la inversion entera para la heuristica.
        Deja self.pi0, self.xm0 y self.LB_ext (cota valida)."""
        E, F = self.E, self.E.F
        t0 = time.time()
        F.update()                  # relax()/copy() no ven cambios pendientes (ver _pulir)
        R = F.relax()
        R.Params.Method, R.Params.Crossover, R.Params.TimeLimit = 2, 0, timelimit
        R.optimize()
        if R.Status != GRB.OPTIMAL:
            raise RuntimeError(f"relajacion LP sin optimo (status {R.Status})")
        lb = R.ObjVal - 1e-6 * abs(R.ObjVal)
        x = np.array(R.getAttr("X", R.getVars()))
        Pi = np.array(R.getAttr("Pi", R.getConstrs()))
        R.dispose()
        self._log(f"relajacion LP: {lb:,.2f} en {time.time() - t0:.0f}s")
        if modo == "operacional":
            t1 = time.time()
            R = F.copy()
            rv = R.getVars()
            sel = [rv[j] for j in range(E.nv) if E.var_blk[j] >= 0 and E.vtype[j] != "C"]
            R.setAttr("VType", sel, ["C"] * len(sel))
            R.Params.TimeLimit, R.Params.MIPGap = timelimit, gap
            R.optimize()
            if R.SolCount > 0:
                lb = max(lb, R.ObjBound)
                x = np.array(R.getAttr("X", rv))
                self._log(f"relajacion operacional: {R.ObjBound:,.2f} en {time.time() - t1:.0f}s")
            else:
                self._log(f"relajacion operacional sin solucion (status {R.Status}): "
                          f"queda la LP")
            R.dispose()
        elif modo != "lp":
            raise ValueError(f"relajacion desconocida: {modo}")
        self.pi0 = self._proyectar(self._duales_ext(Pi))
        self.xm0 = x[E.mvars]
        self.LB_ext = max(self.LB_ext, lb)
        self._log(f"relajacion {modo} por componente: {self.desglose(x)}")
        self._guardar_vector(f"relajacion_{modo}.npz", x)
        self._punto(f"relajacion {modo}", self.LB_ext)
        return {"LB": lb, "t": time.time() - t0, "modo": modo}

    def _duales_ext(self, Pi):
        """Duales del monolitico -> duales de las filas extendidas. Las del
        maestro son las mismas filas. Para una fila de enlace copia_bj - x_j = 0,
        el costo reducido nulo de la copia (que solo aparece en las filas del
        bloque) da mu_bj = -sum_{i en filas de b} Pi_i a_ij; con eso el costo
        reducido de x_j en la forma extendida coincide con el del monolitico.
        Las filas replicadas quedan con dual 0 (son redundantes)."""
        E = self.E
        pi = np.zeros(E.n_ext)
        for r, f in enumerate(E.ext):
            if "fila" in f:
                pi[r] = Pi[f["fila"]]
        for b in range(E.nb):
            if not E.enlace_de[b]:
                continue
            fb = E.filas_bloque[b]
            idx = np.concatenate([E._filas[i][0] for i in fb])
            val = np.concatenate([E._filas[i][1] * Pi[i] for i in fb])
            cop = np.array([j for _, j in E.enlace_de[b]], int)
            msk = np.isin(idx, cop)
            acc = defaultdict(float)
            for j, v in zip(idx[msk].tolist(), val[msk].tolist()):
                acc[j] += v
            for r, j in E.enlace_de[b]:
                pi[r] = -acc.get(j, 0.0)
        return pi

    def _proyectar(self, pi):
        """Signo de los duales (min: <= -> pi <= 0, >= -> pi >= 0): con el signo
        correcto la cota lagrangiana es valida para cualquier pi."""
        pi = np.array(pi, float)
        E = self.E
        pi[E.fila_menor] = np.minimum(pi[E.fila_menor], 0.0)
        pi[E.fila_mayor] = np.maximum(pi[E.fila_mayor], 0.0)
        return pi

    # ------------------------------------------------------------------ #
    # Nodos y cotas
    # ------------------------------------------------------------------ #
    def _compatible(self, b, z):
        ks = self.E.afectadas[b]
        if not len(ks):
            return True
        lbz, ubz = self._cz[b]
        return bool(np.all(z[ks] >= lbz[ks] - 1e-6) and np.all(z[ks] <= ubz[ks] + 1e-6))

    def _aplicar_nodo(self, cotas):
        """Cotas del nodo {posicion en el maestro: (lb, ub)} sobre las cotas
        globales: maestro, copias en el pricing y lambdas incompatibles."""
        E, M = self.E, self.maestro
        lb, ub = E.lb_m.copy(), E.ub_m.copy()
        for kk, (l, u) in cotas.items():
            lb[kk], ub[kk] = max(lb[kk], l), min(ub[kk], u)
        if cotas:
            lb, ub = E.propagar(lb, ub)
        if np.any(lb > ub + 1e-9):
            return False
        self.lb_nodo, self.ub_nodo, self.cotas_nodo = lb, ub, dict(cotas)
        M.poner_cotas_x(lb, ub)
        for b in range(E.nb):
            lbz, ubz = E.cotas_bloque(b, lb, ub)
            self._cz[b] = (lbz, ubz)
            self.pricing[b].poner_cotas(lbz, ubz)
            M.filtrar(b, self._compatible)
        return True

    def acotar_por_costo(self):
        """x_k <= (UB - c0)/c_k para las variables del maestro con costo c_k > 0
        (valido si todo costo es >= 0 sobre variables >= 0: no corta ninguna
        solucion mejor que la UB) y propagacion. Vuelve finitas las cotas de
        n_ssee_k o P_pot, que sin esto pueden dejar la cota lagrangiana en -inf."""
        E = self.E
        if not E.obj_no_negativo or self.UB >= INF:
            return 0
        lb, ub = E.lb_m.copy(), E.ub_m.copy()
        n = 0
        tope = self.UB - E.objcon
        for kk, j in enumerate(E.mvars):
            c = E.obj[j]
            if c <= 1e-9:
                continue
            u = tope / c
            if E.vtype[j] != "C":
                u = np.floor(u + 1e-9)
            u = max(u, lb[kk])
            if u < ub[kk] - 1e-9:
                ub[kk] = u
                n += 1
        if n:
            lb, ub = E.propagar(lb, ub)
            E.actualizar_cotas_maestro(lb, ub)
            self._aplicar_nodo(self.cotas_nodo)
        return n

    # ------------------------------------------------------------------ #
    # Mejora 1: UB dirigida por el maestro
    # ------------------------------------------------------------------ #
    def _cotas_heur(self, b, xm, capacidades=True, extra=0):
        """Cotas de un dia para la heuristica: capacidades <= redondeo hacia
        arriba del maestro (+ extra unidades) y estado (b_bar) fijo en el valor
        del maestro. capacidades=False: sin tope de capacidad."""
        E = self.E
        B = E.bloques[b]
        nloc = len(B["loc"])
        cot = {}
        if capacidades:
            for jm, k in E.sombra[b].items():
                v = float(xm[jm])
                if E.vtype[E.mvars[jm]] != "C":
                    u = float(np.ceil(v - 1e-6)) + extra
                else:
                    u = v * (1 + 1e-6) + 1e-6 + extra * max(0.1 * v, 100.0)
                cot[k] = (-INF, u)
        for k in E.propias[b]:
            jm = E.pos_m[int(B["cop"][k - nloc])]
            v = float(xm[jm])
            if B["vtype"][k] != "C":
                if capacidades:
                    cot[k] = (-INF, float(np.ceil(v - 1e-6)) + extra)
            else:
                p = self.pricing[b]
                v = min(max(v, p.lb_act[k]), p.ub_act[k])
                cot[k] = (v, v)
        return cot

    def _objetivo_heur(self, b, pi):
        """Objetivo de un dia en la heuristica: el de pricing en pi, pero cada
        copia de capacidad paga al menos su precio de INVERSION (el costo de una
        unidad mas en ese año, repartido entre los dias que la comparten). Con
        los duales de capacidad ~0 un dia aislado usaba toda la capacidad que se
        le dejara para ahorrar algo de energia, y el pulido la compraba para el
        resto del horizonte (10 años: +1 bahia, cargador y bateria desde el año
        3-4 y un modulo de subestacion mas que la relajacion operacional,
        +930 mil)."""
        if not hasattr(self, "_precios_inv"):
            E = self.E
            pos = {E.nombre[j]: kk for kk, j in enumerate(E.mvars)}
            n_comp = Counter(jm for bb in range(E.nb) for jm in E.sombra[bb])
            self._precios_inv = {}
            for bb in range(E.nb):
                d = {}
                for jm, k in E.sombra[bb].items():
                    nom = E.nombre[E.mvars[jm]]
                    c = float(E.obj[E.mvars[jm]])
                    if c <= 0:                        # stock: el costo esta en Delta_
                        kk = pos.get("Delta_" + nom)
                        c = float(E.obj[E.mvars[kk]]) if kk is not None else 0.0
                    if c > 0:
                        d[k] = c / max(1, n_comp[jm])
                self._precios_inv[bb] = d
        c = self.pricing[b].objetivo(pi)
        for k, p in self._precios_inv[b].items():
            c[k] = max(c[k], p)
        return c

    def _resolver_dias(self, bloques, objs, cotas, tl, gap):
        out = list(self.pool.map(
            lambda b: (b, self.pricing[b].resolver(objs[b], tl, gap, 1, cotas=cotas[b])),
            bloques))
        return dict(out)

    def _dias_graduales(self, bloques, objs, xm, tl, gap, xm_resp=None, pi=None):
        """Dias con las capacidades del maestro. Los que no caben se reintentan
        (xm_resp: con las capacidades de otro punto, p.ej. el del pulido
        parcial del respaldo año por año) y despues con la capacidad LIBRE pero
        cobrando la inversion (la fase 1 de _dias_fases, solo para esos dias):
        asi toman solo la capacidad extra que necesitan. Probado y descartado:
        +1/+2 unidades de TODAS las capacidades a la vez -- el dia usa todo lo
        que se le da (con duales de capacidad ~0 sale gratis) y el pulido lo
        compra (4 años: 2 bahias en vez de 1, 2,22 M en vez de 2,07 M). Si ni
        asi hay solucion, sin tope y con el objetivo de pricing."""
        res = self._resolver_dias(bloques, objs, {b: self._cotas_heur(b, xm) for b in bloques},
                                  tl, gap)
        if xm_resp is not None:
            malos = [b for b in bloques if not res[b]["ok"]]
            if malos:
                res.update(self._resolver_dias(
                    malos, objs, {b: self._cotas_heur(b, xm_resp) for b in malos}, tl, gap))
        malos = [b for b in bloques if not res[b]["ok"]]
        if malos:
            libres = {b: self._cotas_heur(b, xm, capacidades=False) for b in malos}
            res.update(self._resolver_dias(
                malos, {b: self._objetivo_heur(b, pi if pi is not None else getattr(self, "_pi_heur", None))
                        for b in malos}, libres,
                2 * tl, max(gap, 0.05)))
            malos = [b for b in malos if not res[b]["ok"]]
            if malos:
                self._log(f"   heuristica: {len(malos)} dias sin solucion ni con la capacidad "
                          f"libre cobrando la inversion -> objetivo de pricing")
                res.update(self._resolver_dias(malos, objs, {b: libres[b] for b in malos},
                                               2 * tl, max(gap, 0.05)))
        return res

    def _indices_estado(self):
        """[(año, pos b_bar(y), pos D(y-1), pos R(y))] en el maestro, por nombre."""
        import re
        E = self.E
        pos = {E.nombre[j]: k for k, j in enumerate(E.mvars)}
        out = []
        for n, k in pos.items():
            m = re.fullmatch(r"b_bar\((\d+)\)", n)
            if m:
                y = int(m.group(1))
                out.append((y, k, pos.get(f"D({y - 1})"), pos.get(f"R({y})")))
        return sorted(out)

    def _estado_sin_mezcla(self, xm):
        """El maestro LP mezcla columnas con reemplazo de baterias (R = 1,
        b_bar = nominal) y sin el, y da un b_bar intermedio que ninguna
        trayectoria real tiene: resueltos los dias con ese b_bar, el pulido
        termina pagando reemplazos para cuadrarlo (medido a 4 años: UB 5,4-6,0 M
        contra 2,07 M). Aca R se redondea y b_bar se pone coherente con la
        degradacion: D(y-1) si R(y) = 0, la nominal si R(y) = 1. Devuelve el
        maestro corregido y {variable del MPS: R redondeado} para el pulido."""
        E = self.E
        xm = np.array(xm, float)
        fijar = {}
        if not hasattr(self, "_estado"):
            self._estado = self._indices_estado()
        for y, kb, kd, kr in self._estado:
            r = int(round(xm[kr])) if kr is not None else 0
            if kr is not None:
                xm[kr] = r
                fijar[int(E.mvars[kr])] = float(r)
            if r == 0 and kd is not None:
                xm[kb] = min(max(xm[kd], self.lb_nodo[kb]), self.ub_nodo[kb])
            elif r == 1:
                xm[kb] = self.ub_nodo[kb]
        return xm, fijar

    def _pulir(self, zs, timelimit, gap=1e-4, fijar=None):
        """Fija las enteras propias de cada dia (operacion y copias de
        capacidad) y resuelve el resto del monolitico: inversion entera,
        degradacion y energia. fijar: {variable del MPS: valor} adicionales.
        Devuelve (costo, vector) o (None, None)."""
        E, F, vs = self.E, self.E.F, self._F_vars
        idx, vals = [], []
        for b, z in enumerate(zs):
            B = E.bloques[b]
            nloc = len(B["loc"])
            for k in B["ent"]:
                if k < nloc:
                    idx.append(int(B["loc"][k]))
                    vals.append(float(round(z[k])))
        for j, v in (fijar or {}).items():
            idx.append(int(j))
            vals.append(float(v))
        sel = [vs[j] for j in idx]
        lb0, ub0 = F.getAttr("LB", sel), F.getAttr("UB", sel)
        F.setAttr("LB", sel, vals)
        F.setAttr("UB", sel, vals)
        F.Params.TimeLimit = max(1.0, timelimit)
        F.Params.MIPGap = gap
        try:
            F.optimize()
            if F.SolCount == 0:
                return None, None
            return F.ObjVal, np.array(F.getAttr("X", vs))
        finally:
            F.setAttr("LB", sel, lb0)
            F.setAttr("UB", sel, ub0)
            # Gurobi aplica los cambios en el proximo optimize/update, y
            # Model.copy()/relax() copian SIN los pendientes: sin esto, una
            # relajacion armada despues de un pulido relajaba el modelo con la
            # operacion todavia fija (la "cota" era el costo de la solucion
            # pulida; medido en Nested Benders con --cg_lb, 2026-10-14).
            F.update()

    def _pulir_secuencial(self, xm, objs, tl_dia, gap_dia, tl_pulido, fijar=None):
        """Respaldo si el pulido es infactible (la operacion de un año se
        resolvio con un b_bar que la degradacion real no da): año por año, el
        monolitico con los años anteriores fijos y los siguientes relajados da
        el b_bar real del año, se resuelven sus dias con ese valor (y las
        capacidades del maestro) y se sigue. fijar: {variable del MPS: valor}
        en todos los pulidos (el R redondeado: asi el b_bar real sale sin
        reemplazos que el maestro no pidio)."""
        E, F, vs = self.E, self.E.F, self._F_vars
        anios = sorted({a for a in E.anio if a is not None})
        if not anios:
            return None, None, None
        if not hasattr(self, "_estado"):
            self._estado = self._indices_estado()
        ints = {a: [] for a in anios}
        for b in range(E.nb):
            B = E.bloques[b]
            nloc = len(B["loc"])
            ints[E.anio[b]].extend((b, k, int(B["loc"][k])) for k in B["ent"] if k < nloc)
        extra = list((fijar or {}).items())
        sel_x = [vs[j] for j, _ in extra]
        zs = [None] * E.nb
        for a in anios:
            fijas = [(b, k, j) for aa in anios if aa < a for (b, k, j) in ints[aa]]
            relaj = [j for aa in anios if aa >= a for (_, _, j) in ints[aa]]
            sel_f = [vs[j] for _, _, j in fijas] + sel_x
            sel_r = [vs[j] for j in relaj]
            lb0, ub0 = F.getAttr("LB", sel_f), F.getAttr("UB", sel_f)
            vt0 = F.getAttr("VType", sel_r)
            try:
                vals = [float(round(zs[b][k])) for b, k, _ in fijas] + [float(v) for _, v in extra]
                F.setAttr("LB", sel_f, vals)
                F.setAttr("UB", sel_f, vals)
                F.setAttr("VType", sel_r, ["C"] * len(sel_r))
                F.Params.TimeLimit, F.Params.MIPGap = max(1.0, tl_pulido), 1e-3
                F.optimize()
                if F.SolCount == 0:
                    return None, None, None
                xa = np.array(F.getAttr("X", vs))[E.mvars]
            finally:
                F.setAttr("LB", sel_f, lb0)
                F.setAttr("UB", sel_f, ub0)
                F.setAttr("VType", sel_r, vt0)
                F.update()                        # ver _pulir
            # Capacidades del maestro, estado (b_bar) real de este pulido.
            xh = np.array(xm, float)
            for _y, kb, _kd, _kr in self._estado:
                xh[kb] = xa[kb]
            bl = [b for b in range(E.nb) if E.anio[b] == a]
            res = self._dias_graduales(bl, objs, xh, tl_dia, gap_dia, xm_resp=xa)
            for b in bl:
                if not res[b]["ok"]:
                    return None, None, None
                zs[b] = res[b]["sols"][0]
        costo, vec = self._pulir(zs, tl_pulido, fijar=fijar)
        return costo, vec, zs

    def heuristica_maestro(self, xm, pi=None, tl_dia=60.0, gap_dia=0.01, tl_pulido=300.0,
                           refinar=1, variantes=("coherente",)):
        """Mejora 1. Dias resueltos por separado con la inversion y el estado
        del maestro fijos, pulido del monolitico con la operacion fija, y
        refinamiento de b_bar. pi: duales para el objetivo de los dias (cobra la
        capacidad y la energia); None = costos originales.
        variantes: "coherente" (b_bar coherente con el R redondeado, R fijo en
        el pulido; capacidades del maestro), "directa" (b_bar y R tal como
        vienen del maestro), "redondeo" (como coherente, pero la capacidad del
        maestro al entero mas cercano) y/o "fases" (estado coherente, pero la capacidad la
        deciden los dias: el esquema de day_blocks de Nested Benders, ver
        _dias_fases). Devuelve la mejor."""
        mejor = (None, None)
        for v in variantes:
            c, vec = self._heuristica_maestro_una(xm, pi, tl_dia, gap_dia, tl_pulido, refinar,
                                                  modo=v)
            if len(variantes) > 1:
                self._log(f"   variante {v}: "
                          + ("sin solucion" if c is None else f"{c:,.2f} ({self.desglose(vec)})"))
            if c is not None and (mejor[0] is None or c < mejor[0]):
                mejor = (c, vec)
        return mejor

    def desglose(self, vec, n=8):
        """Costo por componente (el nombre antes del indice) de un vector del
        MPS, de mayor a menor: para ver de donde sale una UB mala."""
        E = self.E
        por = defaultdict(float)
        for j in np.flatnonzero(np.abs(E.obj * vec) > 1e-9):
            por[E.nombre[j].split("(")[0]] += float(E.obj[j] * vec[j])
        top = sorted(por.items(), key=lambda kv: -abs(kv[1]))[:n]
        return ", ".join(f"{k} {v:,.0f}" for k, v in top)

    def _guardar_vector(self, nombre, vec):
        if not self.out or vec is None:
            return
        E = self.E
        nz = np.flatnonzero(np.abs(vec) > 1e-9)
        np.savez_compressed(os.path.join(self.out, nombre),
                            nombres=np.array([E.nombre[j] for j in nz]), valores=vec[nz])

    def _dias_fases(self, xm, pi, objs, tl, gap):
        """Capacidad decidida por los dias, como day_blocks de Nested Benders:
          Fase 1      cada dia con la infraestructura LIBRE y cobrando la
                      inversion (_objetivo_heur): sin tope siempre hay solucion
                      rapida desde el start del pricing, y la inversion la
                      mantiene minima.
          Agregacion  por capacidad, el maximo entre dias; propagado por las
                      filas de stock (N(y) >= N(y-1)).
          Fase 2      cada dia otra vez con esa capacidad como tope, desde la
                      solucion de la fase 1 (que cabe por construccion).
        Cobrar la inversion CON tope (lo primero que se probo) dejaba al MILP
        del dia sin solucion en 60 s. Devuelve (resultados, punto del maestro
        con la capacidad agregada) o (None, None)."""
        E = self.E
        bl = list(range(E.nb))
        cot1 = {b: self._cotas_heur(b, xm, capacidades=False) for b in bl}
        obj1 = {b: self._objetivo_heur(b, pi) for b in bl}
        r1 = self._resolver_dias(bl, obj1, cot1, 2 * tl, max(gap, 0.05))
        malos = [b for b in bl if not r1[b]["ok"]]
        if malos:
            r1.update(self._resolver_dias(malos, objs, {b: cot1[b] for b in malos},
                                          2 * tl, max(gap, 0.05)))
            if any(not r1[b]["ok"] for b in malos):
                self._log(f"   fases: fase 1 sin solucion en "
                          f"{[E.claves[b] for b in malos if not r1[b]['ok']]}")
                return None, None
        lb, ub = self.lb_nodo.copy(), self.ub_nodo.copy()
        for b in bl:
            z = r1[b]["sols"][0]
            for jm, k in E.sombra[b].items():
                v = float(z[k])
                if E.vtype[E.mvars[jm]] != "C":
                    v = float(np.ceil(v - 1e-6))
                lb[jm] = max(lb[jm], v)
        lb, _ub = E.propagar(lb, np.maximum(ub, lb))
        xm2 = np.array(xm, float)
        for b in bl:
            for jm in E.sombra[b]:
                xm2[jm] = lb[jm]
        for b in bl:
            self.pricing[b].start = r1[b]["sols"][0]
        return self._dias_graduales(bl, objs, xm2, tl, gap), xm2

    def _capacidades_redondeadas(self, xm):
        """Capacidades enteras del maestro al entero MAS CERCANO (en vez de
        hacia arriba). El maestro de la CG trae capacidades apenas fraccionarias
        (p.ej. 2,3 cargadores) y redondear hacia arriba suma una unidad de cada
        cosa (10 años: 3.750.698 contra 2.790.392 con la capacidad entera de la
        relajacion). Los dias que no caben compran solo lo que les falta en el
        respaldo que cobra la inversion (_dias_graduales)."""
        E = self.E
        xm = np.array(xm, float)
        caps = {jm for b in range(E.nb) for jm in E.sombra[b]}
        for b in range(E.nb):
            B = E.bloques[b]
            nloc = len(B["loc"])
            for k in E.propias[b]:
                if B["vtype"][k] != "C":
                    caps.add(E.pos_m[int(B["cop"][k - nloc])])
        for jm in caps:
            if E.vtype[E.mvars[jm]] != "C":
                xm[jm] = min(max(np.floor(xm[jm] + 0.5), self.lb_nodo[jm]), self.ub_nodo[jm])
        return xm

    def _heuristica_maestro_una(self, xm, pi, tl_dia, gap_dia, tl_pulido, refinar, modo):
        E = self.E
        self._pi_heur = pi
        t0 = time.time()
        presupuesto = getattr(self, "tl_heur_max", 900.0)

        def sin_tiempo(paso):
            if time.time() - t0 > presupuesto:
                self._log(f"   heuristica del maestro: {time.time() - t0:.0f}s > {presupuesto:.0f}s, "
                          f"no se intenta {paso}")
                return True
            return False
        if modo in ("coherente", "fases", "redondeo"):
            xm, fijar_R = self._estado_sin_mezcla(xm)
        else:
            fijar_R = {}
        if modo == "redondeo":
            xm = self._capacidades_redondeadas(xm)
        objs = {b: self.pricing[b].objetivo(pi) for b in range(E.nb)}
        if modo == "fases":
            res, xm2 = self._dias_fases(xm, pi, objs, tl_dia, gap_dia)
            if res is None:
                return None, None
            xm = xm2
        else:
            # Capacidad del maestro insuficiente para la operacion entera de un
            # dia: se agranda de a poco (el pulido la paga).
            res = self._dias_graduales(list(range(E.nb)), objs, xm, tl_dia, gap_dia)
        malos = [b for b in range(E.nb) if not res[b]["ok"]]
        if malos:
            self._log(f"   heuristica del maestro: dias sin solucion {[E.claves[b] for b in malos]}")
            return None, None
        zs = [res[b]["sols"][0] for b in range(E.nb)]
        # Orden de respaldo: pulido con R fijo; año por año con R fijo (b_bar
        # real de cada año); recien despues R libre, que puede terminar pagando
        # reemplazos que el maestro no pidio (medido a 4 años: 5,2-6,4 M).
        costo, vec = self._pulir(zs, tl_pulido, fijar=fijar_R)
        camino = "directo"
        if costo is None and fijar_R and not sin_tiempo("año por año con R fijo"):
            costo, vec, zs2 = self._pulir_secuencial(xm, objs, tl_dia, gap_dia, tl_pulido,
                                                     fijar=fijar_R)
            camino = "año por año con R fijo"
            if costo is not None:
                zs = zs2
        if costo is None and not sin_tiempo("R libre"):
            costo, vec = self._pulir(zs, tl_pulido)
            camino = "R libre"
        if costo is None and not sin_tiempo("año por año con R libre"):
            costo, vec, zs2 = self._pulir_secuencial(xm, objs, tl_dia, gap_dia, tl_pulido)
            camino = "año por año con R libre"
            if costo is not None:
                zs = zs2
        if costo is None:
            self._log("   heuristica del maestro: ningun pulido factible")
            return None, None
        if camino != "directo":
            self._log(f"   heuristica del maestro: pulido directo infactible -> {camino}: "
                      f"{costo:,.2f}")
        for _ in range(refinar):
            rehacer = []
            for b in range(E.nb):
                B = E.bloques[b]
                nloc = len(B["loc"])
                for k in E.propias[b]:
                    if B["vtype"][k] != "C":
                        continue
                    real = vec[int(B["cop"][k - nloc])]
                    if abs(real - zs[b][k]) > 1e-3 * max(1.0, abs(real)):
                        rehacer.append(b)
                        break
            if not rehacer:
                break
            xv = vec[E.mvars]
            r2 = self._resolver_dias(rehacer, objs, {b: self._cotas_heur(b, xv) for b in rehacer},
                                     tl_dia, gap_dia)
            zs2 = list(zs)
            for b, r in r2.items():
                if r["ok"]:
                    zs2[b] = r["sols"][0]
            c2, v2 = self._pulir(zs2, tl_pulido)
            if c2 is not None and c2 < costo - 1e-6:
                costo, vec, zs = c2, v2, zs2
            else:
                break
        return costo, vec

    def _registrar_ub(self, costo, vec, origen):
        if costo is None:
            return False
        E = self.E
        # Las columnas de la solucion entran igual: el maestro restringido
        # queda con una combinacion entera factible.
        for b in range(E.nb):
            z = E.z_de(b, vec)
            z[E.bloques[b]["ent"]] = np.round(z[E.bloques[b]["ent"]])
            if self._compatible(b, z):
                self.maestro.agregar(b, z)
            else:
                c, cf = self.maestro.costo_cf(b, z)
                self.maestro.agregar_pool(b, z, c, cf)
        if costo < self.UB - 1e-6:
            self.UB, self.mejor_vec, self.origen_ub = costo, vec, origen
            self._punto(origen)
            self._log(f"   nueva UB {costo:,.2f} por componente: {self.desglose(vec)}")
            self._guardar_vector("mejor_solucion.npz", vec)
            n = self.acotar_por_costo()
            if n:
                self._log(f"   cotas por costo: {n} variables del maestro acotadas con la UB")
            return True
        return False

    # Heuristica anterior: maestro entero sobre las columnas.
    def _elegir_columnas_mip(self, timelimit, soltar_enlace=False):
        """Maestro restringido ENTERO sobre el pool: una columna por bloque
        (lambda binaria), inversion entera, sin artificiales. Con
        soltar_enlace=True se sueltan las filas de enlace de las variables
        CONTINUAS (b_bar, ...) -- solo como respaldo: sin ellas el maestro
        combina dias con b_bar distintas y el monolitico termina pagando un
        reemplazo de baterias para cuadrarlas (medido en el año 6: UB ~1,06 M
        contra 56 mil). Devuelve el indice elegido por bloque, o None."""
        E, M = self.E, self.maestro
        M.m.update()
        R = M.m.copy()
        try:
            R.Params.OutputFlag = 0
            R.Params.TimeLimit = timelimit
            R.Params.MIPGap = 1e-3
            rv = R.getVars()
            nx = len(E.mvars)
            for k, j in enumerate(E.mvars):
                if E.vtype[j] != "C":
                    rv[k].VType = GRB.INTEGER
            for v in rv[nx:nx + len(M.art_vars)]:
                v.UB = 0.0
            pos_lam = {}
            for b in range(E.nb):
                for i, (lam, *_r) in enumerate(M.lams[b]):
                    pos_lam[(b, i)] = lam.index
                    rv[lam.index].VType = GRB.BINARY
            if soltar_enlace:
                rc = R.getConstrs()
                for r, f in enumerate(E.ext):
                    if f["nombre"].startswith("enlace_"):
                        j = E.mvars[f["m_pos"][0]]
                        if E.vtype[j] == "C":
                            R.remove(rc[r])
            R.optimize()
            if R.SolCount == 0:
                return None
            x = R.getAttr("X", rv)
            return [max(range(len(M.lams[b])), key=lambda i: x[pos_lam[(b, i)]])
                    for b in range(E.nb)]
        finally:
            R.dispose()

    def ub_columnas(self, timelimit=300.0, gap=1e-3):
        """UB anterior, por precio-y-fijacion: en cada bloque la columna elegida
        por el maestro entero; se fijan sus enteras en el monolitico y se
        resuelve el resto. Devuelve (costo, vector) o (None, None)."""
        E, M = self.E, self.maestro
        if any(not M.lams[b] for b in range(E.nb)):
            return None, None
        elegidas = self._elegir_columnas_mip(min(timelimit, 120.0))
        self.ub_con_enlace = elegidas is not None
        if elegidas is None:
            elegidas = self._elegir_columnas_mip(min(timelimit, 120.0), soltar_enlace=True)
        if elegidas is None:
            M.resolver(barrier=False)
            elegidas = []
            for b in range(E.nb):
                xs = M.m.getAttr("X", [l[0] for l in M.lams[b]])
                elegidas.append(int(np.argmax(xs)))
        zs = [M.lams[b][elegidas[b]][1] for b in range(E.nb)]
        return self._pulir(zs, timelimit, gap)

    ub_heuristica = ub_columnas      # nombre anterior

    def _heuristicas_rmp(self, xm, art, caja_activa, pi_c, delta, pi, modo, etiqueta, kw_h):
        """Heuristicas desde el maestro restringido de una ronda. Con la caja
        activa el punto del maestro viene distorsionado (artificiales baratas):
        se re-resuelve sin caja y, si todavia usa artificiales, no se intenta
        (a 10 años la heuristica desde ese punto gastaba 27 min y fallaba)."""
        if caja_activa:
            xm, art = self._xm_sin_caja()
            self.maestro.poner_box(pi_c, delta)
        if art > 1e-6:
            self._log(f"   UB ({etiqueta}): se omite, el maestro restringido todavia usa "
                      f"artificiales ({art:.2g})")
            return
        self._heuristicas(xm, pi, modo, art, etiqueta, **kw_h)

    def _heuristicas(self, xm, pi, modo, art, etiqueta, tl_dia, gap_dia, tl_pulido, refinar,
                     variantes=("coherente",)):
        if xm is None:
            return
        if modo in ("maestro", "ambos"):
            tu = time.time()
            c, v = self.heuristica_maestro(xm, pi, tl_dia, gap_dia, tl_pulido, refinar,
                                           variantes=variantes)
            mej = self._registrar_ub(c, v, f"maestro {etiqueta}")
            txt = "sin solucion" if c is None else f"{c:,.2f}"
            self._log(f"   UB maestro ({etiqueta}): {txt}{'  *** nueva UB' if mej else ''}"
                      f"  ({time.time() - tu:.0f}s)")
        if modo in ("columnas", "ambos") and art < 1e-6:
            tu = time.time()
            c, v = self.ub_columnas(tl_pulido)
            mej = self._registrar_ub(c, v, f"columnas {etiqueta}")
            txt = "sin solucion" if c is None else f"{c:,.2f}"
            enl = "con enlace" if getattr(self, "ub_con_enlace", False) else "enlace SUELTO"
            self._log(f"   UB columnas ({etiqueta}, {enl}): {txt}"
                      f"{'  *** nueva UB' if mej else ''}  ({time.time() - tu:.0f}s)")

    # ------------------------------------------------------------------ #
    def lagrangiano(self, pi, bounds, xm_rmp):
        """Cota lagrangiana en pi (con las cotas del nodo) y la x* del maestro
        que la realiza."""
        E = self.E
        rc = E.obj[E.mvars] - E.A_M.T @ pi
        lb, ub = self.lb_nodo, self.ub_nodo
        tol = 1e-7 * np.maximum(1.0, np.abs(E.obj[E.mvars]))
        xs = xm_rmp.copy()
        total = float(np.dot(pi, E.rhs_ext)) + E.objcon
        for k in range(len(rc)):
            if rc[k] > tol[k]:
                if lb[k] <= -INF:
                    return -INF, None
                xs[k] = lb[k]
            elif rc[k] < -tol[k]:
                if ub[k] >= INF:
                    return -INF, None
                xs[k] = ub[k]
            else:
                continue
            total += rc[k] * xs[k]
        for bnd in bounds:
            if bnd <= -INF:
                return -INF, None
            total += min(bnd, self.maestro.M_art)
        return total, xs

    def _subgradiente(self, xs, zs):
        E = self.E
        act = E.A_M @ xs
        for b, z in enumerate(zs):
            if z is None:
                continue
            for r, v in E.coefs(b, z):
                act[r] += v
        return E.rhs_ext - act

    def _delta(self, pi_c, rel):
        a = np.abs(pi_c)
        escala = float(a[a > 0].mean()) if np.any(a > 0) else 1.0
        return rel * np.maximum(a, 0.1 * escala)

    def _procesar(self, b, sols, pi_rmp, sigma, thr, usar_pool):
        """Columnas del bloque b (y de sus gemelos): al maestro las de costo
        reducido < -thr en los duales del maestro, al pool las demas."""
        M = self.maestro
        cands = [(b, z) for z in sols]
        for z in sols:
            for b2, perm in self.gemelos.get(b, ()):
                z2 = np.empty_like(z)
                z2[perm] = z
                cands.append((b2, z2))
        n, mejor = 0, None
        for bb, z in cands:
            if not self._compatible(bb, z):
                continue
            costo, cf = M.costo_cf(bb, z)
            rc = costo - sum(pi_rmp[r] * v for r, v in cf) - sigma[bb]
            if rc < -thr:
                if M.agregar(bb, z, costo, cf):
                    n += 1
                    if bb == b and (mejor is None or rc < mejor[0]):
                        mejor = (rc, z)
            elif usar_pool:
                M.agregar_pool(bb, z, costo, cf)
        return n, mejor

    # ------------------------------------------------------------------ #
    def fraccionalidad(self):
        """Por bloque: columnas con lambda > 0, la mayor lambda y el rango de
        las copias de enlace (b_bar, ...) entre las columnas usadas. Dice si el
        maestro LP es casi entero o una mezcla."""
        E, M = self.E, self.maestro
        M.resolver(barrier=False)
        filas = []
        for b in range(E.nb):
            lams = M.lams[b]
            if not lams:
                continue
            xs = np.array(M.m.getAttr("X", [l[0] for l in lams]))
            usadas = np.where(xs > 1e-6)[0]
            enl = {}
            for j, k in E.bloques[b]["cpos"].items():
                vals = [lams[i][1][k] for i in usadas]
                enl[E.nombre[j]] = (min(vals), max(vals)) if vals else None
            filas.append({"bloque": E.claves[b], "usadas": int(len(usadas)),
                          "lambda_max": float(xs.max()), "enlace": enl})
        return filas

    # ------------------------------------------------------------------ #
    def correr(self, estab="box+wentges", alpha0=0.5, compartir=True,
               heur=(30.0, 0.02, 5), exacto=(300.0, None), tl_compartir=10.0,
               tiempo_max=None, max_rondas=10_000, tol_gap=1e-4, objetivo=None,
               ub_cada=5, ub_timelimit=300.0, UB0=INF,
               ub_modo="ambos", tl_dia=60.0, gap_dia=0.01, refinar=1,
               exacto_cada=0, estanc_ventana=5, estanc_tol=1e-4, rc_min=1e-5,
               pool_columnas=True, parcial=True, precio_completo_cada=5, saltar_tras=2,
               compartir_cada=3, box_rel=1.0, box_crece=4.0,
               centro=None, LB0=-INF, deadline=None, fase="raiz", gap_ub=None,
               ub_final=True, tl_exacto_corto=60.0, variantes=("coherente",)):
        """estab: combinacion con '+' de "wentges", "barrier", "box" (o
        "ninguna"). ub_cada: cada cuantas rondas se intentan las UB heuristicas
        (0 = solo al final); ub_modo: "maestro", "columnas" o "ambos".
        exacto = (tope, gap relativo o None = solo el absoluto tol_gap*|z|/nb):
        la ronda exacta COMPLETA, cuando el pricing heuristico ya no da columnas.
        Las disparadas por estancamiento o cada `exacto_cada` rondas son un
        intento CORTO (tl_exacto_corto): con duales lejos del optimo el pricing
        exacto agota cualquier tope y la cota casi no sube (medido a 2 años:
        rondas de 300 s que subian la cota de a 0,1 %, contra 28 s de la exacta
        final con los duales ya buenos).
        Devuelve LB (cota del nodo), UB, z_rmp, xm (maestro), pi_c (centro)."""
        E, M = self.E, self.maestro
        t0 = time.time()
        if tiempo_max is not None:
            deadline = min(deadline if deadline is not None else INF, t0 + tiempo_max)
        if UB0 < self.UB:
            self.UB = UB0
        tok = set(estab.split("+")) - {"ninguna", ""}
        barrier, suavizar, caja = "barrier" in tok, "wentges" in tok, "box" in tok
        pi_c = centro if centro is not None else self.pi0
        pi_c = None if pi_c is None else self._proyectar(pi_c)
        alpha, delta = alpha0, None
        if caja and pi_c is not None:
            delta = self._delta(pi_c, box_rel)
            M.poner_box(pi_c, delta)
        else:
            M.quitar_box()
        LB, L_best = LB0, -INF
        modo, vertice, tl_ex = "heur", False, exacto[0]
        gap_ex = exacto[1] if exacto[1] is not None else 1e-9
        ultima_exacta, hist_z, estancado, forzar_completa = 0, [], False, False
        dormido = np.zeros(E.nb, int)
        z_rmp, xm, pi_rmp, art, k, convergio = INF, None, None, INF, 0, False
        reintentos = 0
        kw_h = dict(tl_dia=tl_dia, gap_dia=gap_dia, tl_pulido=ub_timelimit, refinar=refinar,
                    variantes=tuple(variantes))

        while k < max_rondas:
            k += 1
            tr = time.time()
            try:
                z_rmp, pi_rmp, sigma, xm, art, art_filas = M.resolver(barrier=barrier and not vertice)
            except MaestroNoAcotado:
                # La caja excluye todo dual factible (centro malo): se agranda,
                # y si no alcanza se saca.
                reintentos += 1
                if caja and delta is not None and reintentos <= 6:
                    delta = delta * 10.0
                    M.poner_box(pi_c, delta)
                elif caja:
                    M.quitar_box()
                    caja, delta = False, None
                else:
                    raise RuntimeError("maestro restringido no acotado sin caja")
                k -= 1
                continue
            reintentos = 0
            pi_rmp = self._proyectar(pi_rmp)
            t_rmp = time.time() - tr
            exacta_completa = modo == "exact"
            exacta = exacta_completa or estancado or bool(
                exacto_cada and k > 1 and k - ultima_exacta >= exacto_cada)
            estancado = False
            thr = rc_min * max(1.0, abs(z_rmp))
            completa = (exacta or not parcial or k == 1 or forzar_completa
                        or (precio_completo_cada and k % precio_completo_cada == 0))
            forzar_completa = False

            # Pool: columnas viejas que hoy mejoran (mejora 4).
            nuevas_pool, satisf = 0, set()
            if pool_columnas:
                for b in range(E.nb):
                    n = M.reprecio(b, pi_rmp, sigma[b], thr, self._compatible)
                    if n:
                        satisf.add(b)
                        nuevas_pool += n
            if completa:
                bloques = list(range(E.nb))
            else:
                bloques = [b for b in range(E.nb) if b not in satisf and dormido[b] < saltar_tras]
                if not bloques and nuevas_pool == 0:
                    completa, bloques = True, list(range(E.nb))

            # Pricing; si con Wentges no sale nada (mispricing) se reintenta en
            # la misma ronda con los duales del maestro (mejora 3).
            usar_w = suavizar and pi_c is not None and alpha > 0
            alpha_usado = alpha if usar_w else 0.0
            if exacta:
                tl, gap, npool = (tl_ex if exacta_completa else tl_exacto_corto), gap_ex, 2
                gabs = max(1e-6, tol_gap * max(1.0, abs(z_rmp)) / E.nb)
            else:
                tl, gap, npool, gabs = heur[0], heur[1], heur[2], None
            if deadline is not None:
                tl = min(tl, max(1.0, deadline - time.time()))
            nuevas, mejores, L, xs_L, pi_L, t_pr, res = 0, {}, -INF, None, None, 0.0, {}
            for _intento in range(2):
                pi = alpha * pi_c + (1 - alpha) * pi_rmp if usar_w else pi_rmp
                objs = {b: self.pricing[b].objetivo(pi) for b in bloques}
                tp = time.time()
                res = dict(self.pool.map(
                    lambda b: (b, self.pricing[b].resolver(objs[b], tl, gap, npool, gap_abs=gabs)),
                    bloques))
                t_pr += time.time() - tp
                for b in bloques:
                    r = res[b]
                    n_b, mej = self._procesar(b, r["sols"], pi_rmp, sigma, thr, pool_columnas)
                    nuevas += n_b
                    if mej is not None:
                        mejores[b] = mej
                    if r["sols"]:
                        self.pricing[b].start = r["sols"][0]
                if len(bloques) == E.nb:
                    Li, xs = self.lagrangiano(pi, [res[b]["bound"] for b in range(E.nb)], xm)
                    if Li > L:
                        L, xs_L, pi_L = Li, xs, pi.copy()
                if usar_w and nuevas == 0 and nuevas_pool == 0:
                    alpha, usar_w = 0.0, False
                    continue
                break

            # Paso serio: contra la mejor lagrangiana VISTA, no contra LB (que
            # puede venir de afuera, p.ej. la cota operacional, y entonces el
            # centro no se moveria nunca).
            serio = L > -INF and (L_best <= -INF or L > L_best + 1e-9 * max(1.0, abs(L_best)))
            if serio:
                L_best = L
            mejora = L > -INF and (LB <= -INF or L > LB + 1e-9 * max(1.0, abs(LB)))
            if mejora:
                LB = L
            if pi_L is not None and (serio or pi_c is None):
                pi_c = pi_L.copy()                    # se mueve el centro
                if caja:
                    delta = self._delta(pi_c, box_rel)
                    M.poner_box(pi_c, delta)
            for b in bloques:
                dormido[b] = 0 if b in mejores else dormido[b] + 1

            # Column Sharing: la capacidad de la mejor columna nueva, a los hermanos.
            compartidas, t_cs = 0, 0.0
            if (compartir and mejores and not exacta
                    and (compartir_cada <= 1 or k % compartir_cada == 0)):
                tc = time.time()
                trabajos = []
                for b, (_, z) in mejores.items():
                    for bb in E.hermanos[b]:
                        fijar = {E.sombra[bb][j]: z[p] for j, p in E.sombra[b].items()}
                        # Tambien las copias de enlace (b_bar, ...): asi los
                        # hermanos traen columnas con la MISMA b_bar y el maestro
                        # entero puede combinarlas sin soltar el enlace.
                        cp_bb = E.bloques[bb]["cpos"]
                        for j, p in E.bloques[b]["cpos"].items():
                            if j in cp_bb:
                                fijar[cp_bb[j]] = z[p]
                        trabajos.append((bb, fijar))
                objs_c = {bb: self.pricing[bb].objetivo(pi) for bb, _ in trabajos}
                out = list(self.pool.map(
                    lambda tb: (tb[0], self.pricing[tb[0]].resolver(
                        objs_c[tb[0]], tl_compartir, heur[1], 1, fijar=tb[1])), trabajos))
                for bb, r in out:
                    for z in r["sols"]:
                        if self._compatible(bb, z):
                            compartidas += int(M.agregar(bb, z))
                t_cs = time.time() - tc

            # Wentges: a automatico (Pessoa et al. 2018).
            if alpha_usado > 0 and usar_w and completa and xs_L is not None:
                g = self._subgradiente(xs_L, [res[b]["sols"][0] if res[b]["sols"] else None
                                              for b in range(E.nb)])
                if float(np.dot(g, pi_rmp - pi)) > 0:
                    alpha = max(0.0, alpha - 0.1)
                else:
                    alpha = min(0.99, alpha + 0.1 * (1 - alpha))
            elif suavizar and pi_c is not None and alpha == 0.0 and nuevas > 0:
                alpha = alpha0                        # vuelve a suavizar

            if ub_cada and k % ub_cada == 0:
                self._heuristicas_rmp(xm, art, caja and delta is not None, pi_c, delta, pi_rmp,
                                      ub_modo, f"{fase} r{k}", kw_h)

            if mejora:
                self._punto(f"{fase} r{k}", LB if fase == "raiz" else None)
            nuevas_tot = nuevas + nuevas_pool
            gap_rel = (z_rmp - LB) / abs(z_rmp) if LB > -INF and art < 1e-6 else INF
            gap_u = (self.UB - LB) / abs(self.UB) if LB > -INF and self.UB < INF else INF
            t_tot = time.time() - t0
            fila = {"fase": fase, "ronda": k, "t": round(self._t(), 1), "t_fase": round(t_tot, 1),
                    "modo": "exact" if exacta else "heur", "completa": completa,
                    "z_rmp": z_rmp, "L": L, "LB": LB, "UB": self.UB, "gap_rmp_lb": gap_rel,
                    "gap_ub_lb": gap_u, "nuevas": nuevas, "nuevas_pool": nuevas_pool,
                    "compartidas": compartidas, "cols": M.n_cols,
                    "pool": sum(len(v) for v in M.pool.values()), "bloques": len(bloques),
                    "alpha": alpha_usado, "box": bool(caja and delta is not None),
                    "artificiales": art, "art_filas": art_filas,
                    "t_rmp": round(t_rmp, 1), "t_pricing": round(t_pr, 1),
                    "t_compartir": round(t_cs, 1),
                    "pricing_max_s": round(max((r["t"] for r in res.values()), default=0), 1),
                    "pricing_sin_sol": [E.claves[b] for b, r in res.items() if not r["ok"]]}
            self.historia.append(fila)
            txt_L = "-inf" if L <= -INF else f"{L:,.2f}"
            txt_LB = "-inf" if LB <= -INF else f"{LB:,.2f}"
            txt_g = "      -" if gap_rel >= INF else f"{gap_rel:7.2%}"
            self._log(f"{fase[:9]:9s} r{k:>3} {self._t():>7.0f}s {fila['modo']:5s} "
                      f"{'' if completa else f'({len(bloques)}/{E.nb}) '}RMP {z_rmp:>14,.2f}  "
                      f"L {txt_L:>14}  LB {txt_LB:>14}  gap {txt_g}  +{nuevas} cols "
                      f"(+{nuevas_pool} pool, +{compartidas} comp.)  a={alpha_usado:.2f}  "
                      f"pricing {t_pr:.0f}s (max {fila['pricing_max_s']:.0f}s)"
                      + (f"  art={art:.2g}" if art > 1e-6 else "")
                      + (f"  UB {self.UB:,.2f} (gap {gap_u:.2%})" if self.UB < INF else "")
                      + (f"  SIN SOL {fila['pricing_sin_sol']}" if fila["pricing_sin_sol"] else ""))
            self._guardar()

            if gap_rel <= tol_gap:
                self._log(f"convergio: RMP {z_rmp:,.2f}  LB {LB:,.2f}")
                convergio = True
                break
            if gap_ub is not None and gap_u <= gap_ub:
                self._log(f"gap UB-LB {gap_u:.4%} <= {gap_ub:.4%}")
                break
            if objetivo is not None and LB >= objetivo:
                self._log(f"LB {LB:,.2f} alcanzo el objetivo {objetivo:,.2f}")
                break
            if deadline is not None and time.time() >= deadline:
                self._log("tope de tiempo")
                break
            if nuevas_tot == 0:
                if caja and delta is not None and art_filas > 1e-6:
                    delta = delta * box_crece
                    if float(np.max(np.abs(pi_c) + delta)) >= M.M_art:
                        M.quitar_box()                # la caja ya no restringe nada
                        caja, delta = False, None
                        self._log("   caja activa sin columnas y ya mayor que M -> sin caja")
                    else:
                        M.poner_box(pi_c, delta)
                        self._log(f"   caja activa sin columnas -> caja x{box_crece:g}")
                    continue
                t_max_pr = max((r["t"] for r in res.values()), default=0)
                if not completa:
                    forzar_completa = True            # descansaban bloques: ronda completa
                elif not exacta:
                    modo = "exact"
                    self._log("sin columnas en el pricing heuristico -> ronda exacta")
                elif not exacta_completa and t_max_pr >= tl - 1:
                    modo = "exact"                    # intento corto cortado por tiempo
                    self._log("intento exacto corto sin columnas pero cortado -> ronda exacta")
                elif barrier and not vertice:
                    vertice = True
                    self._log("ronda exacta sin columnas con duales de barrier -> duales de "
                              "vertice (simplex)")
                elif (max((r["t"] for r in res.values()), default=0) >= tl_ex - 1
                      and tl_ex < 3600):
                    tl_ex = min(2 * tl_ex, 3600.0)
                    self._log(f"ronda exacta sin columnas pero pricing cortado por tiempo: "
                              f"tope a {tl_ex:.0f}s")
                else:
                    self._log(f"ronda exacta sin columnas: maestro restringido optimo para la "
                              f"DW (RMP {z_rmp:,.2f}); LB {LB:,.2f}")
                    convergio = art < 1e-6
                    break
            else:
                vertice = False
                if exacta:
                    modo, ultima_exacta, hist_z = "heur", k, []
            if art < 1e-6 and LB > -INF:
                hist_z.append(z_rmp)
                if estanc_ventana and len(hist_z) > estanc_ventana:
                    z0, z1 = hist_z[-estanc_ventana - 1], hist_z[-1]
                    if (z0 - z1) / max(1.0, abs(z1)) < estanc_tol:
                        estancado, hist_z = True, []
                        self._log("maestro estancado -> ronda exacta")
        if ub_final and xm is not None:
            self._heuristicas_rmp(xm, art, caja and delta is not None, pi_c, delta, pi_rmp,
                                  ub_modo, f"{fase} final", kw_h)
        return {"LB": LB, "UB": self.UB, "z_rmp": z_rmp, "rondas": k, "cols": M.n_cols,
                "t": time.time() - t0, "xm": xm, "pi_c": pi_c, "convergio": convergio,
                "art": art}

    # ------------------------------------------------------------------ #
    # Mejora 6: branch-and-price sobre las enteras del maestro
    # ------------------------------------------------------------------ #
    def _elegir_rama(self, xm):
        E = self.E
        mejor, bf = None, 1e-4
        for kk, j in enumerate(E.mvars):
            if E.vtype[j] == "C" or self.ub_nodo[kk] - self.lb_nodo[kk] < 0.5:
                continue
            f = abs(xm[kk] - round(xm[kk]))
            if f > bf + 1e-12:
                mejor, bf = kk, f
        return mejor

    def _xm_sin_caja(self):
        """Maestro restringido sin la caja (artificiales a costo M): el punto
        del maestro sobre el que se ramifica, sin la distorsion de una caja
        activa. La caja se repone en el proximo correr."""
        self.maestro.quitar_box()
        _z, _pi, _s, xm, art, _af = self.maestro.resolver(barrier=False)
        return xm, art

    def _dive(self, xm, centro, kw, rondas, deadline, kw_h, ub_modo):
        """Diving: fija la entera del maestro mas fraccional en su redondeo y
        re-optimiza unas rondas, hasta que la inversion es entera; ahi corre la
        heuristica del maestro. Si el redondeo deja el nodo infactible prueba el
        otro lado (una vuelta atras por nivel)."""
        E = self.E
        cotas = {}
        for prof in range(10 * len(E.mvars)):
            if time.time() >= deadline:
                break
            kk = self._elegir_rama(xm)
            if kk is None:
                break
            v = float(round(xm[kk]))
            otro = float(np.floor(xm[kk])) if v > xm[kk] else float(np.ceil(xm[kk]))
            r = None
            for val in (v, otro):
                c2 = dict(cotas)
                l0, u0 = c2.get(kk, (-INF, INF))
                c2[kk] = (max(l0, val), min(u0, val))
                if not self._aplicar_nodo(c2):
                    continue
                r = self.correr(**{**kw, "ub_cada": 0}, max_rondas=rondas, centro=centro,
                                deadline=deadline, fase=f"dive {prof}", ub_final=False)
                xr, art = self._xm_sin_caja() if r["xm"] is not None else (None, INF)
                if xr is not None and art <= 1e-6:
                    cotas, xm, centro = c2, xr, r["pi_c"]
                    break
                r = None
                if time.time() >= deadline:
                    break
            if r is None:
                break
        self._aplicar_nodo(cotas)
        self._heuristicas(xm, None, ub_modo, 0.0, "dive", **kw_h)
        self._aplicar_nodo({})

    def branch_and_price(self, raiz, kw, deadline, gap_tol=1e-4, rondas_nodo=30,
                         rondas_dive=15, dive=True, max_nodos=100_000):
        """Mejor-primero sobre las enteras del maestro, desde la raiz ya
        resuelta (raiz = lo que devolvio correr). Devuelve LB global y UB."""
        E = self.E
        kw = dict(kw)
        ub_modo = kw.get("ub_modo", "ambos")
        kw_h = dict(tl_dia=kw.get("tl_dia", 60.0), gap_dia=kw.get("gap_dia", 0.01),
                    tl_pulido=kw.get("ub_timelimit", 300.0), refinar=kw.get("refinar", 1),
                    variantes=tuple(kw.get("variantes", ("coherente",))))
        for clave in ("max_rondas", "centro", "LB0", "deadline", "fase", "objetivo",
                      "ub_final", "tiempo_max", "gap_ub"):
            kw.pop(clave, None)
        corte = lambda: self.UB * (1 - gap_tol) if self.UB < INF else INF  # noqa: E731
        cont = itertools.count()
        abiertos, hojas, podados = [], [], []
        nodos = 0

        def lb_global():
            vals = [n[0] for n in abiertos] + hojas + podados
            return min(vals) if vals else (self.UB if self.UB < INF else raiz["LB"])

        def hijos(cotas, kk, xm, lb, centro, prof):
            f = float(np.floor(xm[kk] + 1e-9))
            l0, u0 = cotas.get(kk, (-INF, INF))
            izq, der = dict(cotas), dict(cotas)
            izq[kk] = (l0, min(u0, f))
            der[kk] = (max(l0, f + 1), u0)
            for c in (izq, der):
                heapq.heappush(abiertos, (lb, next(cont), c, centro, prof))

        self._aplicar_nodo({})
        while True:
            xr = None
            if raiz["xm"] is not None:
                xr, _art = self._xm_sin_caja()
            kk = self._elegir_rama(xr) if xr is not None else None
            if kk is not None:
                break
            if raiz["convergio"] or time.time() >= deadline or raiz["LB"] >= corte():
                self._log("B&P: la inversion de la raiz es entera; no hay sobre que ramificar")
                hojas.append(raiz["LB"])
                return {"LB": lb_global(), "UB": self.UB, "nodos": 0, "abiertos": 0,
                        "hojas": 1}
            # Inversion entera en el maestro pero la raiz sin converger: el
            # tiempo reservado al arbol se usa en seguir generando columnas.
            self._log("B&P: inversion de la raiz entera pero la raiz no convergio -> "
                      "sigue la raiz")
            r2 = self.correr(**kw, LB0=raiz["LB"], centro=raiz["pi_c"], deadline=deadline,
                             fase="raiz", objetivo=corte(), gap_ub=gap_tol)
            raiz = {**r2, "LB": max(raiz["LB"], r2["LB"])}
        if dive and time.time() < deadline:
            self._log("B&P: diving desde la raiz")
            self._dive(xr, raiz["pi_c"], kw, rondas_dive, deadline, kw_h, ub_modo)
        hijos({}, kk, xr, raiz["LB"], raiz["pi_c"], 1)
        while abiertos and time.time() < deadline and nodos < max_nodos:
            lbn, nid, cotas, centro, prof = heapq.heappop(abiertos)
            if lbn >= corte():
                podados.append(lbn)
                continue
            if not self._aplicar_nodo(cotas):
                continue                              # infactible por propagacion
            nodos += 1
            r = self.correr(**kw, max_rondas=rondas_nodo, LB0=lbn, centro=centro,
                            deadline=deadline, fase=f"nodo {nid}", objetivo=corte(),
                            ub_final=False)
            lb_n = max(lbn, r["LB"])
            if r["xm"] is None:
                hojas.append(lb_n)
                continue
            xr, art_n = self._xm_sin_caja()
            if lb_n < corte():
                self._heuristicas(xr, None, ub_modo, art_n, f"nodo {nid}", **kw_h)
            if lb_n >= corte():
                podados.append(lb_n)
            else:
                kk = self._elegir_rama(xr)
                if kk is None:
                    hojas.append(lb_n)
                    self._log(f"B&P nodo {nid}: inversion entera sin podar (cota {lb_n:,.2f}); "
                              f"queda como hoja abierta")
                else:
                    hijos(cotas, kk, xr, lb_n, r["pi_c"], prof + 1)
            lg = lb_global()
            self._punto(f"nodo {nid}", lg)
            self._log(f"B&P nodo {nid} (prof {prof}): cota {lb_n:,.2f}; LB global {lg:,.2f}  "
                      f"UB {self.UB:,.2f}  abiertos {len(abiertos)}  hojas {len(hojas)}  "
                      f"podados {len(podados)}")
        self._aplicar_nodo({})
        lg = lb_global()
        self._punto("B&P fin", lg)
        return {"LB": lg, "UB": self.UB, "nodos": nodos, "abiertos": len(abiertos),
                "hojas": len(hojas), "podados": len(podados)}

    def _guardar(self):
        if not self.out:
            return
        with open(os.path.join(self.out, "historia_cg.json"), "w", encoding="utf-8") as f:
            json.dump(self.historia, f, indent=1, default=float)
        with open(os.path.join(self.out, "trayectoria.json"), "w", encoding="utf-8") as f:
            json.dump(self.trayectoria, f, indent=1, default=float)
