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
    if result.solver.termination_condition == TerminationCondition.maxTimeLimit:
        ub = result.problem.upper_bound
        lb = result.problem.lower_bound
        if ub not in (None, 0) and lb is not None:
            gap_rel = abs(ub - lb) / abs(ub)
            print(f"[NestedBenders] {label}  TimeLimit alcanzado sin cerrar MIPGap: "
                  f"UB={ub:,.2f}  LB={lb:,.2f}  gap={gap_rel:.4%}")
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
      N_ciclos*b_bar.

    Un tercer camino, "disjunctive" (corte big-M sobre la disyuncion
    R_y=0/R_y=1 del reemplazo de bateria, ver _disjunctive_replace_cut),
    quedo implementado en este archivo pero PAUSADO/no seleccionable
    (merge 2026-08 con origin/carga_ob_multiaño): origin reestructuro G/H
    a estado "global_once" en year_block.py/cuts.py, cambiando el terreno
    sobre el que se diagnostico el problema original (mu=0 para
    N_chargers/G/H). Antes de reactivarlo (ver comentarios "PAUSADO" en
    BackwardPass.run) hay que re-evaluar si el diagnostico sigue
    aplicando con el modelo nuevo."""

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
        el mu final: en este modelo el "recurso completo garantizado"
        (documento sec. 1) hace que la relajacion LP quede degenerada y
        mu salga sistematicamente ~0 para N_chargers/G/H (confirmado en
        una corrida real: mu=0 en las 3 primeras iteraciones del
        escenario 960kW_2dias), lo cual no aporta ninguna guia al forward
        pass. Un MILP con integralidad completa SI puede tener
        sensibilidad Lagrangeana no nula donde el LP relajado es
        degenerado -- de ahi la busqueda, no solo reusar mu_init tal
        cual (que es lo que hacia la version anterior, ya removida).

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
            return target, mu_init

        mu = {k: (dict(v) if isinstance(v, dict) else v) for k, v in mu_init.items()}
        best_L = float("-inf")
        best_mu = mu
        prev_L = None
        gap_scale = max(abs(target), 1.0)

        for it in range(1, max_iter + 1):
            clone = self._build_strengthened_relaxation(child, mu, fix_vars=fix_vars)
            _solve(clone, label=f"{label_prefix}strengthened it={it} y={y}", **self.solver_kwargs)

            L_mu = value(clone.obj_lagrangian)
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
                # PAUSADO (merge 2026-08 con origin/carga_ob_multiaño): origin
                # reestructuro G/H a estado "global_once" (decision unica al
                # inicio del horizonte, ver year_block.py/cuts.py), lo que
                # cambia el terreno sobre el que se diagnostico el problema
                # original (mu=0 para N_chargers/G/H bajo "recurso completo
                # garantizado"). Antes de reactivar esto hay que re-evaluar si
                # el diagnostico y el fix siguen aplicando con el modelo
                # nuevo. La implementacion (_strengthened_subgradient_cut,
                # _build_strengthened_relaxation) sigue intacta mas abajo.
                # if strengthen:
                #     if verbose:
                #         print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                #               f"fortaleciendo corte (Lagrangeano con subgradiente sobre MILP completo)...")
                #     phi_cut, mu_cut = self._strengthened_subgradient_cut(
                #         child, x_hat_by_year[parent.year], mu_init=mu,
                #         verbose=verbose, label_prefix=f"{k_tag}",
                #     )

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
        first_clone = self._relax_and_solve(self.blocks[0], label=f"backward LB y={self.blocks[0].year}")
        return value(first_clone.obj)
