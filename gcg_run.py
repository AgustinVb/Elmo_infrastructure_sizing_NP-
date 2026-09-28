"""Resuelve un MPS con GCG (Dantzig-Wolfe / branch-and-price) usando la
descomposicion manual de decomp.json.

Portado de carga_ob_multiaño (Fase 3/4/5 del plan GCG, bloques LHD-dia-anio
del monolitico). En battery_swapping_multiaño lo usa
src/optimization/decomposition/gcg_block.py para resolver el MILP del BLOQUE
ANUAL del forward, con bloques por DIA (o LHD-dia), y ademas de las cotas
escribe la solucion (best.sol).

Corre en .venv_gcg (aislado). No necesita pyomo ni pandas: solo lee archivos.

  .venv_gcg\Scripts\python.exe gcg_run.py --mps runs/y01/model.mps \
      --decomp runs/y01/decomp.json --out runs/y01/gcg_mono --no-presolve

Con --incumbent agrega la solucion de Nested Benders antes de optimizar
(Experiencia 2 del plan).

CORRECCIONES RESPECTO DEL PLAN
------------------------------
1. `createPartialDecomposition` no existe en PyGCGOpt 1.0.0b0. Se usa
   `addDecompositionFromConss(master, *bloques)`, que hace crear + fijar +
   agregar en una sola llamada. Una descomposicion COMPLETA hace que GCG
   saltee su loop de deteccion, que es lo que el plan queria verificar.
2. La descomposicion no se deduce de los nombres: viene en decomp.json, con
   el nombre EXACTO de cada fila del MPS (ver decomp_rules.py).
3. `dual` se reporta como None si getDualbound falla. Nunca se cae al primal:
   confundir una cota dual con la UB invalidaria toda la comparacion.
"""
import argparse
import faulthandler
import json
import os
import sys
import time

import pygcgopt as gcg


def safe(f, default=None):
    try:
        return f()
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mps", required=True)
    ap.add_argument("--decomp", default=None,
                    help="decomp.json de export_mps.py. Si se OMITE, GCG corre su "
                         "propia deteccion automatica y elige la descomposicion.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--time-limit", type=float, default=36000)
    ap.add_argument("--gap", type=float, default=1e-4)
    ap.add_argument("--incumbent", default=None, help=".sol exportado por Benders")
    ap.add_argument("--objlimit", type=float, default=None)
    ap.add_argument("--no-presolve", action="store_true")
    ap.add_argument("--pricing", choices=["scip", "gurobi"], default="scip",
                    help="quien resuelve los subproblemas de pricing. gurobi: "
                         "gcg_pricing_gurobi.py (necesita gurobipy en este entorno)")
    ap.add_argument("--pricing-exact-timelimit", type=float, default=60.0)
    ap.add_argument("--pricing-heur-timelimit", type=float, default=20.0)
    ap.add_argument("--nodes", type=int, default=None,
                    help="limits/nodes de SCIP. 1 = solo la raiz: con un incumbente "
                         "dado, la cota de la raiz de Dantzig-Wolfe es el certificado")
    ap.add_argument("--convexification", action="store_true",
                    help="convexificacion en vez de discretizacion (GCG avisa que la "
                         "discretizacion con variables continuas es experimental)")
    ap.add_argument("--detect-only", action="store_true",
                    help="corre la deteccion de GCG, reporta las descomposiciones "
                         "candidatas, las escribe a .dec y para SIN optimizar")
    ap.add_argument("--dry-run", action="store_true",
                    help="corre todas las validaciones y la construccion de la "
                         "descomposicion, y para ANTES de optimizar")
    a = ap.parse_args()
    # Un crash nativo (access violation) no deja traceback de Python: con esto
    # se imprime la pila de Python en el punto donde murio.
    faulthandler.enable(all_threads=True)
    os.makedirs(a.out, exist_ok=True)

    desc = None
    if a.decomp:
        with open(a.decomp, encoding="utf-8") as f:
            desc = json.load(f)

    m = gcg.Model()
    m.setLogfile(os.path.join(a.out, "gcg.log"))
    print(f"[gcg] leyendo {a.mps} ...", flush=True)
    m.readProblem(a.mps)
    m.setParam("limits/time", a.time_limit)
    m.setParam("limits/gap", a.gap)
    if a.no_presolve:
        m.setParam("presolving/maxrounds", 0)
    if a.nodes is not None:
        m.setParam("limits/nodes", a.nodes)
    pricing_gurobi = None
    if a.pricing == "gurobi":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gcg_pricing_gurobi
        pricing_gurobi = gcg_pricing_gurobi.registrar(
            m, exact_timelimit=a.pricing_exact_timelimit,
            heur_timelimit=a.pricing_heur_timelimit)
        print("[gcg] pricing con Gurobi registrado (pricing de SCIP deshabilitado)", flush=True)
    if a.convexification:
        m.setParam("relaxing/gcg/discretization", False)
        m.setParam("relaxing/gcg/mipdiscretization", False)

    # --- descomposicion ---------------------------------------------------
    master, bloques, enlace = [], [], None

    if desc is None:
        # Sin --decomp: GCG corre sus propios detectores y elige. Es la linea
        # base contra la que comparar la descomposicion manual LHD-dia-anio.
        print("[gcg] sin --decomp: la descomposicion la deciden los detectores de GCG",
              flush=True)
        if a.detect_only:
            print("[gcg] corriendo detect() ...", flush=True)
            m.detect()
            decs = safe(lambda: list(m.listDecompositions()), []) or []
            print(f"\n[gcg] descomposiciones candidatas: {len(decs)}")
            for i, dec in enumerate(decs):
                nb = safe(lambda: dec.getNBlocks(), "?")
                nm = safe(lambda: dec.getNMasterconss(), "?")
                print(f"    #{i}: {nb} bloques, {nm} restricciones de maestro")
            safe(lambda: m.writeAllDecomps(a.out, "dec"))
            print(f"[gcg] escritas en {a.out} (.dec)")
            print("[gcg] --detect-only: no se optimiza.")
            return 0
    else:
        por_nombre = {c.name: c for c in m.getConss()}
        print(f"[gcg] restricciones en el modelo leido: {len(por_nombre):,}", flush=True)

        def tomar(filas, donde):
            faltan = [f for f in filas if f not in por_nombre]
            if faltan:
                raise SystemExit(
                    f"[gcg] {len(faltan)} filas de {donde} no existen en el MPS leido. "
                    f"Primeras 3: {faltan[:3]}\n"
                    f"Suele significar que decomp.json se genero desde otro MPS, o que "
                    f"SCIP renombro filas (probar con --no-presolve)."
                )
            return [por_nombre[f] for f in filas]

        master = tomar(desc["master"], "master")
        bloques = [tomar(desc["blocks"][k], f"block {k}")
                   for k in sorted(desc["blocks"], key=int)]

        cubiertas = len(master) + sum(len(b) for b in bloques)
        if cubiertas != len(por_nombre):
            raise SystemExit(
                f"[gcg] ABORTA: decomp.json cubre {cubiertas:,} restricciones pero el "
                f"MPS tiene {len(por_nombre):,}. Una descomposicion incompleta deja que "
                f"GCG la complete con su detector, que es lo que se quiere evitar."
            )

        print(f"[gcg] descomposicion manual: {len(bloques)} bloques, "
              f"{len(master):,} filas en el maestro", flush=True)
        dec = m.addDecompositionFromConss(master, *bloques)
        print(f"[gcg] GCG reporta {safe(lambda: dec.getNBlocks(), '?')} bloques y "
              f"{safe(lambda: dec.getNMasterconss(), '?')} restricciones de maestro",
              flush=True)
        enlace = safe(lambda: dec.findVarsLinkingToMaster())
        if enlace is not None:
            print(f"[gcg] variables de enlace detectadas: {len(enlace)}", flush=True)
        safe(lambda: m.writeAllDecomps(a.out, "dec"))

        if a.detect_only:
            print("[gcg] --detect-only con --decomp: no hay nada que detectar, "
                  "la descomposicion vino dada. No se optimiza.")
            return 0
    # --- incumbente opcional ---------------------------------------------
    if a.incumbent:
        print(f"[gcg] cargando incumbente {a.incumbent} ...", flush=True)
        sol = m.readSolFile(a.incumbent)
        # trySol y NO addSol: addSol guarda el punto SIN verificarlo. Medido: un
        # incumbente al que le faltaba ONE_VAR_CONSTANT (la variable con que el
        # writer de Pyomo carga la constante del objetivo) entro con un objetivo
        # 199.540,91 por debajo del real, violando una fila -- un primal bound
        # falso con el que GCG podria podar nodos buenos o "demostrar" un optimo
        # que no es. trySol lo rechaza si no es factible y dice por que.
        aceptada = m.trySol(sol, printreason=True, completely=True)
        print(f"[gcg] trySol (incumbente factible?) -> {aceptada}", flush=True)
    if a.objlimit is not None:
        m.setObjlimit(a.objlimit)

    if a.dry_run:
        print("\n[gcg] --dry-run: validaciones OK, no se optimiza.")
        return 0

    # --- resolver ---------------------------------------------------------
    t0 = time.time()
    interrumpido = False
    error = None
    try:
        m.optimize()
    except KeyboardInterrupt:
        # Sin esto, un Ctrl+C mata el script antes de escribir results.json y se
        # pierde la cota que el solver ya habia alcanzado. En una corrida de
        # cotas eso es justo el dato que importa: el dual bound al momento del
        # corte es una cota inferior valida igual.
        interrumpido = True
        print("\n[gcg] interrumpido -- se guarda el estado alcanzado.", flush=True)
    except MemoryError:
        # GCG se queda sin memoria si la descomposicion elegida genera miles de
        # problemas de pricing (le paso con la deteccion automatica: 56.943
        # bloques). Se reporta y se guarda lo que haya.
        interrumpido = True
        print("\n[gcg] MemoryError -- se guarda el estado alcanzado.", flush=True)
    except Exception as exc:  # noqa: BLE001
        # Errores internos de GCG/SCIP (p.ej. "the value is invalid for the given
        # parameter" cuando el tiempo restante que GCG les pasa a los pricing sale
        # negativo): se registran y se guarda igual lo alcanzado. Sin esto el
        # script moria sin results.json y se perdian las cotas.
        interrumpido = True
        error = f"{type(exc).__name__}: {exc}"
        print(f"\n[gcg] error durante optimize(): {error} -- se guarda el estado.",
              flush=True)

    # Solucion: en la rama carga_ob_multiaño este script solo comparaba COTAS.
    # En battery_swapping_multiaño resuelve el bloque anual del forward de
    # Nested Benders, que necesita los VALORES para seguir con el año
    # siguiente: se escriben todas las variables (write_zeros=True, para que
    # quien lee no tenga que asumir ceros) en nombres del MPS original.
    sol_path = None
    n_sols = safe(m.getNSols, 0) or 0
    if n_sols > 0:
        sol_path = os.path.join(a.out, "best.sol")
        if safe(lambda: m.writeBestSol(sol_path, write_zeros=True), "err") == "err":
            sol_path = None

    res = {
        "interrumpido": interrumpido,
        "error": error,
        "n_sols": n_sols,
        "sol_path": sol_path,
        "status": str(safe(m.getStatus)),
        "primal": safe(m.getPrimalbound),
        "dual": safe(m.getDualbound),            # sin fallback al primal
        "dual_root": safe(m.getDualboundRoot),
        "gap": safe(m.getGap),
        "nodes": safe(m.getNNodes),
        "time_sec": time.time() - t0,
        "n_blocks": len(bloques),
        "n_master_conss": len(master),
        "n_linking_vars": None if enlace is None else len(enlace),
        "mps": a.mps,
        "decomp": a.decomp,  # None = deteccion automatica
        "incumbent": a.incumbent,
        "objlimit": a.objlimit,
        "time_limit": a.time_limit,
        "presolve": not a.no_presolve,
        "convexification": a.convexification,
        "nodes_limit": a.nodes,
        "pricing": a.pricing,
        "pricing_stats": None if pricing_gurobi is None else pricing_gurobi.stats,
        "meta_decomp": None if desc is None else desc.get("meta"),
    }
    destino = os.path.join(a.out, "results.json")
    with open(destino, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    print(f"\n[gcg] status={res['status']}  primal={res['primal']}  "
          f"dual={res['dual']}  dual_root={res['dual_root']}  "
          f"tiempo={res['time_sec']:.0f}s")
    print(f"[gcg] {destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
