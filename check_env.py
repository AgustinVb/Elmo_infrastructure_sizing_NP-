"""Fase 0 del plan GCG: verificar que la API beta de PyGCGOpt 1.0.0b0 trae
los metodos de los que depende el resto del plan.

PyGCGOpt 1.0.0b0 es una beta: los nombres pueden haber cambiado respecto de
la documentacion. Este script NO resuelve nada -- solo construye un modelo de
juguete e inspecciona que exista cada metodo, para no descubrir un rename a
mitad de la Fase 3.

Uso:  .venv_gcg\Scripts\python.exe check_env.py
"""
import sys

# metodo -> sobre que objeto se espera encontrarlo
# NOMBRES REALES en PyGCGOpt 1.0.0b0 (verificados 2026-09-24). El plan citaba
# `createPartialDecomposition`, que NO existe: se llama `createDecomposition`.
# El resto de la API si coincide.
ESPERADOS = {
    "Model": [
        "readProblem", "getConss", "getValsLinear",
        "createDecomposition",            # el plan decia createPartialDecomposition
        "addDecomposition",               # salta la deteccion si la desc. esta completa
        "addDecompositionFromConss",      # atajo: master + *bloques en una llamada
        "addPreexistingPartialDecomposition",
        "readSolFile", "addSol", "setObjlimit",
        "getDualbound", "getDualboundRoot", "getPrimalbound",
        "getStatus", "getGap", "getNNodes",
        "setParam", "setLogfile", "optimize",
    ],
    # Los tres ultimos cubren las validaciones de la Fase 2.2 con la
    # contabilidad de la propia libreria, en vez de recalcularla a mano.
    "PartialDecomposition": [
        "fixConssToMaster", "fixConssToBlock", "fixConssToBlockId",
        "findVarsLinkingToMaster", "getNBlocks", "getNMasterconss",
    ],
}


def main():
    try:
        import pygcgopt as gcg
    except Exception as exc:
        print(f"FALLO import pygcgopt: {type(exc).__name__}: {exc}")
        return 1

    print(f"pygcgopt   : {getattr(gcg, '__version__', '(sin __version__)')}")
    try:
        import pyscipopt
        print(f"pyscipopt  : {getattr(pyscipopt, '__version__', '(sin __version__)')}")
    except Exception as exc:
        print(f"pyscipopt  : FALLO {type(exc).__name__}: {exc}")
    print(f"python     : {sys.version.split()[0]}")
    print()

    m = gcg.Model()
    objetos = {"Model": m}

    # createPartialDecomposition solo puede llamarse con un problema cargado;
    # si falla, se inspecciona la clase en vez de una instancia.
    try:
        objetos["PartialDecomposition"] = m.createDecomposition()
    except Exception as exc:
        print(f"(createDecomposition sobre un modelo vacio fallo: "
              f"{type(exc).__name__}: {exc})")
        clase = getattr(gcg, "PartialDecomposition", None)
        if clase is not None:
            objetos["PartialDecomposition"] = clase
            print("(se inspecciona la CLASE gcg.PartialDecomposition)")
        print()

    faltan = []
    for nombre_obj, metodos in ESPERADOS.items():
        obj = objetos.get(nombre_obj)
        print(f"--- {nombre_obj} ---")
        if obj is None:
            print("  NO SE PUDO OBTENER -- no verificado")
            faltan.extend(metodos)
            continue
        for met in metodos:
            ok = hasattr(obj, met)
            print(f"  {'OK  ' if ok else 'FALTA'}  {met}")
            if not ok:
                faltan.append(met)
        print()

    if faltan:
        print(f"FALTAN {len(faltan)}: {', '.join(faltan)}")
        print("Buscar el nombre real con: "
              "[x for x in dir(obj) if 'parte_del_nombre' in x.lower()]")
        return 1
    print("Todos los metodos que el plan necesita existen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
