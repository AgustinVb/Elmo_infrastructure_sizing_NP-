from src.optimization.decomposition.year_block import YearBlockBuilder
from src.optimization.decomposition.cuts import BendersCutManager
from src.optimization.decomposition.passes import ForwardPass, BackwardPass


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
                 strengthen=False):
        """
        :param exogenous_stations_by_year: dict {year: {k: 0/1}} -- X fijo
            por año y estacion (documento sec. 2.1; ver decision de diseño
            confirmada en el plan de M0). Si se omite (None), se infiere
            automaticamente de la asignacion LHD-estacion de los datos de
            entrada (infer_exogenous_stations): se construye toda estacion
            con al menos un LHD asignado.
        :param strengthen: EXPERIMENTAL, default False -- deshabilitado por
            una falla de correccion encontrada en una corrida real de 5
            años (LB > UB en la iteracion 2, imposible en un Benders/
            L-shaped correcto). Evalua el Lagrangeano sobre el MILP
            completo con N_chargers/G/H relajadas y `prev` acotado por el
            punto de linealizacion de esa resolucion (documento sec. 6.2,
            "Strengthened Benders"), pero ese acotamiento solo garantiza
            validez LOCAL (cerca del punto donde se calibro), no GLOBAL --
            la validacion original solo chequeo un punto de referencia, no
            alcanza. No usar hasta corregir (ver memoria del proyecto).
        """
        if mine_system.battery_degradation is not None:
            # year_block.py/cuts.py todavia implementan la formulacion de
            # degradacion VIEJA (AN_ciclos/NB/b_bar_def, con N_L/N_U vía
            # greedy) que precede a la actual b_y_link/n_ciclos_link/
            # d_y_fade de ConstraintRules (ver functions.py) -- nunca se
            # portaron. Ademas b_y_link se registra en build_all_constraints
            # sin guard is_decomposed_block, así que con datos de
            # degradacion cargados esto rompe con un KeyError críptico al
            # construir el primer bloque (D[prev_year] fuera de índice) en
            # vez de este mensaje claro. Hasta que se porte la formulacion
            # nueva a la descomposicion, modo decomposed + degradacion no
            # esta soportado.
            raise NotImplementedError(
                "Modo 'decomposed' no soporta datos de BatteryDegradation todavia: "
                "year_block.py/cuts.py usan la formulacion de degradacion vieja, "
                "no portada a b_y_link/n_ciclos_link/d_y_fade (ver functions.py). "
                "Usa --mode monolithic para escenarios con hoja BatteryDegradation."
            )
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

        self.blocks = [
            YearBlockBuilder(
                mine_system, time_series, year=y,
                is_last_year=(y == self.years[-1]),
                exogenous_stations=self.exogenous_stations_by_year[y],
                autonomous_mode=autonomous_mode,
            )
            for y in self.years
        ]

        self.cut_manager = BendersCutManager()
        self.forward_pass = ForwardPass(self.blocks, solver_kwargs=self.solver_kwargs)
        self.backward_pass = BackwardPass(self.blocks, cut_manager=self.cut_manager, solver_kwargs=self.solver_kwargs)

        self.ub = float("inf")
        self.lb = float("-inf")
        self.best_ub = float("inf")
        self.best_solution = None
        self.best_full_solution = None
        self.gap_history = []
        self.iterations_run = 0

    def solve(self, verbose=True):
        k = 0
        gap = float("inf")

        while gap > self.gap_tol and k < self.max_iter:
            k += 1

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
            self.gap_history.append({"iteration": k, "ub": self.ub, "lb": self.lb, "gap": gap})
            if verbose:
                print(f"[NestedBenders] k={k}  UB={self.ub:.4f}  LB={self.lb:.4f}  gap={gap:.4%}")

        self.iterations_run = k
        return {
            "ub": self.ub,
            "lb": self.lb,
            "gap": gap,
            "best_solution": self.best_solution,
            "iterations": k,
            "gap_history": self.gap_history,
        }

    def build_report_model(self, output_folder):
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

        om = OptModel(self.mine_system, self.time_series, output_folder, autonomous_mode=self.autonomous_mode)
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

        for y, var_values in self.best_full_solution.items():
            for var_name, values in var_values.items():
                if not hasattr(model, var_name):
                    # Variable interna del bloque sin equivalente monolitico
                    # (alpha, *_hat, *_prev, W_s, ...).
                    continue
                var_comp = getattr(model, var_name)
                if isinstance(values, dict):
                    if not var_comp.is_indexed():
                        continue
                    for idx, v in values.items():
                        if idx not in var_comp:
                            continue
                        vardata = var_comp[idx]
                        if vardata.is_binary() or vardata.is_integer():
                            v = int(round(v))
                        vardata.set_value(v, skip_validation=True)
                else:
                    if var_comp.is_indexed():
                        continue
                    var_comp.set_value(values, skip_validation=True)

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

        return om
