"""Resolver el MILP de un BLOQUE ANUAL con GCG (Dantzig-Wolfe / branch-and-price).

Corre en .venv_elmo (necesita pyomo). GCG corre aparte, en .venv_gcg, via
gcg_run.py: PySCIPOpt 6 arrastra numpy 2, que rompe el pandas 2.0.3 de los
entornos del proyecto (ver memoria del entorno GCG). La comunicacion es por
archivos: model.mps + decomp.json de ida, results.json + best.sol de vuelta.

QUE DESCOMPOSICION. Medido sobre el bloque anual del año 4 de Mina_modelo
(60.592 restricciones, regimen liberado):

    bloques por VEHICULO-DIA (16, la del colega)  maestro 22.096 filas (36,5%)
    bloques por DIA (4)                           maestro     36 filas ( 0,1%)

En swap todos los LHD comparten, intervalo por intervalo, el pool de baterias
de la estacion (inventario, cargadores, bahias) y el balance de potencia: todo
eso cae al maestro con bloques por vehiculo. En la rama OB, con un maestro de
13,7%, la generacion de columnas ya se estanco en la raiz de 1 año. Con bloques
por dia, en cambio, lo unico que ata los dias son ~23 variables anuales
(inversion, flota, P_pot, degradacion) y una sola restriccion
(energy_consumed_def). Default "day"; "vehicle_day" queda para comparar.

LA CLASIFICACION se hace sobre el modelo de Pyomo, donde los indices son
tuplas reales, y se traduce a los nombres exactos del MPS con el symbol map
del writer -- mismo mecanismo que export_mps.py de carga_ob_multiaño, que
documento por que no se pueden parsear los nombres del MPS. El dia y el LHD de
cada variable se identifican por POSICION en el indice (la posicion cuyo
conjunto de valores esta contenido en model.days / model.slhd_set): por VALOR
no se puede, porque los dias {15, 105, 196, 288} colisionan con los intervalos
t = 15 y t = 105.

Una restriccion va a un bloque si todas sus variables CON dia son del mismo
dia (las variables anuales, sin dia, son de enlace: GCG las maneja). Si no
tiene ninguna variable con dia, o mezcla dias, va al maestro.
"""
import json
import os
import re
import subprocess
import time

import pyomo.environ as pyo
from pyomo.core.expr.visitor import identify_variables
from pyomo.environ import value

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
GCG_PYTHON = os.path.join(REPO, ".venv_gcg", "Scripts", "python.exe")
GCG_RUN = os.path.join(REPO, "gcg_run.py")

MASTER = "__master__"
# El writer de Pyomo decora las filas: c_u_<etiqueta>_ (o r_l_/r_u_ para las
# de rango, que se parten en dos filas con la misma etiqueta).
DECORACION = re.compile(r"^[cr]_[ule]_(?P<label>.+)_$")

# Variable splitting (forma SV1), en TODOS los modos por defecto (decision del
# 2026-10-01: de aca en adelante la descomposicion de columnas va con
# desigualdad): cada bloque usa su PROPIA copia de estas capacidades y el
# maestro las une con copia <= capacidad, en vez de la igualdad
# copia = capacidad que GCG arma para una variable de enlace. Es la misma cota
# DW solo con LIBRE DISPOSICION: que mas capacidad nunca vuelva infactible ni
# mas cara la operacion del bloque. split_capacities lo verifica fila por fila
# y aborta si alguna lo rompe. b_bar, D y R quedan como enlace con igualdad:
# son estado de degradacion, no capacidad (y B >= bmin*b_bar no es monotona en
# b_bar). split=False en export/solve_with_gcg vuelve a la igualdad.
SV_CAPACIDADES = ("N_bays", "N_chargers", "N_batteries", "P_pot", "G_g", "H", "n_ssee_k")
# Filas de bloque que NO son monotonas en una capacidad, pero tienen una
# holgura gratis que absorbe la capacidad de mas (verificado 2026-10-01):
#   CB_general: S[t0] + X_dch[t0] = N_batteries. Las baterias de mas quedan
#     quietas en S, que solo aparece en su dinamica (diferencias), en el
#     inventario ciclico y aca: sin cota superior ni costo.
#   gen_limit: P_gen + Curt_g = G*p_max*alpha. Curt_g no tiene costo ni
#     aparece en otra fila.
SV_FILAS_CON_HOLGURA = ("CB_general", "gen_limit")


# --------------------------------------------------------------------------
# Clasificacion de restricciones
# --------------------------------------------------------------------------

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


def classify(model, mode="day"):
    """{ConstraintData: clave de bloque o MASTER}.

    mode="day":          clave = dia.
    mode="vehicle_day":  clave = (LHD, dia); una restriccion que toque
                         variables operativas COMPARTIDAS (con dia y sin LHD:
                         inventario de la estacion, potencia, ...) va al maestro.
    mode="year_day":     clave = (año, dia). Para el MONOLITICO de varios años:
                         con "day" a secas, el dia 15 de los diez años caeria en
                         un mismo bloque. Lo que ata años (stock, degradacion)
                         no lleva dia y va al maestro.
    mode="vehicle_station_day": como "vehicle_day", pero las restricciones que
                         solo tocan variables COMPARTIDAS de un mismo dia (sin
                         LHD: estacion, inventario de baterias, potencia, BESS,
                         generacion) van a un bloque propio ("estacion", dia) en
                         vez de al maestro. Idea tomada del modelo de acarreo del
                         colega: el maestro solo ve de los vehiculos sus swaps
                         (total_swaps, bays_limit_swap) y su extraccion
                         (daily_extraction). En el año 4 de Mina_modelo eso son
                         1.445 de las 22.096 filas que "vehicle_day" deja en el
                         maestro; las otras 20.651 no tocan ninguna variable de
                         LHD.
    """
    modos = ("day", "vehicle_day", "vehicle_station_day", "year_day")
    if mode not in modos:
        raise ValueError(f"mode tiene que ser uno de {modos}")
    por_vehiculo = mode in ("vehicle_day", "vehicle_station_day")
    dias = set(model.days)
    pos_d = _positions(model, dias)
    pos_i = _positions(model, set(model.slhd_set)) if por_vehiculo else {}
    # Años: solo si hay mas de uno (con uno solo, ningun indice "varia" y la
    # clave (año, dia) se reduce a dia).
    pos_y = (_positions(model, set(model.years))
             if mode == "year_day" and len(model.years) > 1 else {})

    asignacion = {}
    for cd in model.component_data_objects(pyo.Constraint, active=True):
        vs = list(identify_variables(cd.body, include_fixed=False))
        ds = {_key(v, pos_d) for v in vs} - {None}
        if mode == "day":
            asignacion[cd] = next(iter(ds)) if len(ds) == 1 else MASTER
        elif mode == "year_day":
            yd = {(_key(v, pos_y) if pos_y else None, _key(v, pos_d)) for v in vs
                  if _key(v, pos_d) is not None}
            asignacion[cd] = next(iter(yd)) if len(yd) == 1 else MASTER
        else:
            con_i = {(_key(v, pos_i), _key(v, pos_d)) for v in vs
                     if _key(v, pos_i) is not None}
            if mode == "vehicle_station_day" and not con_i:
                ds = {_key(v, pos_d) for v in vs} - {None}
                asignacion[cd] = ("estacion", next(iter(ds))) if len(ds) == 1 else MASTER
                continue
            compartida = any(_key(v, pos_i) is None and _key(v, pos_d) is not None
                             for v in vs)
            ok = len(con_i) == 1 and None not in next(iter(con_i)) and not compartida
            asignacion[cd] = next(iter(con_i)) if ok else MASTER
    return asignacion


def _sync_copias(model):
    """copia = capacidad, para las que tengan valor: con eso el punto cargado
    en el modelo (incumbente) cumple las filas separadas y copia <= capacidad."""
    for i, v in enumerate(model._sv_originales):
        if v.value is not None:
            model.sv_copia[i].set_value(v.value, skip_validation=True)


def split_capacities(model, asignacion, verbose=True):
    """Variable splitting (ver SV_CAPACIDADES): MODIFICA `model` (desactiva las
    filas de bloque que usan capacidades y agrega sus versiones con copias) y
    devuelve (asignacion nueva, resumen o None si no habia nada que separar).

    - una copia sv_copia[i] por par (bloque, capacidad usada en el bloque),
      con el dominio y las cotas de la original, y su valor si lo tiene (asi
      el incumbente sigue siendo factible: copia = capacidad);
    - sv_filas: las filas del bloque con la copia en vez de la capacidad;
    - sv_enlace: copia <= capacidad, al maestro.

    Si el modelo YA esta separado (un bloque anual que el forward resuelve con
    GCG en cada iteracion), classify ya manda sv_filas a su bloque y
    sv_enlace al maestro: solo se igualan las copias a las capacidades.
    """
    from pyomo.core.expr.visitor import replace_expressions
    from pyomo.repn import generate_standard_repn

    if hasattr(model, "sv_copia"):
        _sync_copias(model)
        return asignacion, None

    filas = {}
    for cd, clave in asignacion.items():
        if clave == MASTER:
            continue
        caps = [v for v in identify_variables(cd.body, include_fixed=False)
                if v.parent_component().name in SV_CAPACIDADES]
        if caps:
            filas[cd] = (clave, caps)
    if not filas:
        if verbose:
            print("[GCG] separacion de capacidades: ninguna fila de bloque usa "
                  "capacidades, no hay nada que separar", flush=True)
        return asignacion, None

    # Libre disposicion, fila por fila: subir la capacidad no puede violar la
    # fila (coeficiente <= 0 en una cota superior, >= 0 en una inferior). Las
    # igualdades solo pasan si tienen una holgura gratis (SV_FILAS_CON_HOLGURA).
    rompen = {}
    for cd, (_, caps) in filas.items():
        nombre = cd.parent_component().name
        repn = generate_standard_repn(cd.body, compute_values=True)
        if not repn.is_linear():
            rompen.setdefault(nombre, set()).add("(no lineal)")
            continue
        coef = {id(v): c for v, c in zip(repn.linear_vars, repn.linear_coefs)}
        for v in caps:
            c = coef.get(id(v), 0.0)
            if abs(c) < 1e-12:
                continue
            monotona = (not cd.equality
                        and not (cd.has_ub() and c > 0)
                        and not (cd.has_lb() and c < 0))
            if not monotona and nombre not in SV_FILAS_CON_HOLGURA:
                rompen.setdefault(nombre, set()).add(v.parent_component().name)
    if rompen:
        raise RuntimeError(
            "separacion de capacidades: estas filas de bloque no cumplen libre "
            "disposicion en las capacidades indicadas, la desigualdad relajaria el "
            f"modelo (usar split=False): {rompen}")

    # Copias, en el orden (determinista) de las filas
    orig, idx_de = [], {}
    for cd, (clave, caps) in filas.items():
        for v in caps:
            if (clave, id(v)) not in idx_de:
                idx_de[(clave, id(v))] = len(orig)
                orig.append((clave, v))
    model.sv_copia = pyo.Var(range(len(orig)))
    model._sv_originales = [v for _, v in orig]
    for i, (_, v) in enumerate(orig):
        c = model.sv_copia[i]
        c.domain = v.domain
        c.setlb(v.lb)
        c.setub(v.ub)
    _sync_copias(model)

    model.sv_filas = pyo.ConstraintList()
    model.sv_enlace = pyo.ConstraintList()
    nueva = {}
    for cd, clave in asignacion.items():
        if cd not in filas:
            nueva[cd] = clave
            continue
        sub = {id(v): model.sv_copia[idx_de[(clave, id(v))]] for v in filas[cd][1]}
        nc = model.sv_filas.add(replace_expressions(cd.expr, sub))
        cd.deactivate()
        nueva[nc] = clave
    for i, (_, v) in enumerate(orig):
        nueva[model.sv_enlace.add(model.sv_copia[i] <= v)] = MASTER

    por_cap = {}
    for _, v in orig:
        por_cap[v.parent_component().name] = por_cap.get(v.parent_component().name, 0) + 1
    resumen = {"copias": len(orig), "filas_reescritas": len(filas), "por_capacidad": por_cap}
    if verbose:
        print(f"[GCG] separacion de capacidades: {len(orig)} copias "
              f"({', '.join(f'{k} {n}' for k, n in sorted(por_cap.items()))}), "
              f"{len(filas):,} filas de bloque reescritas, {len(orig)} filas "
              f"copia <= capacidad al maestro", flush=True)
    return nueva, resumen


# --------------------------------------------------------------------------
# Exportacion
# --------------------------------------------------------------------------

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


def export(model, out_dir, mode="day", split=True):
    """Escribe model.mps y decomp.json en `out_dir`. Devuelve
    (info, etiqueta->VarData). split=True (default): capacidades separadas
    por bloque con copia <= capacidad (ver SV_CAPACIDADES); OJO, modifica
    `model`."""
    os.makedirs(out_dir, exist_ok=True)
    asignacion = classify(model, mode)
    if split:
        asignacion, _ = split_capacities(model, asignacion)
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
        # Cada write() deja un symbol map colgado del modelo: sin borrarlo, un
        # forward de 10 años x varias iteraciones acumula memoria.
        del model.solutions.symbol_map[smap_id]

    decomp = os.path.join(out_dir, "decomp.json")
    with open(decomp, "w", encoding="utf-8") as f:
        json.dump({"meta": {"mode": mode, "split": split,
                            "block_keys": [str(c) for c in claves]},
                   "master": master,
                   "blocks": {str(i): fl for i, fl in sorted(bloques.items())}}, f)
    n = len(master) + sum(len(v) for v in bloques.values())
    info = {"mps": mps, "decomp": decomp, "mode": mode, "split": split, "n_rows": n,
            "n_master": len(master), "n_blocks": len(bloques),
            "master_frac": len(master) / n if n else 0.0,
            "one_var_constant": tiene_constante}
    return info, por_var


def write_incumbent(por_var, path, objective, one_var_constant=False):
    """Vuelca los valores actuales del modelo como .sol de SCIP (MIP start).

    one_var_constant: el MPS tiene la variable auxiliar ONE_VAR_CONSTANT (fijada
    a 1 por su fila) con que el writer de Pyomo carga la constante del objetivo.
    No existe en Pyomo, asi que hay que escribirla a mano: sin ella SCIP la toma
    como 0 y el punto viola esa fila."""
    lineas = ["ONE_VAR_CONSTANT 1"] if one_var_constant else []
    for et, vd in por_var.items():
        v = value(vd, exception=False)
        if v is None:
            return False          # start incompleto: mejor no pasarlo
        if not vd.is_continuous():
            v = float(round(v))
        if abs(v) > 1e-9:
            lineas.append(f"{et} {v!r}")  # repr: precision completa. Con %.12g, w_deg ~2,2e6 perdia ~1e-6 y SCIP rechazaba el punto
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"objective value: {objective:.12g}\n" + "\n".join(lineas) + "\n")
    return True


def read_solution(path):
    """{etiqueta: valor} de un .sol de SCIP."""
    sol = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for linea in f:
            if not linea.strip() or linea.startswith(("solution status", "objective value")):
                continue
            partes = linea.split()
            if len(partes) >= 2:
                try:
                    sol[partes[0]] = float(partes[1])
                except ValueError:
                    pass
    return sol


# --------------------------------------------------------------------------
# Resolucion
# --------------------------------------------------------------------------

def solve_with_gcg(model, out_dir, timelimit=600, gap=0.01, mode="day",
                   use_incumbent=True, no_presolve=True, convexification=True,
                   nodes=None, pricing="scip", verbose=True, label="", split=True,
                   hybrid_ascent=False):
    """Resuelve `model` (un MILP de Pyomo, tipicamente el bloque anual) con GCG y
    carga la mejor solucion en sus variables. Devuelve un dict con
    ok/status/primal/dual/tiempos. ok=False si GCG no encontro solucion: las
    variables quedan como estaban. split: ver export. hybrid_ascent: suavizado
    de duales + subgradiente (ver --hybrid-ascent de gcg_run.py)."""
    t0 = time.time()
    if not os.path.exists(GCG_PYTHON):
        raise RuntimeError(f"no existe el entorno de GCG: {GCG_PYTHON}")
    info, por_var = export(model, out_dir, mode, split=split)
    t_export = time.time() - t0
    if verbose:
        print(f"[GCG] {label} exportado en {t_export:.0f}s: {info['n_blocks']} bloques "
              f"({mode}), maestro {info['n_master']:,} filas "
              f"({100 * info['master_frac']:.1f}%)", flush=True)

    cmd = [GCG_PYTHON, GCG_RUN, "--mps", info["mps"], "--decomp", info["decomp"],
           "--out", out_dir, "--time-limit", str(timelimit), "--gap", str(gap)]
    if no_presolve:
        cmd.append("--no-presolve")
    if nodes is not None:
        cmd += ["--nodes", str(nodes)]
    if pricing != "scip":
        cmd += ["--pricing", pricing]
    if hybrid_ascent:
        cmd.append("--hybrid-ascent")
    if convexification:
        # El bloque tiene ~13.700 variables continuas y GCG avisa que la
        # discretizacion (su default) con continuas es experimental.
        cmd.append("--convexification")
    if use_incumbent:
        inc = os.path.join(out_dir, "incumbent.sol")
        obj = value(model.obj, exception=False)
        if obj is not None and write_incumbent(por_var, inc, obj,
                                               info["one_var_constant"]):
            cmd += ["--incumbent", inc]

    res_path = os.path.join(out_dir, "results.json")
    for viejo in (res_path, os.path.join(out_dir, "best.sol")):
        # Uno de una corrida anterior en la misma carpeta se leeria como si fuera
        # el de esta.
        if os.path.exists(viejo):
            os.remove(viejo)
    with open(os.path.join(out_dir, "gcg_stdout.log"), "w", encoding="utf-8") as f:
        rc = subprocess.run(cmd, cwd=REPO, stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0 or not os.path.exists(res_path):
        raise RuntimeError(f"gcg_run.py termino con codigo {rc} "
                           f"(ver {os.path.join(out_dir, 'gcg_stdout.log')})")
    with open(res_path, encoding="utf-8") as f:
        res = json.load(f)

    ok = False
    if res.get("sol_path") and os.path.exists(res["sol_path"]):
        sol = read_solution(res["sol_path"])
        cargadas = 0
        for et, v in sol.items():
            vd = por_var.get(et)
            if vd is None or vd.fixed:
                continue
            vd.set_value(round(v) if not vd.is_continuous() else v, skip_validation=True)
            cargadas += 1
        # Las variables con valor 0 que el .sol omite (si no vino con ceros)
        for et, vd in por_var.items():
            if et not in sol and not vd.fixed:
                vd.set_value(0, skip_validation=True)
        ok = cargadas > 0

    out = {"ok": ok, "status": res.get("status"), "error": res.get("error"),
           "primal": res.get("primal"),
           "dual": res.get("dual"), "dual_root": res.get("dual_root"),
           "gap": res.get("gap"), "nodes": res.get("nodes"),
           "pricing_stats": res.get("pricing_stats"),
           "time_gcg_sec": res.get("time_sec"), "time_total_sec": time.time() - t0,
           "time_export_sec": t_export, **{k: info[k] for k in
                                           ("n_blocks", "n_master", "master_frac", "mode",
                                            "split")}}
    if verbose:
        print(f"[GCG] {label} status={out['status']}  primal={out['primal']}  "
              f"dual={out['dual']}  ({out['time_total_sec']:.0f}s)", flush=True)
    return out
