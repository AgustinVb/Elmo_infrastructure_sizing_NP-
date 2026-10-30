"""Generacion de columnas orientada a la COTA INFERIOR del certificado por
inversiones (certificar_inversiones.py).

El certificado enumera las inversiones candidatas (N_bays, N_chargers,
n_ssee_k, N_batteries por año) cuya cota operacional queda bajo el umbral, con
el pool COMPLETO, y a cada una le calcula una LB con la inversion fija
(operacion LP + corte de cargas por LHD). La LB global es

    LB = min( LB de las candidatas VIVAS , umbral )

asi que solo sube si suben las LB de las candidatas mas baratas. Aca, a cada
candidata se le aplica Dantzig-Wolfe year_day con su inversion FIJA (un nodo de
cg_propia: el maestro queda sin las enteras de capacidad y el pricing
convexifica la operacion ENTERA de cada dia):

    LB_DW(cand) = max( LB del certificado , L lagrangiana del nodo )

Es valida para toda solucion con esa inversion; una candidata con
LB_DW >= UB queda descartada. Orden MEJOR-PRIMERO: siempre se trabaja la
candidata de menor LB, unas pocas rondas por visita (--rondas_visita) y hasta
que su LB supere la de la siguiente (objetivo de correr); despues vuelve a la
cola con su centro dual. Las columnas se comparten entre candidatas: el maestro
es uno solo y _aplicar_nodo deja fuera las incompatibles con la capacidad.

Centro de cada candidata: los duales del LP con su inversion fija
(relajacion_nodo), con L(pi) >= z_LP(nodo).

Se para cuando la candidata de menor LB convergio (la DW no da mas sin
ramificar), cuando ya no quedan vivas o por tiempo. Salidas en --out:
candidatas_dw.json (estado de cada candidata, se reescribe en cada visita),
historia_cg.json, trayectoria.json (LB global contra el tiempo) y resumen.json.

    python -u cg_certificado.py --data_folder data/.../RED_GEN_BESS_tope2 --n_years 10 \
        --solucion .../mejorar_ub_2/solucion --ub_esperado 2620765.4506587693 \
        --certificado .../certificado/certificado.json \
        --corte_cargas_lhd .../corte_cargas_lhd.json --out output/.../cg_certificado
"""
import argparse
import heapq
import json
import os
import sys
import time
from argparse import Namespace

T_INICIO = time.time()

import numpy as np                                                       # noqa: E402

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from pyomo.environ import value                                          # noqa: E402

import gcg_certificado                                                   # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.cg_propia import (EstructuraDW,      # noqa: E402
                                                      GeneracionColumnas, _env,
                                                      gemelos_por_anio)
from src.optimization.decomposition.driver import NestedBendersSolver   # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

INF = float("inf")
FAMILIAS = ("N_bays", "N_chargers", "n_ssee_k", "N_batteries")
ETIQUETA = {"N_bays": "bays", "N_chargers": "chargers", "n_ssee_k": "n_ssee_k",
            "N_batteries": "batteries"}


def posiciones_inversion(E):
    """{familia: [posicion en el maestro]} en el orden del MPS, el mismo con
    que BendersDias.inversion arma el texto del certificado."""
    pos = {f: [] for f in FAMILIAS}
    for kk, j in enumerate(E.mvars):
        fam = E.nombre[j].split("(")[0]
        if fam in pos:
            pos[fam].append(kk)
    return pos


def texto_inversion(pos, xm):
    return " | ".join(f"{ETIQUETA[f]} {','.join(str(int(round(xm[k]))) for k in pos[f])}"
                      for f in FAMILIAS if pos[f])


def cotas_de(pos, texto):
    """Texto del certificado -> cotas del nodo {posicion: (v, v)}."""
    vals = {}
    for parte in texto.split("|"):
        nombre, nums = parte.strip().split(" ", 1)
        vals[nombre] = [int(v) for v in nums.split(",")]
    cotas = {}
    for f in FAMILIAS:
        v = vals[ETIQUETA[f]]
        if len(v) != len(pos[f]):
            raise ValueError(f"{f}: {len(v)} valores en el certificado y {len(pos[f])} "
                             f"variables en el maestro")
        for k, x in zip(pos[f], v):
            cotas[k] = (float(x), float(x))
    return cotas


def factibilidad(cg, tl):
    """Dias sin ninguna columna compatible con el nodo: MILP de factibilidad
    (costo original, para en la primera solucion). Con la inversion fija la
    region de un dia no depende de los duales, y las copias de enlace (b_bar,
    D, R, ...) quedan libres en sus cotas: si un dia es INFACTIBLE, ninguna
    solucion tiene esta inversion y la candidata se descarta (LB = +inf). Si
    hay solucion entra como columna y saca la artificial del dia.
    Devuelve (bloques infactibles, bloques sin respuesta, columnas nuevas)."""
    import gurobipy as gp
    from gurobipy import GRB
    E, M = cg.E, cg.maestro
    faltan = [b for b in range(E.nb)
              if not any(cg._compatible(b, l[1]) for l in M.lams[b])]
    if not faltan:
        return [], [], 0

    def tarea(b):
        p = cg.pricing[b]
        m = p.m
        m.setAttr("Obj", p.zl, p.objetivo(None).tolist())
        m.Params.TimeLimit = max(1.0, tl)
        m.Params.MIPGap, m.Params.SolutionLimit = 1.0, 1
        m.Params.MIPFocus, m.Params.DualReductions = 1, 0
        try:
            m.optimize()
            st = m.Status
            z = None
            if m.SolCount > 0:
                z = np.array(m.getAttr("X", p.zl))
                z[p.B["ent"]] = np.round(z[p.B["ent"]])
            return b, st, z
        except gp.GurobiError as e:
            return b, f"error {e}", None
        finally:
            m.Params.SolutionLimit = 2000000000
            m.Params.MIPFocus, m.Params.DualReductions = 0, 1

    t0 = time.time()
    res = list(cg.pool.map(tarea, faltan))
    inf, nada, nuevas = [], [], 0
    for b, st, z in res:
        if st == GRB.INFEASIBLE:
            inf.append(E.claves[b])
        elif z is None:
            nada.append((E.claves[b], st))
        elif cg._compatible(b, z):
            nuevas += int(M.agregar(b, z))
            cg.pricing[b].start = z
    print(f"[CC]    factibilidad de {len(faltan)} dias sin columnas en {time.time() - t0:.0f}s: "
          f"{len(inf)} infactibles {inf}, {nuevas} columnas nuevas, "
          f"{len(nada)} sin respuesta {nada}", flush=True)
    return inf, nada, nuevas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del incumbente")
    ap.add_argument("--ub_esperado", type=float, required=True)
    ap.add_argument("--certificado", required=True, help="certificado.json del certificado")
    ap.add_argument("--corte_cargas_lhd", default=None,
                    help="JSON de corte_cargas_op.py (valido para toda inversion)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tiempo_max", type=float, default=None,
                    help="segundos desde el arranque del proceso")
    ap.add_argument("--candidatas", type=int, default=None,
                    help="solo las N vivas de menor LB (default: todas)")
    ap.add_argument("--rondas_visita", type=int, default=8,
                    help="rondas de CG por visita a una candidata")
    ap.add_argument("--tl_relajacion", type=float, default=1800.0,
                    help="tope del LP de cada candidata (centro dual)")
    ap.add_argument("--tl_factibilidad", type=float, default=600.0,
                    help="tope del MILP de factibilidad de cada dia sin columnas, en la "
                         "primera visita a una candidata (0 = no)")
    # Generacion de columnas (ver cg_propia.py)
    ap.add_argument("--estab", default="box+barrier+wentges")
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--box_rel", type=float, default=1.0)
    ap.add_argument("--box_crece", type=float, default=4.0)
    ap.add_argument("--M_art", type=float, default=1e7)
    ap.add_argument("--tl_heur", type=float, default=30.0)
    ap.add_argument("--gap_heur", type=float, default=0.02)
    ap.add_argument("--pool", type=int, default=5)
    ap.add_argument("--tl_exacto", type=float, default=300.0)
    ap.add_argument("--gap_exacto", type=float, default=None)
    ap.add_argument("--exacto_cada", type=int, default=3)
    ap.add_argument("--tl_exacto_corto", type=float, default=120.0)
    ap.add_argument("--rc_min", type=float, default=1e-5)
    ap.add_argument("--tol_gap", type=float, default=1e-4)
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--hilos", type=int, default=None)
    ap.add_argument("--parcial", action="store_true",
                    help="pricing parcial (default: todos los bloques en cada ronda, "
                         "asi hay cota lagrangiana en cada ronda)")
    ap.add_argument("--modo", default="year_day",
                    choices=["year_day", "year_vehicle_station_day"],
                    help="bloques por (año, dia) o por (vehiculo, (año, dia)) + "
                         "(estacion, (año, dia))")
    ap.add_argument("--sin_compartir", action="store_true")
    ap.add_argument("--sin_gemelos", action="store_true")
    ap.add_argument("--lag_entera", type=float, default=60.0, metavar="SEG",
                    help="cota con el maestro como MIP (R y el resto de las enteras que "
                         "quedan libres); 0 = no")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    deadline = T_INICIO + a.tiempo_max if a.tiempo_max else INF

    with open(a.certificado, encoding="utf-8") as fh:
        cert = json.load(fh)
    umbral = float(cert["umbral"])
    vivas = sorted((c for c in cert["candidatas"] if c["estado"] == "VIVA"),
                   key=lambda c: c["lb_con_corte"])
    if a.candidatas:
        resto = vivas[a.candidatas:]
        vivas = vivas[:a.candidatas]
    else:
        resto = []
    print(f"[CC] certificado: UB {cert['UB']:,.2f}, umbral {umbral:,.2f}, "
          f"{len(vivas)} vivas a trabajar (LB {vivas[0]['lb_con_corte']:,.2f} .. "
          f"{vivas[-1]['lb_con_corte']:,.2f})"
          + (f", {len(resto)} vivas fuera (su LB sigue siendo la del certificado)" if resto else ""),
          flush=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    t0 = time.time()
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    if a.corte_cargas_lhd:
        # No usa self: es solo el modelo y la ruta.
        NestedBendersSolver._agregar_corte_cargas_lhd(None, om.model, a.corte_cargas_lhd)
    costo0, _, fijadas = gcg_certificado.reconstruir(om.model)
    print(f"[CC] monolitico armado; incumbente reconstruido: {costo0:,.2f} "
          f"(esperado {a.ub_esperado:,.2f}) en {time.time() - t0:.0f}s", flush=True)
    if abs(costo0 - a.ub_esperado) > 1e-6 * abs(a.ub_esperado):
        print("[CC] ABORTA: el incumbente no se reconstruye con el costo esperado "
              "(¿datos con otro tope de generacion/BESS?)", flush=True)
        return 1
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode=a.modo, split=True)
    print(f"[CC] descomposicion {a.modo}: {info['n_blocks']} bloques, maestro "
          f"{info['n_master']:,} de {info['n_rows']:,} filas ({info['master_frac']:.1%})",
          flush=True)

    t0 = time.time()
    E = EstructuraDW(info["mps"], info["decomp"], _env(os.cpu_count() or 8))
    print(f"[CC] estructura en {time.time() - t0:.0f}s: {E.resumen()}", flush=True)
    cg = GeneracionColumnas(E, jobs=a.jobs, hilos=a.hilos, out=a.out, M_art=a.M_art)
    cg.t_inicio = T_INICIO
    cg.tl_lag_entera = a.lag_entera if a.lag_entera and a.lag_entera > 0 else None
    if not a.sin_gemelos:
        cg.gemelos = gemelos_por_anio(E, om.model, por_var,
                                      vehiculo=a.modo == "year_vehicle_station_day")
        print(f"[CC] gemelos: {sum(len(v) for v in cg.gemelos.values())} pares de bloques",
              flush=True)

    vec = np.zeros(E.nv)
    for j, n in enumerate(E.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec[j] = (1.0 if n == "ONE_VAR_CONSTANT" else 0.0) if v is None else v
    cg.inicializar(vec)
    cg.UB, cg.mejor_vec, cg.origen_ub = costo0, vec, "solucion inicial"
    UB = costo0

    pos = posiciones_inversion(E)
    txt_inc = texto_inversion(pos, vec[E.mvars])
    inc = [c for c in cert["candidatas"] if c.get("es_incumbente")]
    if inc and inc[0]["inversion"] != txt_inc:
        print(f"[CC] ABORTA: la inversion del incumbente no coincide con la del certificado:\n"
              f"     MPS          {txt_inc}\n     certificado  {inc[0]['inversion']}", flush=True)
        return 1
    print(f"[CC] inversion del incumbente: {txt_inc} (coincide con el certificado)", flush=True)
    n = cg.acotar_por_costo()
    print(f"[CC] cotas por costo: {n} variables del maestro acotadas con la UB", flush=True)

    kw = dict(estab=a.estab, alpha0=a.alpha, compartir=not a.sin_compartir,
              heur=(a.tl_heur, a.gap_heur, a.pool), exacto=(a.tl_exacto, a.gap_exacto),
              tol_gap=a.tol_gap, ub_cada=0, exacto_cada=a.exacto_cada,
              tl_exacto_corto=a.tl_exacto_corto, rc_min=a.rc_min, parcial=a.parcial,
              box_rel=a.box_rel, box_crece=a.box_crece)

    # Estado de cada candidata
    est = {}
    for c in vivas:
        est[c["i"]] = {"i": c["i"], "inversion": c["inversion"], "lb_cert": c["lb_con_corte"],
                       "lb": c["lb_con_corte"], "lb_lp_nodo": None, "estado": "VIVA",
                       "visitas": 0, "rondas": 0, "t": 0.0, "convergio": False,
                       "cotas": cotas_de(pos, c["inversion"]), "centro": None}
    lb_fuera = min([c["lb_con_corte"] for c in resto] + [umbral])

    def lb_global():
        vivos = [s["lb"] for s in est.values() if s["estado"] == "VIVA"]
        return min(vivos + [lb_fuera, UB])

    def guardar():
        filas = [{k: v for k, v in s.items() if k not in ("cotas", "centro")}
                 for s in sorted(est.values(), key=lambda s: s["lb"])]
        with open(os.path.join(a.out, "candidatas_dw.json"), "w", encoding="utf-8") as fh:
            json.dump({"UB": UB, "umbral": umbral, "LB_global": lb_global(),
                       "LB_certificado": vivas[0]["lb_con_corte"], "lb_fuera": lb_fuera,
                       "candidatas": filas}, fh, indent=1, default=float)
        cg._guardar()

    def punto(fase):
        cg.trayectoria.append({"t": round(time.time() - T_INICIO, 1), "UB": UB,
                               "LB": lb_global(), "fase": fase})

    punto("certificado")
    guardar()
    cola = [(s["lb"], i) for i, s in est.items()]
    heapq.heapify(cola)
    while cola and time.time() < deadline:
        lb_c, i = heapq.heappop(cola)
        s = est[i]
        if s["estado"] != "VIVA" or lb_c != s["lb"]:
            continue
        # Basta con pasar a la siguiente: desde ahi la LB global es la de ella.
        sig = min(cola[0][0], lb_fuera) if cola else lb_fuera
        objetivo = min(UB, sig)
        t_v = time.time()
        print(f"\n[CC] === candidata {i} (visita {s['visitas'] + 1}) LB {s['lb']:,.2f} "
              f"-> objetivo {objetivo:,.2f}; LB global {lb_global():,.2f}  [{s['inversion']}]",
              flush=True)
        if not cg._aplicar_nodo(s["cotas"]):
            s["estado"], s["lb"] = "INFACTIBLE (propagacion)", INF
            print(f"[CC] candidata {i}: cotas incompatibles con las filas del maestro", flush=True)
            punto(f"cand {i}")
            guardar()
            continue
        if a.tl_factibilidad > 0 and s["visitas"] == 0:
            inf, _nada, _n = factibilidad(cg, a.tl_factibilidad)
            if inf:
                s["estado"], s["lb"] = "INFACTIBLE (dias sin operacion entera)", INF
                s["dias_infactibles"] = inf
                s["visitas"] += 1
                s["t"] += time.time() - t_v
                print(f"[CC] candidata {i}: INFACTIBLE ({len(inf)} dias sin operacion entera "
                      f"factible) -> descartada; LB global {lb_global():,.2f} "
                      f"(gap {(UB - lb_global()) / UB:.3%})", flush=True)
                punto(f"cand {i}")
                guardar()
                continue
        if s["centro"] is None:
            lb_r, pi = cg.relajacion_nodo(a.tl_relajacion)
            s["lb_lp_nodo"] = lb_r
            if lb_r is not None and lb_r > s["lb"]:
                s["lb"] = lb_r
            s["centro"] = pi
        if s["lb"] >= UB:
            s["estado"] = "DESCARTADA"
        else:
            r = cg.correr(**kw, max_rondas=a.rondas_visita, centro=s["centro"], LB0=s["lb"],
                          deadline=deadline, fase=f"cand {i}", objetivo=objetivo,
                          ub_final=False)
            s["lb"] = max(s["lb"], r["LB"])
            s["centro"] = r["pi_c"] if r["pi_c"] is not None else s["centro"]
            s["rondas"] += r["rondas"]
            s["convergio"] = bool(r["convergio"])
            if s["lb"] >= UB:
                s["estado"] = "DESCARTADA"
        s["visitas"] += 1
        s["t"] += time.time() - t_v
        print(f"[CC] candidata {i}: LB {s['lb_cert']:,.2f} -> {s['lb']:,.2f} "
              f"(+{s['lb'] - s['lb_cert']:,.2f}) {s['estado']}"
              f"{' (DW convergida)' if s['convergio'] else ''} en {time.time() - t_v:.0f}s; "
              f"LB global {lb_global():,.2f} (gap {(UB - lb_global()) / UB:.3%})", flush=True)
        punto(f"cand {i}")
        guardar()
        if s["estado"] == "VIVA":
            if s["convergio"] and (not cola or s["lb"] <= cola[0][0]):
                print(f"[CC] la candidata de menor LB ({i}) tiene la DW convergida: sin "
                      f"ramificar no sube mas", flush=True)
                break
            heapq.heappush(cola, (s["lb"], i))

    cg._aplicar_nodo({})
    LB = lb_global()
    punto("fin")
    guardar()
    res = {"UB": UB, "LB_certificado": vivas[0]["lb_con_corte"], "LB_global": LB,
           "gap_certificado": (UB - vivas[0]["lb_con_corte"]) / UB, "gap": (UB - LB) / UB,
           "descartadas": sum(1 for s in est.values() if s["estado"] != "VIVA"),
           "trabajadas": sum(1 for s in est.values() if s["visitas"]),
           "columnas": cg.maestro.n_cols, "tiempo_s": time.time() - T_INICIO, "args": vars(a)}
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2, default=float)
    print(f"\n[CC] LB {vivas[0]['lb_con_corte']:,.2f} (certificado) -> {LB:,.2f}; "
          f"gap {res['gap_certificado']:.3%} -> {res['gap']:.3%}; "
          f"{res['descartadas']} descartadas de {res['trabajadas']} trabajadas; "
          f"{time.time() - T_INICIO:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
