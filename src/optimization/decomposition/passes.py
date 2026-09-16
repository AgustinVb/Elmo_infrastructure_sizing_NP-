import logging

from pyomo.environ import value, SolverFactory
from pyomo.opt import TerminationCondition, SolverStatus

from src.optimization.decomposition.cuts import BendersCutManager

_ACCEPTABLE = (
    TerminationCondition.optimal,
    TerminationCondition.maxTimeLimit,
    TerminationCondition.feasible,
)

# Bandera de interrupcion (Ctrl+C) compartida entre driver.py y _solve().
# NECESARIA, y no alcanza sola: un SIGINT que llega en medio de un solve activo
# de Gurobi puede ser capturado por el propio Gurobi a nivel NATIVO (imprime su
# "Interrupt request received"), y en ese caso el signal.signal() de Python de
# driver.py nunca llega a ejecutarse -- Gurobi instala su propio manejador
# mientras optimiza. Por eso _solve() chequea ADEMAS el estado del resultado.
_interrupt_requested = False


def request_interrupt():
    global _interrupt_requested
    _interrupt_requested = True


def clear_interrupt():
    global _interrupt_requested
    _interrupt_requested = False


# El backend directo de Gurobi avisa "Cannot get duals for MIP." cada vez que
# resuelve un bloque como MILP teniendo declarado un Suffix de duales (aunque no
# se le pidan en ese solve: son para el backward, no para el forward). Es
# esperado y no indica ningun problema.
logging.getLogger("pyomo.solvers").setLevel(logging.ERROR)


class SubproblemInfeasible(RuntimeError):
    """El subproblema no tiene solucion. Se distingue de cualquier otro fallo
    del solver porque es el unico caso que el forward puede recuperar con un
    corte de factibilidad."""


def _fmt_bound(v):
    if v is None or v in (float("inf"), float("-inf")):
        return "(sin cota aun)"
    return f"{v:,.2f}"


def _bounds_tag(current_ub, current_lb):
    return f"[UB={_fmt_bound(current_ub)}  LB={_fmt_bound(current_lb)}]"


def _solve(model, solvername="gurobi", gap=0.001, timelimit=900, tee=False, label="",
           extra_options=None, require_optimal=False):
    if solvername == "gurobi":
        opt = SolverFactory("gurobi", solver_io="python")
        opt.options["MIPGap"] = gap
        opt.options["TimeLimit"] = timelimit
        opt.options["OutputFlag"] = 0
        if extra_options:
            for opt_name, opt_val in extra_options.items():
                opt.options[opt_name] = opt_val
    else:
        opt = SolverFactory(solvername)

    result = opt.solve(model, tee=tee, load_solutions=True)

    # result.solver.status == aborted NO es exclusivo de una interrupcion real:
    # pyomo le asigna el mismo estado a grb.INTERRUPTED (Ctrl+C) y a
    # grb.TIME_LIMIT (un desenlace normal, ya contemplado como aceptable). La
    # señal confiable de interrupcion es termination_condition, no status solo.
    aborted_by_interrupt = (
        result.solver.status == SolverStatus.aborted
        and result.solver.termination_condition not in _ACCEPTABLE
    )
    if _interrupt_requested or aborted_by_interrupt:
        raise KeyboardInterrupt(f"Interrumpido por el usuario durante el solve ({label}).")
    if result.solver.termination_condition == TerminationCondition.maxTimeLimit:
        ub = result.problem.upper_bound
        lb = result.problem.lower_bound
        if ub not in (None, 0) and lb is not None:
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
        # Los cortes se arman con la constante del LP (phi_lp, v_hat) emparejada
        # con los duales. La constante VALIDA es el valor objetivo DUAL, que
        # coincide con el primal solo en el optimo: si el LP se corta por tiempo
        # queda un primal por ENCIMA del optimo junto a un mu dual-factible, y el
        # corte resultante es demasiado fuerte -- puede cortar estados factibles.
        # Se manifiesta despues como LB > UB, lejos de su causa. Preferimos fallar
        # aca antes que emitir un corte invalido.
        raise RuntimeError(
            f"El LP de {label} no llego al optimo ({result.solver.termination_condition}): "
            f"un corte armado con su valor primal podria ser invalido. Subir "
            f"timelimit para los solves de LP."
        )
    return result


class ForwardPass(object):
    """Resuelve cada bloque como MILP completo en orden y1 -> Y, propagando el
    estado optimo de cada año al parametro heredado del siguiente. Produce una
    solucion factible de todo el horizonte, que es el candidato a cota
    superior."""

    def __init__(self, blocks, solver_kwargs=None, cut_manager=None):
        self.blocks = blocks  # lista de YearBlockBuilder, ordenada y1..Y
        self.solver_kwargs = solver_kwargs or {}
        # Necesario para los cortes de factibilidad: cuando un año no tiene
        # continuacion hay que prohibir ese estado en el año anterior.
        self.cut_manager = cut_manager
        self.feasibility_cuts_added = 0

    def run(self, iteration=None, verbose=True, current_ub=None, current_lb=None,
            max_recoveries=25):
        """Recorre los años y, si alguno queda infactible con el estado que
        recibio, agrega un corte de factibilidad al año anterior y REEMPIEZA el
        recorrido. Sin ese mecanismo la trayectoria simplemente no se puede
        completar: el modelo no tiene holguras que garanticen recurso
        relativamente completo, y las inversiones que se deciden una sola vez
        (N_max_k, G, H) no se pueden ampliar despues."""
        recoveries = 0
        while True:
            try:
                return self._sweep(iteration, verbose, current_ub, current_lb)
            except _YearInfeasible as exc:
                if exc.index == 0:
                    raise RuntimeError(
                        f"El primer año ({self.blocks[0].year}) es infactible por si "
                        f"solo: no hay año anterior al que agregarle un corte, asi que "
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
                f"El año {child.year} es infactible y ForwardPass no tiene "
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
                f"El año {child.year} es infactible como MILP pero su relajacion "
                f"elastica da violacion nula: la infactibilidad no viene de que le "
                f"falte estado heredado sino de la integralidad, y un corte de "
                f"factibilidad no la puede corregir."
            )

        if verbose:
            faltantes = {k: v for k, v in mu.items()
                         if (max(v.values()) if isinstance(v, dict) else v) > 1e-7}
            print(f"[NestedBenders] FORWARD  falta de estado v={v_hat:,.4f}; "
                  f"familias que atan: {sorted(faltantes)}")
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

        Hace falta porque el objetivo elastico l1 tiene duales +-1 por
        construccion y, en un optimo degenerado, reparte mu = 1 entre familias
        cuya holgura es CERO, que no tienen nada que ver con la infactibilidad.
        Medido sobre este modelo: al anio 2 solo le faltaba N_max_k, pero el
        corte salia con mu = 1 tambien en bahias, cargadores, baterias,
        generacion y almacenamiento; el anio anterior lo satisfacia agregando
        0.03 de almacenamiento -- gratis y completamente inutil -- sin construir
        la subestacion que hacia falta, y el forward volvia a fallar.

        Fijar las holguras nulas NO cambia v: la solucion anterior sigue siendo
        factible en el problema mas restringido, asi que el optimo es el mismo
        (verificado, 0.028216 en los dos casos). Lo unico que cambia es el dual,
        que queda concentrado en las familias que realmente atan. De paso deja
        el corte soportado solo en N_max_k, que es entera, y eso es lo que
        habilita el redondeo de add_feasibility_cut.
        """
        fijadas = 0
        for link in child.state_links:
            if link["hat"] is None:
                continue
            s = getattr(feas, "feas_slack_" + link["state"])
            componentes = ([s] if link["index_set"] is None
                           else [s[idx] for idx in link["index_set"]])
            for comp in componentes:
                if not comp.fixed and value(comp) <= tol:
                    comp.fix(0.0)
                    fijadas += 1
        return fijadas

    def _sweep(self, iteration=None, verbose=True, current_ub=None, current_lb=None):
        x_hat_by_year = {}
        phi_by_year = {}
        alpha_by_year = {}
        full_solution_by_year = {}

        n = len(self.blocks)
        for i, block in enumerate(self.blocks):
            k_tag = f"k={iteration} " if iteration is not None else ""
            if verbose:
                print(f"[NestedBenders] {k_tag}FORWARD  anio {block.year} ({i + 1}/{n})  "
                      f"{_bounds_tag(current_ub, current_lb)}  resolviendo MILP...")
            if i > 0:
                block.set_heritage(x_hat_by_year[self.blocks[i - 1].year])
            try:
                _solve(block.model, label=f"forward y={block.year}", **self.solver_kwargs)
            except SubproblemInfeasible:
                raise _YearInfeasible(i, x_hat_by_year)
            phi_by_year[block.year] = value(block.model.obj)
            alpha_by_year[block.year] = value(block.model.alpha)
            x_hat_by_year[block.year] = block.extract_state()
            full_solution_by_year[block.year] = block.extract_full_solution()

        # UB_k = sum_y (Phi_{y,k} - alpha_{y,k}): se resta alpha para no contar
        # dos veces el costo futuro, que ya esta en los f_y de los años
        # siguientes.
        ub = sum(phi_by_year[b.year] - alpha_by_year[b.year] for b in self.blocks)

        return {
            "ub": ub,
            "phi": phi_by_year,
            "alpha": alpha_by_year,
            "x_hat": x_hat_by_year,
            "full_solution": full_solution_by_year,
        }


class BackwardPass(object):
    """Para y=Y..y1+1, relaja la integralidad del bloque de ese año (con el
    estado que dejo el forward de esta iteracion), lee los duales de las
    igualdades de enlace y agrega el corte resultante al año y-1.

    Como el bucle va de mayor a menor, el corte agregado a un año ya queda
    reflejado cuando ese mismo bloque se relaja como "hijo" en el paso
    siguiente: asi la informacion del final del horizonte alcanza a y1 dentro de
    una misma iteracion, y no a razon de una iteracion por año. Eso es lo que
    distingue al esquema anidado de aplicar Benders año a año por separado.
    """

    def __init__(self, blocks, cut_manager=None, solver_kwargs=None):
        self.blocks = blocks
        self.cut_manager = cut_manager or BendersCutManager()
        self.solver_kwargs = solver_kwargs or {}

    def _relax_and_solve(self, block, label):
        """Devuelve (Phi^LP, mu) del bloque relajado. Se relaja EN SITU: ver
        YearBlockBuilder.relaxed_mode -- clonar cuesta dos ordenes de magnitud
        mas que construir el bloque entero, y aca se relaja una vez por anio y
        por iteracion."""
        with block.relaxed_mode() as model:
            _solve(model, label=label, require_optimal=True, **self.solver_kwargs)
            phi_lp = value(model.obj)
            mu = self.cut_manager.read_duals(model, block.year, block.state_links)
        return phi_lp, mu

    def run(self, x_hat_by_year, iteration=None, verbose=True, current_ub=None,
            current_lb=None):
        k_tag = f"k={iteration} " if iteration is not None else ""
        bounds_tag = _bounds_tag(current_ub, current_lb)

        for i in range(len(self.blocks) - 1, 0, -1):
            child = self.blocks[i]
            parent = self.blocks[i - 1]

            if verbose:
                print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                      f"relajando LP y leyendo duales (corte -> anio {parent.year})...")
            phi_lp, mu = self._relax_and_solve(child, label=f"backward y={child.year}")

            self.cut_manager.add_cut(
                parent, phi_lp, mu, x_hat_by_year[parent.year], iteration=iteration
            )

        # LB_k = Phi_1 relajado, con el corte que se le acaba de agregar. Si el
        # horizonte tiene un solo año no hay nada que propagar y la relajacion
        # del unico bloque ya es la cota.
        first = self.blocks[0]
        if verbose:
            print(f"[NestedBenders] {k_tag}BACKWARD anio {first.year}  {bounds_tag}  "
                  f"relajando LP con los cortes actualizados (cota inferior)...")
        phi_lp, _ = self._relax_and_solve(first, label=f"backward y={first.year}")
        return phi_lp


class _YearInfeasible(Exception):
    """Interna: un año del forward no tiene solucion con el estado que recibio.
    Lleva el indice del bloque y los estados ya calculados, para que run()
    pueda armar el corte de factibilidad sin volver a resolver nada."""

    def __init__(self, index, x_hat_by_year):
        super().__init__(f"bloque {index} infactible")
        self.index = index
        self.x_hat_by_year = x_hat_by_year
