"""Certificado independiente con GCG: cota de la raiz de Dantzig-Wolfe sobre el
MONOLITICO, con la solucion de Nested Benders como incumbente.

    Benders da el UB. GCG, sobre el monolitico con bloques (año, dia), da en la
    raiz la cota del dual lagrangiano: cada dia conserva su casco entero. Con el
    incumbente dado solo interesa esa cota, asi que se pide solo la raiz
    (--nodes 1).

OJO con la comparacion. Esa cota es >= la LP, pero NO esta ordenada con la
cota operacional (la LB que usamos hoy): la operacional conserva la inversion
entera y relaja la operacion; la de DW convexifica la operacion de cada dia y
relaja la inversion (queda en el maestro). Cual es mas fuerte es empirico.

LA SOLUCION DE BENDERS se reconstruye desde los JSON que escribio el reporte.
Esos JSON omiten los ceros, asi que: se cargan (OptModel init_solution_folder),
las enteras que falten van a 0, y se PULE (enteras fijas, LP de las
continuas). Si el costo pulido no coincide con --ub_esperado, la
reconstruccion esta mal y se aborta: un incumbente equivocado podria podar el
arbol de GCG.

    python -u gcg_certificado.py --n_years 4 \
        --solucion output/.../solucion_benders_4anios --ub_esperado 1916141.92 \
        --out output/.../gcg_certificado_4anios --timelimit 7200
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

import pyomo.environ as pyo
from pyomo.core.expr.visitor import identify_variables
from pyomo.environ import SolverFactory, value
from pyomo.opt import TerminationCondition

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from setup import build_mine                                        # noqa: E402
from src.optimization.decomposition.gcg_block import solve_with_gcg  # noqa: E402
from src.optimization.opt_model import OptModel                      # noqa: E402


def reconstruir(model):
    """Enteras sin valor -> 0; luego LP con las enteras fijas. Devuelve el costo."""
    usadas = set()
    for c in model.component_data_objects(pyo.Constraint, active=True):
        usadas.update(id(v) for v in identify_variables(c.body))
    puestas_a_cero, fijadas = 0, []
    for v in model.component_data_objects(pyo.Var):
        if id(v) not in usadas or v.fixed:
            continue
        if not v.is_continuous():
            if v.value is None:
                v.set_value(0, skip_validation=True)
                puestas_a_cero += 1
            v.fix(int(round(v.value)))
            fijadas.append(v)
    opt = SolverFactory("gurobi", solver_io="python")
    opt.options["OutputFlag"] = 0
    opt.options["TimeLimit"] = 1800
    try:
        res = opt.solve(model, load_solutions=False)
        cond = res.solver.termination_condition
        if cond != TerminationCondition.optimal:
            raise RuntimeError(f"el LP de reconstruccion no llego al optimo ({cond}): "
                               f"la solucion cargada no es factible")
        model.solutions.load_from(res)
    finally:
        for v in fijadas:
            v.unfix()
    return value(model.obj), puestas_a_cero, len(fijadas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/")
    ap.add_argument("--n_years", type=int, required=True)
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del reporte")
    ap.add_argument("--ub_esperado", type=float, required=True,
                    help="costo de la solucion de Benders (reporte pulido)")
    ap.add_argument("--cota_operacional", type=float, default=None,
                    help="para comparar en el resumen")
    ap.add_argument("--mode", default="year_day",
                    choices=["year_day", "day", "vehicle_day"])
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--pricing", choices=["scip", "gurobi"], default="gurobi",
                    help="quien resuelve los subproblemas. gurobi (default): con SCIP la "
                         "raiz de 4 años no termino en 2 h (cota 1.112.173, < LP)")
    ap.add_argument("--timelimit", type=float, default=7200)
    ap.add_argument("--out", required=True)
    ap.add_argument("--solo_reconstruir", action="store_true",
                    help="verifica la reconstruccion de la solucion y termina, sin GCG")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    t0 = time.time()
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"),
                  init_solution_folder=a.solucion, mccormick_degradation=True,
                  free_charging=True, free_maintenance=True)
    print(f"[cert] monolitico de {a.n_years} años armado en {time.time() - t0:.0f}s",
          flush=True)

    costo, a_cero, fijadas = reconstruir(om.model)
    desvio = abs(costo - a.ub_esperado) / abs(a.ub_esperado)
    print(f"[cert] solucion reconstruida: {fijadas:,} enteras fijas ({a_cero:,} "
          f"ceros que omitian los JSON), costo {costo:,.2f} contra esperado "
          f"{a.ub_esperado:,.2f} (desvio relativo {desvio:.2e})", flush=True)
    if desvio > 1e-6:
        print("[cert] ABORTA: la reconstruccion no reproduce el costo de Benders. "
              "Un incumbente equivocado podria podar el arbol de GCG.")
        return 1
    if a.solo_reconstruir:
        print("[cert] --solo_reconstruir: reconstruccion OK, no se llama a GCG.")
        return 0

    r = solve_with_gcg(om.model, os.path.join(a.out, "gcg"), timelimit=a.timelimit,
                       gap=1e-4, mode=a.mode, use_incumbent=True, nodes=a.nodes,
                       pricing=a.pricing,
                       label=f"monolitico {a.n_years} años ({a.mode})")
    cota = r.get("dual_root") if r.get("dual_root") is not None else r.get("dual")
    resumen = {"n_years": a.n_years, "mode": a.mode, "ub_benders": costo,
               "gcg_primal": r.get("primal"), "gcg_dual": r.get("dual"),
               "gcg_dual_root": r.get("dual_root"), "gcg_status": r.get("status"),
               "gcg_error": r.get("error"), "n_blocks": r.get("n_blocks"),
               "n_master": r.get("n_master"), "master_frac": r.get("master_frac"),
               "tiempo_gcg_sec": r.get("time_total_sec"), "pricing": a.pricing,
               "pricing_stats": r.get("pricing_stats"),
               "cota_operacional": a.cota_operacional}
    with open(os.path.join(a.out, "certificado.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=2, default=str)

    print("\n[cert] ================= RESUMEN =================")
    print(f"[cert] UB (Benders)            {costo:>16,.2f}")
    if cota is not None and abs(cota) < 1e19:
        print(f"[cert] cota raiz DW (GCG)      {cota:>16,.2f}   gap {(costo - cota) / costo:.2%}")
    else:
        print(f"[cert] cota raiz DW (GCG)      {'(sin cota)':>16}   status {r.get('status')}")
    if a.cota_operacional is not None:
        c = a.cota_operacional
        print(f"[cert] cota operacional        {c:>16,.2f}   gap {(costo - c) / costo:.2%}")
        if cota is not None and abs(cota) < 1e19:
            print(f"[cert] -> la cota de GCG {'SUPERA' if cota > c else 'NO supera'} "
                  f"a la operacional ({cota - c:+,.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
