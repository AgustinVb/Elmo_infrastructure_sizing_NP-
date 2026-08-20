import logging

import pyomo.environ as pyo
from pyomo.environ import value, SolverFactory, TransformationFactory
from pyomo.opt import TerminationCondition, SolverStatus

from src.optimization.decomposition.cuts import BendersCutManager

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

# El backend directo de Gurobi (solver_io="python") avisa "Cannot get duals
# for MIP." cada vez que resuelve un bloque como MILP con un Suffix de
# duales declarado (aunque no se le pidan duales en ese solve -- son para
# el backward pass, no el forward). Es esperado y no indica un problema;
# se silencia subiendo el nivel del logger que lo emite.
logging.getLogger("pyomo.solvers").setLevel(logging.ERROR)


def _fmt_bound(v):
    if v is None or v in (float("inf"), float("-inf")):
        return "(sin cota aun)"
    return f"{v:,.2f}"


def _bounds_tag(current_ub, current_lb):
    return f"[UB={_fmt_bound(current_ub)}  LB={_fmt_bound(current_lb)}]"


def _solve(model, solvername="gurobi", gap=0.001, timelimit=900, tee=False, label="",
           extra_options=None):
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

    result = opt.solve(model, tee=tee, load_solutions=True)
    if _interrupt_requested or result.solver.status == SolverStatus.aborted:
        # Ver comentario junto a _interrupt_requested. Dos señales, no una
        # sola -- confirmado con una corrida real que la primera no
        # alcanza: cuando el SIGINT llega en medio de un solve activo,
        # Gurobi lo atrapa el mismo a nivel nativo (imprime su propio
        # "Interrupt request received") SIN que nuestro signal.signal()
        # de Python llegue siquiera a ejecutarse -- ahi _interrupt_requested
        # nunca se activa porque el handler nunca corrio. La señal
        # confiable en ese caso es result.solver.status == SolverStatus.
        # aborted (la MISMA condicion detras del WARNING "Loading a
        # SolverResults object with an 'aborted' status, but containing a
        # solution" que se ve en todas estas interrupciones, incluida la
        # del monolitico) -- Pyomo la usa especificamente para "el solver
        # fue detenido antes de terminar", no para errores de computo
        # (esos vendrian como excepcion de opt.solve() o SolverStatus.error).
        raise KeyboardInterrupt(f"Interrumpido por el usuario durante el solve ({label}).")
    if result.solver.termination_condition not in _ACCEPTABLE:
        raise RuntimeError(
            f"Solve no llego a una solucion aceptable ({label}): "
            f"{result.solver.termination_condition}"
        )
    return result


class ForwardPass(object):
    """Documento sec. 7.1: resuelve cada bloque como MILP completo en orden
    y1 -> Y, propagando el estado optimo de cada año al parametro heredado
    del año siguiente. Produce una solucion factible completa (candidato a
    UB)."""

    def __init__(self, blocks, solver_kwargs=None):
        self.blocks = blocks  # lista de YearBlockBuilder, ordenada y1..Y
        self.solver_kwargs = solver_kwargs or {}

    def run(self, iteration=None, verbose=True, current_ub=None, current_lb=None):
        x_hat_by_year = {}
        phi_by_year = {}
        alpha_by_year = {}
        full_solution_by_year = {}
        mccormick_residual_by_year = {}

        n = len(self.blocks)
        for i, block in enumerate(self.blocks):
            if verbose:
                k_tag = f"k={iteration} " if iteration is not None else ""
                print(f"[NestedBenders] {k_tag}FORWARD  anio {block.year} ({i + 1}/{n})  "
                      f"{_bounds_tag(current_ub, current_lb)}  resolviendo MILP...")
            if i > 0:
                block.set_heritage(x_hat_by_year[self.blocks[i - 1].year])
            _solve(block.model, label=f"forward y={block.year}", **self.solver_kwargs)
            phi_by_year[block.year] = value(block.model.obj)
            alpha_by_year[block.year] = value(block.model.alpha)
            x_hat_by_year[block.year] = block.extract_state()
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
        }


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
      N_ciclos*b_bar."""

    def __init__(self, blocks, cut_manager=None, solver_kwargs=None,
                 degradation_cut_mode="mccormick", lagrangean_kwargs=None):
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

    def _relax_and_solve(self, block, label):
        clone = block.model.clone()
        TransformationFactory("core.relax_integer_vars").apply_to(clone)
        _solve(clone, label=label, **self.solver_kwargs)
        return clone

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
        completo). `prev` de cada familia se acota por x_hat_base -- mismo
        argumento que _lagrangian_relax_and_solve mas abajo: sin cota, el
        Lagrangeano puede "inventar" estado heredado por encima de lo
        economicamente sensato y el corte sale valido pero inutil.

        Convencion de signo: penalizacion +mu*prev en el objetivo (NO
        -mu*(prev-x_hat)) -- la misma que ya usa (y valido empiricamente)
        _lagrangian_relax_and_solve para N_chargers/G/H. Con esta
        convencion L(mu) = valor_objetivo_aca - mu*x_hat_base (restado
        DESPUES de resolver, ver _lagrangean_subgradient_cut)."""
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
                prev.setub(x_hat_base[state_name])
                penalty_terms.append(mu_fam * prev)
            else:
                for idx, mu_v in mu_fam.items():
                    prev[idx].setub(x_hat_base[state_name][idx])
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
        _build_lagrangian_relaxation, validada empiricamente para este
        backend Pyomo/Gurobi en _lagrangian_relax_and_solve), el ascenso de
        subgradiente correcto es mu <- mu + step*(prev*(mu) - x_hat_base),
        NO mu <- mu - step*(...) como aparece literal en el documento (que
        asume la convencion de signo opuesta, -mu*(z-x_hat) dentro del
        min). Derivacion completa: para L(mu)=g(mu)-mu*x_hat con
        g(mu)=min[f+mu*z], dL/dmu = z*(mu)-x_hat; ascender sobre L (el
        objetivo, ya que L(mu)<=Phi(x_hat) para todo mu por dualidad debil
        y buscamos el mu que de la cota mas ajustada) es mu += step*dL/dmu.
        El paso de Polyak usa Phi^OP (el MIQCP exacto con el estado
        heredado FIJO, sec. 4.3 paso 1) como cota superior conocida de
        max_mu L(mu)."""
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
            _solve(clone, label=f"{label_prefix}lagrangian it={it} y={y}", **exact_kwargs)

            L_mu = value(clone.obj_lagrangian)
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

    def _lagrangian_relax_and_solve(self, block, mu, x_hat_base, label):
        """Corte Strengthened Benders (documento sec. 6.2, opcion 2):
        h^MILP(mu) - mu*x_hat_lin, donde h^MILP(mu) = min_x { f(x) + mu*prev(x) }
        se resuelve como MILP COMPLETO (integralidad restaurada), con las
        restricciones de enlace de las familias "simple" (N_chargers/G/H)
        RELAJADAS -- no fijas al heredado, penalizadas en el objetivo con
        +mu*prev usando el MISMO mu que ya se leyo en la relajacion LP
        (casi el mismo costo: reusa mu, sin busqueda de subgradiente).

        "D" (degradacion) NO participa aca aunque ahora sea kind "simple"
        -- es un mecanismo distinto y ya deshabilitado (ver `strengthen` en
        driver.py) del Camino B (Lagrangeano con subgradiente,
        degradacion_descomposicion_mccormick.md sec. 4, implementado en
        _lagrangean_subgradient_cut): mezclarlos aca reusaria McCormick (no
        la fisica exacta) sin el beneficio que motiva al Camino B, y
        arrastraria el mismo bug de cota conocido de esta ruta.

        Por dualidad debil de Lagrange, h^MILP(mu) >= h^LP(mu) siempre en
        teoria -- PERO solo si "prev" queda acotado de forma economicamente
        sensata: en este modelo el costo de inversion se cobra sobre el
        INCREMENTO (Delta), nunca sobre el stock acumulado, asi que al
        relajar la igualdad "prev" no tiene ningun freno propio hasta su
        tope fisico (g_max_g/h_max/max_bays_k) -- que resulto ser
        demasiado generoso (ver conversacion de diseño: sin acotar mas,
        el Lagrangeano "inventa" capacidad heredada muy por encima de lo
        economicamente sensato y da un corte valido pero inutil).
        Se acota "prev" en ESTA resolucion por el propio punto de
        linealizacion x_hat_base -- no le permite a la relajacion suponer
        mas capacidad heredada de la que el forward pass de esta iteracion
        realmente produjo.

        Devuelve un valor compatible con el parametro `phi_lp` de
        add_cut: "el valor de Phi evaluado en el punto de linealizacion",
        para poder reusar exactamente la misma formula de corte."""
        clone = block.model.clone()

        penalty_terms = []
        penalty_at_lin_point = 0.0
        for link in block.state_links:
            if link.get("kind") != "simple":
                continue
            state_name = link["state"]
            if state_name == "D":
                continue
            if state_name not in mu:
                continue
            getattr(clone, f"link_{state_name}").deactivate()

            prev = getattr(clone, link["prev"])
            mu_fam = mu[state_name]
            if link["index_set"] is None:
                prev.setub(x_hat_base[state_name])
                penalty_terms.append(mu_fam * prev)
                penalty_at_lin_point += mu_fam * x_hat_base[state_name]
            else:
                for idx, mu_v in mu_fam.items():
                    prev[idx].setub(x_hat_base[state_name][idx])
                    penalty_terms.append(mu_v * prev[idx])
                    penalty_at_lin_point += mu_v * x_hat_base[state_name][idx]

        clone.obj.deactivate()
        clone.obj_lagrangian = pyo.Objective(
            expr=clone.obj.expr + sum(penalty_terms), sense=pyo.minimize
        )

        # MILP completo -- SIN relajar integralidad (a diferencia de
        # _relax_and_solve): la fuerza extra viene justamente de resolver
        # con integralidad, no con la relajacion LP.
        _solve(clone, label=label, **self.solver_kwargs)
        h_milp = value(clone.obj_lagrangian)
        return h_milp - penalty_at_lin_point

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
            clone = self._relax_and_solve(child, label=f"backward y={child.year}")
            phi_lp = value(clone.obj)
            mu = self.cut_manager.read_duals(clone, child.year, child.state_links)

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
                if strengthen:
                    if verbose:
                        print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                              f"fortaleciendo corte (Lagrangeano sobre MILP completo)...")
                    phi_cut = self._lagrangian_relax_and_solve(
                        child, mu, x_hat_by_year[parent.year], label=f"strengthened y={child.year}"
                    )

            self.cut_manager.add_cut(
                parent, phi_cut, mu_cut, x_hat_by_year[parent.year], iteration=iteration
            )

        # LB_k = Phi_1 relajado, con el corte que se le acaba de agregar
        # (documento sec. 7.2/8). Si el horizonte tiene un solo año, el
        # bucle de arriba no corre y esto es simplemente la relajacion de
        # ese unico bloque.
        if verbose:
            print(f"[NestedBenders] {k_tag}BACKWARD anio {self.blocks[0].year}  {bounds_tag}  "
                  f"relajando LP (cota inferior LB)...")
        first_clone = self._relax_and_solve(self.blocks[0], label=f"backward LB y={self.blocks[0].year}")
        return value(first_clone.obj)
