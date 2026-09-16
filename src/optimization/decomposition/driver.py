import os
import signal
import sys
import time

from src.optimization.decomposition import passes as passes_module
from src.optimization.decomposition.cuts import BendersCutManager
from src.optimization.decomposition.passes import BackwardPass, ForwardPass
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
                 block_build_jobs=None, monolithic_lp_bound=True):
        """
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

        return {
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
