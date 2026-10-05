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

RESOLVER, NO SOLO CERTIFICAR (--nodes 0): branch-and-price completo sobre el
monolitico, arrancando desde la solucion dada. Si GCG encuentra algo mejor se
escriben sus JSON en <out>/solucion (mismas unidades fisicas que el reporte de
setup.py), y el resumen informa el gap contra max(cota de GCG, --cota_operacional).
Descomposicion recomendada: year_day (un bloque por año-dia; el maestro es la
inversion y la degradacion entre años, ~0,04 %; el pricing es la operacion de un
dia, que con la inversion libre se resuelve en segundos).
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
    ap.add_argument("--igualdad", action="store_true",
                    help="enlace copia = capacidad (la igualdad que arma GCG para una "
                         "variable de enlace), como en las corridas anteriores al "
                         "2026-10-01. Por defecto las capacidades van separadas por "
                         "bloque con copia <= capacidad (variable splitting, forma SV1; "
                         "ver gcg_block.SV_CAPACIDADES)")
    ap.add_argument("--nodes", type=int, default=1,
                    help="limite de nodos de GCG. 1 (default) = solo la raiz, el "
                         "certificado. 0 = sin limite: branch-and-price completo, "
                         "para RESOLVER el problema y no solo acotarlo")
    ap.add_argument("--pricing", choices=["scip", "gurobi"], default="gurobi",
                    help="quien resuelve los subproblemas. gurobi (default): con SCIP la "
                         "raiz de 4 años no termino en 2 h (cota 1.112.173, < LP)")
    ap.add_argument("--hybrid_ascent", action="store_true",
                    help="estabilizacion de GCG con hybridascent (suavizado de Wentges + "
                         "subgradiente); sin el flag queda solo el suavizado, el default")
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
                       gap=1e-4, mode=a.mode, use_incumbent=True,
                       nodes=(a.nodes if a.nodes > 0 else None),
                       pricing=a.pricing, split=not a.igualdad,
                       hybrid_ascent=a.hybrid_ascent,
                       label=f"monolitico {a.n_years} años ({a.mode}, "
                             f"{'igualdad' if a.igualdad else 'copia <= capacidad'})")
    # Con --nodes 1 la cota que interesa es la de la raiz; con el arbol, la
    # global (>= la de la raiz).
    if a.nodes == 1:
        cota = r.get("dual_root") if r.get("dual_root") is not None else r.get("dual")
    else:
        cota = r.get("dual") if r.get("dual") is not None else r.get("dual_root")

    # solve_with_gcg deja cargada en el modelo la MEJOR solucion de GCG, que es
    # el incumbente dado o una mejor. Si mejoro, se escribe.
    primal = r.get("primal")
    mejoro = (r.get("ok") and primal is not None and abs(primal) < 1e19
              and primal < costo * (1 - 1e-7))
    if mejoro:
        from src.io.printer import Printer
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        print(f"[cert] GCG mejoro el incumbente: {costo:,.2f} -> {primal:,.2f}. "
              f"JSON en {destino}", flush=True)
    ub_final = primal if mejoro else costo
    cotas = [x for x in (cota, a.cota_operacional) if x is not None and abs(x) < 1e19]
    lb_final = max(cotas) if cotas else None
    resumen = {"n_years": a.n_years, "mode": a.mode, "split": not a.igualdad,
               "hybrid_ascent": a.hybrid_ascent,
               "ub_benders": costo,
               "gcg_primal": r.get("primal"), "gcg_dual": r.get("dual"),
               "gcg_dual_root": r.get("dual_root"), "gcg_status": r.get("status"),
               "gcg_error": r.get("error"), "n_blocks": r.get("n_blocks"),
               "n_master": r.get("n_master"), "master_frac": r.get("master_frac"),
               "tiempo_gcg_sec": r.get("time_total_sec"), "pricing": a.pricing,
               "pricing_stats": r.get("pricing_stats"),
               "cota_operacional": a.cota_operacional,
               "nodes_limit": a.nodes, "gcg_mejoro_incumbente": bool(mejoro),
               "ub_final": ub_final, "lb_final": lb_final,
               "gap_final": ((ub_final - lb_final) / ub_final
                             if lb_final is not None and ub_final else None)}
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
    if lb_final is not None:
        print(f"[cert] FINAL: UB {ub_final:,.2f} ({'GCG' if mejoro else 'incumbente dado'})  "
              f"LB {lb_final:,.2f}  gap {(ub_final - lb_final) / ub_final:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
