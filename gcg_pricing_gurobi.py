"""Pricing solver de GCG que resuelve cada subproblema con GUROBI en vez de SCIP.

Corre DENTRO del proceso de GCG (.venv_gcg, con gurobipy 13 instalado ahi; la
licencia academica es v13). Lo registra gcg_run.py con --pricing gurobi.

POR QUE. La prueba de fuego sobre el bloque anual del año 4 (bloques por dia,
maestro de 36 filas) mostro que la descomposicion no es el problema: GCG con
el pricing de SCIP genero 29 columnas en 20 minutos y la cota no se movio de
la inicial, mientras Gurobi resuelve ese mismo dia en 4-60 s. El cuello de
botella es QUIEN resuelve cada subproblema diario (~11.000 enteras).

COMO. GCG le pasa a solve() el subproblema de un bloque como modelo SCIP, con
los costos reducidos ya puestos en el objetivo. La estructura (variables,
restricciones) no cambia entre llamadas: se traduce a gurobipy UNA vez por
bloque y en cada llamada solo se actualizan objetivo y cotas (las cotas
cambian al ramificar). Se devuelven como columnas TODAS las soluciones del pool
de Gurobi con costo reducido negativo, no solo la mejor: con subproblemas caros,
cada llamada tiene que rendir lo maximo posible.

Costo reducido de una columna = objetivo del pricing (con su offset) - dual de
la restriccion de convexidad del bloque (dualsolconv). Mejora el maestro si es
negativo. La cota que se devuelve (lowerbound) es la cota DUAL de Gurobi sobre
el objetivo del pricing: con status OPTIMAL o SOLVERLIMIT, GCG la usa para la
cota lagrangiana, asi que tiene que ser una cota valida, nunca el primal.
"""
import time

import gurobipy as gp
from gurobipy import GRB

import pygcgopt as gcg


# --------------------------------------------------------------------------
# Crear y agregar columnas SIN el binding (PyGCGOpt 1.0.0b0 esta roto aca)
# --------------------------------------------------------------------------
# GCGPricingModel.createGcgCol hace `GCGpricerGetGcg(self._scip)` con el SCIP del
# SUBPROBLEMA, pero el pricer de GCG vive en el MAESTRO: no lo encuentra y el
# puntero nulo termina en un access violation (0xC0000005), sin traceback. Se
# llama a libgcg directamente con ctypes, sacando el GCG* del maestro. Se carga
# la MISMA DLL que usa pygcgopt (misma ruta -> Windows devuelve el modulo ya
# cargado), asi que comparten el estado.
import ctypes
import glob
import os as _os

_LIBS = _os.path.join(_os.path.dirname(gcg.__file__) + ".libs")
_os.add_dll_directory(_LIBS)
_libgcg = ctypes.CDLL(glob.glob(_os.path.join(_LIBS, "libgcg-*.dll"))[0])
_libgcg.GCGmasterGetGcg.restype = ctypes.c_void_p
_libgcg.GCGmasterGetGcg.argtypes = [ctypes.c_void_p]
_libgcg.GCGcreateGcgCol.restype = ctypes.c_int
_libgcg.GCGcreateGcgCol.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_double), ctypes.c_int,
    ctypes.c_uint, ctypes.c_double]
_libgcg.GCGpricerAddCol.restype = ctypes.c_int
_libgcg.GCGpricerAddCol.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_cap_ptr = ctypes.pythonapi.PyCapsule_GetPointer
_cap_ptr.restype = ctypes.c_void_p
_cap_ptr.argtypes = [ctypes.py_object, ctypes.c_char_p]
SCIP_OKAY = 1


def _scip_ptr(model):
    return _cap_ptr(model.to_ptr(False), b"scip")


def agregar_columna(master, pricingprob, probnr, variables, vals, redcost):
    """Crea la columna en `pricingprob` y la agrega al maestro via libgcg."""
    gcg_ptr = _libgcg.GCGmasterGetGcg(_scip_ptr(master))
    if not gcg_ptr:
        raise RuntimeError("GCGmasterGetGcg devolvio NULL")
    n = len(variables)
    c_vars = (ctypes.c_void_p * n)(*[v.ptr() for v in variables])
    c_vals = (ctypes.c_double * n)(*vals)
    col = ctypes.c_void_p()
    rc = _libgcg.GCGcreateGcgCol(gcg_ptr, _scip_ptr(pricingprob), ctypes.byref(col),
                                 probnr, c_vars, c_vals, n, 0, redcost)
    if rc != SCIP_OKAY:
        raise RuntimeError(f"GCGcreateGcgCol devolvio {rc}")
    rc = _libgcg.GCGpricerAddCol(gcg_ptr, col)
    if rc != SCIP_OKAY:
        raise RuntimeError(f"GCGpricerAddCol devolvio {rc}")


INF_SCIP = 1e19          # SCIP usa 1e20 como infinito
TOL_REDCOST = 1e-6


class GurobiPricing(gcg.PricingSolver):

    def __init__(self, exact_timelimit=60.0, heur_timelimit=20.0, max_cols=20,
                 threads=0, verbose=True):
        self.exact_timelimit = exact_timelimit
        self.heur_timelimit = heur_timelimit
        self.max_cols = max_cols
        self.threads = threads
        self.verbose = verbose
        self._modelos = {}      # probnr -> (grb_model, grb_vars, scip_vars)
        self.stats = {"llamadas_exactas": 0, "llamadas_heur": 0, "columnas": 0,
                      "tiempo_gurobi": 0.0, "tiempo_traduccion": 0.0}

    # ------------------------------------------------------------------ #
    def _traducir(self, pricingprob, probnr):
        t0 = time.time()
        scip_vars = list(pricingprob.getVars())
        m = gp.Model(f"pricing_{probnr}")
        m.Params.OutputFlag = 0
        if self.threads:
            m.Params.Threads = self.threads
        tipos = {"BINARY": GRB.BINARY, "INTEGER": GRB.INTEGER,
                 "IMPLINT": GRB.CONTINUOUS, "CONTINUOUS": GRB.CONTINUOUS}
        gvars, por_nombre = [], {}
        for v in scip_vars:
            lb, ub = v.getLbLocal(), v.getUbLocal()
            gv = m.addVar(lb=-GRB.INFINITY if lb <= -INF_SCIP else lb,
                          ub=GRB.INFINITY if ub >= INF_SCIP else ub,
                          vtype=tipos.get(v.vtype(), GRB.CONTINUOUS))
            gvars.append(gv)
            por_nombre[v.name] = gv
        for c in pricingprob.getConss():
            if c.getConshdlrName() != "linear":
                raise NotImplementedError(f"restriccion no lineal en el pricing: "
                                          f"{c.getConshdlrName()}")
            coefs = pricingprob.getValsLinear(c)
            expr = gp.LinExpr([(a, por_nombre[n]) for n, a in coefs.items()])
            lhs, rhs = pricingprob.getLhs(c), pricingprob.getRhs(c)
            if lhs > -INF_SCIP and rhs < INF_SCIP and abs(lhs - rhs) < 1e-12:
                m.addLConstr(expr, GRB.EQUAL, rhs)
            else:
                if lhs > -INF_SCIP:
                    m.addLConstr(expr, GRB.GREATER_EQUAL, lhs)
                if rhs < INF_SCIP:
                    m.addLConstr(expr, GRB.LESS_EQUAL, rhs)
        m.ModelSense = GRB.MINIMIZE
        m.update()
        # Se guarda el mapeo por NOMBRE, no los objetos Variable de SCIP: esos
        # envuelven punteros nativos que GCG podria recrear entre rondas, y leer
        # uno colgado es un access violation.
        self._modelos[probnr] = (m, por_nombre)
        self.stats["tiempo_traduccion"] += time.time() - t0
        if self.verbose:
            print(f"[pricing-gurobi] bloque {probnr}: traducido en "
                  f"{time.time() - t0:.1f}s ({len(gvars):,} vars, {m.NumConstrs:,} filas)",
                  flush=True)

    def _preparar(self, pricingprob, probnr):
        if probnr not in self._modelos:
            self._traducir(pricingprob, probnr)
        m, por_nombre = self._modelos[probnr]
        # Variables de SCIP releidas en CADA llamada (ver _traducir).
        scip_vars = list(pricingprob.getVars())
        gvars = [por_nombre[v.name] for v in scip_vars]
        # Objetivo (costos reducidos) y cotas: lo unico que cambia entre llamadas.
        objs, lbs, ubs = [], [], []
        for v in scip_vars:
            objs.append(v.getObj())
            lb, ub = v.getLbLocal(), v.getUbLocal()
            lbs.append(-GRB.INFINITY if lb <= -INF_SCIP else lb)
            ubs.append(GRB.INFINITY if ub >= INF_SCIP else ub)
        m.setAttr("Obj", gvars, objs)
        m.setAttr("LB", gvars, lbs)
        m.setAttr("UB", gvars, ubs)
        m.ObjCon = pricingprob.getObjoffset()
        return m, gvars, scip_vars

    def _resolver(self, pricingprob, probnr, dualsolconv, exacto):
        clave = "llamadas_exactas" if exacto else "llamadas_heur"
        self.stats[clave] += 1
        try:
            m, gvars, scip_vars = self._preparar(pricingprob, probnr)
        except NotImplementedError:
            return {"status": gcg.GCG_PRICINGSTATUS.NOTAPPLICABLE}

        if exacto:
            m.Params.TimeLimit = self.exact_timelimit
            m.Params.MIPGap = 1e-6
            m.Params.MIPFocus = 0
            m.Params.SolutionLimit = 2000000000
        else:
            m.Params.TimeLimit = self.heur_timelimit
            m.Params.MIPGap = 1e-2
            m.Params.MIPFocus = 1
        # Si Gurobi demuestra que ninguna solucion tiene costo reducido negativo
        # (cota >= dual de convexidad), no hace falta seguir: esa cota ya sirve.
        m.Params.BestBdStop = dualsolconv - TOL_REDCOST if exacto else GRB.INFINITY

        t0 = time.time()
        m.optimize()
        self.stats["tiempo_gurobi"] += time.time() - t0
        st = m.Status

        if st in (GRB.INFEASIBLE,):
            return {"status": gcg.GCG_PRICINGSTATUS.INFEASIBLE}
        if st in (GRB.UNBOUNDED, GRB.INF_OR_UNBD):
            # Sin rayos: que lo resuelva el pricing de respaldo (SCIP).
            return {"status": gcg.GCG_PRICINGSTATUS.NOTAPPLICABLE}

        # Columnas: todas las soluciones del pool con costo reducido negativo.
        master = self.model.getMasterProb()
        agregadas = 0
        for k in range(min(m.SolCount, 2 * self.max_cols)):
            m.Params.SolutionNumber = k
            objk = m.PoolObjVal
            redcost = objk - dualsolconv
            if redcost >= -TOL_REDCOST:
                continue
            xs = m.getAttr("Xn", gvars)
            vs, vals = [], []
            for v, x, gv in zip(scip_vars, xs, gvars):
                if abs(x) > 1e-9:
                    if gv.VType != GRB.CONTINUOUS:
                        x = float(round(x))
                    vs.append(v)
                    vals.append(x)
            orden = sorted(range(len(vs)), key=lambda i: vs[i].getIndex())
            agregar_columna(master, pricingprob, probnr, [vs[i] for i in orden],
                            [vals[i] for i in orden], redcost)
            agregadas += 1
            if agregadas >= self.max_cols:
                break
        self.stats["columnas"] += agregadas

        # La cota dual vale aunque no haya solucion (p.ej. corte por BestBdStop,
        # que es justamente cuando la cota demuestra que no hay columna mejoradora).
        try:
            cota = m.ObjBound
        except gp.GurobiError:
            cota = -GRB.INFINITY
        if exacto:
            if st == GRB.OPTIMAL or st == GRB.USER_OBJ_LIMIT:
                # USER_OBJ_LIMIT = corto por BestBdStop: la cota ya demuestra que
                # no hay columna mejoradora, que es lo que el exacto tiene que decir.
                status = gcg.GCG_PRICINGSTATUS.OPTIMAL
            else:
                status = gcg.GCG_PRICINGSTATUS.SOLVERLIMIT
        else:
            status = gcg.GCG_PRICINGSTATUS.UNKNOWN
        return {"status": status, "lowerbound": cota}

    # ------------------------------------------------------------------ #
    def solve(self, pricingprob, probnr, dualsolconv):
        return self._resolver(pricingprob, probnr, dualsolconv, exacto=True)

    def solveHeuristic(self, pricingprob, probnr, dualsolconv):
        return self._resolver(pricingprob, probnr, dualsolconv, exacto=False)

    def exitSolver(self):
        if self.verbose:
            s = self.stats
            print(f"[pricing-gurobi] llamadas exactas {s['llamadas_exactas']}, "
                  f"heuristicas {s['llamadas_heur']}, columnas {s['columnas']}, "
                  f"Gurobi {s['tiempo_gurobi']:.0f}s, traduccion "
                  f"{s['tiempo_traduccion']:.0f}s", flush=True)


def registrar(model, exact_timelimit=60.0, heur_timelimit=20.0, max_cols=20,
              desactivar_scip=True):
    """Registra el pricing de Gurobi.

    desactivar_scip: apaga los pricing solvers de SCIP ('mip', 'gcg'). Medido en el
    bloque del año 4: dejandolos de respaldo, cada vez que Gurobi devolvia
    SOLVERLIMIT (no demostro en su tope) GCG delegaba en el de SCIP, que se
    quedaba resolviendo un dia de ~11.000 enteras en un solo hilo -- el mismo
    cuello de botella que se queria sacar. Sin respaldo, GCG sigue con las
    columnas que ya devolvio Gurobi."""
    ps = GurobiPricing(exact_timelimit=exact_timelimit, heur_timelimit=heur_timelimit,
                       max_cols=max_cols)
    model.includePricingSolver(ps, "gurobi", "pricing con Gurobi (gurobipy)",
                               priority=1000, heuristicEnabled=True, exactEnabled=True)
    if desactivar_scip:
        for nombre in ("mip", "gcg"):
            try:
                model.setPricingSolverEnabled(nombre, False)
            except Exception:
                pass
    return ps
