"""Descomposicion por DIA dentro de un año, usada como generador de MIP start
del bloque anual del Nested Benders. Portada de battery_swapping_multiaño
(2026-10), que a su vez la tomo de la rama `battery_swapping`
(`run_descomposicion.py --parallel_days`).

POR QUE. En swap, en los años de mayor meta de produccion Gurobi llegaba al
timelimit SIN encontrar ninguna solucion factible del bloque anual, y ahi el
forward no puede continuar. Lo que falta no es cota, es un incumbente.

LA ESTRUCTURA QUE LO HACE POSIBLE, en carga on-board:

  - Casi todas las enteras (Y, Z, Z_charge, StartCharge/EndCharge,
    StartAssign/EndAssign) llevan indice de dia.
  - Las que no lo llevan son las que atan los dias entre si: la inversion
    (N_chargers, n_ssee_k/P_max_k, G_g, H), la potencia contratada P_pot y la
    degradacion de la flota (R, b_bar, S, N_ciclos, w_deg, D).
  - Las restricciones que cruzan dias son las de la degradacion: s_def suma la
    energia cargada de TODOS los dias, y de ahi salen la envolvente de
    McCormick (mccormick_*), n_ciclos_link y d_y_fade.
  - Cada dia es un ciclo cerrado (battery_boundary: B[.,d,0] = B[.,d,tf];
    bess_soc_init/bess_soc_cyclic): no hay estado que fluya de un dia al
    siguiente.
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

DEGRADACION. Se separa en dos:

  - la parte que depende de la ENERGIA del año (s_def, la envolvente de
    McCormick, n_ciclos_link y d_y_fade) necesita todos los dias: en un
    sub-bloque diario S quedaria en una fraccion. Se desactiva, y la
    reconcilia el pulido sobre el bloque anual.
  - la parte que NO depende del dia (reemplazo R, b_y_link_local) queda
    activa: el reemplazo sigue siendo una decision real, con su costo, y
    b_bar --que es lo unico de la degradacion que ve la operacion, via
    battery_lower/battery_upper-- queda acotado por la capacidad heredada.
"""
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pyomo.environ as pyo
from pyomo.environ import SolverFactory, value
from pyomo.opt import TerminationCondition
from pyomo.solvers.plugins.solvers.gurobi_direct import GurobiDirect

from src.optimization.decomposition.year_block import YearBlockBuilder
from src.optimization.functions import ObjectiveRules

# Variables compartidas por los dias de un año (las variables anuales de
# functions.BoundRules que no llevan indice de dia). La Fase 1 las decide libres
# y la agregacion toma el MAXIMO entre dias antes de la Fase 2.
#   N_chargers, Delta_N_chargers  cargadores; con el mismo prev en todos los
#               dias, max N y max Delta coinciden.
#   n_ssee_k, P_max_k  modulos y potencia de subestacion (P_max_k = paso *
#               n_ssee_k, ver ConstraintRules.ssee_discreta).
#   G_g, H      generacion y BESS.
#   P_pot       potencia contratada. Solo la atan los dias de punta
#               (91 <= d <= 244).
#   R, b_bar    reemplazo de baterias y capacidad de referencia del año.
# X no esta: en modo descompuesto es exogena (un Param).
SHARED_VARS = (
    "N_chargers", "Delta_N_chargers", "n_ssee_k", "P_max_k",
    "G_g", "H", "P_pot", "R", "b_bar",
)

# Parte de la degradacion que depende de la energia de todos los dias: s_def
# suma la energia cargada del año, y de ahi la envolvente de McCormick del
# bloque (YearBlockBuilder._add_degradation_state), el bilineal exacto si
# estuviera y d_y_fade.
ENERGY_DEGRADATION_CONSTRAINTS = (
    "s_def", "n_ciclos_link",
    "mccormick_lb1", "mccormick_lb2", "mccormick_ub1", "mccormick_ub2",
    "mccormick_energy", "d_y_fade",
)
# Sus variables: se fijan en el sub-bloque para que no floten, y NO se pasan al
# bloque anual -- ahi las determina el pulido con la energia de todos los dias.
ENERGY_DEGRADATION_VARS = ("S", "N_ciclos", "w_deg", "D")

# Nunca se trasladan del sub-bloque al anual. R (reemplazo de baterias) tampoco:
# los dias no ven la degradacion, asi que siempre eligen R = 0, y si la bateria
# heredada esta cerca del piso (B_L) el año necesita R = 1 -- con R fijo en 0 el
# pulido salia INFACTIBLE (medido en P_red, años 3 y 7 del forward de k=1). Sin
# trasladarlo, el pulido lo decide (un MILP de una sola binaria).
NOT_TRANSFERRED = set(ENERGY_DEGRADATION_VARS) | {"alpha", "R"}


def _fix_capacity_to_heritage(model, year):
    """Fija b_bar del sub-bloque en la capacidad HEREDADA (D_hat), en vez de
    dejarla libre en [B_L, D_hat].

    Dentro de un dia b_bar no tiene costo propio (la degradacion esta
    desactivada) y solo entra en los limites del SOC, asi que es indiferente y
    Gurobi la deja en su cota inferior: el PISO de degradacion (80 % de la
    nominal). Ese valor pasaba al bloque anual como punto de arranque y la
    bateria "perdia" ~90 kWh en un año, sin ninguna razon fisica (la
    degradacion real es ~4-5 kWh/año), lo que despues forzaba reemplazos. En
    swap no pasa porque cada swap repone b_bar (mas capacidad, menos swaps) y
    los dias la prefieren alta.

    Solo se fija si la capacidad heredada esta dentro de [B_L, B_U]: por debajo
    del piso el año necesita reemplazo y se deja libre (el pulido decide R).
    El primer año no tiene D_hat (b_bar ya viene fijo en la nominal)."""
    b_bar = getattr(model, "b_bar", None)
    d_hat = getattr(model, "D_hat", None)
    if b_bar is None or d_hat is None or year not in b_bar or b_bar[year].fixed:
        return
    cap = value(d_hat)
    lb, ub = b_bar[year].lb, b_bar[year].ub
    if (lb is not None and cap < lb - 1e-9) or (ub is not None and cap > ub + 1e-9):
        return
    b_bar[year].fix(cap)


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
        free_charging=year_block.free_charging,
        free_maintenance=year_block.free_maintenance,
        days_override=[day],
    )
    if heritage is not None:
        blk.set_heritage(heritage)
    # Forward estabilizado: la misma caja que el bloque anual. Sin ella el start
    # por dias podria elegir una inversion fuera de la caja y el bloque anual
    # lo rechazaria por infactible.
    caja = getattr(year_block, "_trust_region", None)
    if caja:
        blk.set_trust_region(**caja)
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
        free_charging=year_block.free_charging,
        free_maintenance=year_block.free_maintenance,
    )
    m.obj_dia = pyo.Objective(rule=_day_objective(obj_rules, n_dias),
                              sense=pyo.minimize)

    _neutralize_energy_degradation(m)
    _fix_capacity_to_heritage(m, year_block.year)
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
    """costos_del_año / n_dias + opex_del_dia.

    total_cost mezcla costos que se pagan una vez por año y sirven a todos los
    dias con costos del dia ya escalados por scaling_factor_op_cost. Con
    total_cost tal cual, cada sub-bloque pagaria el costo anual ENTERO contra
    una fraccion del beneficio y quedaria sesgado en contra de invertir.
    Prorrateando, la suma sobre los dias reconstruye el costo anual exacto:

        sum_d [anual/n + opex_d] = anual + sum_d opex_d

    Van con lo anual: la inversion, power_cost (cobra sobre P_pot, la
    potencia contratada del año), battery_replace_cost (el reemplazo es del
    año) y TAMBIEN gen_op_cost y bess_op_cost: son el O&M anual de la
    capacidad G/H, no dependen del dia. En battery_swapping_multiaño quedaron
    del lado del opex diario, asi que cada dia pagaba el O&M anual completo
    contra una fraccion de la inversion: la fase 1 quedaba sesgada en contra
    de la generacion y del BESS (solo afecta la calidad del MIP start, no la
    validez de nada). Aca se corrige.
    """
    capex = (obj_rules.inversion_cost,
             obj_rules.station_constant_cost,
             obj_rules.substation_investment_cost,
             obj_rules.gen_investment_cost,
             obj_rules.bess_investment_cost,
             obj_rules.power_cost,
             obj_rules.battery_replace_cost,
             obj_rules.gen_op_cost,
             obj_rules.bess_op_cost)
    opex = (obj_rules.lhd_charge_cost,)

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

# Dias en paralelo (--day_jobs). Los dias de un año son independientes, asi que
# cada fase resuelve sus dias a la vez, en HILOS y no en procesos: los modelos
# ya estan armados en este proceso, no hay que serializarlos ni duplicar
# memoria (lo que colgaba el equipo anterior con un Pool de `multiprocess`).
# Gurobi suelta el GIL en optimize(), asi que los solves corren de verdad en
# paralelo. Pyomo, en cambio, NO es thread-safe (pila global de TempfileManager,
# StaleFlagManager, symbol maps): todo lo que no es optimize() va bajo este
# candado, y _GurobiDirectParalelo lo suelta solo mientras Gurobi optimiza.
_PYOMO_LOCK = threading.Lock()


class _GurobiDirectParalelo(GurobiDirect):
    """GurobiDirect que libera _PYOMO_LOCK SOLO durante optimize(). Se crea con
    manage_env=True: un entorno de Gurobi por hilo, como pide Gurobi para
    optimizar modelos en paralelo.

    _apply_solver es el de GurobiDirect (pyomo 6.8.2) con el candado soltado
    unicamente alrededor de optimize(): antes se soltaba en todo _apply_solver,
    y eso dejaba StaleFlagManager.mark_all_as_stale() (estado global de Pyomo)
    corriendo sin candado."""

    def _apply_solver(self):
        from pyomo.solvers.plugins.solvers import gurobi_direct as _gd

        _gd.StaleFlagManager.mark_all_as_stale()
        self._solver_model.setParam('OutputFlag', 1 if self._tee else 0)
        if self._env_options:
            nuevas = {k: o for k, o in self.options.items()
                      if k not in self._env_options or self._env_options[k] != o}
        else:
            nuevas = self.options
        _gd._set_options(self._solver_model, nuevas)
        if self._version_major >= 5:
            for suffix in self._suffixes:
                if _gd.re.match(suffix, "dual"):
                    self._solver_model.setParam(_gd.gurobipy.GRB.Param.QCPDual, 1)
        _PYOMO_LOCK.release()
        try:
            self._solver_model.optimize(self._callback)
        finally:
            _PYOMO_LOCK.acquire()
        self._needs_updated = False
        return _gd.Bunch(rc=None, log=None)

    def cerrar(self):
        """close() pero soltando ANTES las referencias de Pyomo a objetos de
        gurobipy (Var/Constr) del modelo. Con close() a secas el modelo y el
        entorno se destruyen mientras esos objetos siguen vivos en los mapas
        del solver, y se liberan despues, cuando el recolector pasa -- en
        cualquier punto del programa. Es la causa probable de las violaciones
        de acceso (0xc0000005) de 2026-10-04/05: la ultima, con faulthandler,
        reviento en una list comprehension de Python puro (functions.py,
        production) al armar el modelo del reporte justo despues de los dias
        en paralelo, o sea memoria ya corrupta."""
        for nombre in ("_pyomo_var_to_solver_var_map", "_solver_var_to_pyomo_var_map",
                       "_pyomo_con_to_solver_con_map", "_solver_con_to_pyomo_con_map",
                       "_pyomo_sos_to_solver_sos_map", "_solver_sos_to_pyomo_sos_map",
                       "_vars_referenced_by_con", "_vars_referenced_by_obj",
                       "_referenced_variables"):
            mapa = getattr(self, nombre, None)
            if hasattr(mapa, "clear"):
                mapa.clear()
        self.close()


def _solve_day(model, solver_kwargs, label, warmstart=False, paralelo=False):
    """Resuelve un sub-bloque diario con MIPFocus=1: aca solo interesa
    ENCONTRAR una solucion, no cerrar la cota. Devuelve (ok, texto).

    `paralelo`: se llama desde un hilo de _solve_days (solo con Gurobi)."""
    solvername = solver_kwargs.get("solvername", "gurobi")
    t0 = time.time()
    if paralelo:
        with _PYOMO_LOCK:
            opt = _GurobiDirectParalelo(manage_env=True)
            try:
                return _solve_day_con(opt, model, solver_kwargs, label, warmstart,
                                      solvername, t0)
            finally:
                opt.cerrar()
    opt = SolverFactory("gurobi", solver_io="python") if solvername == "gurobi" \
        else SolverFactory(solvername)
    return _solve_day_con(opt, model, solver_kwargs, label, warmstart, solvername, t0)


def _solve_day_con(opt, model, solver_kwargs, label, warmstart, solvername, t0):
    if solvername == "gurobi":
        opt.options["MIPGap"] = solver_kwargs.get("gap", 0.01)
        opt.options["TimeLimit"] = solver_kwargs.get("timelimit", 600)
        opt.options["OutputFlag"] = 0
        for k, v in (solver_kwargs.get("extra_options") or {}).items():
            opt.options[k] = v
        opt.options["MIPFocus"] = 1
        # Diagnostico: ELMO_LOG_DIAS=<carpeta> deja el log de Gurobi de cada
        # dia (p.ej. para ver si acepto el MIP start: "Loaded user MIP start").
        # Solo con --day_jobs 1: en paralelo (manage_env) estas opciones van al
        # entorno y Pyomo apaga OutputFlag en el modelo, el log queda vacio.
        carpeta_log = os.environ.get("ELMO_LOG_DIAS")
        if carpeta_log:
            os.makedirs(carpeta_log, exist_ok=True)
            opt.options["OutputFlag"] = 1
            opt.options["LogToConsole"] = 0
            opt.options["LogFile"] = os.path.join(carpeta_log, label.replace(" ", "_") + ".log")

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


def _solve_days(trabajos, solver_kwargs, verbose=True):
    """Resuelve `trabajos` = [(modelo, label, warmstart)] y devuelve
    [(ok, texto)] en el mismo orden. Con solver_kwargs["jobs"] > 1 (y Gurobi)
    los resuelve en paralelo, repartiendo los hilos de la maquina entre ellos;
    si no, en serie como antes."""
    jobs = min(int(solver_kwargs.get("jobs") or 1), len(trabajos))
    if jobs <= 1 or solver_kwargs.get("solvername", "gurobi") != "gurobi":
        salida = []
        for m, label, warm in trabajos:
            ok, txt = _solve_day(m, solver_kwargs, label, warmstart=warm)
            if verbose:
                print(f"[DayDecomp]   {txt}")
                sys.stdout.flush()
            salida.append((ok, txt))
            if not ok:
                break          # en serie no tiene sentido seguir con los demas
        return salida

    hilos = max(1, (os.cpu_count() or jobs) // jobs)
    kw = dict(solver_kwargs)
    kw["extra_options"] = {**(solver_kwargs.get("extra_options") or {}), "Threads": hilos}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futuros = [ex.submit(_solve_day, m, kw, label, warm, True)
                   for m, label, warm in trabajos]
        salida = [f.result() for f in futuros]
    if verbose:
        for _, txt in salida:
            print(f"[DayDecomp]   {txt}")
        print(f"[DayDecomp]   {len(trabajos)} dias en paralelo ({jobs} x {hilos} "
              f"hilos): {time.time() - t0:.0f}s")
        sys.stdout.flush()
    return salida


def _enteras_del_incumbente(model, incumbente):
    """{nombre: {idx: valor}} de las enteras/binarias libres de `model` que
    trae `incumbente` (formato de YearBlockBuilder.extract_full_solution, del
    año entero: _set_values se queda solo con los indices del dia)."""
    out = {}
    for v in model.component_objects(pyo.Var, active=True):
        if v.name in NOT_TRANSFERRED:
            continue
        sol = incumbente.get(v.name)
        if not isinstance(sol, dict):
            continue
        vals = {idx: sol[idx] for idx, vd in v.items()
                if not vd.fixed and not vd.is_continuous() and idx in sol}
        if vals:
            out[v.name] = vals
    return out


def solve_year_by_days(year_block, heritage, solver_kwargs, verbose=True,
                       incumbente=None):
    """Fases 1 y 2 para el año de `year_block`. Devuelve (solucion, informe).

    `solucion`: {nombre_var: {idx: valor}} con la operacion de todos los dias
    y la infraestructura agregada, o None si alguna fase fallo.

    `incumbente`: solucion completa de ESTE año en la mejor trayectoria
    conocida (el centro de la caja), o None. Sus enteras van de MIP start a la
    fase 1 de cada dia, para que un dia cortado por --day_timelimit no termine
    peor que lo que ya se tenia. Medido en la v4 de 10 años: sin start, el año
    6 abrio una 2da bahia en las iteraciones 1 y 3 por UN dia (fase 1: 109.348
    y 91.237 contra ~17-34 mil de los otros tres) y el año paso de ~120 mil a
    373-464 mil. Con la herencia cambiada el start puede ser infactible: Gurobi
    intenta repararlo y si no, el dia parte de cero como antes.

    Dentro de cada fase los dias son independientes: con solver_kwargs["jobs"]
    > 1 se resuelven en paralelo (ver _solve_days); la agregacion es la unica
    barrera entre fases.
    """
    dias = list(year_block.time_series.days_within_year)
    y = year_block.year
    informe = {"year": y, "days": dias}
    t0 = time.time()

    # Fase 1
    bloques = [build_day_block(year_block, d, heritage=heritage) for d in dias]
    con_start = []
    for b in bloques:
        enteras_inc = _enteras_del_incumbente(b.model, incumbente) if incumbente else {}
        _set_values(b.model, enteras_inc)
        con_start.append(sum(len(v) for v in enteras_inc.values()))
    if verbose and any(con_start):
        print(f"[DayDecomp]   fase 1 con MIP start del incumbente "
              f"({', '.join(f'd{d}: {n:,}' for d, n in zip(dias, con_start))} enteras)")
    resultados = _solve_days([(b.model, f"y{y} d{d} fase1", n > 0)
                              for d, b, n in zip(dias, bloques, con_start)],
                             solver_kwargs, verbose=verbose)
    for ok, txt in resultados:
        if not ok:
            return None, {**informe, "error": txt}
    compartidas = [read_shared(b.model) for b in bloques]
    enteras_fase1 = {d: _integer_values(b.model) for d, b in zip(dias, bloques)}
    del bloques

    agregada = aggregate_shared(compartidas, verbose=verbose)

    # Fase 2
    bloques = []
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
        bloques.append(blk)
    resultados = _solve_days([(b.model, f"y{y} d{d} fase2", bool(enteras_fase1.get(d)))
                              for d, b in zip(dias, bloques)],
                             solver_kwargs, verbose=verbose)
    for ok, txt in resultados:
        if not ok:
            return None, {**informe, "error": txt}

    solucion = {}
    for blk in bloques:
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


def day_warm_start(year_block, heritage, solver_kwargs, verbose=True, incumbente=None):
    """Punto de entrada para el forward: descomposicion por dia + pulido.
    Devuelve True si el bloque anual quedo con un MIP start completo.
    `incumbente`: ver solve_year_by_days."""
    t0 = time.time()
    if verbose:
        print(f"[DayDecomp] año {year_block.year}: MIP start por dias "
              f"(fase 1 libre, max, fase 2 fija, pulido)...")
        sys.stdout.flush()
    solucion, informe = solve_year_by_days(year_block, heritage, solver_kwargs,
                                           verbose=verbose, incumbente=incumbente)
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
