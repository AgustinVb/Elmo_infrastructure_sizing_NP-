"""Relajacion lagrangiana por LHD del MILP de un dia, con la inversion fija.

Motivo (resultados_estacion_flujo.md, 2026-10-23): con los swaps enteros la
estacion es exacta (su LP da la DP), y el LP del dia queda bajo solo porque los
swaps salen fraccionarios. Convexificar cada LHD por separado deberia subir la
cota. Este script mide cuanto, en un dia.

Bloques: las columnas locales del dia se reparten por dueno (el LHD que aparece
en su indice: Y, Z, Z_swap, B_s, StartAssign, ...) y un bloque EST con el resto
(estacion: S, Sv, X_ini, X_dch, W; potencia: P_red, P_gen, Curt_g, BESS). Las
copias del maestro (inversion) quedan FIJAS en el incumbente y pasan al lado
derecho. Una fila del dia que solo toca un bloque va a ese bloque; las que tocan
varios (W = sum Z_swap, meta diaria, filas entre LHD) se dualizan:

    L(mu) = -sum_r mu_r rhs_r + sum_b min_{x_b in X_b} (c_b + sum_r mu_r a_rb) x_b

con mu_r >= 0 en filas <=, <= 0 en filas >= y libre en =. Cada bloque es un MILP
chico; se usa su ObjBound, asi que L es una cota valida aunque un bloque no
cierre. Arranque: duales del LP del dia (mu = -Pi). Subgradiente con paso de
Polyak contra la mejor solucion conocida del dia.

    python -u lagrangiana_lhd.py --data_folder data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte/05C/RED_GEN \\
        --respaldo output/.../RED_GEN/benders/incumbente_respaldo_k2.pkl --dia "4,196" \\
        --ub_dia 20948.57 --minutos 40 --out output/.../lagrangiana_lhd/caso3_v4_4_196
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)
# Misma referencia que experimento_estacion_flujo (LP 17.489, MILP 18.433 /
# 20.949, LHD fijos 24.151 en (4,196)): pipeline ciclico.
os.environ.setdefault("ELMO_PIPELINE_CICLICO", "1")

import gurobipy as gp                                                    # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from experimento_estacion_flujo import comp_fila                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.functions import pipeline_ciclico                  # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402
from validar_dp_estacion import adaptar_incumbente_ciclico               # noqa: E402


def log(fh, s):
    print(s, flush=True)
    fh.write(s + "\n")
    fh.flush()


def generacion_columnas(a, fh, t_ini, lp, y, d, bloques, cols_de, filas_de, tipos, modelos,
                        evaluar, A_por, c0, rhs_A, sent_A, xB, loc, env):
    """Cota lagrangiana por generacion de columnas (Dantzig-Wolfe por bloques).

    Maestro: min sum_{b,k} (c_b x_b^k) l_bk  s.a. filas de acople con sum_k
    (A_b x_b^k) l_bk, sum_k l_bk = 1 por bloque, l >= 0. Arranca con las columnas
    del incumbente (factible). En cada ronda, mu_sep = alpha mu_centro + (1 -
    alpha) mu_maestro (Wentges), con mu = -Pi. Se resuelven los bloques con esos
    precios y se obtiene L(mu_sep), cota valida. Se agregan las soluciones con
    costo reducido negativo respecto de los duales del maestro. Converge a la
    misma cota que la lagrangiana (max_mu L(mu)), sin tamano de paso."""
    M = gp.Model(env=env())
    M.Params.OutputFlag = 0
    filas_M = [M.addLConstr(gp.LinExpr(), s, float(r)) for s, r in zip(sent_A, rhs_A)]
    conv = {bl: M.addLConstr(gp.LinExpr(), "=", 1.0) for bl in bloques}
    n_cols = {bl: 0 for bl in bloques}

    def agregar(bl, xb):
        ps = modelos[bl][2]
        x = np.zeros(len(loc))
        x[ps] = xb
        ax = A_por(x)
        nz = np.nonzero(np.abs(ax) > 1e-12)[0]
        col = gp.Column(ax[nz].tolist() + [1.0], [filas_M[r] for r in nz] + [conv[bl]])
        M.addVar(lb=0.0, obj=float(c0[ps] @ xb), column=col)
        n_cols[bl] += 1

    pos = {j: k for k, j in enumerate(loc)}
    for bl in bloques:
        agregar(bl, np.array([xB[j] for j in cols_de[bl]]))
    M.update()

    mu_centro, L_mejor, alpha, it, historia, zM = None, -np.inf, a.alpha, 0, [], float("nan")
    limite = time.time() + 60 * a.minutos
    while time.time() < limite:
        it += 1
        t0 = time.time()
        M.optimize()
        if M.Status != 2:
            log(fh, f"[cg] maestro sin optimo (status {M.Status})")
            break
        zM = M.ObjVal
        mu_M = -np.array(M.getAttr("Pi", filas_M))
        sig = {bl: conv[bl].Pi for bl in bloques}
        mu_sep = mu_M if mu_centro is None else alpha * mu_centro + (1 - alpha) * mu_M
        L, _g, det, x = evaluar(mu_sep)
        if L > L_mejor + 1e-9:
            L_mejor, mu_centro = L, mu_sep.copy()
        nuevas = 0
        for bl in bloques:
            ps = modelos[bl][2]
            xb = x[ps]
            xx = np.zeros(len(loc))
            xx[ps] = xb
            rc = float(c0[ps] @ xb + mu_M @ A_por(xx) - sig[bl])
            if rc < -1e-7 * max(1.0, abs(zM)):
                agregar(bl, xb)
                nuevas += 1
        M.update()
        gap = (zM - L_mejor) / max(abs(zM), 1e-9)
        t_bl = max(v["t"] for v in det.values())
        historia.append({"it": it, "maestro": zM, "L": L, "mejor": L_mejor, "alpha": alpha,
                         "nuevas": nuevas, "t": time.time() - t0, "bloques": det})
        log(fh, f"[cg] it {it:3d}  maestro {zM:12,.2f}  L {L:12,.2f}  mejor {L_mejor:12,.2f}  "
                f"gap {gap:7.3%}  alpha {alpha:.2f}  +{nuevas} col (total "
                f"{sum(n_cols.values())})  bloque mas lento {t_bl:5.1f}s  ({time.time() - t_ini:.0f}s)")
        if gap <= 1e-4:
            log(fh, "[cg] convergio: maestro = cota lagrangiana")
            break
        if nuevas == 0:
            if alpha > 0:
                alpha = 0.0           # mispricing: la proxima ronda con los duales del maestro
            else:
                log(fh, "[cg] sin columnas con los duales del maestro: convergio")
                break
        elif alpha == 0.0:
            alpha = a.alpha
    log(fh, f"[cg] FIN: cota lagrangiana {L_mejor:,.2f}; maestro {zM:,.2f}; LP del dia {lp:,.2f}; "
            f"mejor solucion conocida {a.ub_dia:,.2f}; {it} rondas ({time.time() - t_ini:.0f}s)")
    with open(os.path.join(a.out, "cg.json"), "w", encoding="utf-8") as fo:
        json.dump({"dia": [y, d], "lp": lp, "cota": L_mejor, "maestro": zM, "ub_dia": a.ub_dia,
                   "bloques": {bl: [len(cols_de[bl]), len(filas_de[bl])] for bl in bloques},
                   "acople": dict(tipos), "historia": historia}, fo, indent=1, default=float)
    fh.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--respaldo", required=True)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--dia", default="4,196")
    ap.add_argument("--ub_dia", type=float, required=True,
                    help="mejor solucion conocida del dia (objetivo del paso de Polyak)")
    ap.add_argument("--minutos", type=float, default=40)
    ap.add_argument("--theta", type=float, default=1.0)
    ap.add_argument("--paciencia", type=int, default=5,
                    help="iteraciones sin mejorar antes de partir theta a la mitad")
    ap.add_argument("--tl_bloque", type=float, default=120)
    ap.add_argument("--metodo", choices=("cg", "subgradiente"), default="cg",
                    help="cg: maestro de Dantzig-Wolfe (Kelley en el dual) con suavizado de "
                         "Wentges; subgradiente: paso de Polyak desde los duales del LP")
    ap.add_argument("--alpha", type=float, default=0.5, help="suavizado de Wentges (cg)")
    ap.add_argument("--enlace_igualdad", action="store_true",
                    help="W = sum Z_swap como igualdad (por defecto, W >= sum Z_swap)")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    fh = open(os.path.join(a.out, "lagrangiana.log"), "w", encoding="utf-8")
    t_ini = time.time()
    log(fh, f"[lag] pipeline ciclico: {pipeline_ciclico()}")

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx", series="time_series.xlsx",
                   consumption_model="wp1", wp2_consumption_json=None, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    cargar_respaldo(om, ms, ts, a.respaldo)
    m = om.model
    if pipeline_ciclico():
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
    log(fh, f"[lag] incumbente pulido {costo:,.2f} ({time.time() - t_ini:.0f}s)")

    y, d = (int(v) for v in a.dia.split(","))
    b = P.claves.index(str((y, d)))
    loc = [int(j) for j in P.locales[b]]
    pos_loc = {j: k for k, j in enumerate(loc)}
    lhds = {str(i) for i in m.lhd_set}

    # Dueno de cada columna local: el LHD de su indice, o EST.
    comp, dueno = {}, {}
    for j in loc:
        vd = por_var.get(P.nombre[j])
        if vd is None:
            comp[j], dueno[j] = "?", "EST"
            continue
        idx = vd.index()
        idx = idx if isinstance(idx, tuple) else (idx,)
        comp[j] = vd.parent_component().name
        ds = {str(e) for e in idx if str(e) in lhds}
        if len(ds) > 1:
            raise SystemExit(f"{P.nombre[j]} tiene dos LHD en el indice")
        dueno[j] = ds.pop() if ds else "EST"
    bloques = sorted(set(dueno.values()))
    cols_de = {bl: [j for j in loc if dueno[j] == bl] for bl in bloques}

    # Filas del dia: rhs efectivo (copias fijas al lado derecho) y reparto.
    def valor_copia(j):
        return float(round(xB[j])) if P.vtype[j] != "C" else float(xB[j])

    filas_de = {bl: [] for bl in bloques}
    acople = []          # (nombre, cols, coefs, sentido, rhs)
    for i in P.filas_bloque[b]:
        idx, coef = P.filas[i]
        rhs = float(P.rhs[i])
        cols, cs = [], []
        for j, c in zip(idx.tolist(), coef.tolist()):
            if P.var_blk[j] == b:
                cols.append(j)
                cs.append(c)
            else:
                rhs -= c * valor_copia(j)
        if not cols:
            continue
        ds = {dueno[j] for j in cols}
        sentido = P.sense[i]
        # Enlace por DESIGUALDAD (decision de la usuaria para toda descomposicion
        # por columnas, 2026-10-01): W >= sum_i Z_swap, la estacion entrega al
        # menos los swaps que piden los LHD. Relajacion valida (un swap de mas
        # solo le cuesta carga a la estacion) y saca al maestro de la igualdad
        # exacta intervalo a intervalo, que lo dejaba pegado en el incumbente.
        if (len(ds) > 1 and sentido == "=" and not a.enlace_igualdad
                and comp_fila(P.cname[i]) == "total_swaps"):
            cw = sum(c for j, c in zip(cols, cs) if comp[j] == "W")
            sentido = ">" if cw > 0 else "<"
        fila = (P.cname[i], cols, cs, sentido, rhs)
        if len(ds) == 1:
            filas_de[ds.pop()].append(fila)
        else:
            acople.append(fila)
    from collections import Counter
    tipos = Counter(comp_fila(f[0]) for f in acople)
    log(fh, f"[lag] dia ({y},{d}): {len(loc):,} columnas; bloques "
            + ", ".join(f"{bl}: {len(cols_de[bl]):,} col / {len(filas_de[bl]):,} filas" for bl in bloques)
            + f"; {len(acople)} filas de acople {dict(tipos)}")

    # Matriz de acople (filas de acople x columnas locales).
    filas_A, cols_A, vals_A = [], [], []
    for r, (_n, cols, cs, _s2, _rhs) in enumerate(acople):
        for j, c in zip(cols, cs):
            filas_A.append(r)
            cols_A.append(pos_loc[j])
            vals_A.append(c)
    filas_A = np.array(filas_A, dtype=np.int64)
    cols_A = np.array(cols_A, dtype=np.int64)
    vals_A = np.array(vals_A, dtype=float)
    n_acople = len(acople)

    def At_por(v):          # A^T v, sin scipy (no esta en .venv_elmo)
        out = np.zeros(len(loc))
        np.add.at(out, cols_A, vals_A * v[filas_A])
        return out

    def A_por(x):           # A x
        return np.bincount(filas_A, weights=vals_A * x[cols_A], minlength=n_acople)
    rhs_A = np.array([f[4] for f in acople])
    sent_A = [f[3] for f in acople]
    c0 = np.array([P.obj[j] for j in loc])

    def env():
        e = gp.Env(empty=True)
        e.setParam("OutputFlag", 0)
        e.setParam("Threads", a.threads)
        e.start()
        return e

    # LP completo del dia (chequeo: debe dar el LP de siempre) y duales de acople.
    F = gp.Model(env=env())
    vF = [F.addVar(lb=P.lb[j], ub=P.ub[j], obj=P.obj[j]) for j in loc]
    for bl in bloques:
        for (_n, cols, cs, s, rhs) in filas_de[bl]:
            F.addLConstr(gp.LinExpr(cs, [vF[pos_loc[j]] for j in cols]), s, rhs)
    cA = [F.addLConstr(gp.LinExpr(cs, [vF[pos_loc[j]] for j in cols]), s, rhs)
          for (_n, cols, cs, s, rhs) in acople]
    F.update()
    F.optimize()
    lp = F.ObjVal
    mu = -np.array(F.getAttr("Pi", cA))
    log(fh, f"[lag] LP del dia {lp:,.2f} (debe ser el LP de siempre); |mu_LP| = {np.abs(mu).sum():,.2f}")
    F.dispose()

    # Un modelo MILP por bloque.
    modelos = {}
    for bl in bloques:
        mb = gp.Model(env=env())
        vs = [mb.addVar(lb=P.lb[j], ub=P.ub[j], vtype=P.vtype[j]) for j in cols_de[bl]]
        mapa = dict(zip(cols_de[bl], vs))
        for (_n, cols, cs, s, rhs) in filas_de[bl]:
            mb.addLConstr(gp.LinExpr(cs, [mapa[j] for j in cols]), s, rhs)
        mb.Params.MIPGap = 1e-6
        mb.Params.TimeLimit = a.tl_bloque
        mb.update()
        modelos[bl] = (mb, vs, np.array([pos_loc[j] for j in cols_de[bl]]))

    def proyectar(v):
        v = v.copy()
        for r, s in enumerate(sent_A):
            if s == "<":
                v[r] = max(v[r], 0.0)
            elif s == ">":
                v[r] = min(v[r], 0.0)
        return v

    def evaluar(mu):
        costo = c0 + At_por(mu)

        def tarea(bl):
            mb, vs, ps = modelos[bl]
            mb.setAttr("Obj", vs, costo[ps].tolist())
            t0 = time.time()
            mb.optimize()
            x = np.array(mb.getAttr("X", vs)) if mb.SolCount else None
            return bl, mb.ObjBound, mb.MIPGap if mb.SolCount else None, x, time.time() - t0, mb.Status
        res = list(ThreadPoolExecutor(max_workers=len(bloques)).map(tarea, bloques))
        x = np.zeros(len(loc))
        L = -float(mu @ rhs_A)
        detalle = {}
        for bl, bound, gap, xb, t, st in res:
            L += bound
            if xb is not None:
                x[modelos[bl][2]] = xb
            detalle[bl] = {"bound": bound, "gap": gap, "t": round(t, 2), "status": st}
        g = A_por(x) - rhs_A
        return L, g, detalle, x

    if a.metodo == "cg":
        return generacion_columnas(a, fh, t_ini, lp, y, d, bloques, cols_de, filas_de, tipos,
                                   modelos, evaluar, A_por, c0, rhs_A, sent_A, xB, loc, env)

    mu = proyectar(mu)
    theta, mejor, sin_mejora, it = a.theta, -np.inf, 0, 0
    historia = []
    limite = time.time() + 60 * a.minutos
    while time.time() < limite and theta > 1e-4:
        it += 1
        t0 = time.time()
        L, g, det, _x = evaluar(mu)
        if L > mejor + 1e-6:
            mejor, sin_mejora, mu_mejor = L, 0, mu.copy()
        else:
            sin_mejora += 1
            if sin_mejora >= a.paciencia:
                theta /= 2
                sin_mejora = 0
                mu = mu_mejor.copy()       # vuelve al mejor punto al achicar el paso
        nn = float(g @ g)
        t_bl = max(v["t"] for v in det.values())
        gap_max = max((v["gap"] or 0) for v in det.values())
        historia.append({"it": it, "L": L, "mejor": mejor, "theta": theta, "norma_g": nn ** 0.5,
                         "t": time.time() - t0, "bloques": det})
        log(fh, f"[lag] it {it:3d}  L {L:12,.2f}  mejor {mejor:12,.2f}  theta {theta:.4f}  "
                f"|g| {nn ** 0.5:10.3f}  bloque mas lento {t_bl:5.1f}s  gap max {gap_max:.1e}  "
                f"({time.time() - t_ini:.0f}s)")
        if nn < 1e-12:
            log(fh, "[lag] subgradiente nulo: optimo del dual")
            break
        paso = theta * max(a.ub_dia - L, 1e-6 * abs(a.ub_dia)) / nn
        mu = proyectar(mu + paso * g)

    log(fh, f"[lag] FIN: mejor cota lagrangiana {mejor:,.2f} en {it} iteraciones "
            f"({time.time() - t_ini:.0f}s). Referencias del dia: LP {lp:,.2f}; ub {a.ub_dia:,.2f}")
    with open(os.path.join(a.out, "lagrangiana.json"), "w", encoding="utf-8") as fo:
        json.dump({"dia": [y, d], "lp": lp, "mejor": mejor, "ub_dia": a.ub_dia,
                   "bloques": {bl: [len(cols_de[bl]), len(filas_de[bl])] for bl in bloques},
                   "acople": dict(tipos), "historia": historia}, fo, indent=1, default=float)
    fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
