import logging
import os

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


# Archivo centinela de parada manual (ver opt_model.make_gurobi_stop_callback).
# Hace falta ADEMAS del Ctrl+C porque en PowerShell un Ctrl+C sobre un pipeline
# con Tee-Object mata el proceso sin que Python vea nada. Aca se chequea DESPUES
# de cada bloque anual -- no durante --, asi que la parada tarda a lo sumo un
# solve de bloque (--solve_timelimit) en hacerse efectiva; a cambio, el bloque
# en curso termina limpio y el driver conserva la ultima pasada forward
# COMPLETA, que es la unica que deja un UB valido.
_stop_file = None


def set_stop_file(path):
    global _stop_file
    _stop_file = path


def stop_file_requested():
    return _stop_file is not None and os.path.exists(_stop_file)


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
           extra_options=None, require_optimal=False, load_solutions=True):
    """load_solutions=False deja las variables del modelo como estaban y solo
    devuelve el resultado (cotas primal/dual, condicion de termino). Lo usa el
    presolve de capacidad (driver._capacity_presolve), al que le alcanza con
    la cota dual de Gurobi (result.problem.lower_bound) y que no quiere pisar
    el punto de arranque del bloque -- y ademas asi un MILP que llega al
    TimeLimit sin incumbente sigue devolviendo su cota en vez de fallar al
    cargar una solucion que no existe."""
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

    result = opt.solve(model, tee=tee, load_solutions=load_solutions)

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
    if stop_file_requested():
        print(f"[NestedBenders] parada manual pedida ({_stop_file}): se corta despues "
              f"de {label} y se conserva la mejor solucion completa.", flush=True)
        raise KeyboardInterrupt(f"Parada manual por archivo STOP (despues de {label}).")
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
        (n_ssee_k, G, H) no se pueden ampliar despues."""
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

        Hace falta porque el objetivo elastico l1 tiene duales +-1 por
        construccion y, en un optimo degenerado, reparte mu = 1 entre familias
        cuya holgura es CERO, que no tienen nada que ver con la infactibilidad.
        Medido sobre este modelo: al anio 2 solo le faltaba n_ssee_k, pero el
        corte salia con mu = 1 tambien en bahias, cargadores, baterias,
        generacion y almacenamiento; el anio anterior lo satisfacia agregando
        0.03 de almacenamiento -- gratis y completamente inutil -- sin construir
        la subestacion que hacia falta, y el forward volvia a fallar.

        Fijar las holguras nulas NO cambia v: la solucion anterior sigue siendo
        factible en el problema mas restringido, asi que el optimo es el mismo
        (verificado, 0.028216 en los dos casos). Lo unico que cambia es el dual,
        que queda concentrado en las familias que realmente atan. De paso deja
        el corte soportado solo en n_ssee_k, que es entera, y eso es lo que
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

    def __init__(self, blocks, cut_manager=None, solver_kwargs=None,
                 cut_type="benders", strengthened_timelimit=300):
        """:param cut_type: "benders" (ec. 59 de Lara et al. 2018) toma la
            constante del corte de la relajacion LINEAL del bloque hijo.
            "strengthened" (ec. 63) reusa EL MISMO mu del dual del LP pero
            calcula la constante con la relajacion LAGRANGEANA, que conserva la
            integralidad. No lleva el bucle de subgradiente del corte
            lagrangeano completo (ec. 62): un MILP extra por bloque y nada mas.

            Cuando elegir cada uno: el paper mide que el Benders puro es el mas
            rapido en SU instancia, y lo atribuye explicitamente a que ahi la
            relajacion lineal es apretada. Aca no lo es -- el LP del monolitico
            da 1.807.131 contra 2.212.229 que Gurobi prueba --, y el LB del
            backward se quedo clavado en ese valor 3 iteraciones seguidas. Con
            una relajacion floja el criterio del paper apunta al corte fuerte.

        :param strengthened_timelimit: segundos por MILP lagrangeano. Cortarlo
            NO invalida el corte: se usa la cota DUAL del solver, que subestima
            Phi^LR (ver _lagrangean_bound).
        """
        self.blocks = blocks
        self.cut_manager = cut_manager or BendersCutManager()
        self.solver_kwargs = solver_kwargs or {}
        if cut_type not in ("benders", "strengthened"):
            raise ValueError("cut_type tiene que ser 'benders' o 'strengthened'")
        self.cut_type = cut_type
        self.strengthened_timelimit = strengthened_timelimit
        # Cuanto levanto el corte fuerte sobre el de LP, para poder decidir si
        # paga su costo sin tener que releer los logs. `attempts` cuenta los
        # intentos REALES: el pre-backward del warm start agrega una pasada de
        # mas sobre las iteraciones, asi que el total no se deduce de k.
        self.strengthen_gain = []
        self.strengthen_attempts = 0

    def _lagrangean_bound(self, block, mu, phi_lp, label):
        """Constante del Strengthened Benders cut: Phi^LR(mu) con la
        integralidad del bloque PUESTA, usando el mu que ya salio del dual del
        LP. Devuelve (valor, se_uso_el_fuerte).

        VALIDEZ CON TIMELIMIT. El corte necesita un valor que SUBESTIME
        Phi^LR; si el MILP se corta por tiempo, su incumbente lo SOBREestima y
        armar el corte con el lo haria demasiado fuerte -- podria recortar
        soluciones factibles y producir LB > UB. Por eso se lee la cota DUAL
        (result.problem.lower_bound), que subestima siempre, y por eso el solve
        va con load_solutions=False: asi un MILP que ni siquiera encontro
        incumbente igual devuelve su cota en vez de fallar al cargar.

        PISO EN Phi^LP. Con el mu optimo del LP vale Phi^LR >= Phi^LP, porque
        el minimo sobre el conjunto entero no puede ser menor que sobre su
        relajacion. Si el MILP se corta antes de levantar la cota por encima de
        Phi^LP, se devuelve Phi^LP: el corte queda exactamente igual al de
        Benders puro, nunca peor.
        """
        kwargs = dict(self.solver_kwargs)
        kwargs["timelimit"] = self.strengthened_timelimit
        with block.lagrangean_mode(mu) as model:
            result = _solve(model, label=label, load_solutions=False, **kwargs)
        dual = result.problem.lower_bound
        if dual is None or dual != dual or dual in (float("inf"), float("-inf")):
            return phi_lp, False
        if dual <= phi_lp:
            return phi_lp, False
        return float(dual), True

    def _relax_and_solve(self, block, label):
        """Devuelve (Phi^LP, mu) del bloque relajado. Se relaja EN SITU: ver
        YearBlockBuilder.relaxed_mode -- aca se relaja una vez por anio y por
        iteracion, asi que el costo del clone se paga N_anios * N_iteraciones
        veces (medido: 1.5x mas rapido en sitio, y mucho mas si falta memoria)."""
        with block.relaxed_mode() as model:
            _solve(model, label=label, require_optimal=True, **self.solver_kwargs)
            phi_lp = value(model.obj)
            mu = self.cut_manager.read_duals(model, block.year, block.state_links)
        return phi_lp, mu

    def run(self, x_hat_by_year, iteration=None, verbose=True, current_ub=None,
            current_lb=None):
        k_tag = f"k={iteration} " if iteration is not None else ""
        bounds_tag = _bounds_tag(current_ub, current_lb)

        fuerte = self.cut_type == "strengthened"

        for i in range(len(self.blocks) - 1, 0, -1):
            child = self.blocks[i]
            parent = self.blocks[i - 1]

            if verbose:
                print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                      f"relajando LP y leyendo duales (corte -> anio {parent.year})...")
            phi_lp, mu = self._relax_and_solve(child, label=f"backward y={child.year}")

            phi = phi_lp
            if fuerte:
                if verbose:
                    print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  "
                          f"lagrangeano con integralidad (corte fortalecido)...")
                phi, uso = self._lagrangean_bound(
                    child, mu, phi_lp, label=f"lagrangeano y={child.year}")
                self.strengthen_attempts += 1
                if uso:
                    self.strengthen_gain.append(phi - phi_lp)
                    if verbose:
                        print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  "
                              f"Phi^LP={phi_lp:,.2f} -> Phi^LR={phi:,.2f} "
                              f"(+{phi - phi_lp:,.2f})")
                elif verbose:
                    print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  "
                          f"el lagrangeano no supero Phi^LP={phi_lp:,.2f}: "
                          f"el corte queda como el de Benders puro")

            self.cut_manager.add_cut(
                parent, phi, mu, x_hat_by_year[parent.year], iteration=iteration
            )

        # LB_k = Phi_1 con los cortes recien agregados (ec. 57 del paper). Si el
        # horizonte tiene un solo año no hay nada que propagar y el unico bloque
        # ya es la cota.
        #
        # Con cortes fortalecidos se resuelve el año 1 como MILP y no como LP:
        # los cortes ya valen para el casco entero, asi que relajar ademas la
        # integralidad del PRIMER año tiraria gratis parte de lo que acaban de
        # comprar. Sigue siendo cota inferior valida porque alpha_1 subestima el
        # costo futuro verdadero. Con timelimit se lee la cota dual, que
        # subestima siempre.
        first = self.blocks[0]
        if verbose:
            modo = "MILP" if fuerte else "LP"
            print(f"[NestedBenders] {k_tag}BACKWARD anio {first.year}  {bounds_tag}  "
                  f"resolviendo {modo} con los cortes actualizados (cota inferior)...")
        phi_lp, _ = self._relax_and_solve(first, label=f"backward y={first.year}")
        if not fuerte:
            return phi_lp

        kwargs = dict(self.solver_kwargs)
        kwargs["timelimit"] = self.strengthened_timelimit
        result = _solve(first.model, label=f"cota y={first.year} (MILP)",
                        load_solutions=False, **kwargs)
        dual = result.problem.lower_bound
        if dual is None or dual != dual or dual in (float("inf"), float("-inf")):
            return phi_lp
        return max(phi_lp, float(dual))


class _YearInfeasible(Exception):
    """Interna: un año del forward no tiene solucion con el estado que recibio.
    Lleva el indice del bloque y los estados ya calculados, para que run()
    pueda armar el corte de factibilidad sin volver a resolver nada."""

    def __init__(self, index, x_hat_by_year):
        super().__init__(f"bloque {index} infactible")
        self.index = index
        self.x_hat_by_year = x_hat_by_year
