"""Generacion de columnas propia sobre el monolitico de Mina_modelo (ver
src/optimization/decomposition/cg_propia.py): Dantzig-Wolfe year_day con pricing
en Gurobi en paralelo, estabilizacion, UB heuristicas y branch-and-price sobre la
inversion.

Arranque (--inicio):
  relajacion  (default) relajacion operacional o LP del monolitico (--relajacion):
              cota inferior valida, duales iniciales para el centro de
              estabilizacion y una primera UB con la heuristica del maestro,
              cuyas columnas sacan al maestro de la fase de artificiales.
  frio        solo artificiales con costo --M_art (la version anterior).
Con --solucion se cargan ademas las columnas de una solucion conocida (los JSON
de un reporte), que se reconstruye y pule como en gcg_certificado.py.

El tiempo (--tiempo_max) se cuenta desde el arranque del proceso, como
--max_hours de Nested Benders. Salidas en --out: resumen.json, historia_cg.json
(una fila por ronda) y trayectoria.json (UB y LB global contra el tiempo).

    python -u cg_propia.py --data_folder data/Tesis_final/Mina_modelo/RED --n_years 2 \
        --out output/.../cg_2anios --tiempo_max 3600

Version anterior (para comparar): --inicio frio --estab barrier+wentges
--ub_modo columnas --estanc_ventana 0 --rc_min 1e-6 --sin_pool
--sin_parcial --compartir_cada 1 --tl_compartir 30 --sin_gemelos --sin_bp
--sin_acotar_por_costo --jobs 4
"""
import argparse
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
from src.optimization.opt_model import OptModel                          # noqa: E402

INF = float("inf")


def escribir_reporte(om, por_var, nombres, vec, ts, ms, destino):
    """Carga un vector del MPS en el modelo de Pyomo y escribe los JSON de las
    variables con el Printer de setup.py (que deshace la normalizacion de
    unidades): el formato que leen --incumbente_inicial y --solucion."""
    from src.io.printer import Printer
    for j, n in enumerate(nombres):
        vd = por_var.get(n)
        if vd is not None and not vd.fixed:
            vd.set_value(float(vec[j]), skip_validation=True)
    Printer(om, destino, ts, ms).write_variables_jsons()
    print(f"[CG] solucion escrita como reporte JSON en {destino}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED")
    ap.add_argument("--n_years", type=int, required=True)
    ap.add_argument("--days_per_year", type=int, default=4)
    ap.add_argument("--solucion", default=None,
                    help="carpeta con los JSON de una solucion inicial")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cota_operacional", type=float, default=None,
                    help="para comparar; con --parar_en_operacional, objetivo de la LB")
    ap.add_argument("--tiempo_max", type=float, default=None,
                    help="segundos desde el arranque del proceso")
    # Mejora 2: arranque
    ap.add_argument("--inicio", choices=["relajacion", "frio"], default="relajacion")
    ap.add_argument("--relajacion", choices=["operacional", "lp"], default="operacional")
    ap.add_argument("--tl_relajacion", type=float, default=600.0)
    ap.add_argument("--gap_relajacion", type=float, default=1e-3)
    # Estabilizacion (mejora 5: box)
    ap.add_argument("--estab", default="box+wentges",
                    help="combinacion con '+' de wentges, barrier, box (o ninguna)")
    ap.add_argument("--alpha", type=float, default=0.5, help="alpha inicial de Wentges")
    ap.add_argument("--box_rel", type=float, default=1.0,
                    help="semiancho de la caja de duales, relativo a |pi_centro|")
    ap.add_argument("--box_crece", type=float, default=4.0)
    ap.add_argument("--M_art", type=float, default=1e7,
                    help="costo de las artificiales del maestro (arranque en frio)")
    # Mejora 1: UB
    ap.add_argument("--ub_modo", choices=["maestro", "columnas", "ip", "ambos"], default="ambos",
                    help="ambos = las tres: del maestro, por columnas e IP con b_bar comun")
    ap.add_argument("--ub_cada", type=int, default=10,
                    help="UB heuristica cada tantas rondas (0 = solo al final)")
    ap.add_argument("--ub_timelimit", type=float, default=300.0, help="tope del pulido")
    ap.add_argument("--tl_dia", type=float, default=60.0, help="tope de cada dia (heuristica)")
    ap.add_argument("--gap_dia", type=float, default=0.01)
    ap.add_argument("--refinar", type=int, default=1, help="pasadas de refinamiento de b_bar")
    ap.add_argument("--variantes_inicio", default="fases,directa",
                    help="variantes de la heuristica del maestro al arranque (coherente, "
                         "directa, redondeo, fases), separadas por coma; queda la mejor. "
                         "ninguna: no correrla (p.ej. con --solucion de Nested Benders)")
    ap.add_argument("--variantes", default="redondeo",
                    help="variantes de la heuristica del maestro en las rondas")
    ap.add_argument("--tl_heur_max", type=float, default=900.0,
                    help="presupuesto de una heuristica del maestro: pasado, no prueba mas respaldos")
    # Mejora 3: certificacion
    ap.add_argument("--exacto_cada", type=int, default=0,
                    help="intento exacto corto cada tantas rondas (0 = no)")
    ap.add_argument("--tl_exacto_corto", type=float, default=60.0,
                    help="tope del intento exacto por estancamiento o periodico")
    ap.add_argument("--estanc_ventana", type=int, default=5,
                    help="rondas para detectar estancamiento del maestro (0 = no)")
    ap.add_argument("--estanc_tol", type=float, default=1e-4)
    ap.add_argument("--rc_min", type=float, default=1e-5,
                    help="costo reducido minimo (relativo a |z|) para que una columna cuente")
    ap.add_argument("--tl_heur", type=float, default=30.0)
    ap.add_argument("--gap_heur", type=float, default=0.02)
    ap.add_argument("--pool", type=int, default=5, help="soluciones del pool por pricing")
    ap.add_argument("--tl_exacto", type=float, default=300.0)
    ap.add_argument("--gap_exacto", type=float, default=None,
                    help="gap relativo del pricing exacto (default: solo el absoluto)")
    ap.add_argument("--tol_gap", type=float, default=1e-4,
                    help="para cuando (RMP - LB)/RMP <= tol_gap")
    # Mejora 4: velocidad
    ap.add_argument("--jobs", type=int, default=None, help="bloques a la vez (default: auto)")
    ap.add_argument("--hilos", type=int, default=None, help="hilos por bloque (default: 4)")
    ap.add_argument("--sin_pool", action="store_true", help="sin pool de columnas")
    ap.add_argument("--sin_parcial", action="store_true", help="sin pricing parcial")
    ap.add_argument("--precio_completo_cada", type=int, default=5)
    ap.add_argument("--saltar_tras", type=int, default=2,
                    help="rondas sin columnas tras las que un bloque descansa")
    ap.add_argument("--sin_compartir", action="store_true", help="sin Column Sharing")
    ap.add_argument("--compartir_cada", type=int, default=3)
    ap.add_argument("--tl_compartir", type=float, default=10.0)
    ap.add_argument("--sin_gemelos", action="store_true",
                    help="sin transferencia de columnas entre años gemelos")
    ap.add_argument("--igualdad", action="store_true",
                    help="capacidades como enlace con igualdad (sin separar por bloque)")
    # Mejora 6: branch-and-price
    ap.add_argument("--sin_bp", action="store_true", help="solo la raiz")
    ap.add_argument("--sin_dive", action="store_true")
    ap.add_argument("--frac_raiz", type=float, default=0.5,
                    help="fraccion de --tiempo_max para la raiz si hay B&P")
    ap.add_argument("--rondas_nodo", type=int, default=30)
    ap.add_argument("--rondas_dive", type=int, default=15)
    ap.add_argument("--gap_bp", type=float, default=1e-4, help="gap (UB-LB)/UB para podar/parar")
    ap.add_argument("--sin_acotar_por_costo", action="store_true")
    ap.add_argument("--max_rondas", type=int, default=10_000)
    ap.add_argument("--parar_en_operacional", action="store_true",
                    help="para cuando la LB supera --cota_operacional")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    deadline = T_INICIO + a.tiempo_max if a.tiempo_max else None

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    t0 = time.time()
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    costo0 = None
    if a.solucion:
        costo0, _, fijadas = gcg_certificado.reconstruir(om.model)
        print(f"[CG] monolitico de {a.n_years} años armado; incumbente reconstruido: "
              f"{costo0:,.2f} ({fijadas:,} enteras fijas) en {time.time() - t0:.0f}s", flush=True)
    else:
        print(f"[CG] monolitico de {a.n_years} años armado en {time.time() - t0:.0f}s", flush=True)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day",
                              split=not a.igualdad)

    t0 = time.time()
    E = EstructuraDW(info["mps"], info["decomp"], _env(os.cpu_count() or 8))
    print(f"[CG] estructura en {time.time() - t0:.0f}s: {E.resumen()}", flush=True)
    cg = GeneracionColumnas(E, jobs=a.jobs, hilos=a.hilos, out=a.out, M_art=a.M_art)
    cg.t_inicio = T_INICIO
    cg.tl_heur_max = a.tl_heur_max
    if not a.sin_gemelos:
        t0 = time.time()
        cg.gemelos = gemelos_por_anio(E, om.model, por_var)
        n_g = sum(len(v) for v in cg.gemelos.values())
        print(f"[CG] gemelos: {n_g} pares de bloques (dia, año) con filas identicas "
              f"({time.time() - t0:.0f}s)", flush=True)

    if a.solucion:
        vec = np.zeros(E.nv)
        for j, n in enumerate(E.nombre):
            vd = por_var.get(n)
            v = value(vd, exception=False) if vd is not None else None
            vec[j] = (1.0 if n == "ONE_VAR_CONSTANT" else 0.0) if v is None else v
        cg.inicializar(vec)
        cg.UB, cg.mejor_vec, cg.origen_ub = costo0, vec, "solucion inicial"
        cg._punto("solucion inicial")

    kw_h = dict(tl_dia=a.tl_dia, gap_dia=a.gap_dia, tl_pulido=a.ub_timelimit, refinar=a.refinar)
    rel = None
    if a.inicio == "relajacion":
        rel = cg.relajacion_inicial(a.relajacion, a.tl_relajacion, a.gap_relajacion)
        if a.variantes_inicio != "ninguna":
            cg._heuristicas(cg.xm0, cg.pi0, "maestro", 0.0, "inicio", **kw_h,
                            variantes=tuple(a.variantes_inicio.split(",")))
    if not a.sin_acotar_por_costo and cg.UB < INF:
        n = cg.acotar_por_costo()
        print(f"[CG] cotas por costo: {n} variables del maestro acotadas con la UB", flush=True)
    else:
        cg.acotar_por_costo = lambda: 0

    kw = dict(estab=a.estab, alpha0=a.alpha, compartir=not a.sin_compartir,
              heur=(a.tl_heur, a.gap_heur, a.pool), exacto=(a.tl_exacto, a.gap_exacto),
              tl_compartir=a.tl_compartir, tol_gap=a.tol_gap, ub_cada=a.ub_cada,
              ub_timelimit=a.ub_timelimit, ub_modo=a.ub_modo, tl_dia=a.tl_dia,
              gap_dia=a.gap_dia, refinar=a.refinar, exacto_cada=a.exacto_cada,
              tl_exacto_corto=a.tl_exacto_corto,
              estanc_ventana=a.estanc_ventana, estanc_tol=a.estanc_tol, rc_min=a.rc_min,
              pool_columnas=not a.sin_pool, parcial=not a.sin_parcial,
              precio_completo_cada=a.precio_completo_cada, saltar_tras=a.saltar_tras,
              compartir_cada=a.compartir_cada, box_rel=a.box_rel, box_crece=a.box_crece,
              variantes=tuple(a.variantes.split(",")))
    objetivo = a.cota_operacional if a.parar_en_operacional else None
    dl_raiz = deadline
    if deadline is not None and not a.sin_bp:
        dl_raiz = time.time() + a.frac_raiz * max(0.0, deadline - time.time())
    r = cg.correr(**kw, max_rondas=a.max_rondas, objetivo=objetivo, deadline=dl_raiz,
                  LB0=cg.LB_ext, gap_ub=a.gap_bp, fase="raiz")
    LB = max(r["LB"], cg.LB_ext)
    bp = None
    hacer_bp = not a.sin_bp and (deadline is None or time.time() < deadline)
    if hacer_bp and cg.UB < INF and (cg.UB - LB) / abs(cg.UB) <= a.gap_bp:
        hacer_bp = False                              # la raiz ya cerro el gap
    if hacer_bp:
        r["LB"] = LB
        bp = cg.branch_and_price(r, kw, deadline if deadline else INF, gap_tol=a.gap_bp,
                                 rondas_nodo=a.rondas_nodo, rondas_dive=a.rondas_dive,
                                 dive=not a.sin_dive)
        LB = max(LB, bp["LB"])
    ub = cg.UB
    cg._punto("fin", LB)
    cg._guardar()

    gap = (ub - LB) / ub if ub < INF else None
    resumen = {"n_years": a.n_years, "ub_inicial": costo0, "ub_final": ub, "lb_final": LB,
               "gap": gap, "origen_ub": cg.origen_ub, "cota_raiz": r["LB"],
               "cota_relajacion": cg.LB_ext if cg.LB_ext > -INF else None,
               "rmp_final_raiz": r["z_rmp"], "raiz_convergio": r["convergio"],
               "rondas_raiz": r["rondas"], "columnas": cg.maestro.n_cols,
               "pool": sum(len(v) for v in cg.maestro.pool.values()),
               "bp": bp, "tiempo_s": time.time() - T_INICIO,
               "cota_operacional": a.cota_operacional, "args": vars(a)}
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=2, default=float)
    if cg.mejor_vec is not None:
        nz = np.flatnonzero(np.abs(cg.mejor_vec) > 1e-9)
        np.savez_compressed(os.path.join(a.out, "mejor_solucion.npz"),
                            nombres=np.array([E.nombre[j] for j in nz]), valores=cg.mejor_vec[nz])
        # Tambien como reporte JSON (mismo formato que setup.py): sirve de
        # --incumbente_inicial para Nested Benders y de --solucion para la CG.
        escribir_reporte(om, por_var, E.nombre, cg.mejor_vec, ts, ms,
                         os.path.join(a.out, "solucion"))
    print("\n[CG] ================= RESUMEN =================")
    print(f"[CG] UB                     {ub:>16,.2f}  ({cg.origen_ub})" if ub < INF
          else "[CG] UB  (ninguna)")
    print(f"[CG] LB                     {LB:>16,.2f}"
          + (f"   gap {gap:.4%}" if gap is not None else ""))
    if cg.LB_ext > -INF:
        print(f"[CG] cota de la relajacion  {cg.LB_ext:>16,.2f}  ({a.relajacion})")
    print(f"[CG] cota de la raiz (DW)   {r['LB']:>16,.2f}   maestro {r['z_rmp']:,.2f}"
          f"  ({'convergio' if r['convergio'] else 'sin converger'})")
    if bp:
        print(f"[CG] B&P: {bp['nodos']} nodos, {bp['abiertos']} abiertos, {bp['hojas']} hojas, "
              f"{bp.get('podados', 0)} podados")
    if a.cota_operacional is not None:
        c = a.cota_operacional
        print(f"[CG] cota operacional       {c:>16,.2f}   -> la LB "
              f"{'SUPERA' if LB > c else 'NO supera'} a la operacional ({LB - c:+,.2f})")
    print(f"[CG] {r['rondas']} rondas en la raiz, {cg.maestro.n_cols} columnas, "
          f"{time.time() - T_INICIO:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
