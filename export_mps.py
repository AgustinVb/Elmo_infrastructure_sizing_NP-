"""Fase 1 del plan GCG: exporta el monolitico linealizado a MPS, junto con la
descomposicion en bloques LHD-dia-anio.

Escribe DOS archivos en la carpeta de salida:

  model.mps    el monolitico con McCormick (lineal), el mismo que resuelve
               `setup.py --mode monolithic`.
  decomp.json  {"master": [filas...], "blocks": {"0": [filas...], ...}}
               donde cada entrada es el nombre EXACTO de la fila en el MPS.

Por que decomp.json y no reglas por nombre: ver el encabezado de
decomp_rules.py. Aca se traduce cada restriccion de Pyomo a su etiqueta del
MPS usando el symbol map del writer, asi que gcg_run.py no parsea nada --
solo compara cadenas.

El modelo se exporta con McCormick SIEMPRE: el bilineal exacto no es lineal y
no se puede escribir en MPS. Es la misma formulacion que usan los bloques de
Nested Benders, con lo que las cotas quedan en la misma base.

Uso:
  .venv_elmo\Scripts\python.exe export_mps.py --years 1 --out runs/y01
"""
import argparse
import json
import os
import re
import sys
import types

import pyomo.environ as pyo

import setup as S
from src.optimization.opt_model import OptModel
from decomp_rules import clasificar_pyomo, MASTER

# El writer de Pyomo envuelve la etiqueta del symbol map con la direccion de
# la restriccion y un guion bajo final: state_unique_elhd(...) pasa a ser
# c_u_state_unique_elhd(...)_ en la seccion ROWS.
#
# Las restricciones de RANGO (lb <= expr <= ub, como `production`, que usa
# pyo.inequality) llevan prefijo r_ y se parten en DOS filas del MPS,
# r_l_... y r_u_..., para una sola ConstraintData de Pyomo. Las dos apuntan a
# la misma etiqueta, asi que caen en el mismo bloque -- que es lo correcto.
DECORACION = re.compile(r"^[cr]_[ule]_(?P<label>.+)_$")


def construir(args):
    ns = types.SimpleNamespace(
        data_folder=args.data_folder, model=args.model, series=args.series,
        days_per_year=args.days_per_year, n_years=args.years,
        consumption_model="wp1", wp2_consumption_json=None,
    )
    series, mine_system, time_series = S.build_mine(ns)
    om = OptModel(
        mine_system, time_series, output_folder=args.out,
        mccormick_degradation=True,
        autonomous_mode=args.autonomous_mode,
        free_charging=args.free_charging,
        free_maintenance=args.free_maintenance,
    )
    return om.model


def filas_del_mps(path):
    """Nombres de fila de la seccion ROWS, en orden, salvo la del objetivo."""
    filas, dentro = [], False
    with open(path, encoding="utf-8") as f:
        for linea in f:
            if linea.startswith("ROWS"):
                dentro = True
                continue
            if dentro:
                if linea and not linea[0].isspace():
                    break
                partes = linea.split()
                if len(partes) == 2 and partes[0] in ("N", "L", "G", "E"):
                    if partes[0] != "N":
                        filas.append(partes[1])
    return filas


def main():
    ap = argparse.ArgumentParser(description="Exporta el monolitico a MPS + decomp.json")
    ap.add_argument("--years", type=int, required=True)
    ap.add_argument("--out", required=True, help="carpeta de salida")
    ap.add_argument("--data_folder",
                    default="data/Resultados_finales_tesis/Mina_modelo/P_red_gen_bat")
    ap.add_argument("--model", default="elmo_data.xlsx")
    ap.add_argument("--series", default="time_series.xlsx")
    ap.add_argument("--days_per_year", type=int, choices=[2, 4], default=4)
    ap.add_argument("--free_charging", action="store_true", default=True)
    ap.add_argument("--no_free_charging", dest="free_charging", action="store_false")
    ap.add_argument("--free_maintenance", action="store_true", default=True)
    ap.add_argument("--no_free_maintenance", dest="free_maintenance", action="store_false")
    ap.add_argument("--autonomous_mode", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    mps = os.path.join(args.out, "model.mps")

    print(f"[export] construyendo el monolitico: {args.years} anio(s), "
          f"{args.days_per_year} dias/anio, free_charging={args.free_charging}, "
          f"free_maintenance={args.free_maintenance}")
    sys.stdout.flush()
    model = construir(args)

    print("[export] clasificando restricciones...")
    asignacion, desconocidos = clasificar_pyomo(model)
    if desconocidos:
        print("\n[export] ABORTA: restricciones sin clasificar (validacion 2.2.1).")
        for pref, n in sorted(desconocidos.items(), key=lambda kv: -kv[1]):
            print(f"    {n:8d}  {pref}")
        print("\nAgregalas a BLOCK_RULES o a MASTER_PREFIXES en decomp_rules.py.")
        return 1

    print(f"[export] escribiendo {mps} ...")
    sys.stdout.flush()
    _, smap_id = model.write(mps, io_options={"symbolic_solver_labels": True})
    smap = model.solutions.symbol_map[smap_id]

    # etiqueta del symbol map -> clave de bloque (o MASTER)
    por_etiqueta = {}
    for cd, clave in asignacion.items():
        etiqueta = smap.byObject.get(id(cd))
        if etiqueta is not None:
            por_etiqueta[etiqueta] = clave

    # nombre exacto de fila del MPS -> clave, quitando la decoracion del writer
    claves = sorted({c for c in asignacion.values() if c != MASTER})
    id_de_bloque = {c: i for i, c in enumerate(claves)}
    master, bloques, sin_mapear = [], {i: [] for i in id_de_bloque.values()}, []
    for fila in filas_del_mps(mps):
        m = DECORACION.match(fila)
        etiqueta = m.group("label") if m else fila
        clave = por_etiqueta.get(etiqueta)
        if clave is None:
            sin_mapear.append(fila)
        elif clave == MASTER:
            master.append(fila)
        else:
            bloques[id_de_bloque[clave]].append(fila)

    if sin_mapear:
        print(f"\n[export] ABORTA: {len(sin_mapear)} filas del MPS no se pudieron "
              f"mapear a ninguna restriccion. Primeras 5:")
        for f in sin_mapear[:5]:
            print(f"    {f}")
        return 1

    # varmap.json: nombre canonico de Pyomo -> etiqueta del MPS. Lo consume
    # `setup.py --export_sol` para escribir el incumbente de Benders con los
    # nombres exactos que espera SCIP, sin tener que reproducir el esquema de
    # etiquetado del writer (que convierte Z_charge[station_1,LH518B_1,1,15,1.0]
    # en Z_charge(station_1_LH518B_1_1_15_1_0)).
    varmap = {}
    for vd in model.component_data_objects(pyo.Var, active=True):
        etiqueta = smap.byObject.get(id(vd))
        if etiqueta is not None:
            varmap[vd.name] = etiqueta
    with open(os.path.join(args.out, "varmap.json"), "w", encoding="utf-8") as f:
        json.dump(varmap, f, indent=0)

    destino = os.path.join(args.out, "decomp.json")
    with open(destino, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "years": args.years, "days_per_year": args.days_per_year,
                "free_charging": args.free_charging,
                "free_maintenance": args.free_maintenance,
                "mccormick_degradation": True,
                "block_key": "(equipo, anio, dia)",
                "block_keys": [list(map(str, c)) for c in claves],
            },
            "master": master,
            "blocks": {str(i): filas for i, filas in sorted(bloques.items())},
        }, f, indent=1)

    n_filas = len(master) + sum(len(v) for v in bloques.values())
    tam = sorted(len(v) for v in bloques.values())
    print(f"\n[export] LISTO")
    print(f"    filas totales     : {n_filas:,}")
    print(f"    bloques           : {len(bloques)}")
    print(f"    filas en maestro  : {len(master):,}  ({100*len(master)/n_filas:.1f}%)")
    if tam:
        print(f"    filas por bloque  : min {tam[0]}, mediana {tam[len(tam)//2]}, max {tam[-1]}")
    print(f"    variables         : {len(varmap):,}")
    print(f"    {mps}")
    print(f"    {destino}")
    print(f"    {os.path.join(args.out, 'varmap.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
