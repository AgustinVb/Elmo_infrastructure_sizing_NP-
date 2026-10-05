"""Benders de un nivel por (año, dia) con level-set (Pecci y Jenkins 2025) sobre
el monolitico de Mina_modelo. Ver src/optimization/decomposition/benders_dias.py.

Arranca desde una solucion conocida (la de Benders anidado): da la UB inicial,
el primer punto de cortes y el start de los dias MILP. Se valida al cargarla:
el pulido tiene que reproducir --ub_esperado.

    python -u benders_dias.py --n_years 4 \
        --solucion output/.../estab_4anios_B_box --ub_esperado 1869867.50 \
        --cota_operacional 1797006.58 --out output/.../benders_dias_4anios

Etapas (--etapas): 1 relajacion continua, 2 maestro entero con cortes LP (tiene
que converger a la cota operacional), 3 cortes fortalecidos de los dias MILP.
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
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--n_years", type=int, required=True)
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del incumbente")
    ap.add_argument("--ub_esperado", type=float, required=True)
    ap.add_argument("--cota_operacional", type=float, default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--etapas", default="123")
    ap.add_argument("--alpha", type=float, default=0.5, help="level-set (paper: 0,5)")
    ap.add_argument("--M", type=float, default=1e6, help="penalidad de los enlaces elasticos")
    ap.add_argument("--jobs", type=int, default=8, help="dias resueltos a la vez")
    ap.add_argument("--max_it", default="30,30,20", help="iteraciones maximas por etapa")
    ap.add_argument("--tol1", type=float, default=1e-3)
    ap.add_argument("--tol2", type=float, default=1e-4)
    ap.add_argument("--ub_timelimit", type=float, default=60)
    ap.add_argument("--ub_gap", type=float, default=0.01)
    ap.add_argument("--lagr_timelimit", type=float, default=60)
    ap.add_argument("--lagr_gap", type=float, default=1e-4)
    ap.add_argument("--corte3", choices=["local", "fortalecido"], default="local",
                    help="corte de la etapa 3. local: lagrangiano con la inversion entera "
                         "fija (exacto en esa inversion, desactivado en las demas); "
                         "fortalecido: Zou et al. con todas las copias libres (debil aca)")
    ap.add_argument("--sin_ub_etapa2", action="store_true",
                    help="no evaluar la UB (dias MILP) en la etapa 2")
    ap.add_argument("--tiempo_max", type=float, default=None, help="segundos")
    ap.add_argument("--capacidad", default="station_1=1",
                    help="cotas del presolve de capacidad (n_ssee_k >= n), k=n por coma")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    t0 = time.time()
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    # split=False: esto es Benders (filas), no generacion de columnas; la
    # Particion espera las capacidades como variables del maestro.
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day",
                              split=False)
    print(f"[BD] monolitico de {a.n_years} años armado y exportado en {time.time() - t0:.0f}s: "
          f"{info['n_blocks']} bloques, maestro {info['n_master']} filas", flush=True)

    capacidad = {k: int(n) for k, n in (p.split("=") for p in a.capacidad.split(",") if p)}
    bd = BendersDias(info, om.model, por_var, M=a.M, jobs=a.jobs, alpha=a.alpha,
                     cotas_capacidad=capacidad, out=a.out)

    # Incumbente: valores de Pyomo -> vector de columnas del MPS (enteras sin
    # valor = 0, como en gcg_certificado.reconstruir).
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    costo0, completo0, e0 = bd.cargar_incumbente(vec0)
    desvio = abs(costo0 - a.ub_esperado) / abs(a.ub_esperado)
    print(f"[BD] incumbente pulido: {costo0:,.2f} contra esperado {a.ub_esperado:,.2f} "
          f"(desvio {desvio:.2e})", flush=True)
    if desvio > 1e-6:
        print("[BD] ABORTA: el incumbente no se reconstruye bien")
        return 1

    max_it = tuple(int(x) for x in a.max_it.split(","))
    L = bd.correr(completo0, e0, max_it=max_it, tol=(a.tol1, a.tol2),
                  ub_timelimit=a.ub_timelimit, ub_gap=a.ub_gap,
                  lagr_timelimit=a.lagr_timelimit, lagr_gap=a.lagr_gap,
                  tiempo_max=a.tiempo_max, etapas=tuple(int(c) for c in a.etapas),
                  ub_en_etapa2=not a.sin_ub_etapa2, corte3=a.corte3)

    mejoro = bd.UB < costo0 * (1 - 1e-7)
    if mejoro:
        from src.io.printer import Printer
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            if vd is not None and not vd.fixed:
                x = bd.mejor_vec[j]
                vd.set_value(round(x) if not vd.is_continuous() else x, skip_validation=True)
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        print(f"[BD] UB mejorada {costo0:,.2f} -> {bd.UB:,.2f}; JSON en {destino}", flush=True)

    lb = max([x for x in (L.get(3), L.get(2), a.cota_operacional) if x is not None])
    resumen = {"n_years": a.n_years, "ub_inicial": costo0, "ub_final": bd.UB,
               "cota_etapa1_relajacion": L.get(1), "cota_etapa2_operacional": L.get(2),
               "cota_etapa3_fortalecida": L.get(3), "cota_operacional_dada": a.cota_operacional,
               "lb_final": lb, "gap_final": (bd.UB - lb) / bd.UB,
               "tiempo_total_s": time.time() - bd.t0, "args": vars(a)}
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=2)
    print("\n[BD] ================= RESUMEN =================")
    for k, v in (("cota etapa 1 (relajacion LP)", L.get(1)),
                 ("cota etapa 2 (op. relajada)", L.get(2)),
                 ("cota etapa 3 (fortalecida)", L.get(3)),
                 ("cota operacional dada", a.cota_operacional)):
        if v is not None:
            print(f"[BD] {k:30s} {v:>16,.2f}")
    print(f"[BD] UB inicial / final            {costo0:>16,.2f} / {bd.UB:,.2f}")
    print(f"[BD] FINAL: UB {bd.UB:,.2f}  LB {lb:,.2f}  gap {(bd.UB - lb) / bd.UB:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
