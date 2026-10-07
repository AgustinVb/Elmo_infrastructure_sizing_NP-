"""Particion del monolitico por (año, dia) para la metodologia de cierre del
gap (benders_dias.py, mejorar_ub.py, certificar_inversiones.py).

Portado de battery_swapping_multiaño, donde vive dentro de gcg_block.py junto
con todo lo de GCG. Aca se trae SOLO la particion: GCG quedo descartado en las
dos ramas, y la separacion de capacidades por bloque (split_capacities) dependia
de los nombres del modelo de swap y no la usa nadie de los que importan esto
(llaman a export con la particion tal cual).

classify asigna cada restriccion a un bloque (año, dia) si todas sus variables
con indice de dia caen en ese (año, dia), o al MAESTRO si no tiene dia o
cruza dias/años. Es generica: no mira nombres, solo en que posicion del indice
de cada variable estan los años y los dias.
"""
import json
import os
import re

import pyomo.environ as pyo
from pyomo.core.expr.visitor import identify_variables

MASTER = "__master__"
# El writer de Pyomo decora las filas: c_u_<etiqueta>_ (o r_l_/r_u_ para las
# de rango, que se parten en dos filas con la misma etiqueta).
DECORACION = re.compile(r"^[cr]_[ule]_(?P<label>.+)_$")


def _positions(model, valores):
    """{nombre_de_Var: posicion del indice cuyos valores caen en `valores`}."""
    pos = {}
    for v in model.component_objects(pyo.Var, active=True):
        porpos = {}
        for vd in v.values():
            idx = vd.index()
            idx = idx if isinstance(idx, tuple) else (idx,)
            for p, x in enumerate(idx):
                porpos.setdefault(p, set()).add(x)
        cand = [p for p, s in porpos.items() if len(s) > 1 and s <= valores]
        pos[v.name] = cand[0] if len(cand) == 1 else None
    return pos


def _key(vd, pos):
    p = pos.get(vd.parent_component().name)
    if p is None:
        return None
    idx = vd.index()
    idx = idx if isinstance(idx, tuple) else (idx,)
    return idx[p] if p < len(idx) else None


def classify(model, mode="year_day"):
    """{ConstraintData: clave de bloque o MASTER}.

    mode="day":       clave = dia (un solo año).
    mode="year_day":  clave = (año, dia). Para el MONOLITICO de varios años:
                      con "day" a secas, el dia 15 de todos los años caeria en
                      un mismo bloque. Lo que ata años (stock, degradacion) no
                      lleva dia y va al maestro.
    """
    if mode not in ("day", "year_day"):
        raise ValueError("mode tiene que ser 'day' o 'year_day'")
    pos_d = _positions(model, set(model.days))
    # Años: solo si hay mas de uno (con uno solo, ningun indice "varia" y la
    # clave (año, dia) se reduce a dia).
    pos_y = (_positions(model, set(model.years))
             if mode == "year_day" and len(model.years) > 1 else {})

    asignacion = {}
    for cd in model.component_data_objects(pyo.Constraint, active=True):
        vs = list(identify_variables(cd.body, include_fixed=False))
        if mode == "day":
            ds = {_key(v, pos_d) for v in vs} - {None}
            asignacion[cd] = next(iter(ds)) if len(ds) == 1 else MASTER
        else:
            yd = {(_key(v, pos_y) if pos_y else None, _key(v, pos_d)) for v in vs
                  if _key(v, pos_d) is not None}
            asignacion[cd] = next(iter(yd)) if len(yd) == 1 else MASTER
    return asignacion


def _rows_of_mps(path):
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
                if len(partes) == 2 and partes[0] in ("L", "G", "E"):
                    filas.append(partes[1])
    return filas


def export(model, out_dir, mode="year_day"):
    """Escribe model.mps y decomp.json en `out_dir`. Devuelve
    (info, etiqueta->VarData). No modifica `model`."""
    os.makedirs(out_dir, exist_ok=True)
    asignacion = classify(model, mode)
    mps = os.path.join(out_dir, "model.mps")
    _, smap_id = model.write(mps, io_options={"symbolic_solver_labels": True})
    smap = model.solutions.symbol_map[smap_id]
    try:
        por_etiqueta = {}
        for cd, clave in asignacion.items():
            et = smap.byObject.get(id(cd))
            if et is not None:
                por_etiqueta[et] = clave
        claves = sorted({c for c in asignacion.values() if c != MASTER}, key=str)
        id_bloque = {c: i for i, c in enumerate(claves)}
        master, bloques, sin_mapear = [], {i: [] for i in id_bloque.values()}, []
        tiene_constante = False
        for fila in _rows_of_mps(mps):
            mm = DECORACION.match(fila)
            clave = por_etiqueta.get(mm.group("label") if mm else fila)
            if clave is None and "ONE_VAR_CONSTANT" in fila:
                tiene_constante = True
                # Fila auxiliar del writer de Pyomo: fija una variable a 1 para
                # cargar la CONSTANTE del objetivo (p.ej. station_constant_cost
                # con X exogena). No es una restriccion del modelo; al maestro.
                master.append(fila)
            elif clave is None:
                sin_mapear.append(fila)
            elif clave == MASTER:
                master.append(fila)
            else:
                bloques[id_bloque[clave]].append(fila)
        if sin_mapear:
            raise RuntimeError(f"{len(sin_mapear)} filas del MPS sin mapear "
                               f"(primeras: {sin_mapear[:3]})")
        # etiqueta del MPS -> VarData, para leer la solucion de vuelta
        por_var = {}
        for vd in model.component_data_objects(pyo.Var):
            et = smap.byObject.get(id(vd))
            if et is not None:
                por_var[et] = vd
    finally:
        # Cada write() deja un symbol map colgado del modelo.
        del model.solutions.symbol_map[smap_id]

    decomp = os.path.join(out_dir, "decomp.json")
    with open(decomp, "w", encoding="utf-8") as f:
        json.dump({"meta": {"mode": mode, "block_keys": [str(c) for c in claves]},
                   "master": master,
                   "blocks": {str(i): fl for i, fl in sorted(bloques.items())}}, f)
    n = len(master) + sum(len(v) for v in bloques.values())
    info = {"mps": mps, "decomp": decomp, "mode": mode, "n_rows": n,
            "n_master": len(master), "n_blocks": len(bloques),
            "master_frac": len(master) / n if n else 0.0,
            "one_var_constant": tiene_constante}
    return info, por_var
