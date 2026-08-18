import logging

import pyomo.environ as pyo
from pyomo.environ import value, SolverFactory, TransformationFactory
from pyomo.opt import TerminationCondition

from src.optimization.decomposition.cuts import BendersCutManager

_ACCEPTABLE = (
    TerminationCondition.optimal,
    TerminationCondition.maxTimeLimit,
    TerminationCondition.feasible,
)

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


def _solve(model, solvername="gurobi", gap=0.001, timelimit=900, tee=False, label=""):
    if solvername == "gurobi":
        opt = SolverFactory("gurobi", solver_io="python")
        opt.options["MIPGap"] = gap
        opt.options["TimeLimit"] = timelimit
        opt.options["OutputFlag"] = 0
    else:
        opt = SolverFactory(solvername)

    result = opt.solve(model, tee=tee, load_solutions=True)
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

        # UB_k = sum_y (Phi_{y,k} - alpha_{y,k}) -- se resta alpha para no
        # contar dos veces el costo futuro (documento sec. 7.1).
        ub = sum(phi_by_year[b.year] - alpha_by_year[b.year] for b in self.blocks)

        return {
            "ub": ub,
            "phi": phi_by_year,
            "alpha": alpha_by_year,
            "x_hat": x_hat_by_year,
            "full_solution": full_solution_by_year,
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
    model.cuts)."""

    def __init__(self, blocks, cut_manager=None, solver_kwargs=None):
        self.blocks = blocks
        self.cut_manager = cut_manager or BendersCutManager()
        self.solver_kwargs = solver_kwargs or {}

    def _relax_and_solve(self, block, label):
        clone = block.model.clone()
        TransformationFactory("core.relax_integer_vars").apply_to(clone)
        _solve(clone, label=label, **self.solver_kwargs)
        return clone

    def _lagrangian_relax_and_solve(self, block, mu, x_hat_base, label):
        """Corte Strengthened Benders (documento sec. 6.2, opcion 2):
        h^MILP(mu) - mu*x_hat_lin, donde h^MILP(mu) = min_x { f(x) + mu*prev(x) }
        se resuelve como MILP COMPLETO (integralidad restaurada), con las
        restricciones de enlace de las familias "simple" (N_chargers/G/H)
        RELAJADAS -- no fijas al heredado, penalizadas en el objetivo con
        +mu*prev usando el MISMO mu que ya se leyo en la relajacion LP
        (casi el mismo costo: reusa mu, sin busqueda de subgradiente).

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

        Degradacion NO se relaja aca -- AN_ciclos_hat sigue fijo, igual
        que en el forward normal; su aporte al corte sigue viniendo de
        BendersCutManager._degradation_sensitivity sobre la relajacion LP,
        sumado aparte por add_cut.

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

            phi_cut = phi_lp
            if strengthen:
                if verbose:
                    print(f"[NestedBenders] {k_tag}BACKWARD anio {child.year}  {bounds_tag}  "
                          f"fortaleciendo corte (Lagrangeano sobre MILP completo)...")
                phi_cut = self._lagrangian_relax_and_solve(
                    child, mu, x_hat_by_year[parent.year], label=f"strengthened y={child.year}"
                )

            self.cut_manager.add_cut(
                parent, phi_cut, mu, x_hat_by_year[parent.year], iteration=iteration
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
