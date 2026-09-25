"""Fase 5.2 del plan GCG: validar el incumbente de Benders ANTES de darselo a GCG.

Usa PySCIPOpt puro, sin GCG, para aislar errores de nombres o de valores de
cualquier cosa que pueda pasar en la descomposicion. Si esto reporta
violaciones, hay que corregir el .sol (o el varmap con el que se escribio)
antes de seguir: una solucion inicial infactible no se rechaza en silencio,
GCG la descarta y la Experiencia 2 queda sin incumbente sin que se note.

Corre en .venv_gcg:

  .venv_gcg\Scripts\python.exe check_sol.py --mps runs/y10/model.mps \
      --sol runs/y10/benders_best.sol
"""
import argparse
import sys

from pyscipopt import Model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mps", required=True)
    ap.add_argument("--sol", required=True)
    a = ap.parse_args()

    m = Model()
    m.hideOutput()
    print(f"[check_sol] leyendo {a.mps} ...", flush=True)
    m.readProblem(a.mps)
    print(f"[check_sol] leyendo {a.sol} ...", flush=True)
    sol = m.readSolFile(a.sol)

    # original=True: se chequea contra el problema ORIGINAL, no el presolveado,
    # que es donde viven los nombres que escribio export_mps.py.
    ok = m.checkSol(sol, printreason=True, original=True)
    print()
    if ok:
        try:
            print(f"[check_sol] FACTIBLE. objetivo = {m.getSolObjVal(sol):,.2f}")
        except Exception:
            print("[check_sol] FACTIBLE.")
        return 0

    print("[check_sol] INFACTIBLE -- ver las razones impresas arriba.")
    print("  Causas tipicas, en orden de frecuencia:")
    print("   1. El .sol y el MPS vienen de horizontes o flags de regimen distintos.")
    print("   2. Faltan variables en el .sol: las ausentes se leen como CERO, y eso")
    print("      rompe balances (SOE, potencia) aunque los nombres esten bien.")
    print("   3. Enteras sin redondear (sol_io.py ya redondea; revisar si el .sol")
    print("      se genero de otra forma).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
