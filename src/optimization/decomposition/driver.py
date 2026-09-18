import math
import os
import signal
import sys
import time

from src.optimization.decomposition import passes as passes_module
from src.optimization.decomposition.cuts import BendersCutManager
import pyomo.environ as pyo
from pyomo.environ import value

from src.optimization.decomposition.passes import BackwardPass, ForwardPass, _solve
from src.optimization.decomposition.year_block import YearBlockBuilder

# El limite de recursion por defecto de Python no alcanza para que dill recorra
# el grafo de objetos de un YearBlockBuilder ya construido (Pyomo encadena
# component -> parent_block -> model -> ... a lo largo de miles de
# ConstraintData/VarData) al devolverlo del worker al proceso principal.
_RECURSION_LIMIT_FOR_DILL = 50000

_worker_state = {}


def _init_year_block_worker(mine_system, time_series, autonomous_mode):
    sys.setrecursionlimit(_RECURSION_LIMIT_FOR_DILL)
    _worker_state["mine_system"] = mine_system
    _worker_state["time_series"] = time_series
    _worker_state["autonomous_mode"] = autonomous_mode


def _build_year_block_task(payload):
    year, is_last_year, exogenous_stations = payload
    return YearBlockBuilder(
        _worker_state["mine_system"], _worker_state["time_series"], year=year,
        is_last_year=is_last_year, exogenous_stations=exogenous_stations,
        autonomous_mode=_worker_state["autonomous_mode"],
    )


def infer_exogenous_stations(mine_system, time_series):
    """X exogeno por defecto: se construye toda nave que tenga al menos un
    equipo asignado en la hoja StationAssignment, en TODOS los años del
    horizonte. La asignacion equipo-nave es estatica, asi que ya esta implicita
    en los datos de entrada y no hace falta pedirla aparte."""
    lhds = mine_system.get_system_lhds()
    stations = mine_system.get_system_stations()
    time_series.get_station_assignment(lhds)
    time_series.get_elhd_at_station(stations)
    per_station = time_series.mapper.get('elhd_per_station', {})
    return {
        y: {k: (1 if per_station.get(k) else 0) for k in stations}
        for y in sorted(time_series.years)
    }


class NestedBendersSolver(object):
    """Bucle completo forward/backward. Construye todos los bloques anuales una
    sola vez y despues itera, guardando la MEJOR solucion factible encontrada:
    la cota superior no es monotona entre iteraciones, porque los cortes cambian
    el subproblema de cada año."""

    def __init__(self, mine_system, time_series, exogenous_stations_by_year=None,
                 gap_tol=0.01, max_iter=20, autonomous_mode=False, solver_kwargs=None,
                 block_build_jobs=None, monolithic_lp_bound=True,
                 capacity_presolve="peak"):
        """
        :param capacity_presolve: "peak" (default), "all" u "off". Presolve de
            capacidad de subestacion (ver _capacity_presolve): antes de
            iterar, calcula por nave la cota inferior n_ssee_k[k] >= max_y
            n*_y(k) resolviendo el problema auxiliar de cada anio elegido con
            todo el estado heredado libre, y la impone en el bloque del anio
            1 y en el LP monolitico. "peak" resuelve solo el anio de mayor
            meta de produccion (el que gobierna la capacidad); "all" resuelve
            todos los anios y toma el maximo (cota mas fuerte, |K|*|Y| MILP en
            vez de |K|). Es una desigualdad valida, no una heuristica: no
            invalida ni el UB ni el LB.
        :param exogenous_stations_by_year: dict {year: {k: 0/1}} con X fijo por
            año y nave. Si se omite se infiere de la asignacion equipo-nave
            (ver infer_exogenous_stations).
        :param monolithic_lp_bound: calcular, una sola vez antes de iterar, la
            relajacion lineal del monolitico completo y usarla como cota
            inferior inicial (ver _monolithic_lp_bound). Cuesta construir el
            monolitico, que es justo lo que la descomposicion evita, asi que en
            horizontes largos puede no entrar en memoria; cualquier fallo se
            reporta y la corrida sigue sin esa cota.
        :param block_build_jobs: procesos para construir los bloques en paralelo.
            None = min(años, cpus). 1 = secuencial (util para debug). Los bloques
            son independientes entre si, y construirlos es Pyomo puro-Python
            (miles de llamadas a `rule=`), CPU-bound y de un solo hilo cada uno.
        """
        self.mine_system = mine_system
        self.time_series = time_series
        self.years = sorted(time_series.years)
        self.gap_tol = gap_tol
        self.max_iter = max_iter
        self.solver_kwargs = solver_kwargs or {}
        self.autonomous_mode = autonomous_mode
        self.exogenous_stations_by_year = (
            exogenous_stations_by_year if exogenous_stations_by_year is not None
            else infer_exogenous_stations(mine_system, time_series)
        )

        self.blocks = self._build_blocks(block_build_jobs)

        self.cut_manager = BendersCutManager()
        self.forward_pass = ForwardPass(self.blocks, solver_kwargs=self.solver_kwargs,
                                        cut_manager=self.cut_manager)
        self.backward_pass = BackwardPass(
            self.blocks, cut_manager=self.cut_manager, solver_kwargs=self.solver_kwargs
        )

        self.monolithic_lp_bound = monolithic_lp_bound
        self.lb_monolithic_lp = None
        if capacity_presolve not in ("peak", "all", "off", None):
            raise ValueError(f"capacity_presolve debe ser 'peak', 'all' u 'off' "
                             f"(recibido {capacity_presolve!r})")
        self.capacity_presolve = capacity_presolve if capacity_presolve != "off" else None
        # {k: cota entera} que dejo el presolve; None si no corrio.
        self.capacity_bounds = None
        # Cota agregada sum_k n_ssee_k >= v, solo si supera la suma de las
        # cotas por nave; None si no corrio o es redundante.
        self.capacity_sum_bound = None
        self.capacity_presolve_time = 0.0

        self.ub = float("inf")
        self.lb = float("-inf")
        self.best_ub = float("inf")
        self.best_solution = None
        self.best_full_solution = None
        self.gap_history = []
        self.iterations_run = 0

    def _build_blocks(self, block_build_jobs):
        tasks = [
            (y, y == self.years[-1], self.exogenous_stations_by_year[y])
            for y in self.years
        ]

        n_jobs = block_build_jobs
        if n_jobs is None:
            n_jobs = min(len(self.years), os.cpu_count() or 1)

        if n_jobs <= 1 or len(self.years) <= 1:
            return [
                YearBlockBuilder(
                    self.mine_system, self.time_series, year=year,
                    is_last_year=is_last_year, exogenous_stations=exogenous_stations,
                    autonomous_mode=self.autonomous_mode,
                )
                for year, is_last_year, exogenous_stations in tasks
            ]

        # `multiprocess` (no `multiprocessing`): serializa con dill, que si
        # soporta las lambdas/closures con que Pyomo arma las reglas.
        try:
            import multiprocess
        except ImportError:
            print("[NestedBenders] `multiprocess` no esta instalado: los bloques "
                  "se construyen secuencialmente.")
            return self._build_blocks(1)

        sys.setrecursionlimit(_RECURSION_LIMIT_FOR_DILL)
        with multiprocess.Pool(
            processes=n_jobs, initializer=_init_year_block_worker,
            initargs=(self.mine_system, self.time_series, self.autonomous_mode),
        ) as pool:
            return pool.map(_build_year_block_task, tasks)

    def _production_target_by_year(self):
        """Meta anual de produccion, sum_j m_j[j, y], leida del bloque de cada
        anio (mismo parametro que usa ConstraintRules.daily_production)."""
        return {
            blk.year: sum(value(blk.model.m_j[j, blk.year]) for j in blk.model.nodes_set)
            for blk in self.blocks
        }

    def _capacity_presolve(self, verbose=True):
        """Presolve de capacidad de subestacion. Devuelve {k: cota entera} y la
        deja impuesta como cota inferior de n_ssee_k[k] en el bloque del anio 1.

        EL PROBLEMA. n_ssee_k se decide una sola vez para todo el horizonte,
        pero el anio 1 la dimensiona mirando solo su propia demanda, que es la
        mas chica del perfil. El requerimiento de los anios de mayor produccion
        viaja hacia atras un anio por vez -- el anio 3 corta al 2, el 2 queda
        infactible y corta al 1 -- y cada corte de factibilidad reinicia el
        barrido completo desde el anio 1. Medido en carga on board a 6 anios:
        9 cortes, 2.060 s.

        LA COTA. Cualquier solucion factible del horizonte tiene que servirle a
        todos los anios, asi que

            n_ssee_k[k]  >=  max_y  n*_y(k),
            n*_y(k) = min { n_ssee_k[k] : anio y factible, estado heredado libre }

        El auxiliar es una relajacion del anio en contexto (ver
        YearBlockBuilder.capacity_presolve_mode), asi que n*_y(k) es cota
        inferior de lo que el anio necesita de verdad y la desigualdad es
        valida para el problema completo con X exogena: se puede imponer en el
        anio 1 sin invalidar el UB ni el LB, y sirve tambien para apretar el LP
        monolitico (la inversion en subestacion sube en la relajacion).

        Se usa la cota dual del MILP, ceil(ObjBound): valida aunque el solve se
        corte por tiempo, y como el objetivo es una entera sin coeficientes
        Gurobi termina apenas ObjBound > incumbente - 1.

        FUERZA. Con el heredado libre el anio elige la combinacion que minimiza
        n_ssee_k, y las naves se acoplan por la meta diaria total, asi que la
        cota puede quedar por debajo de lo que el forward termina comprando.
        Se mide al final de solve() comparandola contra el n_ssee_k elegido.

        COTA AGREGADA. Ademas de los minimos por nave se resuelve, por anio,
        min sum_k n_ssee_k[k] con el mismo auxiliar, y se impone
        sum_k n_ssee_k[k] >= max_y de ese minimo. Es el complemento de los
        minimos por nave: las naves se acoplan por la meta diaria total, asi
        que "una u otra nave necesita un modulo mas" da minimo 0 en cada una
        por separado y si aparece en la suma. Medido en swap (160kW_2dias, 2
        anios): el unico corte de factibilidad del forward era exactamente
        n_2 + n_3 >= 1, invisible para las cotas por nave (1,0,0).

        QUE ANIOS. "peak": solo el de mayor meta de produccion, que es el que
        gobierna la capacidad; cuesta |K|+1 MILP de un anio. "all": todos,
        (|K|+1)*|Y| MILP, cota mas fuerte.
        """
        targets = self._production_target_by_year()
        if self.capacity_presolve == "peak":
            peak = max(targets, key=targets.get)
            selected = [blk for blk in self.blocks if blk.year == peak]
        else:
            selected = list(self.blocks)

        if verbose:
            perfil = "  ".join(f"{y}:{t:,.0f}" for y, t in sorted(targets.items()))
            print(f"[NestedBenders] presolve de capacidad ({self.capacity_presolve}): "
                  f"meta por anio {perfil}")
            print(f"[NestedBenders] presolve de capacidad: anios "
                  f"{[blk.year for blk in selected]}, modulos de {self._ssee_step():,.0f} kW")
            sys.stdout.flush()

        stations = list(self.blocks[0].model.stations_set)
        bounds = {k: 0 for k in stations}
        argmax_year = {k: None for k in stations}
        sum_bound, sum_year = 0, None
        for blk in selected:
            t0 = time.time()
            with blk.capacity_presolve_mode(None) as aux:
                result = _solve(aux, label=f"presolve capacidad y={blk.year} suma",
                                load_solutions=False, **self.solver_kwargs)
            dual = result.problem.lower_bound
            if dual is None:
                raise RuntimeError(
                    f"el presolve de capacidad del anio {blk.year} (suma) no devolvio "
                    f"cota dual ({result.solver.termination_condition})"
                )
            v_min = max(0, int(math.ceil(dual - 1e-6)))
            if verbose:
                primal = result.problem.upper_bound
                primal_tag = f"{primal:.3f}" if primal is not None else "sin incumbente"
                print(f"[NestedBenders] presolve capacidad  anio {blk.year}  suma: "
                      f"sum n_ssee_k >= {v_min}  (dual {dual:.4f}, primal {primal_tag}, "
                      f"{time.time() - t0:.0f}s)")
                sys.stdout.flush()
            if v_min > sum_bound:
                sum_bound, sum_year = v_min, blk.year
            for k in stations:
                if not self.exogenous_stations_by_year[blk.year].get(k, 0):
                    continue  # nave cerrada ese anio: n*_y(k) = 0, trivial
                t0 = time.time()
                with blk.capacity_presolve_mode(k) as aux:
                    result = _solve(aux, label=f"presolve capacidad y={blk.year} {k}",
                                    load_solutions=False, **self.solver_kwargs)
                dual = result.problem.lower_bound
                primal = result.problem.upper_bound
                if dual is None:
                    raise RuntimeError(
                        f"el presolve de capacidad del anio {blk.year}, nave {k}, "
                        f"no devolvio cota dual ({result.solver.termination_condition})"
                    )
                n_min = max(0, int(math.ceil(dual - 1e-6)))
                if verbose:
                    primal_tag = f"{primal:.3f}" if primal is not None else "sin incumbente"
                    print(f"[NestedBenders] presolve capacidad  anio {blk.year}  {k}: "
                          f"n_ssee_k >= {n_min}  (dual {dual:.4f}, primal {primal_tag}, "
                          f"{time.time() - t0:.0f}s)")
                    sys.stdout.flush()
                if n_min > bounds[k]:
                    bounds[k] = n_min
                    argmax_year[k] = blk.year

        first = self.blocks[0].model
        for k, n_min in bounds.items():
            var = first.n_ssee_k[k]
            if var.lb is None or n_min > var.lb:
                var.setlb(n_min)
        # La agregada solo si dice algo mas que la suma de las cotas por nave.
        self.capacity_sum_bound = sum_bound if sum_bound > sum(bounds.values()) else None
        if self.capacity_sum_bound is not None:
            first.presolve_capacity_sum = pyo.Constraint(
                expr=sum(first.n_ssee_k[k] for k in stations) >= self.capacity_sum_bound
            )
        if verbose:
            resumen = "  ".join(
                f"{k}: >= {n} ({n * self._ssee_step():,.0f} kW, anio {argmax_year[k]})"
                for k, n in sorted(bounds.items())
            )
            print(f"[NestedBenders] presolve de capacidad: cotas impuestas al anio "
                  f"{self.blocks[0].year}: {resumen}")
            if self.capacity_sum_bound is not None:
                print(f"[NestedBenders] presolve de capacidad: cota agregada "
                      f"sum n_ssee_k >= {self.capacity_sum_bound} (anio {sum_year}; "
                      f"las cotas por nave suman {sum(bounds.values())})")
            else:
                print(f"[NestedBenders] presolve de capacidad: la cota agregada "
                      f"({sum_bound}) no agrega nada sobre las cotas por nave")
        return bounds

    @staticmethod
    def _ssee_step():
        from src.optimization.functions import P_SSEE_STEP
        return P_SSEE_STEP

    def _report_capacity_bound_strength(self):
        """Fuerza del presolve: la cota contra lo que el forward compro."""
        if self.capacity_bounds is None or self.best_solution is None:
            return
        chosen = self.best_solution[self.blocks[0].year].get("n_ssee_k", {})
        partes = []
        for k, n_min in sorted(self.capacity_bounds.items()):
            n_sol = chosen.get(k)
            if n_sol is None:
                continue
            n_sol = int(round(n_sol))
            tag = "ajustada" if n_sol == n_min else f"floja por {n_sol - n_min}"
            partes.append(f"{k}: cota {n_min}, elegido {n_sol} ({tag})")
        print(f"[NestedBenders] presolve de capacidad, fuerza de la cota: "
              f"{'; '.join(partes)}")

    def _monolithic_lp_bound(self):
        """Cota inferior adicional: el valor optimo de la relajacion lineal del
        modelo monolitico COMPLETO, calculado una sola vez antes de iterar.

        Es valida por construccion (relajar la integralidad no puede subir el
        optimo) y es independiente de los cortes, asi que se toma el maximo con
        la que produce el backward.

        Las dos no estan ordenadas, y por eso el maximo aporta de verdad. Al
        acumular cortes, alpha_1 converge por abajo a Phi^LP_2, que a su vez
        contiene alpha_2 convergiendo a Phi^LP_3, etc.: como el estado separa
        exactamente los anios, esa recursion converge PRECISAMENTE a la
        relajacion lineal del monolitico. Es decir que el backward aproxima
        desde abajo, iteracion a iteracion, el numero que esta funcion entrega
        directo. Pero los cortes de factibilidad fortalecidos con
        Chvatal-Gomory son validos solo para el casco entero y recortan puntos
        fraccionarios legitimos, asi que el LB del backward PUEDE superarla.

        Medido en data/DCH/160kW_2dias a 2 anios: z_LP comparable = 824,782 en
        85 s, contra 629,028 que el backward alcanzo en 5 iteraciones y media
        hora. Aun asi el gap sigue siendo grande (54% contra 65%): la
        relajacion lineal de este modelo es floja, y ninguna de las dos cotas
        converge sola al optimo.

        El objetivo se construye con exogenous_stations puesto -- SIN
        years_override -- para que quede en la misma base que el descompuesto:
        deja fuera el costo de apertura de naves (que en modo descompuesto es
        una constante) pero conserva las link_*_stock del monolitico, que es lo
        que hace que el modelo siga siendo el de siempre.

        Construye el monolitico entero, que es justamente el gasto de memoria
        que la descomposicion evita: en horizontes largos puede no entrar.
        Cualquier fallo se reporta y la corrida sigue sin esta cota.
        """
        import pyomo.environ as pyo
        from pyomo.environ import SolverFactory, TransformationFactory, value
        from pyomo.opt import TerminationCondition

        from src.optimization.functions import (
            BoundRules, ConstraintRules, ObjectiveRules, OptParameters, OptSets,
        )

        exo_flat = {
            (k, y): v
            for y, per_station in self.exogenous_stations_by_year.items()
            for k, v in per_station.items()
        }
        common = dict(exogenous_stations=exo_flat)

        model = pyo.ConcreteModel(name="MonoliticoLP")
        OptSets(self.mine_system, self.time_series,
                autonomous_mode=self.autonomous_mode, **common).build_sets(model)
        OptParameters(self.mine_system, self.time_series, **common).build_parameters(model)
        BoundRules(self.mine_system, self.time_series, **common).build_all_variables(model)
        ConstraintRules(self.mine_system, self.time_series, mccormick_degradation=True,
                        **common).build_all_constraints(model)
        model.obj = pyo.Objective(
            rule=ObjectiveRules(self.mine_system, self.time_series, **common).total_cost,
            sense=pyo.minimize,
        )
        if self.capacity_bounds:
            # Desigualdad valida del problema completo (ver _capacity_presolve):
            # aprieta la relajacion sin cambiar de problema. La inversion en
            # subestacion sube directo en el LB.
            for k, n_min in self.capacity_bounds.items():
                model.n_ssee_k[k].setlb(n_min)
            if self.capacity_sum_bound is not None:
                model.presolve_capacity_sum = pyo.Constraint(
                    expr=sum(model.n_ssee_k[k] for k in model.stations_set)
                    >= self.capacity_sum_bound
                )
        TransformationFactory("core.relax_integer_vars").apply_to(model)

        opt = SolverFactory("gurobi", solver_io="python")
        opt.options["OutputFlag"] = 0
        opt.options["TimeLimit"] = self.solver_kwargs.get("timelimit", 900)
        result = opt.solve(model, load_solutions=True)
        if result.solver.termination_condition != TerminationCondition.optimal:
            # Sin optimo probado no hay cota confiable: para un LP cortado por
            # tiempo la cota valida es la dual, que este backend no expone.
            raise RuntimeError(
                f"el LP del monolitico no llego al optimo "
                f"({result.solver.termination_condition})"
            )
        return value(model.obj)


    def build_report_model(self, output_folder, verbose=True):
        """Arma un OptModel monolitico de SOLO LECTURA con la mejor solucion
        encontrada ya cargada sobre sus variables, para que Printer genere los
        CSV y los graficos sin enterarse de que el problema se resolvio
        descompuesto.

        No se resuelve nada: se construye el modelo completo (que Printer
        necesita para recorrer sets e indices) y se le asignan los valores que
        el forward dejo en best_full_solution, anio por anio. Las variables
        internas de los bloques (alpha, *_hat, *_prev, feas_slack_*) no tienen
        equivalente en el monolitico y se descartan.

        X y Delta_X se reconstruyen aparte: en modo descompuesto son exogenas y
        no viven en la solucion de ningun bloque.

        Devuelve (opt_model, informe) con un informe de la verificacion
        numerica: el costo recomputado sobre este modelo, MENOS la inversion en
        estaciones, tiene que coincidir con el UB que reporto el solver -- si no
        coincide, la solucion se cargo mal o el objetivo descompuesto no es el
        que creemos.
        """
        import pyomo.environ as pyo
        from pyomo.environ import value

        from src.optimization.opt_model import OptModel

        if self.best_full_solution is None:
            raise RuntimeError(
                "No hay solucion que reportar: solve() no completo ninguna "
                "iteracion (o fue interrumpido antes de la primera)."
            )

        report = OptModel(
            self.mine_system, self.time_series, output_folder,
            autonomous_mode=self.autonomous_mode, mccormick_degradation=True,
        )
        model = report.model

        aplicadas = 0
        sin_equivalente = set()
        for year in self.years:
            for nombre, valores in self.best_full_solution.get(year, {}).items():
                comp = getattr(model, nombre, None)
                if comp is None or not isinstance(comp, pyo.Var):
                    sin_equivalente.add(nombre)
                    continue
                if isinstance(valores, dict):
                    for idx, val in valores.items():
                        if idx in comp:
                            comp[idx].value = val
                            aplicadas += 1
                elif comp.is_indexed():
                    sin_equivalente.add(nombre)
                else:
                    comp.value = valores
                    aplicadas += 1

        # X exogeno: Delta_X es lo que se abre en el anio, o sea la diferencia
        # contra el anio anterior (en el primero, el propio X).
        anterior = {k: 0 for k in model.stations_set}
        for y in self.years:
            for k in model.stations_set:
                actual = self.exogenous_stations_by_year[y][k]
                model.X[k, y].value = actual
                model.Delta_X[k, y].value = max(actual - anterior[k], 0)
            anterior = {k: self.exogenous_stations_by_year[y][k] for k in model.stations_set}

        # Printer.save_gurobi_log cae en un print con emoji si no encuentra el
        # log, y eso revienta la consola de Windows en cp1252. El modelo de
        # reporte nunca resuelve, asi que el log no existe: se deja uno.
        if not os.path.exists(report.gurobi_log_path):
            os.makedirs(os.path.dirname(report.gurobi_log_path), exist_ok=True)
            with open(report.gurobi_log_path, "w", encoding="utf-8") as fh:
                fh.write("Resuelto con Nested Benders (descomposicion por anio): "
                         "no hay un unico log de Gurobi para el horizonte completo.\n")

        # Habilita que este modelo se use ademas como MIP start del monolitico
        # (modo hibrido): solve_model solo pasa warmstart=True a Gurobi si esta
        # bandera esta puesta, y como aca los valores se cargan directo sobre las
        # variables -- no via _load_solution_warmstart_folder -- nadie la pondria.
        # Verificado sobre data/DCH/160kW_2dias_1MB: la solucion descompuesta
        # cargada asi no viola ninguna restriccion del monolitico ni deja ninguna
        # entera con valor fraccionario, asi que Gurobi la acepta como start; las
        # variables que quedan sin valor no aparecen en ninguna restriccion activa.
        report.has_warm_start = aplicadas > 0

        costo_total = value(model.obj)
        inv_estaciones = sum(
            value(model.station_cost_k[k]) * value(model.Delta_X[k, y])
            * value(report.objective_rules._discount_factor(model, y))
            for k in model.stations_set for y in self.years
        )
        comparable = costo_total - inv_estaciones
        desvio = abs(comparable - self.best_ub)
        report.opt_cost_result = costo_total

        # El desvio tiene que ser cero: el modelo de reporte es el MISMO
        # problema con los mismos valores. Si no lo es, o la solucion se cargo
        # mal (alguna variable sin equivalente que si importaba) o el objetivo
        # descompuesto no es el que creemos, y los archivos de salida estarian
        # describiendo una solucion distinta de la que reporto el algoritmo.
        # No se aborta -- descartar el reporte despues de horas de computo seria
        # peor -- pero tiene que quedar imposible de pasar por alto.
        tolerancia = max(1e-6 * abs(self.best_ub), 1e-4)
        desvio_ok = desvio <= tolerancia

        informe = {
            "valores_cargados": aplicadas,
            "variables_sin_equivalente": sorted(sin_equivalente),
            "costo_total": costo_total,
            "inversion_estaciones": inv_estaciones,
            "costo_comparable": comparable,
            "ub_del_solver": self.best_ub,
            "desvio": desvio,
            "desvio_ok": desvio_ok,
        }

        if verbose:
            print(f"[NestedBenders] reporte: {aplicadas:,} valores cargados; "
                  f"costo recomputado = {costo_total:,.2f} "
                  f"(comparable {comparable:,.2f} contra UB {self.best_ub:,.2f}, "
                  f"desvio {desvio:,.6f})")
            if sin_equivalente:
                print(f"[NestedBenders] variables de bloque sin equivalente en el "
                      f"monolitico (descartadas): {sorted(sin_equivalente)}")

        if not desvio_ok:
            print("=" * 78)
            print("[NestedBenders] ATENCION: el costo recomputado sobre el modelo de "
                  "reporte NO coincide con el UB del solver.")
            print(f"    comparable = {comparable:,.6f}")
            print(f"    UB         = {self.best_ub:,.6f}")
            print(f"    desvio     = {desvio:,.6f}  (tolerancia {tolerancia:,.6f})")
            print("    Los archivos de salida describen una solucion distinta de la "
                  "que reporto el algoritmo.")
            print("=" * 78)


        return report, informe


    def solve(self, verbose=True):
        """Ctrl+C en cualquier punto -- en medio de un solve de Gurobi o entre
        iteraciones -- corta el bucle limpio y devuelve la mejor solucion
        encontrada hasta ese momento, en vez de perder toda la corrida.

        Hacen falta DOS mecanismos, no uno: un SIGINT que llega durante un solve
        activo puede ser atrapado por Gurobi a nivel nativo sin que el handler de
        Python llegue a ejecutarse, asi que passes._solve() ademas chequea una
        bandera compartida despues de cada opt.solve().

        Es seguro porque best_ub/best_solution solo se actualizan al final de un
        forward_pass.run() COMPLETO: si la interrupcion cae a mitad de camino,
        esa llamada nunca retorna y el estado guardado sigue siendo el de la
        ultima iteracion completa.
        """
        def _sigint_handler(signum, frame):
            print("\n[NestedBenders] Ctrl+C detectado -- terminando la resolucion "
                  "en curso y recuperando la mejor solucion encontrada...")
            sys.stdout.flush()
            passes_module.request_interrupt()
            raise KeyboardInterrupt()

        old_handler = signal.signal(signal.SIGINT, _sigint_handler)
        passes_module.clear_interrupt()

        k = 0
        gap = float("inf")
        interrupted = False
        solve_start = time.time()

        if self.capacity_presolve:
            t_pre = time.time()
            self.capacity_bounds = self._capacity_presolve(verbose=verbose)
            self.capacity_presolve_time = time.time() - t_pre
            if verbose:
                print(f"[NestedBenders] presolve de capacidad: "
                      f"{self.capacity_presolve_time:.0f}s")

        if self.monolithic_lp_bound:
            if verbose:
                print("[NestedBenders] cota inicial: relajacion lineal del monolitico "
                      "completo...")
                sys.stdout.flush()
            t_lp = time.time()
            try:
                self.lb_monolithic_lp = self._monolithic_lp_bound()
                self.lb = max(self.lb, self.lb_monolithic_lp)
                if verbose:
                    print(f"[NestedBenders] cota del LP monolitico = "
                          f"{self.lb_monolithic_lp:,.2f}  ({time.time() - t_lp:.0f}s)")
            except Exception as exc:
                # No es fatal: es una cota extra. Si el monolitico no entra en
                # memoria o el LP no cierra, se sigue con la del backward.
                print(f"[NestedBenders] sin cota del LP monolitico "
                      f"({type(exc).__name__}: {exc}) -- se sigue sin ella.")

        try:
            while gap > self.gap_tol and k < self.max_iter:
                k += 1
                iter_start = time.time()

                fwd = self.forward_pass.run(
                    iteration=k, verbose=verbose,
                    current_ub=(self.ub if self.ub != float("inf") else None),
                    current_lb=(self.lb if self.lb != float("-inf") else None),
                )
                self.ub = min(self.ub, fwd["ub"])
                if fwd["ub"] <= self.best_ub:
                    self.best_ub = fwd["ub"]
                    self.best_solution = fwd["x_hat"]
                    self.best_full_solution = fwd["full_solution"]

                lb_k = self.backward_pass.run(
                    fwd["x_hat"], iteration=k, verbose=verbose,
                    current_ub=self.ub,
                    current_lb=(self.lb if self.lb != float("-inf") else None),
                )
                self.lb = max(self.lb, lb_k)

                gap = ((self.ub - self.lb) / abs(self.ub)
                       if self.ub not in (0, float("inf")) else float("inf"))
                iter_time = time.time() - iter_start
                self.gap_history.append({
                    "iteration": k, "ub": self.ub, "lb": self.lb, "gap": gap,
                    "iter_time_sec": iter_time,
                    # Estado que el forward eligio para el primer anio en ESTA
                    # iteracion. Sirve para ver si los cortes efectivamente
                    # mueven las decisiones de inversion o si el forward vuelve
                    # siempre al mismo punto (sintoma de cortes demasiado
                    # debiles, no de un bug).
                    "x_hat_primer_anio": fwd["x_hat"][self.years[0]],
                })
                if verbose:
                    print(f"[NestedBenders] k={k}  UB={self.ub:.4f}  LB={self.lb:.4f}  "
                          f"gap={gap:.4%}  tiempo_iteracion={iter_time:.1f}s")
        except KeyboardInterrupt:
            interrupted = True
            if self.best_full_solution is None:
                print("[NestedBenders] Interrumpido antes de completar la primera "
                      "iteracion -- no hay solucion factible que recuperar.")
            else:
                print(f"[NestedBenders] Interrumpido en la iteracion {k} -- se "
                      f"conserva la mejor solucion (UB={self.best_ub:.4f}).")
        finally:
            signal.signal(signal.SIGINT, old_handler)
            passes_module.clear_interrupt()

        total_time = time.time() - solve_start
        self.iterations_run = k
        if verbose:
            self._report_capacity_bound_strength()
            print(f"[NestedBenders] tiempo total: {total_time:.1f}s "
                  f"({k} iteracion{'es' if k != 1 else ''}, "
                  f"{self.forward_pass.feasibility_cuts_added} cortes de factibilidad)")

        return {
            "capacity_bounds": self.capacity_bounds,
            "capacity_sum_bound": self.capacity_sum_bound,
            "capacity_presolve_time_sec": self.capacity_presolve_time,
            "feasibility_cuts": self.forward_pass.feasibility_cuts_added,
            "ub": self.best_ub,
            "lb": self.lb,
            "gap": ((self.best_ub - self.lb) / abs(self.best_ub)
                    if self.best_ub not in (0, float("inf")) else float("inf")),
            "iterations": k,
            "interrupted": interrupted,
            "lb_monolithic_lp": self.lb_monolithic_lp,
            "total_time_sec": total_time,
            "best_solution": self.best_solution,
            "best_full_solution": self.best_full_solution,
            "gap_history": self.gap_history,
        }
