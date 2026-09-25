"""Fase 3/4/5 del plan GCG: resuelve un MPS con GCG usando la descomposicion
por bloques LHD-dia-anio que produjo export_mps.py.

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
    ap.add_argument("--detect-only", action="store_true",
                    help="corre la deteccion de GCG, reporta las descomposiciones "
                         "candidatas, las escribe a .dec y para SIN optimizar")
    ap.add_argument("--dry-run", action="store_true",
                    help="corre todas las validaciones y la construccion de la "
                         "descomposicion, y para ANTES de optimizar")
    a = ap.parse_args()
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
        aceptada = m.addSol(sol)
        print(f"[gcg] addSol -> {aceptada}", flush=True)
    if a.objlimit is not None:
        m.setObjlimit(a.objlimit)

    if a.dry_run:
        print("\n[gcg] --dry-run: validaciones OK, no se optimiza.")
        return 0

    # --- resolver ---------------------------------------------------------
    t0 = time.time()
    interrumpido = False
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

    res = {
        "interrumpido": interrumpido,
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
