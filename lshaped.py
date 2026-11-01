"""L-shaped entero multicorte en branch-and-cut (portado de
battery_swapping_multiaño, 2026-10, adaptado a carga on-board), sobre el
Benders por (año, dia) de BendersDias. Ver src/optimization/decomposition/lshaped.py.

  Fase 1: etapas 1 y 2 de BendersDias (maestro continuo y luego entero, cortes LP
          de los dias) -> cota operacional y cortes.
  Fase 2: branch-and-cut del maestro entero con callback: cortes LP, y si no
          separan, cortes enteros locales con el esquema de Riley et al. (2025).
          UB afuera de Gurobi (dias MILP + pulido LP).
  Al final, mejorar UB con la inversion de la mejor solucion fija (--mejora).

Mismo regimen que la corrida que dio el incumbente (--free_charging,
--free_maintenance, --pause_scheme, --n_years, --consumption_model):

    python -u lshaped.py --data_folder data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo/2C/P_red \\
        --free_charging --pause_scheme dch --n_years 11 --consumption_model wp2 \\
        --solucion <JSON del incumbente> --ub_esperado <costo> --out <carpeta> --horas 10
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from pyomo.environ import value                                          # noqa: E402

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import particion                    # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.decomposition.lshaped import LShaped               # noqa: E402
from src.optimization.decomposition.respaldo import cargar_respaldo      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--model", default="elmo_data.xlsx")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, choices=[2, 4], default=4)
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp1")
    ap.add_argument("--wp2_consumption_json", default=None)
    ap.add_argument("--free_charging", action="store_true",
                    help="mismo regimen que la corrida que dio el incumbente")
    ap.add_argument("--free_maintenance", action="store_true",
                    help="mismo regimen que la corrida que dio el incumbente")
    ap.add_argument("--pause_scheme", choices=["det", "dch"], default="det",
                    help="mismo regimen que la corrida que dio el incumbente")
    ap.add_argument("--solucion", default=None,
                    help="carpeta con los JSON de un incumbente (sin el ni --respaldo, parte de cero)")
    ap.add_argument("--respaldo", default=None, help="incumbente_respaldo.pkl (alternativa a --solucion)")
    ap.add_argument("--ub_esperado", type=float, default=None,
                    help="costo del incumbente; si se da, el pulido tiene que reproducirlo")
    ap.add_argument("--solo_mejorar", action="store_true",
                    help="solo el paso final de mejorar UB sobre el incumbente (sin Fase 1 ni 2)")
    ap.add_argument("--mejor_ub", type=float, default=None, help="mejor UB conocida, solo para comparar")
    ap.add_argument("--mejor_lb", type=float, default=None, help="mejor LB conocida, solo para comparar")
    ap.add_argument("--out", required=True)
    ap.add_argument("--horas", type=float, default=10.0, help="tiempo total")
    ap.add_argument("--jobs", type=int, default=8, help="dias resueltos a la vez")
    ap.add_argument("--M", type=float, default=1e6, help="penalidad de los enlaces elasticos")
    ap.add_argument("--alpha", type=float, default=0.5, help="level-set de la Fase 1")
    ap.add_argument("--max_it", default="30,60", help="iteraciones de las etapas 1 y 2 (Fase 1)")
    ap.add_argument("--alfas", default="0.1,0.01,0", help="gaps de los MILP del dia (Riley)")
    ap.add_argument("--t_inicial", type=float, default=120.0, help="tiempo inicial por MILP del dia")
    ap.add_argument("--t_max_dia", type=float, default=1920.0, help="tope del tiempo por MILP del dia")
    ap.add_argument("--ub_timelimit", type=float, default=120.0)
    ap.add_argument("--ub_gap", type=float, default=0.01)
    ap.add_argument("--mejora", default="600,900",
                    help="tiempo por dia MILP de cada pasada final de mejorar UB (vacio: no se hace); "
                         "la segunda y siguientes solo si la anterior mejoro y queda tiempo de --horas")
    ap.add_argument("--gap", type=float, default=1e-4, help="parada (UB - LB) / UB")
    ap.add_argument("--hilos_maestro", type=int, default=8)
    ap.add_argument("--cpu", type=int, default=None, help="nucleos para los dias (por defecto todos)")
    ap.add_argument("--capacidad", default="station_1=1",
                    help="cotas del presolve de capacidad (n_ssee_k >= n), k=n por coma")
    a = ap.parse_args()
    if a.solucion and a.respaldo:
        ap.error("--solucion y --respaldo son excluyentes")
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model=a.model, series="time_series.xlsx",
                   consumption_model=a.consumption_model,
                   wp2_consumption_json=a.wp2_consumption_json, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=a.free_charging,
                  free_maintenance=a.free_maintenance, pause_scheme=a.pause_scheme)
    esperado = a.ub_esperado
    if a.respaldo:
        esperado = cargar_respaldo(om, ms, ts, a.respaldo)["best_cost"]
    info, por_var = particion.export(om.model, os.path.join(a.out, "_mps"), mode="year_day")
    capacidad = {k: int(n) for k, n in (p.split("=") for p in a.capacidad.split(",") if p)}
    bd = BendersDias(info, om.model, por_var, M=a.M, jobs=a.jobs, alpha=a.alpha,
                     cotas_capacidad=capacidad, out=a.out, cpu=a.cpu)
    P = bd.P
    costo0 = float("inf")
    con_incumbente = bool(a.solucion or a.respaldo)

    def escribir(vec):
        """Solucion en JSON (usable como --solucion)."""
        from src.io.printer import Printer
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            if vd is not None and not vd.fixed:
                vd.set_value(round(vec[j]) if not vd.is_continuous() else vec[j], skip_validation=True)
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        return destino
    if con_incumbente:
        vec0 = np.zeros(P.nv)
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            v = value(vd, exception=False) if vd is not None else None
            vec0[j] = 0.0 if v is None else v
        costo0, completo0, e0 = bd.cargar_incumbente(vec0)
        print(f"[LS] incumbente pulido {costo0:,.2f} (esperado "
              f"{'-' if esperado is None else f'{esperado:,.2f}'})", flush=True)
        if esperado is not None and abs(costo0 - esperado) > 1e-6 * abs(esperado):
            print("[LS] ABORTA: el incumbente no se reconstruye bien")
            return 1
    elif a.solo_mejorar:
        ap.error("--solo_mejorar necesita --solucion o --respaldo")

    ls = LShaped(bd, om, por_var, alfas=[float(x) for x in a.alfas.split(",")],
                 t_inicial=a.t_inicial, t_max_dia=a.t_max_dia, ub_timelimit=a.ub_timelimit,
                 ub_gap=a.ub_gap, out=a.out)
    ls.t0 = t0
    ls.tiempo_max = a.horas * 3600
    ls.ub_referencia = a.mejor_ub      # solo para detectar una LB invalida
    if a.solo_mejorar:
        ls.mejorar_ub([float(x) for x in a.mejora.split(",")])
        resumen = {"ub_inicial": costo0, "ub_final": bd.UB, "tiempo_total_s": time.time() - t0,
                   "args": vars(a)}
        if bd.UB < costo0 * (1 - 1e-7):
            resumen["solucion"] = escribir(bd.mejor_vec)
        with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
            json.dump(resumen, f, indent=2, default=str)
        print(f"[LS] FINAL mejorar UB: {costo0:,.2f} -> {bd.UB:,.2f} ({(costo0 - bd.UB) / costo0:.3%}, "
              f"{time.time() - t0:.0f}s)", flush=True)
        return 0

    # Fase 1. Sin incumbente arranca en el optimo del maestro relajado (con la
    # cota de theta de cada dia): es un punto factible del maestro.
    if not con_incumbente:
        bd._cota_theta()
        bd.maestro.resolver(entero=False)
        completo0, e0, _ = bd.maestro.punto()
        print("[LS] sin incumbente: la Fase 1 arranca en el maestro relajado; la UB la daran "
              "las soluciones del branch-and-cut", flush=True)
    it1, it2 = (int(x) for x in a.max_it.split(","))
    L = bd.correr(completo0, e0, max_it=(it1, it2, 0), tol=(1e-3, 1e-4), etapas=(1, 2),
                  ub_en_etapa2=False)
    lb1 = L.get(2)
    print(f"[LS] Fase 1 ({time.time() - t0:.0f}s): LB {lb1:,.2f}, {bd.maestro.n_cortes} cortes",
          flush=True)
    if bd.UB < 1e20:
        print(f"[LS] UB {bd.UB:,.2f}; gap con la LB de la Fase 1 {(bd.UB - lb1) / bd.UB:.3%}", flush=True)

    # Fase 2
    ls.preparar_maestro()
    lb2, status = ls.branch_and_cut(a.horas * 3600, gap_obj=a.gap, hilos=a.hilos_maestro)
    lb = max(x for x in (lb1, lb2) if x is not None)
    print(f"[LS] Fase 2 terminada (status {status}, {time.time() - t0:.0f}s)", flush=True)
    ub_bc = bd.UB
    if a.mejora:
        ls.mejorar_ub([float(x) for x in a.mejora.split(",")])
        print(f"[LS] mejorar UB ({time.time() - t0:.0f}s): {ub_bc:,.2f} -> {bd.UB:,.2f}", flush=True)

    if bd.mejor_vec is not None and bd.UB < costo0 * (1 - 1e-7):
        destino = escribir(bd.mejor_vec)
        print(f"[LS] UB mejorada {costo0:,.2f} -> {bd.UB:,.2f}; JSON en {destino}", flush=True)

    ub_fin = bd.UB if bd.UB < 1e20 else float("inf")
    resumen = {"ub_inicial": costo0, "ub_bc": ub_bc, "ub_final": bd.UB, "lb_fase1": lb1, "lb_fase2": lb2,
               "lb_final": lb, "gap_final": (ub_fin - lb) / ub_fin if ub_fin < 1e20 else None,
               "status_maestro": status, "cortes_lp_bc": ls.n_lp, "cortes_enteros": ls.n_ent,
               "cortes_piso": ls.n_piso, "t_piso": ls.t_piso,
               "cortes_descartados_big_M": ls.n_sin_cota, "callbacks": ls.n_cb,
               "aceptaciones_silenciosas": ls.n_silenciosas,
               "t_lp": ls.t_lp, "t_milp": ls.t_milp, "tiempo_total_s": time.time() - t0, "args": vars(a)}
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=2, default=str)

    def num(v):
        return f"{v:,.2f}" if abs(v) < 1e20 else "-"
    print(f"[LS] FINAL: UB {num(bd.UB)}  LB {lb:,.2f}  gap {(ub_fin - lb) / ub_fin if ub_fin < 1e20 else 1:.3%}  "
          f"(Fase 1 {lb1:,.2f}; cortes LP {ls.n_lp}, enteros {ls.n_ent})", flush=True)
    if a.mejor_ub and a.mejor_lb:
        print(f"[LS] contra lo mejor conocido: UB {a.mejor_ub:,.2f}  LB {a.mejor_lb:,.2f}  gap "
              f"{(a.mejor_ub - a.mejor_lb) / a.mejor_ub:.3%}  |  UB "
              f"{(f'{bd.UB - a.mejor_ub:+,.2f}' if bd.UB < 1e20 else 'sin UB')}  "
              f"LB {lb - a.mejor_lb:+,.2f}  gap combinado "
              f"{(min(bd.UB, a.mejor_ub) - max(lb, a.mejor_lb)) / min(bd.UB, a.mejor_ub):.3%}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
