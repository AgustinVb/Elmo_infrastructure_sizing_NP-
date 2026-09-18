import math
import os
import signal
import sys
import time

import pyomo.environ as pyo
from pyomo.environ import value

from src.optimization.decomposition import passes as passes_module
from src.optimization.decomposition.year_block import YearBlockBuilder
from src.optimization.decomposition.cuts import BendersCutManager
from src.optimization.decomposition.macroblocks import (
    build_macroblocks,
    compute_power_shares,
    split_daily_targets,
)
from src.optimization.decomposition.passes import ForwardPass, BackwardPass, _solve

# Los workers de construccion de bloques (ver _build_blocks) reciben
# mine_system/time_series/autonomous_mode via Pool(initializer=...): en
# 'fork' (default en Linux) eso es gratis -- initargs se hereda por
# copy-on-write al hacer fork(), sin pasar por pickle -- asi que no importa
# que estos objetos no sean serializables. Lo unico que SI viaja por pickle
# entre procesos son los payloads por tarea (year, is_last_year,
# exogenous_stations: livianos) y el YearBlockBuilder ya construido que
# vuelve al proceso principal. Ese YearBlockBuilder contiene reglas Pyomo
# armadas con lambdas/closures locales (year_block.py, functions.py) que el
# modulo estandar `pickle` no soporta -- de ahi que este modulo dependa de
# `multiprocess` (mismo API que la libreria estandar `multiprocessing`, pero
# serializa con `dill`, que si soporta closures) en vez de
# `concurrent.futures`/`multiprocessing` a secas.
#
# El limite de recursion default de Python (1000) no alcanza para que dill
# recorra el grafo de objetos de un YearBlockBuilder ya construido (Pyomo
# encadena referencias component -> parent_block -> model -> ... a lo largo
# de miles de ConstraintData/VarData) -- confirmado con un caso real: revienta
# con RecursionError en el paso de retorno worker -> proceso principal
# ("Error sending result", ver commit que agrego esto) y 5000 alcanzaba para
# el bloque de prueba. 10000 deja margen para escenarios con bloques mas
# grandes (mas dias representativos/año). Hace falta subirlo en AMBOS lados:
# el worker serializa (dill.dumps) el resultado antes de devolverlo, y el
# proceso principal lo deserializa (dill.loads) al recibirlo.
_RECURSION_LIMIT_FOR_DILL = 10000
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
    """X exogeno por defecto para modo descompuesto (documento sec. 2.1): se
    construye toda estacion que tenga al menos un LHD asignado en la hoja
    StationAssignment, en TODOS los años del horizonte. La asignacion
    LHD-estacion es estatica (no varia por año, ver Timeseries.
    sample_station_assignment), asi que no hace falta pedirle al usuario que
    la repita a mano en un JSON aparte -- ya esta implicita en los datos de
    entrada."""
    lhds = mine_system.get_system_lhds()
    stations = mine_system.get_system_stations()
    time_series.get_station_assignment(lhds)
    time_series.get_elhd_at_station(stations)
    elhd_per_station = time_series.mapper.get('elhd_per_station', {})
    built = {k: (1 if len(elhd_per_station.get(k, [])) > 0 else 0) for k in stations}
    return {y: dict(built) for y in time_series.years}


class NestedBendersSolver(object):
    """Documento sec. 7-8: bucle completo forward/backward. Construye todos
    los bloques anuales una sola vez (documento sec. 9.2, punto 1) y luego
    itera forward+backward hasta que (UB-LB)/UB <= gap_tol o se alcanza
    max_iter, guardando la MEJOR solucion factible encontrada -- la UB no
    es monotona entre iteraciones (documento sec. 7.1)."""

    def __init__(self, mine_system, time_series, exogenous_stations_by_year=None,
                 gap_tol=0.01, max_iter=20, autonomous_mode=False, solver_kwargs=None,
                 strengthen=False, degradation_cut_mode="mccormick", lagrangean_kwargs=None,
                 block_build_jobs=None, macroblock_forward=False,
                 monolithic_lp_bound=True, capacity_presolve="peak"):
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
        :param macroblock_forward: default False (comportamiento de siempre).
            Con True, cada año POSTERIOR al primero se resuelve en la fase
            forward como varios MILP de macrobloque --una nave de carga con
            sus equipos y sus puntos de extraccion cada uno-- en vez de un
            solo MILP del año completo, repartiendo entre macrobloques los
            recursos que comparten: la meta de produccion diaria, la potencia
            de red (p_peak/P_pot) y el aporte de la generacion renovable y del
            almacenamiento (ver decomposition/macroblocks.py).

            Solo el forward: el reparto RESTRINGE el problema del año, de modo
            que la trayectoria sigue siendo factible --la cota superior sigue
            siendo valida-- pero su relajacion lineal sobreestima el costo y
            daria cortes invalidos. La fase backward, que es la que produce
            los cortes y la cota inferior, siempre resuelve el año completo.
            El primer año tampoco se reparte: es el que decide las inversiones
            comunes (P_max_k, G_g, H), que no son separables por nave.
        :param exogenous_stations_by_year: dict {year: {k: 0/1}} -- X fijo
            por año y estacion (documento sec. 2.1; ver decision de diseño
            confirmada en el plan de M0). Si se omite (None), se infiere
            automaticamente de la asignacion LHD-estacion de los datos de
            entrada (infer_exogenous_stations): se construye toda estacion
            con al menos un LHD asignado.
        :param strengthen: default False. PAUSADO (merge 2026-08 con
            origin/carga_ob_multiaño): el mecanismo (documento sec. 6.2,
            "Strengthened Benders", implementado en
            BackwardPass._strengthened_subgradient_cut) sigue en el codigo
            pero su invocacion esta comentada en BackwardPass.run -- este
            parametro ya no tiene efecto. Se diagnostico originalmente
            porque el corte LP estandar queda degenerado (mu=0) cuando el
            modelo garantiza "recurso completo" (confirmado en una corrida
            real del escenario 960kW_2dias, N_chargers/G/H con mu=0 en
            TODAS las iteraciones), pero origin reestructuro G/H a estado
            "global_once" (decision unica al inicio del horizonte, ver
            year_block.py/cuts.py) en esos mismos 8 commits, cambiando el
            terreno sobre el que se hizo ese diagnostico. Antes de
            reactivarlo hay que re-evaluar si el problema persiste con el
            modelo nuevo -- ver comentarios "PAUSADO" en BackwardPass.run.
        :param degradation_cut_mode: "mccormick" (default) o "lagrangean" --
            camino usado para el corte del año con datos de
            BatteryDegradation (degradacion_descomposicion_mccormick.md):
            "mccormick" relaja el bilineal N_ciclos*b_bar con la envolvente
            convexa (Camino A) y usa el corte de Benders estandar, barato;
            "lagrangean" mantiene la fisica bilineal EXACTA y genera el
            corte via subgradiente Lagrangeano (Camino B, sec. 4), mas caro
            (varios MIQCP no convexos por año e iteracion) pero sin la
            aproximacion de McCormick. Sin efecto ninguno de los dos si el
            escenario no tiene hoja BatteryDegradation.
            Un tercer camino, "disjunctive" (corte big-M sobre la
            disyuncion R_y=0/R_y=1 del reemplazo de bateria, ver
            BackwardPass._disjunctive_replace_cut), quedo implementado
            pero PAUSADO/no seleccionable por el mismo motivo que
            `strengthen` arriba -- ver ese parametro.
        :param lagrangean_kwargs: dict opcional con max_iter/eps_gap/
            eps_stall para el subgradiente del Camino B (ver
            BackwardPass._lagrangean_subgradient_cut).
        :param block_build_jobs: cuantos procesos usar para construir los
            bloques anuales EN PARALELO antes de arrancar el loop forward/
            backward (ver _build_blocks). None (default) = min(años,
            cpus disponibles). 1 = secuencial (comportamiento original,
            util para debug ya que corre en el proceso principal). Los
            bloques son independientes entre si (cada uno solo depende de
            mine_system/time_series/su propio año), por eso son
            embarrassingly parallel -- construirlos es Pyomo puro-Python
            (miles de llamadas a `rule=` por indice), CPU-bound y de un
            solo hilo cada uno, sin nada que solapar con el forward/
            backward que viene despues.
        """
        self.mine_system = mine_system
        self.time_series = time_series
        self.years = sorted(time_series.years)
        self.gap_tol = gap_tol
        self.max_iter = max_iter
        self.solver_kwargs = solver_kwargs or {}
        self.strengthen = strengthen
        self.exogenous_stations_by_year = (
            exogenous_stations_by_year if exogenous_stations_by_year is not None
            else infer_exogenous_stations(mine_system, time_series)
        )
        self.autonomous_mode = autonomous_mode

        self.blocks = self._build_blocks(block_build_jobs)

        self.cut_manager = BendersCutManager()
        self.macroblock_forward = macroblock_forward
        self.macroblock_blocks, self.fleet_params = (
            self._build_macroblock_blocks() if macroblock_forward else ({}, {})
        )
        self.forward_pass = ForwardPass(
            self.blocks, solver_kwargs=self.solver_kwargs,
            macroblock_blocks=self.macroblock_blocks,
            cut_manager=self.cut_manager, fleet_params=self.fleet_params,
        )
        self.backward_pass = BackwardPass(
            self.blocks, cut_manager=self.cut_manager, solver_kwargs=self.solver_kwargs,
            degradation_cut_mode=degradation_cut_mode, lagrangean_kwargs=lagrangean_kwargs,
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
        """Construye self.blocks (un YearBlockBuilder por año). Secuencial
        si block_build_jobs == 1 o hay un solo año; en paralelo (Pool de
        `multiprocess`) en caso contrario -- ver comentario junto a
        _worker_state/_init_year_block_worker/_build_year_block_task mas
        arriba sobre por que hace falta `multiprocess` (serializa con dill)
        en vez de `multiprocessing`/`concurrent.futures` a secas."""
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

        import multiprocess

        # El proceso principal tambien deserializa (dill.loads) cada
        # resultado dentro de pool.map() -- necesita el mismo limite alto
        # que el worker (ver _RECURSION_LIMIT_FOR_DILL mas arriba).
        sys.setrecursionlimit(_RECURSION_LIMIT_FOR_DILL)

        with multiprocess.Pool(
            processes=n_jobs, initializer=_init_year_block_worker,
            initargs=(self.mine_system, self.time_series, self.autonomous_mode),
        ) as pool:
            return pool.map(_build_year_block_task, tasks)

    def _build_macroblock_blocks(self):
        """Construye {year: {estacion: YearBlockBuilder}} para los años
        POSTERIORES al primero, y los parametros de flota que necesita la
        coordinacion de la degradacion.

        Se construyen secuencialmente: cada bloque de macrobloque es bastante
        mas chico que el del año completo (una nave, sus equipos y sus nodos),
        y son |K| * (|Y|-1) en total.
        """
        macroblocks = build_macroblocks(self.mine_system, self.time_series, self.years)
        shares = compute_power_shares(
            self.mine_system, self.time_series, macroblocks, self.years
        )
        targets = split_daily_targets(
            self.mine_system, self.time_series, macroblocks, self.years
        )

        print(f"[NestedBenders] descomposicion por macrobloque activada: "
              f"{len(macroblocks)} macrobloques "
              f"({', '.join(f'{k} (cuota {v:.1%})' for k, v in sorted(shares.items()))})")

        fleet_params = {}
        b_max_placeholder = 0.0
        if self.mine_system.battery_degradation is not None:
            ref = self.blocks[0].model
            b_max_placeholder = value(ref.b_max_fleet)
            common = {
                "b_max": value(ref.b_max_fleet),
                "rho_rep": value(ref.replace_capacity_fraction),
                "b_upper": value(ref.B_U),
                "gamma_coef": value(ref.gamma_coef),
            }
            for blk in self.blocks[1:]:
                fleet_params[blk.year] = dict(
                    common, n_elhd=value(blk.model.n_elhd_bd[blk.year])
                )

        blocks_by_year = {}
        for blk in self.blocks[1:]:
            y = blk.year
            per_station = {}
            for station, mb in macroblocks.items():
                per_station[station] = YearBlockBuilder(
                    self.mine_system, self.time_series, year=y,
                    is_last_year=(y == self.years[-1]),
                    exogenous_stations=self.exogenous_stations_by_year[y],
                    autonomous_mode=self.autonomous_mode,
                    macroblock={
                        "station": station,
                        "lhds": mb["lhds"],
                        "nodes": mb["nodes"],
                        "share": shares[station],
                        # daily_production compara contra la meta repartida de
                        # este macrobloque, no contra la suma de sus nodos.
                        "daily_target": {y: targets[(station, y)]},
                        # b_bar/R los re-fija el forward en cada iteracion
                        # (dependen del estado heredado): esto es solo el
                        # valor con el que se construye el bloque.
                        "b_bar": b_max_placeholder,
                        "replace": 0,
                    },
                )
            blocks_by_year[y] = per_station

        return blocks_by_year, fleet_params

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
        mas chica del perfil (2% del pico en la instancia de 3 naves). El
        requerimiento de los anios de mayor produccion viaja hacia atras un
        anio por vez -- el anio 3 corta al 2, el 2 queda infactible y corta al
        1 -- y cada corte de factibilidad reinicia el barrido completo desde el
        anio 1. Medido en carga on board a 6 anios: 9 cortes, 2.060 s.

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
        n_ssee_k (mas cargadores, bateria nueva...), asi que la cota puede
        quedar por debajo de lo que el forward termina comprando. Se mide al
        final de solve() comparandola contra el n_ssee_k elegido.

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

        Es valida por construccion y es independiente de los cortes, asi que se
        toma el maximo con la que produce el backward. Las dos no estan
        ordenadas: al acumular cortes, alpha_1 converge por abajo a Phi^LP_2,
        que contiene alpha_2 convergiendo a Phi^LP_3, etc., y como el estado
        separa exactamente los anios esa recursion converge PRECISAMENTE a la
        relajacion lineal del monolitico -- o sea que el backward aproxima desde
        abajo, iteracion a iteracion, el numero que esta funcion da directo.
        Cualquier corte valido solo para el casco entero puede superarla.

        Medido en la rama battery_swapping_multianio (instancia de 1 nave, 2
        anios): la cota sale en 9 s y deja el gap de la primera iteracion en
        26.97%, contra 95.32% sin ella; en 10 iteraciones el backward no la
        supero nunca.

        Se construye con mccormick_degradation=True: con el bilineal exacto el
        modelo no es un LP y su relajacion no seria resoluble como tal. La
        envolvente de McCormick es una RELAJACION del bilineal, asi que su
        optimo sigue siendo una cota inferior valida -- y ademas es la misma
        formulacion que usan los bloques anuales, con lo que la cota queda en la
        misma base que el UB del forward.

        exogenous_stations puesto -- y SIN years_override -- deja fuera el costo
        de apertura de naves (constante en modo descompuesto, ausente del
        objetivo de los bloques) pero conserva las restricciones de acumulacion
        del monolitico.

        Construye el monolitico entero, que es el gasto de memoria que la
        descomposicion evita: en horizontes largos puede no entrar. Cualquier
        fallo se reporta y la corrida sigue sin esta cota.
        """
        import pyomo.environ as pyo
        from pyomo.environ import SolverFactory, TransformationFactory
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
        ConstraintRules(self.mine_system, self.time_series,
                        mccormick_degradation=True, **common).build_all_constraints(model)
        model.obj = pyo.Objective(
            rule=ObjectiveRules(self.mine_system, self.time_series, **common).total_cost,
            sense=pyo.minimize,
        )
        if self.capacity_bounds:
            # Desigualdad valida del problema completo (ver _capacity_presolve):
            # aprieta la relajacion sin cambiar de problema. Es la unica cota
            # del backward que no se estanca en el LP puro (sec. 5.1 del
            # contexto): la inversion en subestacion sube directo en el LB.
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


    def solve(self, verbose=True):
        """Documento sec. 7-8. Ctrl+C (SIGINT) en cualquier punto -- en
        medio de un solve de Gurobi de cualquier año/pase, o entre
        iteraciones -- corta el loop de forma limpia y devuelve la mejor
        solucion factible encontrada hasta ese momento, en vez de perder
        toda la corrida.

        DOS mecanismos, no uno solo (aprendido de una corrida real que
        rompio con RuntimeError sin guardar nada -- ver memoria del
        proyecto): un SIGINT que llega en medio de un solve activo de
        Gurobi puede ser atrapado por el propio Gurobi a nivel nativo
        (imprime su propio "Interrupt request received" y el solve
        termina con termination_condition="error"/aborted) SIN que la
        excepcion Python de nuestro handler llegue a lanzarse -- ahi el
        unico Python-side signal.signal() de mas abajo NO alcanza. Por
        eso _solve() (passes.py) tambien chequea una bandera compartida
        (passes.request_interrupt/_interrupt_requested) DESPUES de cada
        opt.solve(), sin importar como haya terminado ese solve en
        particular, y ahi SI lanza un KeyboardInterrupt real -- como eso
        ocurre en codigo Python normal (no dentro del manejador de
        señal), propaga sin depender de que Gurobi "coopere". El handler
        de signal.signal() de aca abajo sigue sirviendo para el caso mas
        rapido: interrupcion en medio de codigo Python puro entre dos
        solves (lectura de duales, armado de cortes, etc.).

        Seguro porque self.best_ub/best_solution/best_full_solution solo
        se actualizan al final de un forward_pass.run() COMPLETO (las 5
        resoluciones de año, sin excepciones) -- si la interrupcion llega
        a mitad de un forward o backward pass, esa llamada nunca retorna
        su dict y el estado guardado sigue siendo el de la ULTIMA
        iteracion completa, nunca algo a medio actualizar."""
        def _sigint_handler(signum, frame):
            print("\n[NestedBenders] Ctrl+C detectado -- terminando la "
                  "resolucion en curso y recuperando la mejor solucion "
                  "encontrada hasta ahora...")
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
                    # UB no monotona (documento sec. 7.1): guardar la mejor
                    # solucion factible encontrada, no la ultima.
                    self.best_ub = fwd["ub"]
                    self.best_solution = fwd["x_hat"]
                    self.best_full_solution = fwd["full_solution"]

                lb_k = self.backward_pass.run(
                    fwd["x_hat"], iteration=k, verbose=verbose,
                    current_ub=self.ub, current_lb=(self.lb if self.lb != float("-inf") else None),
                    strengthen=self.strengthen,
                )
                self.lb = max(self.lb, lb_k)

                gap = (self.ub - self.lb) / abs(self.ub) if self.ub not in (0, float("inf")) else float("inf")
                iter_time = time.time() - iter_start
                self.gap_history.append({
                    "iteration": k, "ub": self.ub, "lb": self.lb, "gap": gap,
                    "iter_time_sec": iter_time,
                })
                if verbose:
                    print(f"[NestedBenders] k={k}  UB={self.ub:.4f}  LB={self.lb:.4f}  "
                          f"gap={gap:.4%}  tiempo_iteracion={iter_time:.1f}s")
        except KeyboardInterrupt:
            interrupted = True
            if self.best_full_solution is None:
                print("[NestedBenders] Interrumpido antes de completar la "
                      "primera iteracion -- no hay ninguna solucion factible "
                      "que recuperar todavia.")
            else:
                print(f"[NestedBenders] Interrumpido en la iteracion {k} -- "
                      f"se conserva la mejor solucion encontrada "
                      f"(UB={self.best_ub:.4f}).")
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
            "lb_monolithic_lp": self.lb_monolithic_lp,
            "capacity_bounds": self.capacity_bounds,
            "capacity_sum_bound": self.capacity_sum_bound,
            "capacity_presolve_time_sec": self.capacity_presolve_time,
            "feasibility_cuts": self.forward_pass.feasibility_cuts_added,
            "ub": self.ub,
            "lb": self.lb,
            "gap": gap,
            "interrupted": interrupted,
            "best_solution": self.best_solution,
            "iterations": k,
            "gap_history": self.gap_history,
            "total_time_sec": total_time,
        }

    def build_report_model(self, output_folder, scan_residuals=True):
        """Puente de reporte (plan de M5): construye -- sin resolver -- un
        OptModel monolitico sobre el MISMO mine_system/time_series, y le
        carga encima la MEJOR solucion factible encontrada (best_full_
        solution), variable por variable, directamente sobre los Var del
        modelo. El resultado se puede pasar tal cual a Printer sin tocar
        printer.py, igual que si hubiera sido resuelto por el monolitico.

        No se reusa el mecanismo de warm-start-por-JSON (_load_solution_
        warmstart_folder): ya tenemos los valores en memoria como dict de
        Python (extract_full_solution), y setearlos directamente evita
        depender del formato de arbol anidado que espera ese loader.
        """
        if self.best_full_solution is None:
            raise RuntimeError("No hay solucion factible todavia -- llamar a solve() primero.")

        from src.optimization.opt_model import OptModel

        # mccormick_degradation=True: los bloques anuales resuelven la
        # degradacion con la envolvente de McCormick, y su solucion satisface
        # ESA relajacion, no el bilineal exacto. Si el modelo de reporte se
        # armara con el bilineal (el default), la solucion cargada lo violaria
        # -- medido: n_ciclos_link[2] violada en 3,317.6, exactamente el
        # "residuo McCormick" que el forward ya reporta como aviso -- y en modo
        # hibrido Gurobi descartaria el MIP start en silencio ("User MIP start
        # did not produce a new incumbent solution"), dejando al hibrido sin su
        # unica razon de ser.
        #
        # OJO con lo que esto implica: la solucion descompuesta NO es factible
        # para el modelo exacto. El reporte es consistente con la formulacion
        # que se uso para obtenerla, que es lo correcto, pero el residuo de
        # McCormick sigue siendo un error de aproximacion real (ver
        # YearBlockBuilder.mccormick_residual y sec. 3.4/3.5 del documento).
        om = OptModel(self.mine_system, self.time_series, output_folder,
                      autonomous_mode=self.autonomous_mode,
                      mccormick_degradation=True)
        model = om.model
        om.opt_cost_result = self.best_ub

        # No hay un unico gurobi.log: este modelo nunca se resolvio como tal,
        # es un ensamble de N resoluciones (una por bloque/iteracion). Se deja
        # un placeholder para que Printer.save_gurobi_log() no busque un
        # archivo inexistente (evita ademas un print() con emoji que revienta
        # en consolas Windows/cp1252 -- ver setup.py --mode decomposed).
        with open(om.gurobi_log_path, "w", encoding="utf-8") as f:
            f.write(
                "Modo descompuesto (Nested Benders): no hay un unico log de "
                "Gurobi -- cada iteracion resuelve N bloques anuales por "
                "separado. Ver gap_history del NestedBendersSolver para el "
                "detalle de UB/LB/gap por iteracion.\n"
            )

        internas = set()      # sin equivalente en el monolitico: esperado
        sin_cargar = set()    # forma incompatible: NO esperado, hay que mirarlo
        cargadas = 0

        for y, var_values in self.best_full_solution.items():
            for var_name, values in var_values.items():
                if not hasattr(model, var_name):
                    # Variable interna del bloque sin equivalente monolitico
                    # (alpha, *_hat, *_prev, W_s, ...).
                    internas.add(var_name)
                    continue
                var_comp = getattr(model, var_name)
                if isinstance(values, dict):
                    if not var_comp.is_indexed():
                        sin_cargar.add(var_name)
                        continue
                    for idx, v in values.items():
                        if idx not in var_comp:
                            continue
                        vardata = var_comp[idx]
                        if vardata.is_binary() or vardata.is_integer():
                            v = int(round(v))
                        vardata.set_value(v, skip_validation=True)
                        cargadas += 1
                elif var_comp.is_indexed():
                    # El bloque declara como ESCALAR lo que el monolitico indexa
                    # por anio: teniendo un solo anio, el indice sobra alli. Es
                    # el caso de w_deg (la auxiliar de la envolvente de
                    # McCormick). Se mapea al anio de este bloque.
                    #
                    # Antes esto caia en un `continue` mudo y w_deg nunca
                    # llegaba al modelo de reporte: el MIP start quedaba
                    # incompleto y 10 restricciones no se podian ni evaluar.
                    if y in var_comp:
                        vardata = var_comp[y]
                        if vardata.is_binary() or vardata.is_integer():
                            values = int(round(values))
                        vardata.set_value(values, skip_validation=True)
                        cargadas += 1
                    else:
                        sin_cargar.add(var_name)
                else:
                    var_comp.set_value(values, skip_validation=True)
                    cargadas += 1

        if sin_cargar:
            # No es un caso esperado: el nombre existe en el monolitico pero la
            # forma del indice no calza, asi que el valor se perdio.
            print(f"[NestedBenders] AVISO: variables del bloque que NO se pudieron "
                  f"cargar en el modelo de reporte por forma de indice "
                  f"incompatible: {sorted(sin_cargar)}")

        # X/Delta_X: exogenos en modo descompuesto (no forman parte de
        # best_full_solution -- ver documento sec. 2.1). Se reconstruyen
        # aca solo para que el reporte (CSV/plots) muestre X/Delta_X
        # correctos; no afecta el costo ya calculado por el solver.
        prev_x = {k: 0 for k in model.stations_set}
        for y in self.years:
            for k in model.stations_set:
                x_y = self.exogenous_stations_by_year[y][k]
                model.X[k, y].set_value(int(x_y), skip_validation=True)
                model.Delta_X[k, y].set_value(int(x_y - prev_x[k]), skip_validation=True)
                prev_x[k] = x_y

        # Habilita que este modelo sirva ademas de MIP start del monolitico
        # (--mode hybrid): solve_model solo pasa warmstart=True a Gurobi si esta
        # bandera esta puesta, y como aca los valores se cargan directo sobre las
        # variables -- no via _load_solution_warmstart_folder -- nadie la pondria
        # y el arranque se ignoraria en silencio.
        om.has_warm_start = True

        # Verificacion numerica, no opcional: el costo recomputado sobre este
        # modelo MENOS la inversion en estaciones (que en modo descompuesto es
        # constante y queda fuera del objetivo de los bloques) tiene que
        # coincidir con el UB del solver. Si no coincide, los archivos de salida
        # describirian una solucion distinta de la que reporto el algoritmo.
        #
        # No es teorico: esta comprobacion es la que habria cantado, la primera
        # vez que se corrio, que el modelo de reporte se armaba con el bilineal
        # exacto de la degradacion mientras los bloques resolvian la envolvente
        # de McCormick. Sin ella el problema recien aparecio mucho despues, como
        # un MIP start que Gurobi descartaba sin explicar por que.
        #
        # No se aborta -- descartar el reporte despues de horas de computo seria
        # peor -- pero tiene que quedar imposible de pasar por alto.
        try:
            costo_total = value(model.obj)
            inv_estaciones = sum(
                value(model.station_cost_k[k]) * value(model.Delta_X[k, y])
                * value(om.objective_rules._discount_factor(model, y))
                for k in model.stations_set for y in self.years
            )
            comparable = costo_total - inv_estaciones
            desvio = abs(comparable - self.best_ub)
            tolerancia = max(1e-6 * abs(self.best_ub), 1e-4)
            print(f"[NestedBenders] reporte: costo recomputado = {costo_total:,.2f} "
                  f"(comparable {comparable:,.2f} contra UB {self.best_ub:,.2f}, "
                  f"desvio {desvio:,.6f})")
            if desvio > tolerancia:
                print("=" * 78)
                print("[NestedBenders] ATENCION: el costo recomputado sobre el modelo "
                      "de reporte NO coincide con el UB del solver.")
                print(f"    comparable = {comparable:,.6f}")
                print(f"    UB         = {self.best_ub:,.6f}")
                print(f"    desvio     = {desvio:,.6f}  (tolerancia {tolerancia:,.6f})")
                print("    Los archivos de salida describen una solucion distinta de "
                      "la que reporto el algoritmo.")
                print("=" * 78)
        except Exception as exc:
            print(f"[NestedBenders] no se pudo verificar el costo del modelo de "
                  f"reporte ({type(exc).__name__}: {exc}).")

        # Residuo del MIP start por familia de restricciones. Gurobi evalua el
        # arranque contra FeasibilityTol (1e-6) y lo descarta en silencio si
        # alguna fila lo supera -- paso en el hibrido a 6 anios ("violates
        # constraint x1661848 by 0.000057418") sin decir de que familia era.
        # Cada bloque se resuelve por separado con sus propias tolerancias
        # relativas y al ensamblar los anios el residuo se mide contra un
        # umbral absoluto; esto muestra donde se acumula, que es lo que hace
        # falta para sanear el punto antes de entregarlo en vez de aflojar
        # FeasibilityTol para todo el modelo.
        if scan_residuals:
            t_res = time.time()
            om.mip_start_residuals, saltadas, sin_valor = self._mip_start_residuals(model)
            print(f"[NestedBenders] residuo del MIP start por familia "
                  f"(max |violacion|, {time.time() - t_res:.0f}s):")
            for name, (viol, idx, n_over) in om.mip_start_residuals:
                marca = "  <-- supera FeasibilityTol 1e-6" if viol > 1e-6 else ""
                print(f"    {name:<32} {viol:.3e}  en {idx}  "
                      f"({n_over} filas > 1e-6){marca}")
            if saltadas or sin_valor:
                # Una fila sin evaluar es una fila que Gurobi SI evalua: las
                # variables sin valor entran al MIP start como libres y Gurobi
                # las completa, pero si la parte fija ya es incompatible el
                # arranque se descarta igual.
                print(f"[NestedBenders] AVISO: {saltadas} filas no evaluadas por "
                      f"variables sin valor; variables sin valor por familia: "
                      f"{sin_valor}")

        return om

    @staticmethod
    def _mip_start_residuals(model, top=10):
        """([(familia, (max violacion, indice, filas que superan 1e-6))],
        filas no evaluadas, {familia: variables sin valor}): las `top`
        familias con mayor violacion de los valores cargados en `model`.
        Recorre todas las restricciones activas evaluando el cuerpo con los
        valores actuales; en un monolitico de 6 anios son ~2 M filas, del
        orden de un minuto."""
        worst = {}
        saltadas = 0
        sin_valor = {}
        for var in model.component_objects(pyo.Var, active=True):
            n = sum(1 for idx in var if var[idx].value is None) if var.is_indexed() \
                else (1 if var.value is None else 0)
            if n:
                sin_valor[var.name] = n
        for con in model.component_objects(pyo.Constraint, active=True):
            name = con.name
            for idx in con:
                c = con[idx]
                body = value(c.body, exception=False)
                if body is None:
                    saltadas += 1
                    continue
                viol = 0.0
                if c.has_lb():
                    viol = max(viol, value(c.lower) - body)
                if c.has_ub():
                    viol = max(viol, body - value(c.upper))
                if viol <= 0.0:
                    continue
                prev = worst.get(name, (0.0, None, 0))
                worst[name] = (max(prev[0], viol),
                               idx if viol > prev[0] else prev[1],
                               prev[2] + (1 if viol > 1e-6 else 0))
        return sorted(worst.items(), key=lambda kv: -kv[1][0])[:top], saltadas, sin_valor
