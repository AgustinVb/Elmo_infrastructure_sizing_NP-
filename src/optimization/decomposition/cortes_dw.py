"""Cortes de Benders desde la relajacion de DANTZIG-WOLFE del bloque anual:
Benders entre años, Dantzig-Wolfe dentro de cada año.

POR QUE. El backward de Nested Benders arma el corte del año y-1 con la
relajacion LINEAL del bloque del año y (o, con --cut_type strengthened, con el
lagrangeano del MILP completo en el mu del LP). La relajacion lineal del dia es
floja -- el gap entero esta en la operacion diaria, "17 contra 16,35 cargas" --
y el backward nunca supero a la cota operacional. La relajacion de
Dantzig-Wolfe por dia convexifica justo esa operacion (a 2 años es exacta) y,
con los dias como bloques de pricing, se calcula con MILP de un dia en paralelo
en vez de un MILP del año entero.

EL CORTE. En el bloque del año y la herencia entra como filas del maestro
`link_<estado>: prev = x_hat` (x_hat en el lado derecho). La cota lagrangiana
de la generacion de columnas

    L(pi; x) = pi^T b(x) + min_x' (c - pi^T A_M) x' + sum_dias ObjBound_d

es AFIN en x (x solo aparece en b), con pendiente a_r * pi_r en la fila de
enlace r (a_r: coeficiente de prev en esa fila). Para CUALQUIER pi con el signo
correcto, L(pi; x) <= valor de DW <= valor del MILP del año en x, asi que

    alpha_{y-1} >= L(pi; x_hat) + sum_r (a_r pi_r) (x - x_hat)

es un corte valido para todo x, aunque la generacion de columnas se corte por
tiempo antes de converger. En la convencion de BendersCutManager.add_cut
(alpha >= Phi + mu^T (x_hat - x)) eso es Phi = L, mu_r = -a_r pi_r: la misma que
read_duals (mu = -dual), porque a_r pi_r es la derivada de la cota respecto de
x_hat.

Se trabaja sobre un CLON del bloque: gcg_block.export separa las capacidades
(copia <= capacidad) modificando el modelo, y el bloque original lo sigue usando
el forward.
"""
import os
import time

import numpy as np
import pyomo.environ as pyo

from src.optimization.decomposition import gcg_block as gb
from src.optimization.decomposition.cg_propia import (INF, EstructuraDW,
                                                      GeneracionColumnas, _env)


def resolver_bloque_cg(block, out_dir, tiempo=600.0, cargar=True, usar_start=True,
                       bp=True, verbose=False):
    """Resuelve el MILP del bloque anual `block` (con su herencia y sus cortes)
    con la generacion de columnas propia -- un bloque de pricing por dia,
    heuristicas del maestro y branch-and-price -- en vez de Gurobi.

    POR QUE. En Nested Benders a 4 años los MILP anuales se cortan por tiempo
    con gaps de 1-5 %, y la LB que reporta el backward es la cota dual de
    Gurobi en el año 1 con los cortes (2.028.362 contra un incumbente de
    2.041.394 en ese mismo problema): no la limitan los cortes sino ese MILP.
    El año 1 solo es el caso de 1 año, que la CG con B&P cerro en 7,5 min
    (Gurobi: 0,90 % tras 25 min).

    usar_start: los valores que ya tiene el bloque (el MIP start por dias del
    forward, o su solucion previa) se pulen y entran como UB y columnas.
    cargar: la mejor solucion se carga en las variables de `block.model`.
    Devuelve {"UB", "LB", "t"} (UB = inf si no hubo solucion) o None si el
    bloque no se pudo armar (p.ej. relajacion infactible: que lo diga Gurobi)."""
    t0 = time.time()
    dl = t0 + tiempo
    m = block.model.clone()
    info, por_var = gb.export(m, out_dir, mode="day", split=True)
    E = EstructuraDW(info["mps"], info["decomp"], _env(os.cpu_count() or 8))
    cg = GeneracionColumnas(E, jobs=E.nb, out=None, verbose=verbose)
    kw_h = dict(tl_dia=60.0, gap_dia=0.01, tl_pulido=300.0, refinar=1)
    try:
        if usar_start:
            vec0 = np.zeros(E.nv)
            for j, n in enumerate(E.nombre):
                vd = por_var.get(n)
                v = 1.0 if n == "ONE_VAR_CONSTANT" else (None if vd is None else vd.value)
                vec0[j] = 0.0 if v is None else float(v)
            zs = [E.z_de(b, vec0) for b in range(E.nb)]
            c0, v0 = cg._pulir(zs, timelimit=min(300.0, tiempo / 4))
            if c0 is not None:
                cg._registrar_ub(c0, v0, "start del forward")
        try:
            cg.relajacion_inicial("operacional", timelimit=max(10.0, tiempo / 4))
        except RuntimeError as exc:
            print(f"[CG bloque y={block.year}] sin cota: {exc}", flush=True)
            return None                          # relajacion sin optimo: Gurobi
        if cg.UB >= INF:
            cg._heuristicas(cg.xm0, cg.pi0, "maestro", 0.0, "inicio", **kw_h,
                            variantes=("fases", "directa"))
        cg.acotar_por_costo()
        # Solo la cota (cargar=False, --cg_lb): sin heuristicas de UB en las
        # rondas ni al final; la UB para podar ya la trae el start del forward.
        kw = dict(estab="box+wentges", ub_cada=10 if cargar else 0, ub_modo="ambos",
                  tl_dia=60.0, gap_dia=0.01, ub_timelimit=300.0, refinar=1,
                  variantes=("redondeo",))
        dl_raiz = t0 + 0.6 * tiempo if bp else dl
        r = cg.correr(**kw, deadline=max(dl_raiz, time.time() + 1.0), LB0=cg.LB_ext,
                      gap_ub=1e-4, fase=f"bloque y={block.year}", ub_final=cargar)
        LB = max(r["LB"], cg.LB_ext)
        res_bp = None
        if (bp and time.time() < dl and cg.UB < INF
                and (cg.UB - LB) / abs(cg.UB) > 1e-4):
            r["LB"] = LB
            res_bp = cg.branch_and_price(r, kw, dl, gap_tol=1e-4)
            LB = max(LB, res_bp["LB"])
        print(f"[CG bloque y={block.year}] UB {cg.UB:,.2f} ({cg.origen_ub})  cota de la "
              f"relajacion {cg.LB_ext:,.2f}  raiz {r['LB']:,.2f} (maestro {r['z_rmp']:,.2f}, "
              f"{'convergio' if r['convergio'] else 'sin converger'})"
              + (f"  B&P {res_bp['LB']:,.2f} ({res_bp['nodos']} nodos)" if res_bp else ""),
              flush=True)
        if cargar and cg.mejor_vec is not None:
            _cargar(block.model, por_var, E.nombre, cg.mejor_vec)
        return {"UB": cg.UB, "LB": LB, "t": time.time() - t0}
    finally:
        cg.pool.shutdown(wait=True)


def _cargar(model, por_var, nombres, vec):
    """Valores de un vector del MPS (del CLON exportado) en las variables del
    modelo original, por nombre: el clon tiene las mismas variables mas las
    copias sv_copia de la separacion de capacidades, que el original no tiene."""
    orig = {vd.name: vd for vd in model.component_data_objects(pyo.Var)}
    for j, n in enumerate(nombres):
        vc = por_var.get(n)
        if vc is None:
            continue
        vo = orig.get(vc.name)
        if vo is not None and not vo.fixed:
            v = float(vec[j])
            vo.set_value(v if vo.is_continuous() else float(round(v)), skip_validation=True)


def corte_dw(block, out_dir, tiempo=300.0, jobs=None, hilos=None, estab="box+wentges",
             verbose=False):
    """(Phi, mu, info) del corte de Dantzig-Wolfe del bloque anual `block` en
    su estado heredado actual (los <estado>_hat que dejo el forward), o None si
    la cota no salio finita. `tiempo`: tope de la generacion de columnas."""
    t0 = time.time()
    m = block.model.clone()
    info, _ = gb.export(m, out_dir, mode="day", split=True)
    E = EstructuraDW(info["mps"], info["decomp"], _env(os.cpu_count() or 8))
    if hilos is None and jobs is None:
        jobs = E.nb
    cg = GeneracionColumnas(E, jobs=jobs, hilos=hilos, out=None, verbose=verbose)
    try:
        cg.relajacion_inicial("lp", timelimit=max(10.0, tiempo / 4))
        resto = max(1.0, tiempo - (time.time() - t0))
        r = cg.correr(estab=estab, tiempo_max=resto, ub_cada=0, ub_final=False,
                      ub_modo="columnas", fase=f"dw y={block.year}")
        phi, pi = r["LB"], r["pi_c"]
        if phi <= -INF or pi is None:
            return None
        # Fila del maestro extendido de cada link_<estado>[idx].
        fila_de = {}
        for k, f in enumerate(E.ext):
            mm = gb.DECORACION.match(f["nombre"])
            fila_de[mm.group("label") if mm else f["nombre"]] = k
        mu = {}
        for link in block.state_links:
            con = getattr(m, f"link_{link['state']}", None)
            if con is None:
                continue
            items = ([(None, con)] if link["index_set"] is None
                     else [(idx, con[idx]) for idx in link["index_set"]])
            vals = {}
            for idx, cd in items:
                et = info["etiqueta_de"].get(id(cd))
                k = fila_de.get(et)
                if k is None:
                    raise RuntimeError(f"corte DW: no encuentro la fila {et!r} de "
                                       f"link_{link['state']} en el maestro")
                coef = E.ext[k]["m_coef"]
                if len(coef) != 1:
                    raise RuntimeError(f"corte DW: la fila {et!r} tiene {len(coef)} "
                                       f"variables del maestro (se esperaba 1)")
                vals[idx] = -float(pi[k]) * float(coef[0])
            mu[link["state"]] = vals[None] if link["index_set"] is None else vals
        return phi, mu, {"t": time.time() - t0, "rondas": r["rondas"], "z_rmp": r["z_rmp"],
                         "convergio": r["convergio"], "lb_lp": cg.LB_ext}
    finally:
        cg.pool.shutdown(wait=True)
