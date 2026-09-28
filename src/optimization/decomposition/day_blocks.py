"""Descomposicion por DIA dentro de un año, portada de la rama
`battery_swapping` (`run_descomposicion.py --parallel_days`), usada como
generador de MIP start del bloque anual del Nested Benders.

POR QUE. El bloque anual tiene ~51.500 variables enteras (4 dias
representativos x 180 intervalos de 8 min x 4 eLHD). En los años de mayor meta
de produccion Gurobi llega al timelimit SIN encontrar ninguna solucion
factible, y ahi el forward no puede continuar. Lo que falta no es cota, es un
incumbente.

LA ESTRUCTURA QUE LO HACE POSIBLE. Medido sobre este modelo (1 año, 4 dias):

  - 51.528 de las 51.540 enteras (99,98%) llevan indice de dia.
  - Solo 23 variables no lo llevan, y son las que atan los dias entre si:
    la inversion, la flota, la potencia contratada P_pot y la degradacion.
  - Una SOLA restriccion cruza dias: energy_consumed_def.
  - Cada dia es un ciclo cerrado (battery_energy_conservation,
    bess_soc_init): no hay estado que fluya de un dia al siguiente.
  - production(y, d, j) impone la meta POR DIA.

Fijadas esas pocas variables, los dias se separan por completo. A diferencia
de los años --una cadena dinamica, por eso Nested Benders-- los dias
representativos son MUESTRAS: no hay estado, solo variables compartidas.

EL ESQUEMA (el de la rama battery_swapping):

  Fase 1      cada dia por separado, infraestructura libre.
  Agregacion  por cada variable compartida, el MAXIMO entre dias.
  Fase 2      cada dia otra vez, con la infraestructura fija.
  Pulido      (nuevo) la operacion de los cuatro dias se carga en el bloque
              ANUAL, se fijan sus enteras y se resuelve el LP que queda: eso
              reconcilia la degradacion y el costo futuro alpha, y deja una
              solucion completa y consistente para entregarle a Gurobi.

LOS SUB-BLOQUES SON YearBlockBuilder CON days_override=[d]. No se arman a mano
porque con years_override las link_*_stock se omiten y el acople
N = prev + Delta lo agrega YearBlockBuilder: sin el, N_bays queda desatado de
Delta_N_bays, que es sobre lo que se cobra la inversion, y la infraestructura
sale GRATIS. Reusando el builder, cada dia hereda exactamente el mismo estado
que el bloque anual.

ES UNA HEURISTICA. El maximo entre dias es factible para todos los dias pero
puede sobredimensionar. Da un UB, nunca un LB, y por eso solo se usa como
MIP start.

DEGRADACION. Lo unico que no existia en el modelo de regimen. Se separa en dos:

  - la parte que depende de la ENERGIA del año (energy_consumed_def, los
    ciclos y d_y_fade) necesita los cuatro dias: en un sub-bloque diario
    EnergyConsumed quedaria en la cuarta parte. Se desactiva, y la reconcilia
    el pulido sobre el bloque anual.
  - la parte que NO depende del dia (flota, reemplazo Z_repl = R * flota,
    b_y_link_local) queda activa: el reemplazo R sigue siendo una decision
    real, con su costo, y b_bar --que es lo unico de la degradacion que ve la
    operacion, via P_bbar_zagg-- queda acotado por la capacidad heredada.
"""
import math
import sys
import time

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value
from pyomo.opt import TerminationCondition

from src.optimization.decomposition.year_block import YearBlockBuilder
from src.optimization.functions import ObjectiveRules

# Variables compartidas por los dias de un año. La Fase 1 las decide libres y la
# agregacion toma el MAXIMO entre dias antes de la Fase 2.
#
# Amplia la INFRA_VARS de la rama battery_swapping --('N_chargers', 'N_bays',
# 'N_batteries', 'X')-- con lo que el modelo de regimen no tenia:
#   Delta_N_*   el incremento del año, sobre el que se cobra la inversion. Con
#               el mismo prev en todos los dias, max N y max Delta coinciden.
#   n_ssee_k    modulos de subestacion.
#   G_g, H      generacion y BESS (este escenario tiene Generators/Storage).
#   P_pot       potencia contratada. Solo la atan los dias de punta
#               (91 <= d <= 244): de [15, 105, 196, 288], el 105 y el 196.
#   R, b_bar    reemplazo de baterias y capacidad de referencia del año.
# X no esta: en modo descompuesto es exogena (un Param).
SHARED_VARS = (
    "N_bays", "N_chargers", "N_batteries",
    "Delta_N_bays", "Delta_N_chargers", "Delta_N_batteries",
    "n_ssee_k", "G_g", "H", "P_pot", "R", "b_bar",
)

# Parte de la degradacion que depende de la energia de los cuatro dias.
ENERGY_DEGRADATION_CONSTRAINTS = (
    "energy_consumed_def", "n_total_def", "n_ciclos_link",
    "mc_n_total_lb1", "mc_n_total_lb2", "mc_n_total_ub1", "mc_n_total_ub2",
    "mc_w_deg_energy", "mc_w_deg_lb1", "mc_w_deg_lb2", "mc_w_deg_ub1",
    "mc_w_deg_ub2", "d_y_fade",
)
# Sus variables: se fijan en el sub-bloque para que no floten, y NO se pasan al
# bloque anual -- ahi las determina el pulido con la energia de los 4 dias.
ENERGY_DEGRADATION_VARS = ("EnergyConsumed", "N_ciclos", "N_total", "w_deg", "D")

# Nunca se trasladan del sub-bloque al anual.
NOT_TRANSFERRED = set(ENERGY_DEGRADATION_VARS) | {"alpha"}


# --------------------------------------------------------------------------
# Construccion del sub-bloque diario
# --------------------------------------------------------------------------

def build_day_block(year_block, day, heritage=None, fixed_shared=None):
    """Sub-bloque de UN dia, "hermano" del bloque anual `year_block`: misma
    configuracion (año, regimen, X exogena, McCormick), mismo acople de estado,
    solo que con days_override=[day].

    :param heritage: el x_hat del año anterior, el mismo que recibe el bloque
        anual (None en el primer año).
    :param fixed_shared: {nombre: {idx: valor}} de SHARED_VARS a fijar (Fase 2).
    """
    blk = YearBlockBuilder(
        year_block.mine_system, year_block.time_series,
        year=year_block.year, is_last_year=year_block.is_last_year,
        exogenous_stations=year_block.exogenous_stations,
        autonomous_mode=year_block.autonomous_mode,
        mccormick_degradation=year_block.mccormick_degradation,
        free_charging=year_block.free_charging,
        free_maintenance=year_block.free_maintenance,
        days_override=[day],
    )
    if heritage is not None:
        blk.set_heritage(heritage)
    m = blk.model

    # El costo futuro no es de este dia: sin cortes quedaria libre. Se fija.
    m.alpha.fix(0.0)

    # Objetivo con el CAPEX prorrateado (ver _day_objective).
    m.obj.deactivate()
    n_dias = max(1, len(year_block.time_series.days_within_year))
    obj_rules = ObjectiveRules(
        year_block.mine_system, year_block.time_series,
        years_override=[year_block.year], days_override=[day],
        exogenous_stations={(k, year_block.year): v
                            for k, v in year_block.exogenous_stations.items()},
    )
    m.obj_dia = pyo.Objective(rule=_day_objective(obj_rules, n_dias),
                              sense=pyo.minimize)

    _neutralize_energy_degradation(m)
    _inherit_bounds(year_block.model, m)
    if fixed_shared:
        fix_shared(m, fixed_shared)
    return blk


def _inherit_bounds(annual, day_model):
    """Copia al sub-bloque las cotas que el bloque anual tiene de mas sobre
    las SHARED_VARS. Hoy la que importa es la del presolve de capacidad
    (n_ssee_k >= n_min en el año 1, ver driver._capacity_presolve): un
    sub-bloque recien construido no la tiene, el maximo entre dias podria
    quedar por debajo, y Gurobi rechazaria el start por violar la cota."""
    for nombre in SHARED_VARS:
        src = getattr(annual, nombre, None)
        dst = getattr(day_model, nombre, None)
        if not isinstance(src, pyo.Var) or not isinstance(dst, pyo.Var):
            continue
        for idx, vd in src.items():
            if idx not in dst or dst[idx].fixed:
                continue
            if vd.lb is not None and (dst[idx].lb is None or vd.lb > dst[idx].lb):
                dst[idx].setlb(vd.lb)
            if vd.ub is not None and (dst[idx].ub is None or vd.ub < dst[idx].ub):
                dst[idx].setub(vd.ub)


def _day_objective(obj_rules, n_dias):
    """capex / n_dias + opex_del_dia.

    total_cost mezcla costos que se pagan una vez y sirven a los cuatro dias
    (inversion) con costos del dia ya escalados por scaling_factor_op_cost
    (91,25). Con total_cost tal cual, cada sub-bloque pagaria el capex ENTERO
    contra la cuarta parte del beneficio y quedaria sesgado en contra de
    invertir. Prorrateando, la suma sobre los dias reconstruye el costo anual
    exacto:

        sum_d [capex/n + opex_d] = capex + sum_d opex_d

    peak_power_cost va con el capex porque cobra sobre P_pot, la potencia
    contratada del año. battery_replace_cost tambien: el reemplazo es del año.
    """
    capex = (obj_rules.inversion_cost,
             obj_rules.station_constant_cost,
             obj_rules.substation_investment_cost,
             obj_rules.gen_investment_cost,
             obj_rules.bess_investment_cost,
             obj_rules.peak_power_cost,
             obj_rules.battery_replace_cost)
    opex = (obj_rules.lhd_charge_cost_bs,
            obj_rules.gen_op_cost,
            obj_rules.bess_op_cost)

    def _obj(m):
        return sum(f(m) for f in capex) / n_dias + sum(f(m) for f in opex)
    return _obj


def _neutralize_energy_degradation(model):
    for nombre in ENERGY_DEGRADATION_CONSTRAINTS:
        comp = getattr(model, nombre, None)
        if comp is not None:
            comp.deactivate()
    for nombre in ENERGY_DEGRADATION_VARS:
        comp = getattr(model, nombre, None)
        if comp is None:
            continue
        for vd in comp.values():
            if not vd.fixed:
                vd.fix(vd.lb if vd.lb is not None else 0.0)


# --------------------------------------------------------------------------
# Agregacion y fijado
# --------------------------------------------------------------------------

def read_shared(model):
    out = {}
    for nombre in SHARED_VARS:
        comp = getattr(model, nombre, None)
        if comp is None or not isinstance(comp, pyo.Var):
            continue
        vals = {idx: value(vd, exception=False) for idx, vd in comp.items()}
        vals = {i: v for i, v in vals.items() if v is not None}
        if vals:
            out[nombre] = vals
    return out


def aggregate_shared(por_dia, verbose=True):
    """MAXIMO entre dias, indice por indice. La infraestructura tiene que
    servir al dia mas exigente: por eso la Fase 2 es factible en todos, y por
    eso tambien puede sobredimensionar (UB, no LB)."""
    agg = {}
    for sol in por_dia:
        for nombre, vals in sol.items():
            dest = agg.setdefault(nombre, {})
            for idx, v in vals.items():
                dest[idx] = max(dest.get(idx, v), v)
    if verbose and agg:
        partes = []
        for nombre in SHARED_VARS:
            if nombre in agg:
                vals = ", ".join(f"{v:,.4g}" for _, v in sorted(
                    agg[nombre].items(), key=lambda kv: str(kv[0])))
                partes.append(f"{nombre}={vals}")
        print(f"[DayDecomp]   agregado (max entre dias): {'  '.join(partes)}")
    return agg


def fix_shared(model, fixed_shared):
    """Fija las SHARED_VARS. Las enteras se redondean hacia ARRIBA: un N_bays
    de 2,9999997 con ruido de IntFeasTol no puede quedar en 2."""
    for nombre, vals in fixed_shared.items():
        comp = getattr(model, nombre, None)
        if comp is None or not isinstance(comp, pyo.Var):
            continue
        for idx, v in vals.items():
            if idx not in comp:
                continue
            vd = comp[idx]
            if vd.fixed:
                continue          # b_bar/R del primer año: los fija BoundRules
            if not vd.is_continuous():
                v = math.ceil(v - 1e-6)
            vd.fix(v)


# --------------------------------------------------------------------------
# Resolucion
# --------------------------------------------------------------------------


def _integer_values(model):
    """{nombre: {idx: valor}} de las enteras/binarias libres con valor."""
    out = {}
    for v in model.component_objects(pyo.Var, active=True):
        if v.name in NOT_TRANSFERRED:
            continue
        vals = {idx: vd.value for idx, vd in v.items()
                if not vd.fixed and not vd.is_continuous() and vd.value is not None}
        if vals:
            out[v.name] = vals
    return out


def _set_values(model, sol):
    for nombre, vals in sol.items():
        comp = getattr(model, nombre, None)
        if comp is None or not isinstance(comp, pyo.Var):
            continue
        for idx, val in vals.items():
            if idx in comp and not comp[idx].fixed:
                comp[idx].set_value(round(val), skip_validation=True)

def _solve_day(model, solver_kwargs, label, warmstart=False):
    """Resuelve un sub-bloque diario con MIPFocus=1: aca solo interesa
    ENCONTRAR una solucion, no cerrar la cota. Devuelve (ok, texto)."""
    solvername = solver_kwargs.get("solvername", "gurobi")
    opt = SolverFactory("gurobi", solver_io="python") if solvername == "gurobi" \
        else SolverFactory(solvername)
    if solvername == "gurobi":
        opt.options["MIPGap"] = solver_kwargs.get("gap", 0.01)
        opt.options["TimeLimit"] = solver_kwargs.get("timelimit", 600)
        opt.options["OutputFlag"] = 0
        for k, v in (solver_kwargs.get("extra_options") or {}).items():
            opt.options[k] = v
        opt.options["MIPFocus"] = 1

    t0 = time.time()
    kw = {"warmstart": True} if (warmstart and solvername == "gurobi") else {}
    res = opt.solve(model, load_solutions=False, **kw)
    cond = res.solver.termination_condition
    ub = res.problem.upper_bound
    if cond in (TerminationCondition.infeasible,
                TerminationCondition.infeasibleOrUnbounded):
        return False, f"{label}: INFACTIBLE"
    if ub is None or ub in (float("inf"), float("-inf")):
        return False, f"{label}: {cond}, sin solucion en {time.time() - t0:.0f}s"
    model.solutions.load_from(res)
    return True, f"{label}: {ub:,.2f}  ({cond}, {time.time() - t0:.0f}s)"


def solve_year_by_days(year_block, heritage, solver_kwargs, verbose=True):
    """Fases 1 y 2 para el año de `year_block`. Devuelve (solucion, informe).

    `solucion`: {nombre_var: {idx: valor}} con la operacion de todos los dias
    y la infraestructura agregada, o None si alguna fase fallo.

    Los dias van en SERIE: cada solve ya usa todos los cores via Gurobi, y un
    Pool de `multiprocess` con workers de cientos de MB es justo lo que cuelga
    esta maquina (ver --block_build_jobs).
    """
    dias = list(year_block.time_series.days_within_year)
    y = year_block.year
    informe = {"year": y, "days": dias}
    t0 = time.time()

    # Fase 1
    compartidas = []
    enteras_fase1 = {}
    for d in dias:
        blk = build_day_block(year_block, d, heritage=heritage)
        ok, txt = _solve_day(blk.model, solver_kwargs, f"y{y} d{d} fase1")
        if verbose:
            print(f"[DayDecomp]   {txt}")
            sys.stdout.flush()
        if not ok:
            return None, {**informe, "error": txt}
        compartidas.append(read_shared(blk.model))
        enteras_fase1[d] = _integer_values(blk.model)

    agregada = aggregate_shared(compartidas, verbose=verbose)

    # Fase 2
    solucion = {}
    for d in dias:
        blk = build_day_block(year_block, d, heritage=heritage, fixed_shared=agregada)
        # Start de la fase 2 = la solucion de la fase 1 del MISMO dia. Sigue
        # siendo factible: el maximo entre dias solo AGREGA capacidad. Medido: en
        # la v2 el dia 105 (punta) no encontro NINGUNA solucion en 60 s en la
        # fase 2 arrancando de cero, aunque la fase 1 del mismo dia si tenia una,
        # y eso termino matando la corrida. Solo se pasan las ENTERAS: las
        # continuas atadas a la infraestructura (p.ej. P_bbar_zagg a b_bar)
        # pueden necesitar reajuste, y eso lo completa Gurobi.
        _set_values(blk.model, enteras_fase1.get(d, {}))
        ok, txt = _solve_day(blk.model, solver_kwargs, f"y{y} d{d} fase2",
                             warmstart=bool(enteras_fase1.get(d)))
        if verbose:
            print(f"[DayDecomp]   {txt}")
            sys.stdout.flush()
        if not ok:
            return None, {**informe, "error": txt}
        for v in blk.model.component_objects(pyo.Var, active=True):
            if v.name in NOT_TRANSFERRED:
                continue
            dest = solucion.setdefault(v.name, {})
            for idx, vd in v.items():
                val = value(vd, exception=False)
                if val is not None:
                    dest[idx] = val

    informe["time_sec"] = time.time() - t0
    return solucion, informe


# --------------------------------------------------------------------------
# Carga en el bloque anual + pulido
# --------------------------------------------------------------------------

def load_and_polish(year_block, solucion, solver_kwargs, verbose=True):
    """Carga `solucion` en el bloque ANUAL, fija sus enteras y resuelve el LP
    que queda. Devuelve (ok, costo).

    El LP reconcilia todo lo que los sub-bloques no podian ver: la degradacion
    con la energia de los cuatro dias, y alpha contra los cortes de Benders
    acumulados. Si es factible, el bloque queda con una solucion COMPLETA y
    consistente, que es lo que Pyomo le pasa a Gurobi como Start (todas las
    variables con valor, ver GurobiDirect._warm_start). Con eso Gurobi tiene
    incumbente desde el nodo cero, y aunque llegue al timelimit sin mejorar
    devuelve al menos esta solucion.

    Las enteras que se fijan aca se desfijan al terminar, sea cual sea el
    resultado: el bloque sigue siendo el MILP de siempre.
    """
    m = year_block.model
    cargados, fijadas = 0, []
    for nombre, vals in solucion.items():
        comp = getattr(m, nombre, None)
        if comp is None or not isinstance(comp, pyo.Var):
            continue
        for idx, v in vals.items():
            if idx not in comp:
                continue
            vd = comp[idx]
            if vd.fixed:
                continue
            if not vd.is_continuous():
                v = round(v)
                # fix() ignora las cotas: si el valor quedara fuera, el LP
                # podria salir factible igual y el start violaria la cota.
                if vd.lb is not None and v < vd.lb:
                    v = math.ceil(vd.lb - 1e-9)
                if vd.ub is not None and v > vd.ub:
                    v = math.floor(vd.ub + 1e-9)
                vd.fix(v)
                fijadas.append(vd)
            else:
                vd.set_value(v)
            cargados += 1

    try:
        opt = SolverFactory("gurobi", solver_io="python")
        opt.options["OutputFlag"] = 0
        opt.options["TimeLimit"] = solver_kwargs.get("timelimit", 600)
        t0 = time.time()
        res = opt.solve(m, load_solutions=False)
        cond = res.solver.termination_condition
        if cond != TerminationCondition.optimal:
            if verbose:
                print(f"[DayDecomp]   pulido sobre el bloque anual: {cond} -- "
                      f"se sigue SIN MIP start")
            return False, None
        m.solutions.load_from(res)
        costo = value(m.obj)
        if verbose:
            print(f"[DayDecomp]   pulido sobre el bloque anual: {cargados:,} "
                  f"valores, {len(fijadas):,} enteras fijas, LP optimo en "
                  f"{time.time() - t0:.0f}s -> incumbente {costo:,.2f}")
        return True, costo
    finally:
        for vd in fijadas:
            vd.unfix()


def day_warm_start(year_block, heritage, solver_kwargs, verbose=True):
    """Punto de entrada para el forward: descomposicion por dia + pulido.
    Devuelve True si el bloque anual quedo con un MIP start completo."""
    t0 = time.time()
    if verbose:
        print(f"[DayDecomp] año {year_block.year}: MIP start por dias "
              f"(fase 1 libre, max, fase 2 fija, pulido)...")
        sys.stdout.flush()
    solucion, informe = solve_year_by_days(year_block, heritage, solver_kwargs,
                                           verbose=verbose)
    if solucion is None:
        if verbose:
            print(f"[DayDecomp] año {year_block.year}: sin MIP start "
                  f"({informe.get('error')})")
        return False
    ok, _ = load_and_polish(year_block, solucion, solver_kwargs, verbose=verbose)
    if verbose:
        print(f"[DayDecomp] año {year_block.year}: {'listo' if ok else 'fallo'} "
              f"en {time.time() - t0:.0f}s")
    return ok
