"""Generacion de columnas propia (Dantzig-Wolfe) sobre el monolitico, con el
pricing en Gurobi y en paralelo.

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
              una de convexidad por bloque. Artificiales con costo M en todas las
              filas, asi arranca factible aun sin columnas.
  PRICING     un MILP por bloque, persistente, con el objetivo de costo reducido
              c - pi A. Todos los bloques a la vez (ThreadPoolExecutor: Gurobi
              suelta el GIL). Dos niveles: heuristico (tope corto, gap holgado,
              varias soluciones del pool) mientras salgan columnas; exacto (gap
              chico) para cerrar la cota.
  COTA        lagrangiana en CADA ronda, valida para cualquier pi con el signo
              correcto: pi'b + sum_j min_x rc_j x_j + sum_b ObjBound_b, con
              ObjBound la cota dual de Gurobi del pricing (vale aunque se corte
              por tiempo).

ESTABILIZACION (Flores-Quiroz y Strunz, Applied Energy 2021, tabla 3):
  wentges  suavizado de duales: se pricea en pi_sep = a pi_centro + (1-a) pi_RMP,
           con a automatico (Pessoa et al. 2018: baja si el subgradiente en
           pi_sep apunta hacia pi_RMP, sube si no) y mispricing -> a = 0. El
           centro es el pi de la mejor cota. (= in-out; es lo que GCG trae
           activo por defecto.)
  barrier  el maestro con punto interior, sin crossover: duales centrados (CG-ip).
  ninguna  duales del simplex.
  COMPARTIR (CG&S, el aporte del paper): la capacidad de la mejor columna nueva
           de un bloque se prueba en sus "hermanos" (los bloques que separan las
           MISMAS capacidades: los dias del mismo año) con esa capacidad fija, y
           esas columnas entran al maestro aunque no tengan costo reducido
           negativo.
"""
import json
import os
import time
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
        # Se conserva para la UB heuristica (ver GeneracionColumnas.ub_heuristica).
        self.F = F

        with open(decomp, encoding="utf-8") as f:
            dec = json.load(f)
        self.claves = dec["meta"]["block_keys"]
        self.nb = nb = len(self.claves)
        blk_de_fila = {r: int(b) for b, fl in dec["blocks"].items() for r in fl}
        row_blk = np.array([blk_de_fila.get(n, -1) for n in cname])
        self.filas_bloque = {b: np.where(row_blk == b)[0] for b in range(nb)}
        filas_maestro = np.where(row_blk < 0)[0]

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
        self.var_blk = np.where(cnt == 1, dueno, -1)
        self.mvars = np.where(self.var_blk < 0)[0]
        pos_m = {int(j): k for k, j in enumerate(self.mvars)}
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
                                 "filas": filas_b, "costo": costo, "lb": lbz, "ub": ubz,
                                 "vtype": vt, "partes": [],
                                 "ent": np.array([k for k, t in enumerate(vt) if t != "C"], int)})

        # Filas extendidas: las del maestro y las de enlace (copia - x = 0).
        self.ext = []
        sombra = {b: {} for b in range(nb)}
        for i in filas_maestro:
            idx, coef = filas[i]
            mk = self.var_blk[idx] < 0
            r = {"nombre": cname[i], "sense": sense[i], "rhs": float(rhs[i]),
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
        for b in range(nb):
            for j, k in self.bloques[b]["cpos"].items():
                n_r = len(self.ext)
                self.ext.append({"nombre": f"enlace_b{b}_{self.nombre[j]}", "sense": "=",
                                 "rhs": 0.0, "m_pos": np.array([pos_m[j]], int),
                                 "m_coef": np.array([-1.0])})
                self.bloques[b]["partes"].append((n_r, np.array([k], int), np.array([1.0])))
        self.n_ext = len(self.ext)

        # Matriz densa del maestro (filas extendidas x variables del maestro)
        self.A_M = np.zeros((self.n_ext, len(self.mvars)))
        for r, f in enumerate(self.ext):
            np.add.at(self.A_M[r], f["m_pos"], f["m_coef"])
        self.rhs_ext = np.array([f["rhs"] for f in self.ext])
        self.sense_ext = [f["sense"] for f in self.ext]

        # Cotas de las copias. Sin esto el pricing ve las capacidades con cota
        # infinita (N_*, G_g, H no la traen en el MPS: la ponen filas del
        # maestro como N_bays <= max*X o G_g <= g_max), y con los duales de esas
        # filas en 0 -- lo normal al arrancar, el maestro es muy degenerado --
        # devuelve columnas con capacidad ilimitada y gratis (medido a 2 años:
        # 6 de 8 bloques con costo reducido 0 y el maestro clavado en el
        # incumbente). Las cotas implicadas por las filas SOLO del maestro valen
        # para toda solucion factible, y copia <= capacidad las hereda.
        self.lb_m, self.ub_m = self._propagar_cotas()
        n_acot = 0
        for b in range(nb):
            B = self.bloques[b]
            for jm, k in sombra[b].items():
                if self.ub_m[jm] < B["ub"][k]:
                    B["ub"][k] = self.ub_m[jm]
                    n_acot += 1
            for j, k in B["cpos"].items():
                jm = pos_m[j]
                B["lb"][k] = max(B["lb"][k], self.lb_m[jm])
                B["ub"][k] = min(B["ub"][k], self.ub_m[jm])
        self.n_copias_acotadas = n_acot

        # Hermanos (Column Sharing): bloques que separan las mismas capacidades.
        self.sombra = sombra
        self.hermanos = {b: [bb for bb in range(nb) if bb != b and sombra[b]
                             and set(sombra[bb]) == set(sombra[b])] for b in range(nb)}
        self.t_lectura = time.time() - t0

    def _propagar_cotas(self, pasadas=50):
        """Propagacion de cotas (FBBT) sobre las filas que solo tienen
        variables del maestro. Devuelve (lb, ub) de las variables del maestro."""
        mv = self.mvars
        lb, ub = self.lb[mv].astype(float).copy(), self.ub[mv].astype(float).copy()
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


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

class Pricing:
    """El MILP de un bloque, persistente; solo cambia el objetivo."""

    def __init__(self, E, b, threads):
        B = E.bloques[b]
        self.E, self.b, self.B = E, b, B
        self.env = _env(threads)
        m = gp.Model(env=self.env)
        self.z = m.addVars(len(B["costo"]), lb=B["lb"].tolist(), ub=B["ub"].tolist(),
                           vtype=B["vtype"])
        m.update()
        zl = [self.z[k] for k in range(len(B["costo"]))]
        self.zl = zl
        for pos, coef, s, rhs in B["filas"]:
            m.addLConstr(gp.LinExpr(coef.tolist(), [zl[k] for k in pos]), s, rhs)
        m.update()
        self.m = m
        self.start = None

    def objetivo(self, pi):
        c = self.B["costo"].copy()
        for r, p, coef in self.B["partes"]:
            if pi[r] != 0.0:
                np.add.at(c, p, -pi[r] * coef)
        return c

    def resolver(self, c, timelimit, gap, pool, fijar=None):
        """min c z sobre el bloque. fijar: {posicion: valor} (Column Sharing)."""
        m, zl = self.m, self.zl
        t0 = time.time()
        m.setAttr("Obj", zl, c.tolist())
        guard = None
        if fijar:
            ks = list(fijar)
            guard = ([zl[k].LB for k in ks], [zl[k].UB for k in ks], ks)
            vals = [float(fijar[k]) for k in ks]
            m.setAttr("LB", [zl[k] for k in ks], vals)
            m.setAttr("UB", [zl[k] for k in ks], vals)
        if self.start is not None and not fijar:
            m.setAttr("Start", zl, self.start.tolist())
        m.Params.TimeLimit = timelimit
        m.Params.MIPGap = gap
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
            else:
                try:
                    bound = m.ObjBound
                except gp.GurobiError:   # bloque sin enteras (LP)
                    bound = m.ObjVal if st == GRB.OPTIMAL else -INF
            return {"ok": m.SolCount > 0, "status": st, "val": m.ObjVal if m.SolCount else INF,
                    "bound": bound, "sols": sols, "t": time.time() - t0}
        finally:
            if guard:
                lbs, ubs, ks = guard
                m.setAttr("LB", [zl[k] for k in ks], lbs)
                m.setAttr("UB", [zl[k] for k in ks], ubs)


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
        self.conv = []
        for b in range(E.nb):
            a = m.addVar(obj=M_art)
            self.art_vars.append(a)
            self.conv.append(m.addLConstr(a, GRB.EQUAL, 1.0))
        m.update()
        self.m = m
        self.lams = {b: [] for b in range(E.nb)}
        self.vistas = {b: set() for b in range(E.nb)}
        self.n_cols = 0

    def agregar(self, b, z):
        """Agrega la columna z del bloque b si no estaba. Devuelve True si entro."""
        clave = np.round(z, 6).tobytes()
        if clave in self.vistas[b]:
            return False
        self.vistas[b].add(clave)
        costo = float(np.dot(self.E.bloques[b]["costo"], z))
        cf = self.E.coefs(b, z)
        cf = [(r, v) for r, v in cf if v != 0.0]
        col = gp.Column([v for _, v in cf] + [1.0],
                        [self.filas[r] for r, _ in cf] + [self.conv[b]])
        self.lams[b].append((self.m.addVar(obj=costo, column=col), z, costo, cf))
        self.n_cols += 1
        return True

    def resolver(self, barrier=False):
        m = self.m
        m.Params.Method = 2 if barrier else 1
        m.Params.Crossover = 0 if barrier else -1
        m.optimize()
        if m.Status != GRB.OPTIMAL:
            raise RuntimeError(f"maestro restringido sin optimo (status {m.Status})")
        pi = np.array(m.getAttr("Pi", self.filas))
        sigma = np.array(m.getAttr("Pi", self.conv))
        xm = np.array(m.getAttr("X", [self.x[k] for k in range(len(self.E.mvars))]))
        art = float(sum(m.getAttr("X", self.art_vars)))
        return m.ObjVal, pi, sigma, xm, art


# --------------------------------------------------------------------------
# Generacion de columnas
# --------------------------------------------------------------------------

class GeneracionColumnas:

    def __init__(self, E, jobs=4, hilos=None, out=None, verbose=True, M_art=1e7):
        self.E = E
        cpu = os.cpu_count() or 8
        self.jobs = jobs
        hilos = hilos or max(1, cpu // jobs)
        t0 = time.time()
        self.pricing = [Pricing(E, b, hilos) for b in range(E.nb)]
        self.maestro = Maestro(E, _env(min(cpu, 8)), M_art=M_art)
        self.pool = ThreadPoolExecutor(max_workers=jobs)
        self.out, self.verbose = out, verbose
        self.historia = []
        self._log(f"{E.nb} pricing y maestro armados en {time.time() - t0:.0f}s "
                  f"({jobs} bloques a la vez x {hilos} hilos)")

    def _log(self, s):
        if self.verbose:
            print(f"[CG] {s}", flush=True)

    def inicializar(self, vec):
        """Columnas iniciales desde una solucion completa (vector del MPS)."""
        for b in range(self.E.nb):
            z = self.E.z_de(b, vec)
            z[self.E.bloques[b]["ent"]] = np.round(z[self.E.bloques[b]["ent"]])
            self.maestro.agregar(b, z)
            self.pricing[b].start = z

    # ------------------------------------------------------------------ #
    def lagrangiano(self, pi, bounds, xm_rmp):
        """Cota lagrangiana en pi y la x* del maestro que la realiza."""
        E, M = self.E, self.maestro
        rc = E.obj[E.mvars] - E.A_M.T @ pi
        lb, ub = E.lb_m, E.ub_m
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
        for r, s in M.art:
            if M.M_art - pi[r] * s < -1e-6:
                return -INF, None
        for bnd in bounds:
            if bnd <= -INF:
                return -INF, None
            total += min(bnd, M.M_art)
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

    def ub_heuristica(self, timelimit=300.0, gap=1e-3):
        """UB por precio-y-fijacion: en cada bloque la columna con mayor lambda
        en el maestro; se fijan sus enteras (la operacion del dia y las copias
        de capacidad) en el monolitico y se resuelve el resto, que son las
        enteras del maestro (inversion) y todas las continuas (degradacion,
        energia). Devuelve (costo, vector) o (None, None)."""
        E, M = self.E, self.maestro
        if any(not M.lams[b] for b in range(E.nb)):
            return None, None
        elegidas = self._elegir_columnas_mip(min(timelimit, 120.0))
        self.ub_con_enlace = elegidas is not None
        if elegidas is None:
            elegidas = self._elegir_columnas_mip(min(timelimit, 120.0), soltar_enlace=True)
        if elegidas is None:
            # Respaldo: la de mayor lambda en el maestro LP (re-resuelto: el
            # Column Sharing agrega lambdas despues del ultimo solve).
            M.resolver(barrier=False)
            elegidas = []
            for b in range(E.nb):
                xs = M.m.getAttr("X", [l[0] for l in M.lams[b]])
                elegidas.append(int(np.argmax(xs)))
        F = E.F
        vs = F.getVars()
        idx, vals = [], []
        for b in range(E.nb):
            z = M.lams[b][elegidas[b]][1]
            B = E.bloques[b]
            nloc = len(B["loc"])
            for k in B["ent"]:
                if k < nloc:
                    idx.append(int(B["loc"][k]))
                    vals.append(float(round(z[k])))
        sel = [vs[j] for j in idx]
        lb0, ub0 = F.getAttr("LB", sel), F.getAttr("UB", sel)
        F.setAttr("LB", sel, vals)
        F.setAttr("UB", sel, vals)
        F.Params.TimeLimit = timelimit
        F.Params.MIPGap = gap
        try:
            F.optimize()
            if F.SolCount == 0:
                return None, None
            return F.ObjVal, np.array(F.getAttr("X", vs))
        finally:
            F.setAttr("LB", sel, lb0)
            F.setAttr("UB", sel, ub0)

    # ------------------------------------------------------------------ #
    def correr(self, estab="wentges", alpha0=0.5, compartir=True,
               heur=(30.0, 0.02, 5), exacto=(300.0, 1e-4), tl_compartir=30.0,
               tiempo_max=None, max_rondas=10_000, tol_gap=1e-4, objetivo=None,
               ub_cada=0, ub_timelimit=300.0, UB0=INF):
        """estab: "ninguna", "wentges", "barrier" o "barrier+wentges".
        ub_cada: cada cuantas rondas se intenta la UB heuristica (0 = solo al
        final)."""
        E, M = self.E, self.maestro
        t0 = time.time()
        LB, pi_c, alpha = -INF, None, alpha0
        vertice = False
        self.UB, self.mejor_vec = UB0, None
        modo, tl_ex = "heur", exacto[0]
        barrier = "barrier" in estab
        suavizar = "wentges" in estab

        def intentar_ub(k):
            tu = time.time()
            costo, vec = self.ub_heuristica(ub_timelimit)
            mejor = costo is not None and costo < self.UB - 1e-6
            if mejor:
                self.UB, self.mejor_vec = costo, vec
            txt = "infactible/sin solucion" if costo is None else f"{costo:,.2f}"
            enl = ("con enlace" if getattr(self, "ub_con_enlace", False)
                   else "enlace SUELTO")
            self._log(f"   UB heuristica (r{k}, {enl}): {txt}"
                      f"{'  *** nueva UB' if mejor else ''}  ({time.time() - tu:.0f}s)")

        for k in range(1, max_rondas + 1):
            tr = time.time()
            # Duales de vertice (simplex) cuando el barrier ya no da columnas:
            # con duales interiores el pricing puede no ver columnas que si
            # mejoran, y la cota lagrangiana en un dual no optimo se afloja
            # mucho con variables del maestro de cota ancha (w_deg, N_total).
            z_rmp, pi_rmp, sigma, xm, art = M.resolver(barrier=barrier and not vertice)
            t_rmp = time.time() - tr
            usar_wentges = suavizar and pi_c is not None and alpha > 0
            pi = alpha * pi_c + (1 - alpha) * pi_rmp if usar_wentges else pi_rmp
            tl, gap, npool = (heur if modo == "heur" else (tl_ex, exacto[1], 1))

            tp = time.time()
            objs = [self.pricing[b].objetivo(pi) for b in range(E.nb)]
            res = list(self.pool.map(
                lambda b: self.pricing[b].resolver(objs[b], tl, gap, npool), range(E.nb)))
            t_pr = time.time() - tp
            sin_sol = [E.claves[b] for b, r in enumerate(res) if not r["ok"]]
            bounds = [r["bound"] for r in res]
            L, xs = self.lagrangiano(pi, bounds, xm)
            if L > LB + 1e-9:
                LB = L
                if suavizar:
                    pi_c = pi.copy()

            # Columnas: entran las de costo reducido negativo en pi_RMP.
            nuevas, mejores = 0, {}
            for b, r in enumerate(res):
                for z in r["sols"]:
                    rc = (float(np.dot(E.bloques[b]["costo"], z))
                          - sum(pi_rmp[rr] * v for rr, v in E.coefs(b, z)) - sigma[b])
                    if rc < -1e-6 * max(1.0, abs(z_rmp)) and M.agregar(b, z):
                        nuevas += 1
                        if b not in mejores or rc < mejores[b][0]:
                            mejores[b] = (rc, z)
                if r["sols"]:
                    self.pricing[b].start = r["sols"][0]

            # Column Sharing: la capacidad de la mejor columna nueva, a los hermanos.
            compartidas, t_cs = 0, 0.0
            if compartir and mejores:
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
                out = list(self.pool.map(
                    lambda tb: (tb[0], self.pricing[tb[0]].resolver(
                        objs[tb[0]], tl_compartir, heur[1], 1, fijar=tb[1])), trabajos))
                for bb, r in out:
                    for z in r["sols"]:
                        compartidas += int(M.agregar(bb, z))
                t_cs = time.time() - tc

            # Estabilizacion: a automatico (Pessoa et al. 2018) y mispricing.
            alpha_prev = alpha
            if usar_wentges:
                if nuevas == 0:
                    alpha = 0.0                       # mispricing
                elif xs is not None:
                    g = self._subgradiente(xs, [r["sols"][0] if r["sols"] else None
                                                for r in res])
                    if float(np.dot(g, pi_rmp - pi)) > 0:
                        alpha = max(0.0, alpha - 0.1)
                    else:
                        alpha = min(0.99, alpha + 0.1 * (1 - alpha))
            elif suavizar and pi_c is not None and alpha == 0.0 and nuevas > 0:
                alpha = alpha0                        # vuelve a suavizar

            if ub_cada and k % ub_cada == 0 and art < 1e-6:
                intentar_ub(k)

            gap_rel = (z_rmp - LB) / abs(z_rmp) if LB > -INF else INF
            gap_ub = (self.UB - LB) / abs(self.UB) if LB > -INF and self.UB < INF else INF
            t_tot = time.time() - t0
            fila = {"ronda": k, "t": round(t_tot, 1), "modo": modo, "z_rmp": z_rmp, "L": L,
                    "LB": LB, "UB": self.UB, "gap_rmp_lb": gap_rel, "gap_ub_lb": gap_ub,
                    "nuevas": nuevas, "compartidas": compartidas, "cols": M.n_cols,
                    "alpha": alpha_prev, "artificiales": art, "t_rmp": round(t_rmp, 1),
                    "t_pricing": round(t_pr, 1), "t_compartir": round(t_cs, 1),
                    "pricing_max_s": round(max(r["t"] for r in res), 1),
                    "pricing_sin_sol": sin_sol}
            self.historia.append(fila)
            txt_L = "-inf" if L <= -INF else f"{L:,.2f}"
            txt_LB = "-inf" if LB <= -INF else f"{LB:,.2f}"
            self._log(f"r{k:>3} {t_tot:>7.0f}s {modo:5s} RMP {z_rmp:>14,.2f}  "
                      f"L {txt_L:>14}  LB {txt_LB:>14}  gap {gap_rel:7.2%}  "
                      f"+{nuevas} cols (+{compartidas} comp.)  a={alpha_prev:.2f}  "
                      f"pricing {t_pr:.0f}s (max {fila['pricing_max_s']:.0f}s)"
                      + (f"  art={art:.2g}" if art > 1e-6 else "")
                      + (f"  UB {self.UB:,.2f} (gap {gap_ub:.2%})" if self.UB < INF else "")
                      + (f"  SIN SOL {sin_sol}" if sin_sol else ""))
            self._guardar()

            if gap_rel <= tol_gap and art < 1e-6:
                self._log(f"convergio: RMP {z_rmp:,.2f}  LB {LB:,.2f}")
                break
            if objetivo is not None and LB >= objetivo:
                self._log(f"LB {LB:,.2f} alcanzo el objetivo {objetivo:,.2f}")
                break
            if tiempo_max is not None and t_tot >= tiempo_max:
                self._log("tope de tiempo")
                break
            if nuevas == 0 and not usar_wentges:
                if modo == "heur":
                    modo = "exact"
                    self._log("sin columnas con el pricing heuristico -> pricing exacto")
                elif barrier and not vertice:
                    vertice = True
                    self._log("pricing exacto sin columnas con duales de barrier -> "
                              "se reintenta con los duales de vertice (simplex)")
                elif max(r["t"] for r in res) >= tl_ex - 1 and tl_ex < 3600:
                    tl_ex = min(2 * tl_ex, 3600.0)
                    self._log(f"pricing exacto sin columnas pero gap {gap_rel:.2%} y dias "
                              f"cortados por tiempo: tope del pricing a {tl_ex:.0f}s")
                else:
                    self._log(f"pricing exacto sin columnas: el maestro restringido es "
                              f"optimo para la DW (cota DW = {z_rmp:,.2f}); la LB queda en "
                              f"{LB:,.2f} por el gap de los pricing")
                    break
            elif nuevas > 0:
                vertice = False
                if modo == "exact":
                    modo = "heur"
        if art < 1e-6:
            intentar_ub(k)
        return {"LB": LB, "UB": self.UB, "z_rmp": z_rmp, "rondas": k, "cols": M.n_cols,
                "t": time.time() - t0}

    def _guardar(self):
        if not self.out:
            return
        with open(os.path.join(self.out, "historia_cg.json"), "w", encoding="utf-8") as f:
            json.dump(self.historia, f, indent=1, default=float)
