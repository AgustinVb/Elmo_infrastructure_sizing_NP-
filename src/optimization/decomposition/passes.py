import logging
import os
import time

import pyomo.environ as pyo
from pyomo.environ import value, SolverFactory, TransformationFactory
from pyomo.opt import TerminationCondition, SolverStatus

from src.optimization.decomposition.cuts import BendersCutManager
from src.optimization.decomposition.macroblocks import (
    aggregate_degradation,
    coordinate_b_bar,
)

_ACCEPTABLE = (
    TerminationCondition.optimal,
    TerminationCondition.maxTimeLimit,
    TerminationCondition.feasible,
)

# Bandera de interrupcion (Ctrl+C) compartida entre driver.py y _solve().
# NECESARIA (una bandera Python-side no alcanza sola -- ver el chequeo
# adicional result.solver.status == SolverStatus.aborted en _solve()):
# un SIGINT que llega en medio de un solve activo de Gurobi puede ser
# capturado por el propio Gurobi a nivel NATIVO (imprime su propio
# "Interrupt request received"), y en ese caso el signal.signal() de
# Python de driver.py NUNCA LLEGA A EJECUTARSE -- Gurobi parece instalar
# su propio manejador de señal mientras optimiza, reemplazando el de
# Python por la duracion del solve (confirmado con una corrida real: el
# print "Ctrl+C detectado" de nuestro handler no aparecio en el log, pero
# igual crasheo con RuntimeError). Por eso _solve() no puede confiar SOLO
# en esta bandera -- necesita ADEMAS chequear result.solver.status
# directamente (ver mas abajo). Esta bandera sigue sirviendo para el caso
# en que la interrupcion SI cae en codigo Python puro (entre solves), que
# se resuelve mas rapido sin esperar a que termine el proximo solve.
_interrupt_requested = False


def request_interrupt():
    global _interrupt_requested
    _interrupt_requested = True


def clear_interrupt():
    global _interrupt_requested
    _interrupt_requested = False


# Archivo centinela de parada manual (portado de battery_swapping_multiaño).
# Hace falta ADEMAS del Ctrl+C porque en PowerShell un Ctrl+C sobre un pipeline
# con Tee-Object mata el proceso sin que Python vea nada. Se chequea DESPUES de
# cada solve -- no durante --, asi que la parada tarda a lo sumo un solve de
# bloque (--solve_timelimit) en hacerse efectiva; a cambio, el bloque en curso
# termina limpio y el driver conserva la ultima pasada forward COMPLETA, que es
# la unica que deja un UB valido.
_stop_file = None


def set_stop_file(path):
    global _stop_file
    _stop_file = path


def stop_file_requested():
    return _stop_file is not None and os.path.exists(_stop_file)


# Plazo duro de --max_hours (instante absoluto, time.time()). Es la red de
# seguridad: el driver ya no empieza una iteracion que no alcanza a terminar,
# asi que esto solo salta si una iteracion se alarga mas de lo estimado. Se
# chequea en el mismo lugar que el STOP y con el mismo efecto.
_deadline = None


def set_deadline(t):
    global _deadline
    _deadline = t


def deadline_passed():
    return _deadline is not None and time.time() > _deadline

# El backend directo de Gurobi (solver_io="python") avisa "Cannot get duals
# for MIP." cada vez que resuelve un bloque como MILP con un Suffix de
# duales declarado (aunque no se le pidan duales en ese solve -- son para
# el backward pass, no el forward). Es esperado y no indica un problema;
# se silencia subiendo el nivel del logger que lo emite.
logging.getLogger("pyomo.solvers").setLevel(logging.ERROR)


class SubproblemInfeasible(RuntimeError):
    """El subproblema no tiene solucion. Se distingue de cualquier otro fallo
    del solver porque es el unico caso que el forward puede recuperar con un
    corte de factibilidad."""


class SubproblemNoIncumbent(RuntimeError):
    """TimeLimit sin ninguna solucion factible: NO es infactibilidad (Gurobi no
    la probo, y suele dar cota dual finita), es falta de tiempo para encontrar
    un primer punto. Se distingue para que el forward pueda reintentar con mas
    tiempo (o con un MIP start por dias) en vez de cortar la corrida con un
    error criptico de Pyomo mucho despues."""


def _cota_dual(result):
    """Cota DUAL del solve (ObjBound de Gurobi), o None si no hay una finita.
    Es la que hay que usar como constante de un corte: subestima el optimo
    aunque el MILP se haya cortado por tiempo o cerrado con MIPGap > 0, a
    diferencia del valor del incumbente, que lo SOBREestima."""
    lb = getattr(result.problem, "lower_bound", None)
    if lb is None or lb != lb or lb in (float("inf"), float("-inf")):
        return None
    return float(lb)


def _tiene_incumbente(result):
    ub = getattr(result.problem, "upper_bound", None)
    return ub is not None and ub == ub and ub not in (float("inf"), float("-inf"))


class _YearInfeasible(Exception):
    """Interna: un anio del forward no tiene solucion con el estado que
    recibio. Lleva el indice del bloque y los estados ya calculados, para que
    run() arme el corte de factibilidad sin volver a resolver nada."""

    def __init__(self, index, x_hat_by_year):
        super().__init__(f"bloque {index} infactible")
        self.index = index
        self.x_hat_by_year = x_hat_by_year


def _fmt_bound(v):
    if v is None or v in (float("inf"), float("-inf")):
        return "(sin cota aun)"
    return f"{v:,.2f}"


def _bounds_tag(current_ub, current_lb):
    return f"[UB={_fmt_bound(current_ub)}  LB={_fmt_bound(current_lb)}]"


def _solve(model, solvername="gurobi", gap=0.001, timelimit=900, tee=False, label="",
           extra_options=None, require_optimal=False, load_solutions=True,
           warmstart=False):
    """load_solutions=False deja las variables del modelo como estaban y solo
    devuelve el resultado (cotas primal/dual, condicion de termino). Lo usa el
    presolve de capacidad (driver._capacity_presolve), al que le alcanza con
    la cota dual de Gurobi (result.problem.lower_bound) y que no quiere pisar
    el punto de arranque del bloque -- y ademas asi un MILP que llega al
    TimeLimit sin incumbente sigue devolviendo su cota en vez de fallar al
    cargar una solucion que no existe.

    warmstart=True: Pyomo pasa como Start de Gurobi TODAS las variables con
    valor (GurobiDirect._warm_start). Lo usa el forward cuando el bloque trae
    un MIP start completo de la descomposicion por dias."""
    if solvername == "gurobi":
        opt = SolverFactory("gurobi", solver_io="python")
        opt.options["MIPGap"] = gap
        opt.options["TimeLimit"] = timelimit
        opt.options["OutputFlag"] = 0
        if extra_options:
            # Camino B (Lagrangeano, degradacion_descomposicion_mccormick.md
            # sec. 4.2): la fisica exacta de degradacion es bilineal no
            # convexa (mismo flag que usa opt_model.py para el monolitico).
            for opt_name, opt_val in extra_options.items():
                opt.options[opt_name] = opt_val
    else:
        opt = SolverFactory(solvername)

    solve_kw = {"warmstart": True} if warmstart else {}
    result = opt.solve(model, tee=tee, load_solutions=load_solutions, **solve_kw)
    # Ver comentario junto a _interrupt_requested. Dos señales, no una sola
    # -- confirmado con una corrida real que la primera no alcanza: cuando
    # el SIGINT llega en medio de un solve activo, Gurobi lo atrapa el
    # mismo a nivel nativo (imprime su propio "Interrupt request received")
    # SIN que nuestro signal.signal() de Python llegue siquiera a
    # ejecutarse -- ahi _interrupt_requested nunca se activa porque el
    # handler nunca corrio.
    #
    # PERO result.solver.status == SolverStatus.aborted NO es exclusivo de
    # una interrupcion real: pyomo/solvers/plugins/solvers/gurobi_direct.py
    # le asigna el MISMO SolverStatus.aborted tanto a grb.INTERRUPTED (Ctrl+C
    # real, termination_condition=error) como a grb.TIME_LIMIT (el bloque
    # llego al TimeLimit configurado SIN cerrar el MIPGap -- un desenlace
    # normal y ya contemplado como aceptable via TerminationCondition.
    # maxTimeLimit en _ACCEPTABLE, arriba). Confiar solo en `status` hacia
    # que cualquier año cuyo MILP no cerrara el gap en el TimeLimit fuera
    # tratado como si el usuario hubiera apretado Ctrl+C, abortando toda la
    # corrida (bug real: reproducido con el año 7 del escenario 960kW_2dias,
    # que sistematicamente no cierra el gap en el TimeLimit configurado).
    # La señal confiable de interrupcion real es termination_condition
    # (error/otro no-aceptable), no status por si solo.
    aborted_by_interrupt = (
        result.solver.status == SolverStatus.aborted
        and result.solver.termination_condition not in _ACCEPTABLE
    )
    if _interrupt_requested or aborted_by_interrupt:
        raise KeyboardInterrupt(f"Interrumpido por el usuario durante el solve ({label}).")
    if stop_file_requested():
        print(f"[NestedBenders] parada manual pedida ({_stop_file}): se corta despues "
              f"de {label} y se conserva la mejor solucion completa.", flush=True)
        raise KeyboardInterrupt(f"Parada manual por archivo STOP (despues de {label}).")
    if deadline_passed():
        print(f"[NestedBenders] se cumplio el tope de tiempo (--max_hours): se corta "
              f"despues de {label} y se conserva la mejor solucion completa.", flush=True)
        raise KeyboardInterrupt(f"Tope de tiempo cumplido (despues de {label}).")
    if result.solver.termination_condition == TerminationCondition.maxTimeLimit:
        ub = result.problem.upper_bound
        lb = result.problem.lower_bound
        if not _tiene_incumbente(result):
            # TimeLimit con SolCount=0: las variables del modelo quedaron SIN
            # VALOR. Si se deja pasar, el reventon aparece mucho despues y lejos
            # de su causa ("No value for uninitialized NumericValue object" al
            # leer el objetivo). Con load_solutions=False no importa: el
            # llamador solo quiere la cota dual.
            if load_solutions:
                cota = "sin cota dual" if lb is None else f"cota dual {lb:,.2f}"
                raise SubproblemNoIncumbent(
                    f"{label}: TimeLimit ({timelimit}s) alcanzado SIN ninguna "
                    f"solucion factible ({cota}). Subir --solve_timelimit o usar "
                    f"--block_mip_focus 1 para que Gurobi priorice encontrar "
                    f"incumbentes."
                )
            print(f"[NestedBenders] {label}  TimeLimit sin incumbente "
                  f"(solo cota dual{'' if lb is None else f' {lb:,.2f}'})")
        elif ub not in (None, 0) and lb is not None:
            gap_rel = abs(ub - lb) / abs(ub)
            print(f"[NestedBenders] {label}  TimeLimit alcanzado sin cerrar MIPGap: "
                  f"UB={ub:,.2f}  LB={lb:,.2f}  gap={gap_rel:.4%}")
    if result.solver.termination_condition in (TerminationCondition.infeasible,
                                               TerminationCondition.infeasibleOrUnbounded):
        raise SubproblemInfeasible(
            f"Subproblema infactible ({label}): {result.solver.termination_condition}"
        )
    if result.solver.termination_condition not in _ACCEPTABLE:
        raise RuntimeError(
            f"Solve no llego a una solucion aceptable ({label}): "
            f"{result.solver.termination_condition}"
        )
    if require_optimal and result.solver.termination_condition != TerminationCondition.optimal:
        # Los cortes se arman con la constante del LP (phi_lp) emparejada con los
        # duales. La constante VALIDA es el valor objetivo DUAL, que coincide con
        # el primal solo en el optimo: si el LP se corta por tiempo queda un
        # primal por ENCIMA del optimo junto a un mu dual-factible, y el corte
        # resultante es demasiado fuerte -- puede cortar estados factibles. Se
        # manifiesta despues como LB > UB, lejos de su causa.
        raise RuntimeError(
            f"El LP de {label} no llego al optimo ({result.solver.termination_condition}): "
            f"un corte armado con su valor primal podria ser invalido. Subir "
            f"timelimit para los solves de LP."
        )
    return result


class ForwardPass(object):
    """Documento sec. 7.1: resuelve cada bloque como MILP completo en orden
    y1 -> Y, propagando el estado optimo de cada año al parametro heredado
    del año siguiente. Produce una solucion factible completa (candidato a
    UB)."""

    def __init__(self, blocks, solver_kwargs=None, macroblock_blocks=None,
                 cut_manager=None, fleet_params=None, day_warm_start="off",
                 day_solver_overrides=None):
        """
        :param macroblock_blocks: {year: {estacion: YearBlockBuilder}} para
            resolver ese año por macrobloque en vez de como un solo MILP (ver
            decomposition/macroblocks.py). Solo se usa aqui, en el forward:
            el reparto de recursos restringe el problema, asi que la solucion
            sigue siendo factible --la cota superior sigue siendo valida--
            pero su relajacion no sirve para cortes. El primer año nunca se
            reparte: es el que decide las inversiones comunes (P_max_k, G, H)
            y esas no son separables por nave.
        :param cut_manager: necesario en modo macrobloque para replicar los
            cortes del año en cada bloque (add_year_cuts_to_macroblock).
        :param fleet_params: dict con b_max/rho_rep/b_upper/n_elhd/gamma_coef
            por año, para coordinar la degradacion de flota y recomponerla
            despues (ver macroblocks.coordinate_b_bar/aggregate_degradation).
        """
        self.blocks = blocks  # lista de YearBlockBuilder, ordenada y1..Y
        self.solver_kwargs = solver_kwargs or {}
        self.macroblock_blocks = macroblock_blocks or {}
        self.cut_manager = cut_manager
        self.fleet_params = fleet_params or {}
        self.feasibility_cuts_added = 0
        # Estado de cada año en la ULTIMA iteracion, usado como valor de los
        # estados ajenos al replicar los cortes en cada macrobloque.
        self.last_state_by_year = {}
        # Forward estabilizado (Box-step, portado de battery_swapping_multiaño):
        # None = sin caja. Si no, dict con center ({año: estado}, formato de
        # extract_state), delta_int, frac_cont y min_cont -- ver
        # YearBlockBuilder.set_trust_region. Lo fija el driver antes de cada
        # run(). No se aplica a los años resueltos por macrobloque.
        self.trust_region = None
        # Mejor trayectoria conocida, {año: extract_full_solution()}, o None.
        # La fija el driver; la usa el MIP start por dias.
        self.incumbente = None
        # MIP start por descomposicion en dias (decomposition/day_blocks.py,
        # portado de battery_swapping_multiaño):
        #   "off"      nunca (default, comportamiento historico);
        #   "first"    de entrada solo en la primera iteracion, y en las
        #              siguientes como "fallback";
        #   "always"   antes de cada resolucion del forward;
        #   "fallback" solo cuando el bloque anual llega al timelimit SIN
        #              incumbente: se arma el start y se vuelve a resolver.
        if day_warm_start not in ("off", "first", "always", "fallback"):
            raise ValueError("day_warm_start tiene que ser off, first, always o fallback")
        self.day_warm_start = day_warm_start
        self.day_warm_start_time = 0.0
        # Los solves DIARIOS tienen su propio gap/timelimit/jobs: solo tienen
        # que encontrar un punto, no demostrar el gap del bloque anual.
        self.day_solver_kwargs = dict(self.solver_kwargs, **(day_solver_overrides or {}))

    def _use_day_warm_start(self, iteration):
        if self.day_warm_start == "always":
            return True
        return self.day_warm_start == "first" and iteration in (None, 1)

    def _run_day_warm_start(self, block, heritage, verbose):
        # Import local: day_blocks importa YearBlockBuilder, y este modulo lo
        # cargan los workers del Pool que construye bloques.
        from src.optimization.decomposition.day_blocks import day_warm_start
        t_dw = time.time()
        try:
            return day_warm_start(block, heritage, self.day_solver_kwargs,
                                  verbose=verbose,
                                  incumbente=(self.incumbente or {}).get(block.year))
        except Exception as exc:
            # Heuristica de arranque: si falla, se sigue como siempre.
            print(f"[DayDecomp] año {block.year}: fallo "
                  f"({type(exc).__name__}: {exc}) -- se sigue sin MIP start")
            return False
        finally:
            self.day_warm_start_time += time.time() - t_dw

    def run(self, iteration=None, verbose=True, current_ub=None, current_lb=None,
            max_recoveries=25):
        """Recorre los anios y, si alguno queda infactible con el estado que
        recibio, agrega un corte de factibilidad al anio anterior y REEMPIEZA el
        recorrido. Sin ese mecanismo la trayectoria simplemente no se puede
        completar: el modelo no tiene holguras que garanticen recurso
        relativamente completo, y las inversiones que se deciden una sola vez
        (P_max_k, G, H) no se pueden ampliar despues."""
        recoveries = 0
        while True:
            try:
                return self._sweep(iteration, verbose, current_ub, current_lb)
            except _YearInfeasible as exc:
                if exc.index == 0:
                    raise RuntimeError(
                        f"El primer anio ({self.blocks[0].year}) es infactible por si "
                        f"solo: no hay anio anterior al que agregarle un corte, asi que "
                        f"el problema es del escenario, no de la descomposicion."
                    )
                recoveries += 1
                if recoveries > max_recoveries:
                    raise RuntimeError(
                        f"Se agregaron {max_recoveries} cortes de factibilidad en una "
                        f"misma fase forward sin lograr completar el horizonte."
                    )
                self._add_feasibility_cut(exc, iteration, verbose)

    def _add_feasibility_cut(self, exc, iteration, verbose):
        child = self.blocks[exc.index]
        parent = self.blocks[exc.index - 1]
        if self.cut_manager is None:
            raise RuntimeError(
                f"El anio {child.year} es infactible y ForwardPass no tiene "
                f"cut_manager con el que generar el corte de factibilidad."
            )
        if verbose:
            print(f"[NestedBenders] FORWARD  anio {child.year} infactible con el estado "
                  f"recibido -- generando corte de factibilidad para el anio {parent.year}...")

        with child.elastic_mode() as feas:
            _solve(feas, label=f"factibilidad y={child.year}", require_optimal=True,
                   **self.solver_kwargs)
            v_hat = value(feas.feas_obj)
            if v_hat > 1e-7 and self._restringir_al_soporte(feas, child):
                _solve(feas, label=f"factibilidad y={child.year} (soporte)",
                       require_optimal=True, **self.solver_kwargs)
                v_hat = value(feas.feas_obj)
            mu = self.cut_manager.read_duals(feas, child.year, child.state_links,
                                             prefix="feas_link_")

        if v_hat <= 1e-7:
            raise RuntimeError(
                f"El anio {child.year} es infactible como MILP pero su relajacion "
                f"elastica da violacion nula: la infactibilidad no viene de que le "
                f"falte estado heredado sino de la integralidad, y un corte de "
                f"factibilidad no la puede corregir."
            )

        atan = {k: v for k, v in mu.items()
                if (max(v.values()) if isinstance(v, dict) else v) > 1e-7}
        if verbose:
            detalle = {f: ({i: round(m, 4) for i, m in v.items() if m > 1e-7}
                           if isinstance(v, dict) else round(v, 4))
                       for f, v in atan.items()}
            print(f"[NestedBenders] FORWARD  falta de estado v={v_hat:,.4f}; "
                  f"familias que atan (mu): {detalle}")

        # PROPAGACION DIRECTA. Si todas las familias que atan son "global_once"
        # (n_ssee_k, G, H: las decide solo el primer anio y todos los demas las
        # reciben identicas), el corte va derecho al bloque del anio 1. El
        # padre inmediato no puede cambiarlas -- en su bloque estan fijadas al
        # heredado --, asi que dejarle el corte solo lo vuelve infactible y la
        # misma informacion baja un anio por vez, con un barrido completo
        # desde el anio 1 por cada escalon. Medido en carga on board a 6 anios:
        # un unico deficit de n_ssee_k en el anio 5 costo CUATRO reinicios
        # (5->4, 4->3, 3->2, 2->1) en vez de uno.
        #
        # Es riguroso: el corte v_hat + mu^T (x_hat - x) <= 0 vale para el
        # estado x que recibe el hijo, y si mu solo tiene componentes
        # global_once la desigualdad restringe unicamente ese vector, que es
        # literalmente la variable del anio 1 (y su ancla x_hat es la misma en
        # todos los anios del barrido). Nada se pierde respecto de la cascada,
        # que en cada escalon rehace el LP elastico y puede solo aflojar.
        kinds = {link["state"]: link.get("kind") for link in child.state_links}
        if exc.index > 1 and atan and all(kinds.get(f) == "global_once" for f in atan):
            parent = self.blocks[0]
            if verbose:
                print(f"[NestedBenders] FORWARD  todas las familias que atan son "
                      f"globales ({sorted(atan)}): el corte va directo al anio "
                      f"{parent.year}")
        self.cut_manager.add_feasibility_cut(
            parent, v_hat, mu, exc.x_hat_by_year[parent.year], iteration=iteration
        )
        self.feasibility_cuts_added += 1
        registro = self.cut_manager.history[-1]
        if verbose and registro["kind"] == "feasibility-entero":
            print(f"[NestedBenders] FORWARD  corte entero: la suma de "
                  f"{registro['n_terminos']} estados tiene que subir al menos "
                  f"{registro['rhs_redondeado']} (el LP pedia {v_hat:,.4f})")

    @staticmethod
    def _restringir_al_soporte(feas, child, tol=1e-7):
        """Fija en 0 las holguras que ya salieron nulas, dejando libres solo las
        del SOPORTE. Devuelve cuantas fijo.

        El objetivo elastico l1 tiene duales +-1 por construccion y, en un
        optimo degenerado, reparte mu = 1 entre familias cuya holgura es CERO,
        que no tienen nada que ver con la infactibilidad. Fijar las holguras
        nulas NO cambia v -- la solucion anterior sigue siendo factible en el
        problema mas restringido -- pero concentra los multiplicadores en las
        familias que realmente atan, y suele dejar el corte soportado solo en
        variables enteras, que es lo que habilita el redondeo."""
        fijadas = 0
        for link in child.state_links:
            if link["hat"] is None:
                continue
            s = getattr(feas, "feas_slack_" + link["state"], None)
            if s is None:
                continue
            componentes = ([s] if link["index_set"] is None
                           else [s[idx] for idx in link["index_set"]])
            for comp in componentes:
                if not comp.fixed and value(comp) <= tol:
                    comp.fix(0.0)
                    fijadas += 1
        return fijadas

    def _solve_year(self, block, heritage, iteration, verbose):
        """MILP del año en el forward, con los respaldos de battery_swapping_
        multiaño: MIP start por dias y reintento largo. Levanta
        SubproblemInfeasible si el año no tiene solucion con el estado heredado.
        """
        warm = False
        if self._use_day_warm_start(iteration):
            warm = self._run_day_warm_start(block, heritage, verbose)
        try:
            _solve(block.model, label=f"forward y={block.year}", warmstart=warm,
                   **self.solver_kwargs)
        except SubproblemNoIncumbent:
            # "fallback" siempre recupera. "first" tambien, en las iteraciones
            # en que NO armo el start de entrada: los cortes y la herencia
            # cambian el bloque, y nada garantiza que el año que tuvo incumbente
            # gracias al start en la iteracion 1 lo vuelva a encontrar solo.
            respaldo = (self.day_warm_start == "fallback"
                        or (self.day_warm_start == "first"
                            and not self._use_day_warm_start(iteration)))
            if respaldo and not warm:
                print(f"[DayDecomp] año {block.year}: el bloque anual no encontro "
                      f"incumbente -- armando MIP start por dias y resolviendo de nuevo")
                if self._run_day_warm_start(block, heritage, verbose):
                    _solve(block.model, label=f"forward y={block.year} (con start)",
                           warmstart=True, **self.solver_kwargs)
                    return
            # Ultimo recurso antes de perder la iteracion: un reintento con 4x
            # el tope (minimo 20 min). Medido en battery_swapping_multiaño: un
            # año sin incumbente mataba la corrida entera. Si tambien falla, la
            # excepcion sube y el driver corta la descomposicion conservando la
            # mejor solucion.
            largo = dict(self.solver_kwargs)
            largo["timelimit"] = max(4 * largo.get("timelimit", 600), 1200)
            print(f"[NestedBenders] año {block.year}: sin incumbente -- "
                  f"reintento con {largo['timelimit']:.0f}s")
            _solve(block.model, label=f"forward y={block.year} (reintento)",
                   warmstart=warm, **largo)

    def _solve_year_stabilized(self, block, heritage, iteration, verbose):
        """_solve_year dentro de la caja del forward estabilizado, si la hay.
        Devuelve True si la caja termino ACTIVA (la solucion toca un borde).

        Si el año falla DENTRO de la caja -- infactible o sin incumbente -- se
        repite sin caja. Es obligatorio, no una comodidad: una infactibilidad
        causada por la caja no es del problema, y dejarla subir generaria un
        corte de factibilidad INVALIDO sobre el año anterior (prohibiria
        estados que sin caja si tienen continuacion)."""
        tr = self.trust_region
        centro = tr["center"].get(block.year) if tr else None
        if centro is None:
            self._solve_year(block, heritage, iteration, verbose)
            return False
        block.set_trust_region(centro, delta_int=tr["delta_int"],
                               frac_cont=tr["frac_cont"], min_cont=tr["min_cont"])
        try:
            try:
                self._solve_year(block, heritage, iteration, verbose)
                return block.trust_region_binding()
            except (SubproblemInfeasible, SubproblemNoIncumbent) as exc:
                print(f"[Estabilizacion] año {block.year}: sin solucion dentro de la "
                      f"caja ({type(exc).__name__}) -- se repite sin caja")
                block.clear_trust_region()
                self._solve_year(block, heritage, iteration, verbose)
                # Salir de la caja cuenta como caja activa: la limitaba.
                return True
        finally:
            # La caja no puede sobrevivir al solve: el backward y los cortes de
            # factibilidad usan este mismo bloque.
            block.clear_trust_region()

    def _sweep(self, iteration=None, verbose=True, current_ub=None, current_lb=None):
        x_hat_by_year = {}
        phi_by_year = {}
        alpha_by_year = {}
        full_solution_by_year = {}
        mccormick_residual_by_year = {}
        caja_activa = {}

        n = len(self.blocks)
        for i, block in enumerate(self.blocks):
            if verbose:
                k_tag = f"k={iteration} " if iteration is not None else ""
                print(f"[NestedBenders] {k_tag}FORWARD  anio {block.year} ({i + 1}/{n})  "
                      f"{_bounds_tag(current_ub, current_lb)}  resolviendo MILP...")
            heritage = x_hat_by_year[self.blocks[i - 1].year] if i > 0 else None
            if heritage is not None:
                block.set_heritage(heritage)

            mbs = self.macroblock_blocks.get(block.year) if self.macroblock_blocks else None
            if mbs and heritage is not None:
                # Descomposicion por macrobloque DENTRO del año (solo forward,
                # ver macroblocks.py): el año se resuelve como |K| MILP mas
                # chicos con los recursos compartidos repartidos en cuotas.
                phi, alpha, state, full = self._solve_year_by_macroblock(
                    block, mbs, heritage, iteration=iteration, verbose=verbose,
                    state_hint=self.last_state_by_year.get(block.year),
                )
                phi_by_year[block.year] = phi
                alpha_by_year[block.year] = alpha
                x_hat_by_year[block.year] = state
                full_solution_by_year[block.year] = full
                self.last_state_by_year[block.year] = state
                continue

            try:
                caja_activa[block.year] = self._solve_year_stabilized(
                    block, heritage, iteration, verbose)
            except SubproblemInfeasible:
                raise _YearInfeasible(i, x_hat_by_year)
            phi_by_year[block.year] = value(block.model.obj)
            alpha_by_year[block.year] = value(block.model.alpha)
            x_hat_by_year[block.year] = block.extract_state()
            self.last_state_by_year[block.year] = x_hat_by_year[block.year]
            # Solucion completa del bloque (no solo el estado): la necesita
            # el puente de reporte para reconstruir el modelo monolitico "de
            # solo lectura" que consume Printer sin tocarlo.
            full_solution_by_year[block.year] = block.extract_full_solution()

            # Validacion obligatoria del Camino A (doc sec. 3.4): residuo del
            # bilineal exacto evaluado en el optimo de la relajacion
            # McCormick. Solo diagnostico -- no aborta la corrida.
            residual = block.mccormick_residual()
            if residual is not None:
                mccormick_residual_by_year[block.year] = residual
                if verbose and abs(residual) > 1e-6:
                    print(f"[NestedBenders] {k_tag}FORWARD  anio {block.year}  "
                          f"AVISO: residuo McCormick = {residual:.6f} "
                          f"(N_ciclos*b_bar - S/n_elhd) -- revisar sec. 3.4/3.5 "
                          f"del documento si es grande frente a S/n_elhd.")

        # UB_k = sum_y (Phi_{y,k} - alpha_{y,k}) -- se resta alpha para no
        # contar dos veces el costo futuro (documento sec. 7.1).
        ub = sum(phi_by_year[b.year] - alpha_by_year[b.year] for b in self.blocks)

        return {
            "ub": ub,
            "phi": phi_by_year,
            "alpha": alpha_by_year,
            "x_hat": x_hat_by_year,
            "full_solution": full_solution_by_year,
            "mccormick_residual": mccormick_residual_by_year,
            # {año: True si la caja del forward estabilizado quedo activa}
            "box_binding": caja_activa,
        }


    def _solve_year_by_macroblock(self, block, mbs, heritage, iteration,
                                   verbose, state_hint):
        """Resuelve un año como varios MILP de macrobloque y recompone el
        resultado del año. Devuelve (phi, alpha, x_hat, full_solution).

        La degradacion es lo unico que no se reparte: b_bar y R son de la
        flota completa, se coordinan aqui una sola vez y se pasan como datos a
        cada macrobloque (ver year_block._fix_degradation_for_macroblock). Se
        prueba primero sin reemplazar --que es lo barato-- y solo si algun
        macrobloque queda infactible con esa capacidad se reemplaza en TODOS,
        que es lo que hace el modelo completo (R_y es una sola decision).
        """
        y = block.year
        k_tag = f"k={iteration} " if iteration is not None else ""
        params = self.fleet_params.get(y) or {}
        has_degradation = bool(params)
        b_bar = None

        for replace in ((0, 1) if has_degradation else (None,)):
            if has_degradation:
                b_bar = coordinate_b_bar(params, float(heritage["D"]), replace)
            if verbose:
                extra = (f"  b_bar={b_bar:,.2f} R={replace}" if has_degradation else "")
                print(f"[NestedBenders] {k_tag}FORWARD  anio {y}  "
                      f"{len(mbs)} macrobloques{extra}")

            failed = None
            for station, mb in sorted(mbs.items()):
                mb.set_heritage(heritage)
                if has_degradation:
                    mb.set_fleet_degradation(b_bar, replace)
                mb.clear_cuts()
                if self.cut_manager is not None:
                    self.cut_manager.add_year_cuts_to_macroblock(mb, state_hint)
                try:
                    _solve(mb.model, label=f"forward y={y} macrobloque={station}",
                           **self.solver_kwargs)
                except RuntimeError as exc:
                    failed = (station, exc)
                    break

            if failed is None:
                break
            if not has_degradation or replace == 1:
                raise RuntimeError(
                    f"El macrobloque '{failed[0]}' del año {y} no tiene solucion "
                    f"ni reemplazando la bateria de la flota. Puede ser que su "
                    f"cuota de potencia o su meta de produccion repartida sean "
                    f"demasiado ajustadas (ver macroblocks.py): {failed[1]}"
                )
            if verbose:
                print(f"[NestedBenders] {k_tag}FORWARD  anio {y}  el macrobloque "
                      f"'{failed[0]}' no cierra sin reemplazar bateria -- se "
                      f"reemplaza en toda la flota y se resuelve de nuevo.")

        return self._aggregate_macroblocks(block, mbs, heritage, b_bar, params)

    def _aggregate_macroblocks(self, block, mbs, heritage, b_bar, params):
        """Recompone el año a partir de los macrobloques ya resueltos.

        - Costos: se suman. Cada macrobloque cobra su parte de los costos de
          flota via su cuota (ver functions.py), asi que la suma reproduce el
          costo del año completo sin contarlo varias veces.
        - alpha: se suma tambien, porque UB resta sum(alpha) y lo que debe
          quedar es sum(f_k) -- ver ForwardPass.run.
        - Estado: union de los estados por nave. La degradacion se recompone
          con la energia total cargada, que con b_bar fijo es una cuenta
          lineal exacta (macroblocks.aggregate_degradation).
        """
        y = block.year
        phi = sum(value(mb.model.obj) for mb in mbs.values())
        alpha = sum(value(mb.model.alpha) for mb in mbs.values())

        state = {}
        for mb in mbs.values():
            for name, val in mb.extract_state().items():
                if isinstance(val, dict):
                    state.setdefault(name, {}).update(val)
                else:
                    # Estado compartido (H): todos los macrobloques lo tienen
                    # fijado al mismo valor heredado.
                    state[name] = val

        if params:
            s_total = sum(value(mb.model.S[y]) for mb in mbs.values())
            d_y, n_ciclos = aggregate_degradation(
                b_bar, s_total, params["n_elhd"], params["gamma_coef"]
            )
            state["D"] = d_y
            state["_n_ciclos"] = n_ciclos
            state["_b_bar"] = b_bar
            state["_s_total"] = s_total

        return phi, alpha, state, self._merge_full_solutions(mbs, state)

    # Variables de potencia que son del sistema completo y no de una nave: al
    # recomponer el año se SUMAN entre macrobloques (cada uno opero con su
    # cuota). El resto de las variables esta indexado por nave, equipo o nodo,
    # y la union de los macrobloques las cubre sin solaparse.
    _ADDITIVE_VARS = ("P_red", "P_pot", "P_gen", "P_bat", "A_h", "Curt_g", "S")

    def _merge_full_solutions(self, mbs, state):
        merged = {}
        for _station, mb in sorted(mbs.items()):
            for name, values in mb.extract_full_solution().items():
                if name in self._ADDITIVE_VARS and isinstance(values, dict):
                    target = merged.setdefault(name, {})
                    for idx, v in values.items():
                        target[idx] = target.get(idx, 0.0) + v
                elif isinstance(values, dict):
                    merged.setdefault(name, {}).update(values)
                else:
                    merged[name] = values

        # Degradacion: la solucion del año es la coordinada/agregada, no la de
        # ningun macrobloque en particular.
        y = next(iter(mbs.values())).year
        if "_b_bar" in state:
            for name, val in (("b_bar", state["_b_bar"]), ("D", state["D"]),
                              ("N_ciclos", state["_n_ciclos"]), ("S", state["_s_total"])):
                if name in merged and isinstance(merged[name], dict):
                    merged[name][y] = val
        return merged


class BackwardPass(object):
    """Documento sec. 7.2: para y=Y..y1+1, relaja la integralidad del
    bloque de ese año (con el heritage que dejo el forward de esta
    iteracion), lee los duales de las igualdades de enlace, y agrega el
    corte resultante a la lista de cortes del año y-1. Como el bucle
    procesa y de mayor a menor, el corte agregado a un año y-1 en un paso
    ya queda reflejado cuando ese mismo bloque se relaja como "hijo" en el
    paso siguiente -- asi es como los cortes se propagan hacia atras dentro
    de una misma iteracion (ver year_block.py, mutacion in-place de
    model.cuts).

    Dos tipos de corte para el año con degradacion de bateria (ver
    degradacion_descomposicion_mccormick.md), seleccionables via
    `degradation_cut_mode`:

    - "mccormick" (Camino A, default): el bloque ya es MILP puro (la
      degradacion entra via la relajacion de McCormick construida en
      YearBlockBuilder), asi que el corte estandar de Benders (LP relax +
      duales, mecanismo generico de mas abajo) ya es valido para "D" sin
      tratamiento especial -- barato, un LP por año e iteracion.
    - "lagrangean" (Camino B): reemplaza el corte del año con degradacion
      por un corte Lagrangeano con subgradiente (sec. 4.3), resuelto sobre
      la fisica EXACTA (bilineal no convexa) del bloque -- mas caro (varios
      MIQCP no convexos por año e iteracion) pero no aproxima el producto
      N_ciclos*b_bar.

    Un tercer camino, "disjunctive" (corte big-M sobre la disyuncion
    R_y=0/R_y=1 del reemplazo de bateria, ver _disjunctive_replace_cut),
    quedo implementado en este archivo pero PAUSADO/no seleccionable
    (merge 2026-08 con origin/carga_ob_multiaño): depende de como
    year_block.py modela los estados, y origin los reestructuro a
    "global_once" sin que se revisara despues si el corte disyuntivo
    sigue bien planteado sobre ese modelo.

    OJO: el Strengthened Benders estuvo pausado junto con este y por un
    motivo relacionado, pero se REACTIVO en 2026-09 (ver el docstring de
    _strengthened_subgradient_cut). El disyuntivo NO -- su bloqueo es la
    revision pendiente de la disyuncion R_y=0/R_y=1 contra el modelo de
    estados nuevo, no el diagnostico de mu=0."""

    def __init__(self, blocks, cut_manager=None, solver_kwargs=None,
                 degradation_cut_mode="mccormick", lagrangean_kwargs=None,
                 strengthen_max_iter=1, strengthened_timelimit=300):
        """:param strengthened_timelimit: segundos por MILP lagrangeano del
            corte fortalecido (portado de battery_swapping_multiaño). Antes se
            usaba el timelimit del bloque. Cortarlo NO invalida el corte: se usa
            la cota DUAL del solver, que subestima siempre (ver _lagrangean_bound).
        """
        self.strengthened_timelimit = strengthened_timelimit
        # Cuanto levanto el corte fuerte sobre el de LP, para poder decidir si
        # paga su costo sin releer los logs. `attempts` cuenta los intentos
        # REALES (la aceleracion agrega pasadas de mas sobre las iteraciones).
        self.strengthen_gain = []
        self.strengthen_attempts = 0
        self.blocks = blocks
        self.cut_manager = cut_manager or BendersCutManager()
        self.solver_kwargs = solver_kwargs or {}
        if degradation_cut_mode not in ("mccormick", "lagrangean"):
            raise ValueError(
                f"degradation_cut_mode debe ser 'mccormick' o 'lagrangean', "
                f"recibido: {degradation_cut_mode!r}"
            )
        self.degradation_cut_mode = degradation_cut_mode
        self.lagrangean_kwargs = lagrangean_kwargs or {}
        # Iteraciones del subgradiente de _strengthened_subgradient_cut. Define
        # CUAL de los cortes de Lara et al. (2018) sec. 5.2.2 se genera:
        #   1  -> Strengthened Benders cut, ec. (63): UNA relajacion Lagrangeana
        #         resuelta en los duales del LP, sin mejorar los multiplicadores.
        #         "at least as tight as the Benders cut" (Proposicion 3).
        #   >1 -> Lagrangean cut, ec. (62): ascenso de subgradiente para
        #         aproximar el dual Lagrangeano optimo. Mas ajustado y mas caro.
        # Default 1: el compromiso que propone el paper entre Benders (barato,
        # flojo si la relajacion lineal no es apretada) y Lagrangeano (ajustado,
        # caro). Con 1 el costo es UNA resolucion extra del MILP del bloque por
        # año y por iteracion del backward, no max_iter.
        if strengthen_max_iter < 1:
            raise ValueError(
                f"strengthen_max_iter debe ser >= 1, recibido: {strengthen_max_iter!r}"
            )
        self.strengthen_max_iter = strengthen_max_iter

    def _exact_solve_kwargs(self):
        """solver_kwargs para los MIQCP no convexos del Camino B: mezcla
        extra_options del usuario (si trae) con NonConvex=2 -- necesario
        porque _solve(..., extra_options={...}, **self.solver_kwargs)
        chocaria (TypeError: multiple values for 'extra_options') si
        solver_kwargs ya trae su propio extra_options."""
        kwargs = dict(self.solver_kwargs)
        extra = dict(kwargs.pop("extra_options", None) or {})
        extra["NonConvex"] = 2
        kwargs["extra_options"] = extra
        return kwargs

    def _relax_and_solve(self, block, label, read_mu=True):
        """Devuelve (Phi^LP, mu) del bloque relajado. Se relaja EN SITU: ver
        YearBlockBuilder.relaxed_mode -- aqui se relaja una vez por anio y por
        iteracion, asi que el costo del clone se paga N_anios * N_iteraciones
        veces (medido: 1.5x mas rapido en sitio, y mucho mas si falta memoria).

        read_mu=False para el bloque del PRIMER anio, que se relaja solo para
        obtener la cota inferior: alli no hay duales que leer porque ese anio
        DECIDE los estados "global_once" (P_max_k, G, H) en vez de heredarlos,
        de modo que no existen las link_* correspondientes y read_duals
        levantaria AttributeError.
        """
        with block.relaxed_mode() as model:
            _solve(model, label=label, require_optimal=True, **self.solver_kwargs)
            phi_lp = value(model.obj)
            mu = (self.cut_manager.read_duals(model, block.year, block.state_links)
                  if read_mu else None)
        return phi_lp, mu

    def _lagrangean_bound(self, block, mu, phi_lp, label):
        """Constante del Strengthened Benders cut (Lara et al. 2018, ec. 63):
        Phi^LR(mu) con la integralidad del bloque PUESTA, usando el mu que ya
        salio del dual del LP. Devuelve (valor, se_uso_el_fuerte). Portado de
        battery_swapping_multiaño; reemplaza, para strengthen_max_iter=1, a
        _strengthened_subgradient_cut.

        VALIDEZ CON TIMELIMIT O MIPGAP. El corte necesita un valor que SUBESTIME
        Phi^LR. Si el MILP se corta por tiempo -- o cierra con MIPGap > 0, que
        es el caso normal con gap 1 % --, su incumbente lo SOBREestima, y armar
        el corte con el lo haria demasiado fuerte: podria recortar estados
        factibles y producir LB > UB. Por eso se lee la cota DUAL
        (result.problem.lower_bound), que subestima siempre, y por eso el solve
        va con load_solutions=False: asi un MILP sin incumbente igual devuelve
        su cota. La version anterior de esta rama tomaba value(obj_lagrangian),
        el valor del incumbente.

        PISO EN Phi^LP. Con el mu optimo del LP vale Phi^LR >= Phi^LP: el
        minimo sobre el conjunto entero no puede ser menor que sobre su
        relajacion. Si el MILP se corta antes de levantar la cota por encima de
        Phi^LP, se devuelve Phi^LP: el corte queda igual al de Benders puro,
        nunca peor.

        Se hace EN SITU (YearBlockBuilder.lagrangean_mode), sin clonar, y
        dualizando todas las familias de estado, tambien las global_once."""
        kwargs = dict(self.solver_kwargs)
        kwargs["timelimit"] = self.strengthened_timelimit
        with block.lagrangean_mode(mu) as model:
            result = _solve(model, label=label, load_solutions=False, **kwargs)
        dual = _cota_dual(result)
        if dual is None or dual <= phi_lp:
            return phi_lp, False
        return dual, True

    def _make_exact_clone(self, block):
        """Clona el bloque y restaura la fisica EXACTA (bilineal, no
        convexa) de degradacion en lugar de la relajacion de McCormick --
        Camino B (documento sec. 4.2). Reusa la MISMA regla n_ciclos_link
        que arma el monolitico (block.constraint_rules.n_ciclos_link, ver
        functions.py), sin duplicar la formula. Mantiene la integralidad
        intacta (MILP/MIQCP, no relajado) -- requiere Gurobi con
        NonConvex=2 (mismo flag que opt_model.py._configure_solver usa
        para el monolitico con degradacion)."""
        clone = block.model.clone()
        for name in ("mccormick_energy", "mccormick_lb1", "mccormick_lb2",
                     "mccormick_ub1", "mccormick_ub2"):
            getattr(clone, name).deactivate()
        clone.n_ciclos_link_exact = pyo.Constraint(
            expr=block.constraint_rules.n_ciclos_link(clone, block.year)
        )
        return clone

    def _build_lagrangian_relaxation(self, block, mu, x_hat_base):
        """Clon EXACTO (ver _make_exact_clone) con TODAS las familias de
        estado "simple" (N_chargers/G/H/D) relajadas y penalizadas en el
        objetivo con `mu` (documento sec. 4.2, `z_y` = el vector de estado
        completo). `prev` de cada familia queda con su cota FISICA propia
        (ya declarada en year_block.py via prev_bound: max_bays_k/g_max_g/
        h_max/B_U), NO por x_hat_base -- acotar por el punto de
        linealizacion de la iteracion en curso solo garantiza validez
        LOCAL del corte resultante (cerca de donde se calibro mu), no
        GLOBAL, y causo LB>UB en una corrida real de 5 años (bug ya
        corregido tambien en _build_strengthened_relaxation, mismo
        patron -- ver su docstring para la derivacion completa de por que
        la cota fisica es la unica que preserva dualidad debil de
        Lagrange para cualquier x, no solo el x_hat evaluado).

        Convencion de signo: penalizacion +mu*prev en el objetivo (NO
        -mu*(prev-x_hat)). Con esta convencion L(mu) = valor_objetivo_aca
        - mu*x_hat_base (restado DESPUES de resolver, ver
        _lagrangean_subgradient_cut)."""
        clone = self._make_exact_clone(block)
        penalty_terms = []
        for link in block.state_links:
            if link.get("kind") != "simple" or link.get("prev") is None:
                continue
            state_name = link["state"]
            if state_name not in mu:
                continue
            getattr(clone, f"link_{state_name}").deactivate()
            prev = getattr(clone, link["prev"])
            mu_fam = mu[state_name]
            if link["index_set"] is None:
                penalty_terms.append(mu_fam * prev)
            else:
                for idx, mu_v in mu_fam.items():
                    penalty_terms.append(mu_v * prev[idx])

        clone.obj.deactivate()
        clone.obj_lagrangian = pyo.Objective(
            expr=clone.obj.expr + sum(penalty_terms), sense=pyo.minimize
        )
        return clone

    def _lagrangean_subgradient_cut(self, child, x_hat_base, mu_init,
                                     max_iter=10, eps_gap=1e-3, eps_stall=1e-4,
                                     verbose=True, label_prefix=""):
        """Camino B -- corte Lagrangeano con subgradiente (documento sec.
        4.3), dualizando TODAS las familias de estado "simple" del año
        `child` a la vez (z_y = vector de estado completo, igual notacion
        que implementacion_descomposicion_carga_ob.md sec. 2.3) con la
        fisica de degradacion EXACTA (bilineal no convexa) restaurada.

        Devuelve (phi_cut, mu_best) en el MISMO formato que espera
        BendersCutManager.add_cut (compatible con el Camino A: mismo shape
        que read_duals) -- el llamador solo necesita reemplazar phi_lp/mu
        por este resultado antes de llamar add_cut, sin tocar su formula.

        Convencion de signo -- OJO, opuesta a la formula literal del
        documento: con la penalizacion +mu*prev en el objetivo (ver
        _build_lagrangian_relaxation, misma convencion validada
        empiricamente en _build_strengthened_relaxation/
        _strengthened_subgradient_cut), el ascenso de
        subgradiente correcto es mu <- mu + step*(prev*(mu) - x_hat_base),
        NO mu <- mu - step*(...) como aparece literal en el documento (que
        asume la convencion de signo opuesta, -mu*(z-x_hat) dentro del
        min). Derivacion completa: para L(mu)=g(mu)-mu*x_hat con
        g(mu)=min[f+mu*z], dL/dmu = z*(mu)-x_hat; ascender sobre L (el
        objetivo, ya que L(mu)<=Phi(x_hat) para todo mu por dualidad debil
        y buscamos el mu que de la cota mas ajustada) es mu += step*dL/dmu.
        El paso de Polyak usa Phi^OP (el MIQCP exacto con el estado
        heredado FIJO, sec. 4.3 paso 1) como cota superior conocida de
        max_mu L(mu).

        `mu_init` llega ya en esta misma convencion +mu*(z-x_hat):
        read_duals devuelve -pi (ver su docstring), no el dual crudo del
        solver, de modo que el ascenso parte del lado correcto y no tiene
        que cruzar el cero para alcanzar el mu que maximiza L."""
        y = child.year

        exact_kwargs = self._exact_solve_kwargs()

        op_clone = self._make_exact_clone(child)
        _solve(op_clone, label=f"{label_prefix}exact Phi_OP y={y}", **exact_kwargs)
        phi_op = value(op_clone.obj)

        mu = {k: (dict(v) if isinstance(v, dict) else v) for k, v in mu_init.items()}
        best_L = float("-inf")
        best_mu = mu
        prev_L = None
        gap_scale = max(abs(phi_op), 1.0)

        dualized_links = [
            link for link in child.state_links
            if link.get("kind") == "simple" and link.get("prev") is not None
            and link["state"] in mu_init
        ]

        for it in range(1, max_iter + 1):
            clone = self._build_lagrangian_relaxation(child, mu, x_hat_base)
            # L(mu) sale de la COTA DUAL, no del incumbente: con TimeLimit o
            # MIPGap > 0 el incumbente sobreestima L y el corte armado con el
            # podria ser invalido (ver _lagrangean_bound). La solucion primal se
            # carga solo para el subgradiente.
            result = _solve(clone, label=f"{label_prefix}lagrangian it={it} y={y}",
                            load_solutions=False, **exact_kwargs)
            L_mu = _cota_dual(result)
            if L_mu is None:
                break
            hay_primal = _tiene_incumbente(result)
            if hay_primal:
                clone.solutions.load_from(result)
            for link in dualized_links:
                state_name = link["state"]
                if link["index_set"] is None:
                    L_mu -= mu[state_name] * x_hat_base[state_name]
                else:
                    L_mu -= sum(mu[state_name][idx] * x_hat_base[state_name][idx]
                                 for idx in link["index_set"])

            if verbose:
                print(f"[NestedBenders] {label_prefix}Lagrangeano y={y} it={it}  "
                      f"Phi_OP={phi_op:,.2f}  L(mu)={L_mu:,.2f}  "
                      f"gap={phi_op - L_mu:,.2f}")

            if L_mu > best_L:
                best_L = L_mu
                best_mu = {k: (dict(v) if isinstance(v, dict) else v) for k, v in mu.items()}

            if not hay_primal:
                break   # sin punto no hay subgradiente
            if phi_op - L_mu <= eps_gap * gap_scale:
                break
            if prev_L is not None and abs(L_mu - prev_L) <= eps_stall * gap_scale:
                break
            prev_L = L_mu

            # Subgradiente g = prev*(mu) - x_hat_base, ascenso mu += step*g,
            # paso de Polyak con Phi_OP como objetivo (ver docstring).
            grad = {}
            sq_norm = 0.0
            for link in dualized_links:
                state_name = link["state"]
                prev_var = getattr(clone, link["prev"])
                if link["index_set"] is None:
                    g = value(prev_var) - x_hat_base[state_name]
                    grad[state_name] = g
                    sq_norm += g * g
                else:
                    grad[state_name] = {}
                    for idx in link["index_set"]:
                        g = value(prev_var[idx]) - x_hat_base[state_name][idx]
                        grad[state_name][idx] = g
                        sq_norm += g * g

            if sq_norm <= 1e-12:
                # Subgradiente nulo: mu ya reproduce el estado heredado
                # exacto, no hay progreso posible por esta via.
                break
            step = max(phi_op - L_mu, 0.0) / sq_norm

            for state_name, g in grad.items():
                if isinstance(g, dict):
                    for idx, gv in g.items():
                        mu[state_name][idx] = mu[state_name][idx] + step * gv
                else:
                    mu[state_name] = mu[state_name] + step * g

        return best_L, best_mu

    def _build_strengthened_relaxation(self, block, mu, fix_vars=None):
        """Como _build_lagrangian_relaxation (Camino B) pero sobre el MILP
        ESTANDAR del bloque (McCormick para degradacion si corresponde,
        sin restaurar la fisica bilineal exacta) -- para el Strengthened
        Benders GENERICO (documento sec. 6.2, opcion 2) aplicado a
        cualquier familia de estado "simple" con prev (N_chargers/G/H/D),
        no solo degradacion. No hace falta NonConvex=2 ni _make_exact_clone:
        el bloque ya es MILP puro, mucho mas barato que el Camino B.

        'prev' queda con su cota FISICA propia -- la misma que ya declara
        year_block.py via prev_bound (max_bays_k/g_max_g/h_max/B_U) al
        construir el bloque, INDEPENDIENTE de la iteracion. Es la unica
        cota que garantiza validez GLOBAL del corte por dualidad debil de
        Lagrange (Phi(x) >= h^MILP(mu) - mu*x_hat + mu*x para TODO x, no
        solo cerca de donde se calibro mu).

        Version anterior (ya removida) acotaba 'prev' por el punto de
        linealizacion x_hat_base de la iteracion en curso -- eso rompia la
        validez GLOBAL (solo quedaba correcto localmente, cerca de ese
        punto) y causo LB>UB en una corrida real de 5 años (ver memoria
        del proyecto / docstring viejo de `strengthen` en driver.py). La
        cota fisica es mas floja que x_hat_base, pero SIEMPRE valida.

        fix_vars: dict opcional {(nombre_var, indice_o_None): valor} para
        fijar variables ADICIONALES en el clon antes de resolver -- p.ej.
        R[y]=0 para aislar la rama "no reemplazar" del corte disyuntivo
        de reemplazo de bateria (ver _disjunctive_replace_cut). No
        interfiere con las familias relajadas/penalizadas de arriba."""
        clone = block.model.clone()
        penalty_terms = []
        for link in block.state_links:
            if link.get("kind") != "simple" or link.get("prev") is None:
                continue
            state_name = link["state"]
            if state_name not in mu:
                continue
            getattr(clone, f"link_{state_name}").deactivate()
            prev = getattr(clone, link["prev"])
            mu_fam = mu[state_name]
            if link["index_set"] is None:
                penalty_terms.append(mu_fam * prev)
            else:
                for idx, mu_v in mu_fam.items():
                    penalty_terms.append(mu_v * prev[idx])

        clone.obj.deactivate()
        clone.obj_lagrangian = pyo.Objective(
            expr=clone.obj.expr + sum(penalty_terms), sense=pyo.minimize
        )
        if fix_vars:
            for (var_name, idx), val in fix_vars.items():
                var_comp = getattr(clone, var_name)
                if idx is None:
                    var_comp.fix(val)
                else:
                    var_comp[idx].fix(val)
        return clone

    def _strengthened_subgradient_cut(self, child, x_hat_base, mu_init,
                                       max_iter=10, eps_gap=1e-3, eps_stall=1e-4,
                                       verbose=True, label_prefix="",
                                       fix_vars=None, target=None):
        """Strengthened Benders generico (documento sec. 6.2, opcion 2)
        sobre TODAS las familias de estado "simple" con prev a la vez
        (N_chargers/G/H/D) -- no solo degradacion (esa es
        _lagrangean_subgradient_cut, Camino B, que ademas restaura la
        fisica bilineal exacta y necesita NonConvex=2).

        Reusa `mu_init` (los duales de la relajacion LP, ver read_duals)
        solo como PUNTO DE PARTIDA del ascenso de subgradiente -- no como
        el mu final: un MILP con integralidad completa puede tener
        sensibilidad Lagrangeana no nula donde el LP relajado es
        degenerado, de ahi la busqueda y no solo reusar mu_init tal cual
        (que es lo que hacia la version anterior, ya removida).

        CORRECCION 2026-09: este docstring afirmaba que el "recurso
        completo garantizado" (documento sec. 1) hace que mu salga
        "sistematicamente ~0" para N_chargers/G/H, citando mu=0 en las 3
        primeras iteraciones de 960kW_2dias. Eso generalizaba desde una
        sola instancia. Pruebas posteriores mostraron que la degeneracion
        era propia de ESE escenario, no del modelo: en general mu no es
        cero y el corte LP estandar si transmite informacion al año padre.
        El fortalecimiento sigue siendo valido --por dualidad debil nunca
        debilita el corte-- pero su ganancia esperada es menor que la que
        motivaba el diseño original, lo que concuerda con el impacto
        marginal medido cuando estuvo activo.

        target: si se omite, Phi(x_hat_base) = value(child.model.obj), ya
        conocido porque `child` fue resuelto con heritage=x_hat_base en el
        forward pass de ESTA iteracion -- no hace falta re-resolver un
        "Phi_OP" aparte como en _lagrangean_subgradient_cut (esa SI
        necesita re-resolver porque su target es la fisica EXACTA,
        distinta del bloque ya resuelto con McCormick). Pasar `target`
        explicito cuando `fix_vars` cambia el problema respecto del ya
        resuelto en el forward pass (p.ej. R[y] fijo a un valor distinto
        del que eligio el forward -- ver _disjunctive_replace_cut): en
        ese caso el llamador debe resolver aparte el punto fijo
        correspondiente (_fixed_branch_value) y pasarlo aca, porque
        value(child.model.obj) ya no representa ese branch.

        fix_vars: ver _build_strengthened_relaxation -- se propaga tal
        cual a cada clon de cada iteracion del subgradiente.

        Misma convencion de signo y formula de paso de Polyak que
        _lagrangean_subgradient_cut (ver su docstring para la derivacion
        completa)."""
        y = child.year
        if target is None:
            target = value(child.model.obj)

        dualized_links = [
            link for link in child.state_links
            if link.get("kind") == "simple" and link.get("prev") is not None
            and link["state"] in mu_init
        ]
        if not dualized_links:
            # Antes devolvia `target` (un valor primal, que con MIPGap > 0
            # sobreestima Phi): sin familias que dualizar no hay nada que
            # fortalecer, y el llamador cae al corte de Benders (piso Phi^LP).
            return float("-inf"), mu_init

        mu = {k: (dict(v) if isinstance(v, dict) else v) for k, v in mu_init.items()}
        best_L = float("-inf")
        best_mu = mu
        prev_L = None
        gap_scale = max(abs(target), 1.0)

        kwargs_lr = dict(self.solver_kwargs)
        kwargs_lr["timelimit"] = self.strengthened_timelimit
        for it in range(1, max_iter + 1):
            clone = self._build_strengthened_relaxation(child, mu, fix_vars=fix_vars)
            # Cota DUAL, no incumbente (ver _lagrangean_bound).
            result = _solve(clone, label=f"{label_prefix}strengthened it={it} y={y}",
                            load_solutions=False, **kwargs_lr)
            L_mu = _cota_dual(result)
            if L_mu is None:
                break
            hay_primal = _tiene_incumbente(result)
            if hay_primal:
                clone.solutions.load_from(result)
            for link in dualized_links:
                state_name = link["state"]
                if link["index_set"] is None:
                    L_mu -= mu[state_name] * x_hat_base[state_name]
                else:
                    L_mu -= sum(mu[state_name][idx] * x_hat_base[state_name][idx]
                                 for idx in link["index_set"])

            if verbose:
                print(f"[NestedBenders] {label_prefix}Strengthened y={y} it={it}  "
                      f"Phi={target:,.2f}  L(mu)={L_mu:,.2f}  gap={target - L_mu:,.2f}")

            if L_mu > best_L:
                best_L = L_mu
                best_mu = {k: (dict(v) if isinstance(v, dict) else v) for k, v in mu.items()}

            if it == max_iter or not hay_primal:
                # Ultima pasada: el gradiente y el paso de abajo actualizarian
                # `mu`, pero se devuelve `best_mu`, asi que serian trabajo
                # tirado. Importa con max_iter=1 (Strengthened Benders), donde
                # es la UNICA pasada: queda exactamente una resolucion y nada mas.
                # Sin punto primal tampoco hay subgradiente.
                break

            if target - L_mu <= eps_gap * gap_scale:
                break
            if prev_L is not None and abs(L_mu - prev_L) <= eps_stall * gap_scale:
                break
            prev_L = L_mu

            grad = {}
            sq_norm = 0.0
            for link in dualized_links:
                state_name = link["state"]
                prev_var = getattr(clone, link["prev"])
                if link["index_set"] is None:
                    g = value(prev_var) - x_hat_base[state_name]
                    grad[state_name] = g
                    sq_norm += g * g
                else:
                    grad[state_name] = {}
                    for idx in link["index_set"]:
                        g = value(prev_var[idx]) - x_hat_base[state_name][idx]
                        grad[state_name][idx] = g
                        sq_norm += g * g

            if sq_norm <= 1e-12:
                # Subgradiente nulo: mu ya reproduce el estado heredado
                # exacto, no hay progreso posible por esta via.
                break
            step = max(target - L_mu, 0.0) / sq_norm

            for state_name, g in grad.items():
                if isinstance(g, dict):
                    for idx, gv in g.items():
                        mu[state_name][idx] = mu[state_name][idx] + step * gv
                else:
                    mu[state_name] = mu[state_name] + step * g

        return best_L, best_mu

    def _fixed_branch_value(self, block, fix_vars, label, catch_infeasible=False):
        """Resuelve un clon de block.model con variables ADICIONALES fijas
        (p.ej. R[y]=0) y el enlace de estado TAL COMO ESTA (activo:
        prev=hat del heredado actual) -- da el costo EXACTO de ese branch
        en el punto de esta iteracion. Usado como target del subgradiente
        de la rama "no reemplazar" en _disjunctive_replace_cut.

        catch_infeasible=True: devuelve None en vez de propagar la
        excepcion si el solve no llega a una solucion aceptable -- caso
        real y esperado con R=0 fijo: si la bateria heredada ya llego
        demasiado degradada, operar SIN reemplazar puede ser
        directamente INFACTIBLE (no solo suboptimo) en el punto de esta
        iteracion. Ver _disjunctive_replace_cut para como se maneja ese
        caso."""
        clone = block.model.clone()
        for (var_name, idx), val in fix_vars.items():
            var_comp = getattr(clone, var_name)
            if idx is None:
                var_comp.fix(val)
            else:
                var_comp[idx].fix(val)
        if catch_infeasible:
            try:
                _solve(clone, label=label, **self.solver_kwargs)
            except RuntimeError:
                return None
        else:
            _solve(clone, label=label, **self.solver_kwargs)
        return value(clone.obj)

    def _replace_branch_cost(self, child, label):
        """C1 = Phi_y^{R=1}(D_prev): costo de `child` SI reemplaza la
        bateria este año, para CUALQUIER D_prev -- ver
        _disjunctive_replace_cut. Reemplazar borra la degradacion
        heredada (b_y_link_local deja de depender realmente de D_prev
        cuando R=1, ver year_block.py/_add_degradation_state), asi que
        se libera D_prev (se desactiva link_D) en vez de fijarlo al
        heredado de esta iteracion: minimizar tambien sobre D_prev da un
        valor <= el que se obtendria con D_prev fijo a cualquier valor
        particular, lo que vuelve a C1 una cota inferior valida de
        Phi_y^{R=1}(D_prev) para TODO D_prev (no solo el de esta
        iteracion) -- es decir, una constante SIEMPRE valida, no solo
        localmente."""
        clone = child.model.clone()
        link_D = getattr(clone, "link_D", None)
        if link_D is not None:
            link_D.deactivate()
        clone.R[child.year].fix(1)
        _solve(clone, label=label, **self.solver_kwargs)
        return value(clone.obj)

    def _disjunctive_replace_cut(self, child, parent, x_hat_by_year, mu,
                                  iteration=None, verbose=True, label_prefix=""):
        """Corte disyuntivo R=0/R=1 para el año de reemplazo de bateria
        (degradation_cut_mode="disjunctive", ver conversacion de diseño).

        La funcion de costo-to-go real del estado de degradacion es

            Phi_y(D_prev) = min( Phi_y^{R=0}(D_prev), Phi_y^{R=1} )

        -- el minimo de una funcion de D_prev y una CONSTANTE (reemplazar
        borra la degradacion heredada, ver _replace_branch_cost). El
        minimo de una afin y una constante es CONCAVO, no convexo: ningun
        corte LINEAL (ni el LP barato de Camino A ni el Lagrangeano de
        Camino B, que solo dualizan sin resolver la disyuncion) puede
        representarlo globalmente por mas que se refine con mas
        iteraciones -- una tangente a una funcion concava queda por
        ENCIMA de ella en el resto del dominio, asi que el "corte" mas
        ajustado en un punto es invalido en otros (confirmado empirica-
        mente: mu_D no nulo en el escenario 960kW_2dias, pero el UB no se
        movio en 5 iteraciones -- ver memoria del proyecto). Por eso hace
        falta una desigualdad DISYUNTIVA (big-M) en vez de una lineal:

            alpha_parent >= f0_cut(D) - M*z
            alpha_parent >= C1        - M*(1-z)

        con z binaria nueva. Por monotonia del minimo (f0_cut <=
        Phi_y^{R=0} punto a punto, C1 <= Phi_y^{R=1} siempre), el lado
        derecho de esta disyuncion es <= Phi_y(D) para TODO D, sin
        importar que z elija el solver -- corte GLOBALMENTE valido.

        f0_cut se calcula con _strengthened_subgradient_cut restringido a
        SOLO la familia "D" y con R[child.year] fijo a 0 (rama "no
        reemplazar" aislada, sin mezclar regimenes). C1 con
        _replace_branch_cost (rama "reemplazar", constante).

        No reemplaza el corte lineal estandar de "D" que ya agrega
        BackwardPass.run mas arriba (sigue siendo valido, solo mas flojo)
        -- este es un corte ADICIONAL, mas fuerte especificamente en la
        zona donde reemplazar-o-no es la decision relevante.

        Caso especial -- R=0 INFACTIBLE: si la bateria heredada ya llego
        demasiado degradada, "no reemplazar" puede ser directamente
        imposible (no solo caro) en el punto x_hat de esta iteracion
        (confirmado en una corrida real, año 3 del escenario
        960kW_2dias). Ahi no hay target para el subgradiente -- se prueba
        primero la version RELAJADA (D_prev libre en su cota fisica, sin
        refinar mu): si esa tambien es infactible, "no reemplazar" no es
        opcion para NINGUN D_prev fisicamente posible este año, y el
        corte colapsa a la version sin disyuncion `alpha >= C1` (sigue
        siendo valido: Phi_y(D) = C1 para todo D en ese caso).

        Devuelve True si agrego el corte, False si `child` no tiene
        degradacion o "D" no esta en `mu` (nada que hacer)."""
        if child.mine_system.battery_degradation is None:
            return False
        if "D" not in mu:
            return False

        y = child.year
        x_hat_base = x_hat_by_year[parent.year]
        x_hat_D = x_hat_base["D"]

        if verbose:
            print(f"[NestedBenders] {label_prefix}BACKWARD anio {y}  "
                  f"corte disyuntivo R=0/R=1 (reemplazo bateria)...")

        C1 = self._replace_branch_cost(child, label=f"{label_prefix}replace-branch y={y}")

        phi0_target = self._fixed_branch_value(
            child, fix_vars={("R", y): 0}, label=f"{label_prefix}no-replace-branch y={y}",
            catch_infeasible=True,
        )

        if phi0_target is None:
            relaxed = self._build_strengthened_relaxation(
                child, {"D": mu["D"]}, fix_vars={("R", y): 0}
            )
            try:
                _solve(relaxed, label=f"{label_prefix}no-replace-branch-relaxed y={y}",
                       **self.solver_kwargs)
            except RuntimeError:
                relaxed = None

            if relaxed is None:
                parent.model.cuts.add(parent.model.alpha >= C1)
                if verbose:
                    print(f"[NestedBenders] {label_prefix}corte disyuntivo y={y}  "
                          f"R=0 infactible para CUALQUIER D_prev fisicamente posible -- "
                          f"reemplazo obligatorio, corte colapsa a alpha >= C1={C1:,.2f}")
                return True

            mu0_D = mu["D"]
            phi0_cut = value(relaxed.obj_lagrangian) - mu0_D * x_hat_D
        else:
            phi0_cut, mu0 = self._strengthened_subgradient_cut(
                child, x_hat_base, mu_init={"D": mu["D"]},
                verbose=verbose, label_prefix=f"{label_prefix}[R=0] ",
                fix_vars={("R", y): 0}, target=phi0_target,
            )
            mu0_D = mu0["D"]

        # M: cota superior segura para ambos lados de la disyuncion.
        # f0(D) = phi0_cut + mu0_D*(x_hat_base_D - D) es afin en D, asi
        # que su maximo en el rango fisico [0, D_U] cae en un extremo.
        D_U = value(parent.model.B_U)
        f0_max = phi0_cut + mu0_D * x_hat_D - mu0_D * (D_U if mu0_D < 0 else 0.0)
        M = 2.0 * max(f0_max, C1, 0.0) + 1.0

        z = pyo.Var(domain=pyo.Binary)
        setattr(parent.model, f"_replace_disjunct_y{y}_k{iteration}", z)

        D_var = getattr(parent.model, "D")
        expr_f0 = phi0_cut + mu0_D * (x_hat_D - D_var[parent.year])
        parent.model.cuts.add(parent.model.alpha >= expr_f0 - M * z)
        parent.model.cuts.add(parent.model.alpha >= C1 - M * (1 - z))

        if verbose:
            print(f"[NestedBenders] {label_prefix}corte disyuntivo y={y}  "
                  f"C1(reemplazar)={C1:,.2f}  f0_cut(no reemplazar)@x_hat={phi0_cut:,.2f}  "
                  f"mu_D={mu0_D:.6g}  M={M:,.2f}")
        return True

    def run(self, x_hat_by_year, iteration=None, verbose=True, current_ub=None, current_lb=None,
            strengthen=True):
        k_tag = f"k={iteration} " if iteration is not None else ""
        bounds_tag = _bounds_tag(current_ub, current_lb)

        for i in range(len(self.blocks) - 1, 0, -1):
            child = self.blocks[i]
            parent = self.blocks[i - 1]

            if verbose:
                print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                      f"relajando LP y leyendo duales (corte -> anio {parent.year})...")
            phi_lp, mu = self._relax_and_solve(child, label=f"backward y={child.year}")

            use_lagrangean = (
                self.degradation_cut_mode == "lagrangean"
                and child.mine_system.battery_degradation is not None
            )

            if use_lagrangean:
                if verbose:
                    print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                          f"Camino B: corte Lagrangeano con subgradiente "
                          f"(fisica exacta de degradacion)...")
                phi_cut, mu_cut = self._lagrangean_subgradient_cut(
                    child, x_hat_by_year[parent.year], mu_init=mu,
                    verbose=verbose, label_prefix=f"{k_tag}", **self.lagrangean_kwargs,
                )
            else:
                phi_cut, mu_cut = phi_lp, mu
                # REACTIVADO 2026-09 (estuvo pausado desde el merge 2026-08).
                # Se habia pausado para re-evaluar si el diagnostico original
                # --mu=0 por degeneracion del LP bajo "recurso completo
                # garantizado"-- seguia aplicando tras reestructurar G/H a
                # estado "global_once". La re-evaluacion mostro que aquel mu=0
                # era propio de la instancia 960kW_2dias y no del modelo, asi
                # que el bloqueo ya no aplica (ver CORRECCION en el docstring
                # de _strengthened_subgradient_cut). Queda tras el flag
                # `strengthen`, apagado por defecto, porque encarece el
                # backward: hasta max_iter MILP extra por año y por iteracion.
                if strengthen:
                    if verbose:
                        tipo = ("Strengthened Benders, Lara ec. (63)"
                                if self.strengthen_max_iter == 1
                                else f"Lagrangeano, Lara ec. (62), <={self.strengthen_max_iter} it")
                        print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                              f"fortaleciendo corte ({tipo})...")
                    self.strengthen_attempts += 1
                    if self.strengthen_max_iter == 1:
                        # ec. (63) EN SITU y con cota dual (portado de
                        # battery_swapping_multiaño, ver _lagrangean_bound).
                        phi_cut, _uso = self._lagrangean_bound(
                            child, mu, phi_lp, label=f"{k_tag}lagrangeano y={child.year}")
                    else:
                        phi_cut, mu_cut = self._strengthened_subgradient_cut(
                            child, x_hat_by_year[parent.year], mu_init=mu,
                            max_iter=self.strengthen_max_iter,
                            verbose=verbose, label_prefix=f"{k_tag}",
                        )

            # PISO EN Phi^LP: si el fortalecido (o el Camino B) no supero la
            # constante del LP -- o no dejo cota --, el corte de Benders puro
            # (phi_lp, mu) es valido y nunca peor.
            if phi_cut is None or phi_cut <= phi_lp:
                phi_cut, mu_cut = phi_lp, mu
            elif strengthen and not use_lagrangean:
                self.strengthen_gain.append(phi_cut - phi_lp)
                if verbose:
                    print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  "
                          f"Phi^LP={phi_lp:,.2f} -> Phi^LR={phi_cut:,.2f} "
                          f"(+{phi_cut - phi_lp:,.2f})")

            self.cut_manager.add_cut(
                parent, phi_cut, mu_cut, x_hat_by_year[parent.year], iteration=iteration
            )

            # PAUSADO (mismo motivo que arriba, ver comentario junto a
            # `strengthen`): el corte disyuntivo R=0/R=1 (_disjunctive_replace_cut)
            # tambien depende de como year_block.py modela los estados, y
            # degradation_cut_mode ya no acepta "disjunctive" (ver __init__)
            # hasta que se retome este trabajo.
            # if self.degradation_cut_mode == "disjunctive":
            #     self._disjunctive_replace_cut(
            #         child, parent, x_hat_by_year, mu,
            #         iteration=iteration, verbose=verbose, label_prefix=k_tag,
            #     )

        # LB_k = Phi_1 relajado, con el corte que se le acaba de agregar
        # (documento sec. 7.2/8). Si el horizonte tiene un solo año, el
        # bucle de arriba no corre y esto es simplemente la relajacion de
        # ese unico bloque.
        if verbose:
            print(f"[NestedBenders] {k_tag}BACKWARD anio {self.blocks[0].year}  {bounds_tag}  "
                  f"relajando LP (cota inferior LB)...")
        phi_lp, _mu = self._relax_and_solve(
            self.blocks[0], label=f"backward LB y={self.blocks[0].year}", read_mu=False)
        if not strengthen:
            return phi_lp

        # Con cortes fortalecidos el año 1 se resuelve ademas como MILP (portado
        # de battery_swapping_multiaño): los cortes ya valen para el casco
        # entero, y relajar tambien la integralidad del PRIMER año tiraria
        # gratis parte de lo que acaban de comprar. Sigue siendo cota inferior
        # valida porque alpha_1 subestima el costo futuro verdadero; con
        # timelimit se lee la cota dual, que subestima siempre.
        first = self.blocks[0]
        if verbose:
            print(f"[NestedBenders] {k_tag}BACKWARD anio {first.year}  {bounds_tag}  "
                  f"MILP con los cortes actualizados (cota inferior)...")
        kwargs = dict(self.solver_kwargs)
        kwargs["timelimit"] = self.strengthened_timelimit
        result = _solve(first.model, label=f"cota y={first.year} (MILP)",
                        load_solutions=False, **kwargs)
        dual = _cota_dual(result)
        return phi_lp if dual is None else max(phi_lp, dual)
