"""Fase 4 de implementacion_dp_estacion_swap.md: experimento de cota del flujo
de la estacion sobre el MILP del dia, con la inversion fija en un incumbente.

Cada dia se arma en gurobipy desde la particion year_day del MPS (como
benders_dias.Subproblema), con las copias del maestro (N_batteries,
N_chargers, n_ssee_k, P_pot, G_g, H, b_bar, ...) FIJAS en el incumbente y los
presupuestos del dia libres (como Subproblema.ub). Variantes:

  actual      el bloque de estacion del MILP, con el pipeline de carga ciclico
              (ELMO_PIPELINE_CICLICO=1, el que describe el flujo).
  power_link  la estacion se reemplaza por un flujo (circulacion) en el grafo
              de la DP (dp_estacion): nodos (t, s), arcos (w, x) con Q. Se
              quitan S, Sv, X_ini, X_dch, W y las filas internas de la estacion
              (inventarios, duracion, ciclicidad, conservacion, cargadores); en
              las demas filas W -> sum w_a f_a, X_ini -> sum x_a f_a y la suma
              de Sv de la ventana de t -> Q_t = sum Q_a f_a. Costo de arco 0:
              Q entra al balance de potencia. Filtros de arcos que no dependen
              de G/H: N_C (en los estados), subestacion (500 n_ssee / p_c), pico
              (n_peak_max) y swaps <= LHD que pueden hacer swap en t.
  arc_cost    como power_link, pero el costo de energia va en los arcos:
              coef(P_red)_t * max(0, p_c Q_a - A_t), A_t = sum_g G_g p_max_g
              alpha, y P_red sale del objetivo. Solo vale sin BESS y con G fija
              (aca lo esta). Filtra ademas Q_a con P_red <= p_peak y, en horas
              de punta de los dias 91-244, P_red <= P_pot.

  *_u         como la anterior, con el enlace DESAGREGADO por numero de swaps:
              u[t,w] = sum_{a en t, w_a = w} f_a binaria. El LP es el mismo
              (u relajada), pero con u entera el flujo es una red con un solo
              w por intervalo y su LP es entero (Martin-Rardin-Campbell); con el
              enlace agregado sum w_a f_a = W_t el LP puede mezclar caminos con
              distinto w en un mismo t aunque W_t sea entero.

Por variante y dia: LP, cota de la raiz, cota a 180 s y al final (--timelimit),
mejor solucion, tiempo, tamanos (estados, arcos, variables, filas) y la
fraccionalidad del LP (Z_swap y Q_t). Chequeo: con Y, Z y Z_swap fijos en el
incumbente, el LP de arc_cost debe dar la energia de la DP (validar_dp_estacion).

    python -u experimento_estacion_flujo.py \\
        --data_folder data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte/05C/RED_GEN \\
        --respaldo output/.../RED_GEN/benders/incumbente_respaldo_k2.pkl \\
        --dias "4,196;5,288;7,105" --timelimit 900 --jobs 3 --threads 8 \\
        --out output/.../experimento_estacion_flujo/caso3_v4
"""
import argparse
import json
import math
import os
import re
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)
# El flujo es una circulacion: la referencia es el MILP con el pipeline ciclico.
os.environ["ELMO_PIPELINE_CICLICO"] = "1"

import gurobipy as gp                                                    # noqa: E402
from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from dp_estacion import construir_estados, transicion                    # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402
from validar_dp_estacion import EPS, P_SSEE_STEP, adaptar_incumbente_ciclico  # noqa: E402

ESTACION = {"S", "Sv", "X_ini", "X_dch", "W"}
FILAS_INTERNAS = {"inventory_discharged_batteries", "inventory_charged_batteries",
                  "charging_duration", "CI_S", "CI_X_dch", "CB_general", "charger_limit_swap"}
RE_FILA = re.compile(r"^(?:c_[a-z]|r_[lu])_(\w+?)(?:\(|_?$)")
VARIANTES = ("actual", "power_link", "arc_cost", "power_link_u", "arc_cost_u")
# Tambien: actual_pz y actual_pn (prioridad de ramificacion en los swaps; ver armar_dia).


def comp_fila(nombre):
    mm = RE_FILA.match(nombre)
    return mm.group(1) if mm else nombre


# ---------------------------------------------------------------- grafo -- #
def armar_grafo(N, NC, L, T, w_max, q_max):
    """Circulacion de la DP de la estacion en T intervalos.

    Nodo (p, i): estado i al inicio del intervalo p. Arco en p: (i -> j, w, x, Q)
    hacia el nodo (p+1 mod T, j). Se podan los nodos sin arco de entrada o de
    salida hasta que no cambie nada. Devuelve (estados, arcos), con arcos[p]
    un dict de arrays i, j, w, x, Q."""
    estados, idx = construir_estados(N, NC, L)
    ns = len(estados)
    Ai, Aj, Aw, Ax, AQ = [], [], [], [], []
    for i, s in enumerate(estados):
        for w in range(s[0] + 1):
            for x in range(NC + 1):
                tr = transicion(s, w, x, N, NC, L)
                if tr is None:
                    continue
                s2, Q = tr
                Ai.append(i)
                Aj.append(idx[s2])
                Aw.append(w)
                Ax.append(x)
                AQ.append(Q)
    Ai, Aj, Aw, Ax, AQ = (np.array(v, dtype=np.int64) for v in (Ai, Aj, Aw, Ax, AQ))
    base = [(Aw <= w_max[p]) & (AQ <= q_max[p]) for p in range(T)]
    vivo = np.ones((T, ns), dtype=bool)
    while True:
        sale = np.zeros((T, ns), dtype=bool)
        entra = np.zeros((T, ns), dtype=bool)
        for p in range(T):
            q = (p + 1) % T
            mk = base[p] & vivo[p, Ai] & vivo[q, Aj]
            sale[p, Ai[mk]] = True
            entra[q, Aj[mk]] = True
        nuevo = vivo & sale & entra
        if (nuevo == vivo).all():
            break
        vivo = nuevo
    arcos = []
    for p in range(T):
        q = (p + 1) % T
        mk = base[p] & vivo[p, Ai] & vivo[q, Aj]
        arcos.append({"i": Ai[mk], "j": Aj[mk], "w": Aw[mk], "x": Ax[mk], "Q": AQ[mk]})
    return estados, arcos, int(vivo.sum())


# ---------------------------------------------------------------- modelo -- #
def armar_dia(P, b, xB, col, dia, variante, threads):
    """Modelo gurobipy del dia b con la inversion fija en xB."""
    t_ini = time.time()
    env = gp.Env(empty=True)
    env.setParam("OutputFlag", 0)
    env.setParam("Threads", threads)
    env.start()
    m = gp.Model(env=env)
    # actual, actual_pz, actual_pn: la estacion del MILP (sin flujo); las dos
    # ultimas solo cambian la prioridad de ramificacion (ver mas abajo).
    flujo = not variante.startswith("actual")
    base_var = variante[:-2] if variante.endswith("_u") else variante
    loc, cop = P.locales[b], P.copias[b]
    quitar = {j for j in loc if col[j][0] in ESTACION} if flujo else set()

    var = {}
    for j in loc:
        if j in quitar:
            continue
        c = P.obj[j]
        if base_var == "arc_cost" and col[j][0] == "P_red":
            c = 0.0
        var[j] = m.addVar(lb=P.lb[j], ub=P.ub[j], obj=c, vtype=P.vtype[j])
    for j in cop:
        v = float(round(xB[j])) if P.vtype[j] != "C" else float(xB[j])
        var[j] = m.addVar(lb=v, ub=v)

    T, L, pos = dia["T"], dia["L"], dia["pos"]
    Wp = Xp = Qp = None
    n_f = n_cons = 0
    f_por_p = []
    if flujo:
        q_max = dia["q_max_arc"] if base_var == "arc_cost" else dia["q_max_pl"]
        estados, arcos, n_nodos = armar_grafo(dia["N"], dia["NC"], L, T, dia["w_max"], q_max)
        Wp, Xp, Qp = [], [], []
        for p in range(T):
            a = arcos[p]
            na = len(a["i"])
            if base_var == "arc_cost":
                costo = (dia["coef"][p] * np.maximum(0.0, dia["p_c"] * a["Q"] - dia["A"][p])).tolist()
            else:
                costo = [0.0] * na
            fs = m.addVars(na, lb=0.0, obj=costo).values() if na else []
            f_por_p.append(fs)
            n_f += na
            Wp.append(gp.LinExpr(a["w"].astype(float).tolist(), fs))
            Xp.append(gp.LinExpr(a["x"].astype(float).tolist(), fs))
            Qp.append(gp.LinExpr(a["Q"].astype(float).tolist(), fs))
        m.update()
        # Conservacion en cada nodo (p, s): entra desde p-1 = sale en p.
        for p in range(T):
            pa = (p - 1) % T
            ent, sal = {}, {}
            for k, j in enumerate(arcos[pa]["j"].tolist()):
                ent.setdefault(j, []).append(f_por_p[pa][k])
            for k, i in enumerate(arcos[p]["i"].tolist()):
                sal.setdefault(i, []).append(f_por_p[p][k])
            for s in set(ent) | set(sal):
                e = gp.LinExpr([1.0] * len(ent.get(s, [])), ent.get(s, []))
                e.add(gp.LinExpr([-1.0] * len(sal.get(s, [])), sal.get(s, [])))
                m.addLConstr(e, GRB.EQUAL, 0.0)
                n_cons += 1
        # Una unidad de flujo: la estacion existe y su inversion esta fija.
        m.addLConstr(gp.quicksum(f_por_p[0]), GRB.EQUAL, 1.0)
        n_cons += 1
        n_u = 0
        if variante.endswith("_u"):
            for p in range(T):
                ws = arcos[p]["w"].tolist()
                for w in sorted(set(ws)):
                    u = m.addVar(vtype=GRB.BINARY)
                    sel = [f_por_p[p][k] for k, wk in enumerate(ws) if wk == w]
                    m.addLConstr(gp.quicksum(sel) - u, GRB.EQUAL, 0.0)
                    n_u += 1
                    n_cons += 1

    def sustituir(expr, j, c):
        nombre, ix = col[j]
        if nombre == "W":
            expr.add(Wp[pos[ix[3]]], c)
        elif nombre == "X_ini":
            expr.add(Xp[pos[ix[3]]], c)
        else:
            raise RuntimeError(f"{nombre}{ix} fuera de las filas internas: no se sabe sustituir")

    # Costo de las columnas que se quitan (si tuvieran).
    if flujo:
        extra = gp.LinExpr()
        for j in quitar:
            if P.obj[j] != 0.0:
                if col[j][0] == "Sv":
                    extra.add(Xp[pos[col[j][1][4]]], P.obj[j])
                else:
                    sustituir(extra, j, P.obj[j])
        if extra.size():
            m.setObjective(m.getObjective() + extra)

    filas_quitadas = 0
    for i in P.filas_bloque[b]:
        if flujo and comp_fila(P.cname[i]) in FILAS_INTERNAS:
            filas_quitadas += 1
            continue
        idx, coef = P.filas[i]
        expr = gp.LinExpr()
        sv = {}
        for j, c in zip(idx.tolist(), coef.tolist()):
            if j in quitar:
                if col[j][0] == "Sv":
                    sv.setdefault(pos[col[j][1][3]], []).append((pos[col[j][1][4]], c))
                else:
                    sustituir(expr, j, c)
            else:
                expr.add(var[j], c)
        for p, lst in sv.items():
            if len(lst) == L and len({c for _, c in lst}) == 1:
                expr.add(Qp[p], lst[0][1])          # suma de la ventana completa = Q_t
            else:
                for pa, c in lst:
                    expr.add(Xp[pa], c)             # Sv[t, a] = X_ini[a]
        m.addLConstr(expr, P.sense[i], float(P.rhs[i]))
    # Prioridad de ramificacion (2026-10-23): con los swaps enteros la estacion
    # es exacta (chequeo), asi que conviene que Gurobi fije primero los swaps.
    #   actual_pz  Z_swap con prioridad 10 (el resto 0).
    #   actual_pn  ademas n_i = sum_t Z_swap[., i, t] (swaps del LHD i en el
    #              dia, entera) con prioridad 20: ramifica primero en CUANTOS
    #              swaps hace cada LHD y despues en CUANDO.
    if variante in ("actual_pz", "actual_pn"):
        por_lhd = {}
        for j in loc:
            if col[j][0] == "Z_swap" and j in var:
                var[j].BranchPriority = 10
                por_lhd.setdefault(col[j][1][1], []).append(var[j])
        if variante == "actual_pn":
            for i, zs in por_lhd.items():
                n_i = m.addVar(lb=0.0, ub=float(len(zs)), vtype=GRB.INTEGER)
                n_i.BranchPriority = 20
                m.addLConstr(gp.quicksum(zs) - n_i, GRB.EQUAL, 0.0)
    m.update()
    info = {"variante": variante, "vars": m.NumVars, "filas": m.NumConstrs,
            "enteras": m.NumIntVars, "arcos": n_f, "filas_flujo": n_cons,
            "filas_estacion_quitadas": filas_quitadas, "cols_estacion_quitadas": len(quitar),
            "t_armado": time.time() - t_ini}
    if flujo:
        info["estados"] = len(estados)
        info["nodos_vivos"] = n_nodos
        info["u_binarias"] = n_u
        info["arcos_w"] = [arcos[p]["w"].tolist() for p in range(T)]
    return m, var, Qp, f_por_p, info


def q_actual(var, col, loc, pos, T, x):
    """Q_t = sum de Sv en t, del vector x (dict j -> valor) del modelo actual."""
    Q = np.zeros(T)
    for j in loc:
        if col[j][0] == "Sv" and j in var:
            Q[pos[col[j][1][3]]] += x[j]
    return Q


def correr(P, b, xB, col, dia, variante, threads, timelimit, logdir):
    m, var, Qp, f_por_p, info = armar_dia(P, b, xB, col, dia, variante, threads)
    info.pop("arcos_w", None)
    loc = P.locales[b]
    clave = P.claves[b]
    # LP
    r = m.relax()
    t0 = time.time()
    r.optimize()
    info["t_lp"] = time.time() - t0
    info["lp"] = r.ObjVal if r.Status == GRB.OPTIMAL else None
    if r.Status == GRB.OPTIMAL:
        rv = r.getVars()
        xm = dict(zip(m.getVars(), (v.X for v in rv)))
        zs = [xm[var[j]] for j in loc if col[j][0] == "Z_swap" and j in var]
        info["lp_zswap_frac"] = sum(1 for v in zs if abs(v - round(v)) > 1e-6)
        info["lp_zswap_n"] = len(zs)
        if Qp is not None:
            Q = np.array([sum(Qp[p].getCoeff(k) * xm[Qp[p].getVar(k)] for k in range(Qp[p].size()))
                          for p in range(dia["T"])])
            fs = [xm[f] for fp in f_por_p for f in fp]
            info["lp_f_frac"] = sum(1 for v in fs if 1e-6 < v < 1 - 1e-6)
        else:
            Q = q_actual(var, col, loc, dia["pos"], dia["T"],
                         {j: xm[var[j]] for j in loc if j in var})
        info["lp_Q_frac"] = int(sum(1 for v in Q if abs(v - round(v)) > 1e-6))
    r.dispose()
    # MILP con traza de la cota
    traza = []

    def cb(model, where):
        if where == GRB.Callback.MIP:
            traza.append((model.cbGet(GRB.Callback.RUNTIME), model.cbGet(GRB.Callback.MIP_NODCNT),
                          model.cbGet(GRB.Callback.MIP_OBJBND), model.cbGet(GRB.Callback.MIP_OBJBST)))
        elif where == GRB.Callback.MIPNODE:
            traza.append((model.cbGet(GRB.Callback.RUNTIME), model.cbGet(GRB.Callback.MIPNODE_NODCNT),
                          model.cbGet(GRB.Callback.MIPNODE_OBJBND),
                          model.cbGet(GRB.Callback.MIPNODE_OBJBST)))

    m.Params.TimeLimit = timelimit
    m.Params.MIPGap = 1e-4
    m.Params.OutputFlag = 1
    m.Params.LogToConsole = 0
    m.Params.LogFile = os.path.join(logdir, f"{clave.replace(' ', '')}_{variante}.log")
    t0 = time.time()
    m.optimize(cb)
    info["t_milp"] = time.time() - t0
    info["status"] = m.Status
    info["mejor"] = m.ObjVal if m.SolCount else None
    info["cota_final"] = m.ObjBound
    info["nodos"] = m.NodeCount
    raiz = [bd for (_t, n, bd, _b) in traza if n == 0 and bd > -1e99]
    info["cota_raiz"] = max(raiz) if raiz else None
    c180 = [bd for (t, _n, bd, _b) in traza if t <= 180 and bd > -1e99]
    info["cota_180"] = max(c180) if c180 else None
    i180 = [bs for (t, _n, _bd, bs) in traza if t <= 180 and bs < 1e99]
    info["mejor_180"] = min(i180) if i180 else None
    if m.Status == GRB.OPTIMAL:
        info["cota_180"] = info["cota_180"] if info["t_milp"] > 180 else m.ObjBound
    info["dia"] = clave
    m.dispose()
    return info


def chequeo_fijo(P, b, xB, col, dia, threads):
    """LP de arc_cost con Y, Z y Z_swap fijos en el incumbente: la estacion
    queda con los swaps dados y el LP del flujo deberia ser la DP (si f sale
    entero) o menos (si el LP mezcla arcos con distinto w en un mismo t)."""
    out = {}
    # Referencia: el LP del modelo ACTUAL (estacion con su formulacion de
    # siempre) con el lado LHD fijo y entero. Acota por arriba la cota de una
    # DW por vehiculo con la estacion en el maestro (el incumbente es un punto
    # factible de ese maestro, una columna por LHD con peso 1).
    ma, vara, _q, _f, _i = armar_dia(P, b, xB, col, dia, "actual", threads)
    for j in P.locales[b]:
        if j in vara and col[j][0] in ("Y", "Z", "Z_swap"):
            v = float(round(xB[j]))
            vara[j].LB = vara[j].UB = v
    ma.update()
    ra = ma.relax()
    ra.optimize()
    out["actual_lhd_fijo"] = {"lp": ra.ObjVal if ra.Status == GRB.OPTIMAL else None,
                              "status": ra.Status}
    ra.dispose()
    ma.dispose()
    m, var, _Qp, f_por_p, info = armar_dia(P, b, xB, col, dia, "arc_cost", threads)
    for j in P.locales[b]:
        if j in var and col[j][0] in ("Y", "Z", "Z_swap"):
            v = float(round(xB[j]))
            var[j].LB = var[j].UB = v
    # relax() no ve los cambios pendientes: sin este update el modo "agregado"
    # corria con los swaps LIBRES (error corregido 2026-10-23).
    m.update()
    W = np.zeros(dia["T"], dtype=int)
    for j in P.locales[b]:
        if col[j][0] == "Z_swap":
            W[dia["pos"][round(col[j][1][-1])]] += int(round(xB[j]))
    for modo in ("agregado", "w_fijo"):
        if modo == "w_fijo":
            # Solo los arcos con w_a = W_t: la estacion es una red pura.
            for p, fp in enumerate(f_por_p):
                for k, f in enumerate(fp):
                    if info["arcos_w"][p][k] != W[p]:
                        f.UB = 0.0
            m.update()
        r = m.relax()
        r.optimize()
        o = {"lp": r.ObjVal if r.Status == GRB.OPTIMAL else None, "status": r.Status}
        if r.Status == GRB.OPTIMAL:
            xm = dict(zip(m.getVars(), (v.X for v in r.getVars())))
            fs = [xm[f] for fp in f_por_p for f in fp]
            o["f_frac"] = sum(1 for v in fs if 1e-6 < v < 1 - 1e-6)
        out[modo] = o
        r.dispose()
    m.dispose()
    return out


# ------------------------------------------------------------------ main -- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--respaldo", required=True)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--dias", default="4,196;5,288;7,105")
    ap.add_argument("--variantes", default=",".join(VARIANTES))
    ap.add_argument("--timelimit", type=float, default=900)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    logdir = os.path.join(a.out, "gurobi")
    os.makedirs(logdir, exist_ok=True)
    t_ini = time.time()

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                   consumption_model="wp1", wp2_consumption_json=None, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    cargar_respaldo(om, ms, ts, a.respaldo)
    m = om.model
    adaptar_incumbente_ciclico(m)
    info, por_var = gb.export(m, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, m, por_var, jobs=1, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    costo, xB, _eB = bd.cargar_incumbente(vec0)
    print(f"[flujo] incumbente pulido (pipeline ciclico) {costo:,.2f}  ({time.time() - t_ini:.0f}s)",
          flush=True)

    col = {}
    por_comp = {}
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        if vd is None:
            continue
        idx = vd.index()
        idx = idx if isinstance(idx, tuple) else (() if idx is None else (idx,))
        col[j] = (vd.parent_component().name, idx)
        por_comp.setdefault(col[j][0], {})[idx] = j
    for j in range(P.nv):
        col.setdefault(j, ("?", ()))

    def val(c, idx):
        j = por_comp.get(c, {}).get(tuple(idx))
        return float(xB[j]) if j is not None else 0.0

    tiempos = [float(t) for t in ts.get_time_intervals()]
    T = len(tiempos)
    pos = {}
    for k, t in enumerate(tiempos):
        pos[round(t)] = k
        pos[t] = k
    p_c = float(value(m.p_charger))
    L = int(value(m.t_charge))
    n_peak = int(value(m.n_peak_max))
    p_peak = float(value(m.p_peak))
    estaciones = list(m.stations_set)
    if len(estaciones) != 1:
        raise SystemExit("el experimento supone una sola nave")
    k = estaciones[0]
    gens = list(m.gen_set) if hasattr(m, "gen_set") else []
    if hasattr(m, "storage_set") and len(list(m.storage_set)) and val("H", ()) > 1e-6:
        print("[flujo] OJO: hay BESS; arc_cost no es exacto con BESS", flush=True)
    horas_punta = {round(float(t)) for t in m.time_intervals_peak_set}

    tareas, dias = [], {}
    for par in a.dias.split(";"):
        y, d = (int(v) for v in par.split(","))
        clave = str((y, d))
        if clave not in P.claves:
            print(f"[flujo] ({y},{d}) no es un bloque; se salta")
            continue
        b = P.claves.index(clave)
        loc = P.locales[b]
        # Objetivo: que columnas con costo hay en el dia (para arc_cost).
        con_costo = sorted({col[j][0] for j in loc if P.obj[j] != 0.0})
        N = int(round(val("N_batteries", (k, y))))
        NC = int(round(val("N_chargers", (k, y))))
        n_ssee = int(round(val("n_ssee_k", (k,))))
        P_pot = val("P_pot", (y,))
        A = np.zeros(T)
        for g in gens:
            G = val("G_g", (g,))
            for t in tiempos:
                A[pos[round(t)]] += G * float(value(m.p_max_g[g])) * float(value(m.alpha_g[g, y, d, t]))
        coef = np.zeros(T)
        w_max = np.zeros(T, dtype=int)
        for j in loc:
            c, ix = col[j]
            if c == "P_red":
                coef[pos[round(ix[-1])]] = P.obj[j]
            elif c == "Z_swap" and P.ub[j] > 0.5:
                w_max[pos[round(ix[-1])]] += 1
        q_pl, q_arc = [], []
        for p in range(T):
            q = min(NC, math.floor(P_SSEE_STEP * n_ssee / p_c + EPS), n_peak)
            q_pl.append(max(q, 0))
            q = min(q, math.floor((p_peak + A[p]) / p_c + EPS))
            if 91 <= d <= 244 and round(tiempos[p]) in horas_punta:
                q = min(q, math.floor((P_pot + A[p]) / p_c + EPS))
            q_arc.append(max(q, 0))
        dias[b] = {"T": T, "L": L, "pos": pos, "N": N, "NC": NC, "n_ssee": n_ssee, "P_pot": P_pot,
                   "A": A, "coef": coef, "p_c": p_c, "w_max": w_max, "q_max_pl": q_pl,
                   "q_max_arc": q_arc}
        print(f"[flujo] {clave}: N={N} NC={NC} L={L} n_ssee={n_ssee} P_pot={P_pot:.1f} "
              f"swaps del incumbente={int(round(sum(xB[j] for j in loc if col[j][0] == 'Z_swap')))} "
              f"| columnas con costo: {con_costo}", flush=True)
        for v in a.variantes.split(","):
            tareas.append((b, v))

    # Chequeo de integralidad del flujo con los swaps fijos (rapido).
    chequeos = {}
    for b in dias:
        chequeos[P.claves[b]] = chequeo_fijo(P, b, xB, col, dias[b], a.threads)
        print(f"[flujo] chequeo {P.claves[b]} (arc_cost, Y/Z/Z_swap fijos): "
              f"{chequeos[P.claves[b]]}", flush=True)

    def tarea(bv):
        b, v = bv
        r = correr(P, b, xB, col, dias[b], v, a.threads, a.timelimit, logdir)
        r["incumbente_dia"] = float(np.dot(P.obj[P.locales[b]], xB[P.locales[b]]))
        fmt = lambda x: "-" if x is None else f"{x:,.2f}"
        print(f"[flujo] {r['dia']} {v:>10}: vars {r['vars']:,} filas {r['filas']:,} "
              f"(arcos {r['arcos']:,}, estados {r.get('estados', '-')}) | LP {fmt(r['lp'])} "
              f"(Zswap frac {r.get('lp_zswap_frac')}/{r.get('lp_zswap_n')}, Q frac {r.get('lp_Q_frac')}) "
              f"| raiz {fmt(r['cota_raiz'])} | cota 180s {fmt(r['cota_180'])} "
              f"| cota final {fmt(r['cota_final'])} | mejor {fmt(r['mejor'])} "
              f"| incumbente {fmt(r['incumbente_dia'])} | {r['t_milp']:.0f}s status {r['status']}",
              flush=True)
        with open(os.path.join(a.out, "parcial.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(r) + "\n")
        return r

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        filas = list(ex.map(tarea, tareas))

    with open(os.path.join(a.out, "experimento.json"), "w", encoding="utf-8") as fh:
        json.dump({"incumbente": costo, "timelimit": a.timelimit, "threads": a.threads,
                   "filas": filas, "chequeos": chequeos, "data_folder": a.data_folder,
                   "respaldo": a.respaldo}, fh, indent=1, default=float)
    print(f"[flujo] listo en {time.time() - t_ini:.0f}s -> {a.out}/experimento.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
