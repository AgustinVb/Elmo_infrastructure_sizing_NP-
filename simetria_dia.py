"""¿Sube la cota del MILP de un dia si se rompe la simetria entre LHD?

diagnostico_gap_dias.py mostro que en los años de flota grande (5, 7, 8) la
cota MILP de un dia con la inversion FIJA se congela: en el año 5 ni se despega
del LP. Los LHD son identicos (mismos parametros, todos asignados a todos los
nodos), asi que Gurobi explora muchas soluciones equivalentes.

Los LHD NO son intercambiables: cada uno tiene su propia calle y sus nodos
(NodeAssignment). Pero dentro de cada LHD los nodos vienen en PARES identicos
(misma calle, distancia, cuota): PEX_1/PEX_6 a 25 m, PEX_2/PEX_7 a 45 m, ...
Los pares se detectan en el modelo (mismo LHD asignado y mismos d_i, pe_i y m_j)
y cada transposicion se VERIFICA sobre la operacion del incumbente: con los
dos nodos intercambiados tiene que seguir factible y con el mismo costo. Las
que no pasan se descartan. Variantes, en paralelo y con el mismo tiempo:

  base        el dia tal cual (MIPFocus 3)
  sym2        Symmetry=2 (deteccion agresiva de Gurobi)
  pares       sum_t Y[i,p,t] >= sum_t Y[i,q,t] para cada par (p, q)
  pares_sym2  pares + Symmetry=2

Los pares son disjuntos, asi que el grupo es un producto de transposiciones
independientes y ordenar cada par por separado es valido. Si una variante
termina con cota sobre el incumbente, corta el optimo: no es valida.

    python -u simetria_dia.py --respaldo output/.../P_red_only_r4/incumbente_respaldo.pkl \
        --dia "(5, 15)" --timelimit 900 --out output/.../simetria_y5d15
"""
import argparse
import json
import os
import sys
import threading
import time
from argparse import Namespace

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

import gurobipy as gp                                                    # noqa: E402
from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

VARIANTES = ("base", "sym2", "pares", "pares_sym2")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--respaldo", required=True)
    ap.add_argument("--dia", default="(5, 15)", help='clave del bloque, p.ej. "(5, 15)"')
    ap.add_argument("--variantes", default=",".join(VARIANTES))
    ap.add_argument("--timelimit", type=float, default=900)
    ap.add_argument("--threads", type=int, default=12, help="hilos por variante")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    cargar_respaldo(om, ms, ts, a.respaldo)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, om.model, por_var, jobs=1, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    UB, xB, _ = bd.cargar_incumbente(vec0)
    print(f"[sim] incumbente pulido {UB:,.2f}", flush=True)

    b = P.claves.index(a.dia) if a.dia in P.claves else \
        [k.replace(" ", "") for k in P.claves].index(a.dia.replace(" ", ""))
    s = bd.subs[b]
    # Modo "ub": enteras, copias fijas en la inversion del incumbente.
    s._entera(True)
    s._enlaces(s.M, 0.0, 0.0, float("inf"))
    s._copias()
    s._rhs(xB, np.zeros(max(s.pres, default=-1) + 1))
    s.m.update()
    opex_B = float(np.dot(P.obj[s.loc], xB[s.loc]))
    print(f"[sim] dia {P.claves[b]}: {len(s.loc):,} variables locales; operacion del "
          f"incumbente {opex_B:,.2f}", flush=True)

    # Indice (componente, indice pyomo) de cada variable local.
    pos = {}
    info_var = []
    for k, j in enumerate(s.loc):
        vd = por_var.get(P.nombre[j])
        if vd is None:
            info_var.append(None)
            continue
        idx = vd.index()
        idx = idx if isinstance(idx, tuple) else (idx,)
        clave = (vd.parent_component().name, idx)
        pos[clave] = k
        info_var.append(clave)

    # --- 1) pares de nodos identicos dentro de cada LHD ---
    mdl = om.model
    y_dia = eval(P.claves[b])[0]
    nodos_de = {}
    for c in info_var:
        if c and c[0] == "Y":
            nodos_de.setdefault(c[1][0], set()).add(c[1][1])

    def firma(i, j):
        return (round(value(mdl.d_i[i, j]), 9), round(value(mdl.pe_i[i, j]), 9),
                tuple(round(value(mdl.m_j[j, y]), 9) for y in mdl.years))

    candidatos = []
    for i, nodos in nodos_de.items():
        por_firma = {}
        for j in sorted(nodos):
            por_firma.setdefault(firma(i, j), []).append(j)
        for grupo in por_firma.values():
            for p, q in zip(grupo[0::2], grupo[1::2]):
                candidatos.append((i, p, q))
    print(f"[sim] {len(candidatos)} pares candidatos de nodos identicos "
          f"(año {y_dia}): {candidatos[:4]} ...", flush=True)

    xloc = xB[s.loc]

    def transpuesta(p, q):
        perm = list(range(len(s.loc)))
        for k, clave in enumerate(info_var):
            if clave is None:
                continue
            nombre, idx = clave
            if p in idx or q in idx:
                idx2 = tuple(q if e == p else p if e == q else e for e in idx)
                k2 = pos.get((nombre, idx2))
                if k2 is None:
                    return None
                perm[k] = k2
        return perm

    pares = []
    perms = {}
    for i, p, q in candidatos:
        perm = transpuesta(p, q)
        if perm is None:
            print(f"[sim]   {p}<->{q}: sin variable par, se descarta", flush=True)
            continue
        xperm = xloc[perm]
        mp = s.m.copy()
        vs = mp.getVars()[:len(s.loc)]
        mp.setAttr("LB", vs, xperm.tolist())
        mp.setAttr("UB", vs, xperm.tolist())
        mp.Params.OutputFlag = 0
        mp.optimize()
        costo = float(np.dot(P.obj[s.loc], xperm))
        if mp.Status == GRB.OPTIMAL and abs(costo - opex_B) <= 1e-6 * max(1.0, abs(opex_B)):
            pares.append((i, p, q))
            perms[(i, p, q)] = perm
        else:
            print(f"[sim]   {p}<->{q}: status {mp.Status}, costo {costo:,.2f} -> se descarta",
                  flush=True)
    print(f"[sim] {len(pares)} pares verificados (permutados siguen factibles y con el "
          f"mismo costo)", flush=True)
    if not pares:
        print("[sim] sin pares validos: solo se comparan base y sym2")

    # MIP start que cumple el orden de los pares: el incumbente con cada par
    # intercambiado donde visita menos el primer nodo (equivalente: mismo costo).
    def viajes(x, i, j):
        return sum(x[k] for k, c in enumerate(info_var) if c and c[0] == "Y"
                   and c[1][0] == i and c[1][1] == j)

    x_ord = xloc.copy()
    n_inv = 0
    for (i, p, q), perm in perms.items():
        if viajes(x_ord, i, p) < viajes(x_ord, i, q) - 1e-6:
            x_ord = x_ord[perm]
            n_inv += 1
    start_pares = [round(x_ord[k]) for k in s.ent_loc]
    print(f"[sim] start de las variantes 'pares': incumbente con {n_inv} pares intercambiados",
          flush=True)

    # --- 2) variantes en paralelo ---
    def armar(variante):
        m = s.m.copy()
        vs = m.getVars()
        m.Params.Threads = a.threads
        m.Params.TimeLimit = a.timelimit
        m.Params.MIPGap = 1e-4
        m.Params.MIPFocus = 3
        m.Params.OutputFlag = 1
        m.Params.LogToConsole = 0
        m.Params.LogFile = os.path.join(a.out, f"{variante}.log")
        start = start_pares if variante.startswith("pares") else s.start
        if start is not None:
            m.setAttr("Start", [vs[k] for k in s.ent_loc], start)
        if "sym2" in variante:
            m.Params.Symmetry = 2
        if variante.startswith("pares"):
            for i, p, q in pares:
                yp = [vs[k] for k, c in enumerate(info_var) if c and c[0] == "Y"
                      and c[1][0] == i and c[1][1] == p]
                yq = [vs[k] for k, c in enumerate(info_var) if c and c[0] == "Y"
                      and c[1][0] == i and c[1][1] == q]
                m.addLConstr(gp.quicksum(yp) - gp.quicksum(yq), GRB.GREATER_EQUAL, 0.0)
        return m

    variantes = [v for v in a.variantes.split(",") if v and (pares or not v.startswith("pares"))]
    resultados = {}

    def correr(v):
        m = armar(v)
        t0 = time.time()
        m.optimize()
        resultados[v] = {"cota": m.ObjBound, "inc": m.ObjVal if m.SolCount else None,
                         "status": m.Status, "t": time.time() - t0, "nodos": m.NodeCount}

    hilos = [threading.Thread(target=correr, args=(v,)) for v in variantes]
    t0 = time.time()
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    print(f"\n[sim] {len(variantes)} variantes en {time.time() - t0:.0f}s "
          f"({a.threads} hilos c/u, {a.timelimit:.0f}s)\n")
    print(f"{'variante':>12} {'cota':>12} {'incumbente':>12} {'gap':>8} {'nodos':>10}  validez")
    for v in variantes:
        r = resultados[v]
        inc = r["inc"] if r["inc"] is not None else float("nan")
        gap = (inc - r["cota"]) / inc if r["inc"] else float("nan")
        valida = "OK" if r["cota"] <= opex_B * (1 + 1e-6) else "CORTA EL OPTIMO (cota > incumbente)"
        print(f"{v:>12} {r['cota']:>12,.2f} {inc:>12,.2f} {gap:>8.2%} {r['nodos']:>10,.0f}  {valida}")
    with open(os.path.join(a.out, "resultados.json"), "w", encoding="utf-8") as fh:
        json.dump({"dia": P.claves[b], "opex_incumbente": opex_B, "resultados": resultados},
                  fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
