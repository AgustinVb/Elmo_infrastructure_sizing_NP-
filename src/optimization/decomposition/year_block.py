import math

import pyomo.environ as pyo
from pyomo.core.base import Suffix
from pyomo.environ import value

from src.optimization.functions import (
    OptSets,
    OptParameters,
    BoundRules,
    ConstraintRules,
    ObjectiveRules,
)


class YearBlockBuilder(object):
    """Construye el subproblema Pyomo de un solo año `y` para la
    descomposicion Nested Benders (ver
    implementacion_descomposicion_carga_ob.md, secciones 2-6).

    Reutiliza las mismas clases OptSets/OptParameters/BoundRules/
    ConstraintRules/ObjectiveRules que arma el modelo monolitico
    (src/optimization/functions.py), instanciadas con years_override=[y].
    Todas las restricciones y costos intra-año (carga, red, BESS,
    produccion, DCH, degradacion intra-año) quedan identicas al monolitico
    porque esas clases ya operan sobre model.years sin mirar y-1.

    Lo unico que este builder agrega por su cuenta es el acople entre años
    (sec. 3 del documento): por cada familia de estado
    (N_chargers, G, H, CumS) declara un parametro heredado mutable
    `<estado>_hat` (actualizado entre iteraciones por el driver forward/
    backward) y una copia continua `<estado>_prev` ligada a el por
    igualdad — esa igualdad es la que produce, via su dual, el corte de
    Benders (sec. 2.3: "la copia se declara continua aunque el estado
    original sea entero, para que el dual este bien definido").
    """

    def __init__(self, mine_system, time_series, year, is_last_year,
                 exogenous_stations, autonomous_mode=False):
        """
        :param year: año de este bloque (debe pertenecer a time_series.years).
        :param is_last_year: True si `year` es el ultimo año del horizonte
            global -- fija alpha=0 (documento sec. 6.1, "alpha_Y=0 fijado en
            el ultimo año, no tiene futuro").
        :param exogenous_stations: dict {k: 0/1} con X[k, year] fijo para
            este año (documento sec. 2.1: X queda fuera del estado en modo
            descompuesto). Requerido -- en modo descompuesto X siempre es
            exogeno.
        """
        self.mine_system = mine_system
        self.time_series = time_series
        self.year = year
        self.is_last_year = is_last_year

        if exogenous_stations is None:
            raise ValueError(
                "YearBlockBuilder requiere exogenous_stations: en modo "
                "descompuesto X no es estado (ver documento sec. 2.1)."
            )
        # Las reglas de functions.py esperan exogenous_stations indexado por
        # (k, y); years_override=[year] asi que solo necesitan la clave del
        # propio año.
        self._exogenous_stations_for_rules = {
            (k, year): v for k, v in exogenous_stations.items()
        }

        self.set_builder = OptSets(
            mine_system, time_series, autonomous_mode=autonomous_mode, years_override=[year]
        )
        self.param_rules = OptParameters(
            mine_system, time_series, years_override=[year]
        )
        self.bound_rules = BoundRules(
            mine_system, time_series, years_override=[year],
            exogenous_stations=self._exogenous_stations_for_rules,
        )
        self.constraint_rules = ConstraintRules(
            mine_system, time_series, years_override=[year],
            exogenous_stations=self._exogenous_stations_for_rules,
        )
        self.objective_rules = ObjectiveRules(
            mine_system, time_series, years_override=[year],
            exogenous_stations=self._exogenous_stations_for_rules,
        )

        # Nombres de los parametros heredados que el driver forward/backward
        # actualiza entre iteraciones -- (nombre_hat, nombre_prev, index_set)
        # con index_set=None para estados escalares (H, CumS).
        self.state_links = []

        self.model = self._build()

    def _build(self):
        model = pyo.ConcreteModel(name=f"YearBlock_{self.year}")

        self.set_builder.build_sets(model)
        self.param_rules.build_parameters(model)
        self.bound_rules.build_all_variables(model)
        self.constraint_rules.build_all_constraints(model)

        self._add_state_linking(model)
        self._add_cost_to_go(model)

        def _obj(m):
            # f_y (documento sec. 5) + costo-to-go del resto del horizonte.
            # ObjectiveRules.total_cost ya es exactamente f_y porque
            # model.years = {year} en este bloque.
            return self.objective_rules.total_cost(m) + m.alpha
        model.obj = pyo.Objective(rule=_obj, sense=pyo.minimize)

        model.dual = Suffix(direction=Suffix.IMPORT)

        return model

    def _add_linear_state(self, model, state_name, state_var_name, delta_name, accum_var, index_set,
                           prev_bound=None):
        """Acople generico 'stock = copia_continua_del_año_anterior + incremento
        local', para las 3 familias de estado con acumulacion simple
        (N_chargers, G, H) -- ver documento sec. 3, ecuaciones adyacentes
        (y-1 -> y). CumS tiene su propia linealizacion (ver
        _add_degradation_state) porque incluye el big-M de reemplazo R_y.

        state_name: nombre logico del estado (usado para <estado>_hat/_prev
        y para la clave del corte). state_var_name: nombre del componente
        Pyomo real que guarda el stock (p.ej. "G_g" para el estado "G").

        prev_bound: cota superior fisica de "prev" (mismo Param que ya
        acota el stock acumulado en otra restriccion del modelo, p.ej.
        max_bays_k/g_max_g/h_max) -- no cambia nada en el forward/backward
        normal (ahi "prev" siempre queda forzado a "hat" por la igualdad de
        enlace, la cota nunca ata), pero es necesaria para que el corte
        Strengthened Benders (documento sec. 6.2) quede bien planteado: al
        relajar esa igualdad para evaluar el Lagrangeano, "prev" se
        convierte en variable libre, y como el costo de inversion se cobra
        sobre el incremento Delta (no sobre el stock), sin esta cota "prev"
        no tiene nada que lo frene antes de crecer sin limite economico
        real (ver conversacion de diseño: la relajacion sin cota daba un
        corte valido pero inutil, -158900 en vez de un valor cercano al
        de la relajacion LP)."""
        y = self.year
        hat_name = f"{state_name}_hat"
        prev_name = f"{state_name}_prev"

        if index_set is None:
            setattr(model, hat_name, pyo.Param(initialize=0.0, mutable=True))
            bounds = (0, value(prev_bound)) if prev_bound is not None else None
            setattr(model, prev_name, pyo.Var(domain=pyo.NonNegativeReals, bounds=bounds))
            hat = getattr(model, hat_name)
            prev = getattr(model, prev_name)
            setattr(model, f"link_{state_name}", pyo.Constraint(expr=prev == hat))
            setattr(model, f"accum_{state_name}",
                    pyo.Constraint(expr=accum_var[y] == prev + delta_name[y]))
        else:
            setattr(model, hat_name, pyo.Param(index_set, initialize=0.0, mutable=True))
            if prev_bound is not None:
                bounds_rule = lambda m, *idx: (0, value(prev_bound[idx[0]]))
                setattr(model, prev_name, pyo.Var(index_set, domain=pyo.NonNegativeReals, bounds=bounds_rule))
            else:
                setattr(model, prev_name, pyo.Var(index_set, domain=pyo.NonNegativeReals))
            hat = getattr(model, hat_name)
            prev = getattr(model, prev_name)
            setattr(model, f"link_{state_name}",
                    pyo.Constraint(index_set, rule=lambda m, *idx: prev[idx] == hat[idx]))
            setattr(model, f"accum_{state_name}",
                    pyo.Constraint(index_set,
                                   rule=lambda m, *idx: accum_var[idx + (y,)] == prev[idx] + delta_name[idx + (y,)]))

        self.state_links.append({
            "state": state_name, "state_var": state_var_name,
            "hat": hat_name, "prev": prev_name, "index_set": index_set,
            "kind": "simple",
        })

    def _compute_n_ciclos_bounds(self, model):
        """Cotas [N_L, N_U] de N_ciclos_y para ESTE año, adaptando a un solo
        año el calculo greedy que el monolitico usa para su version EXACTA
        (comentado en BoundRules.build_all_variables): N_L es la cobertura
        minima fraccionaria del target de produccion (cota valida para
        CUALQUIER solucion factible); N_U es la cobertura entera greedy con
        margen de seguridad. Documento sec. 10: "Big-M de degradación
        flojo. Recalcular la cota con el B_y del año, no con una constante
        global, o el big-M queda flojo y debilita los cortes" -- por eso
        esto se recalcula por bloque en vez de reusar una cota global."""
        y = self.year
        B_L = value(model.B_L)
        B_U = value(model.B_U)
        n_elhd = value(model.n_elhd_bd)
        N_U_SAFETY_MARGIN = 1.10

        def _greedy_min_cost_fractional(nodes_sorted, total_target):
            remaining = total_target
            energy = 0.0
            for ratio, ppa, ub_j, e_j in nodes_sorted:
                if remaining <= 0:
                    break
                tomar = min(ub_j * ppa, remaining)
                energy += tomar * ratio
                remaining -= tomar
            return energy

        def _greedy_min_cost_integer(nodes_sorted, total_target):
            remaining = total_target
            energy = 0.0
            for ratio, ppa, ub_j, e_j in nodes_sorted:
                if remaining <= 0:
                    break
                visits = min(ub_j, math.ceil(remaining / ppa))
                energy += visits * e_j
                remaining -= visits * ppa
            return energy

        node_lhd_pairs = {}
        for (i2, j2, y2, d2, t2) in model.Y_INDEX:
            node_lhd_pairs.setdefault((y2, d2, j2), []).append(i2)

        e_day_low = 0.0
        e_day_high = 0.0
        for d in model.days:
            nodes_info = []
            total_target_day = 0.0
            for j in model.nodes_set:
                i_list = node_lhd_pairs.get((y, d, j))
                if not i_list:
                    continue
                elec_i_list = [i for i in i_list if i in model.elhd_set]
                if not elec_i_list:
                    continue
                i_j = elec_i_list[0]

                prod_per_assign = (value(model.g_i[i_j]) * self.time_series.get_n_trips(j, i_j)
                                    * value(model.filling_factor[i_j]))
                target = value(model.m_j[j, y])
                ub_j = math.ceil(target / prod_per_assign) + 1

                e_j = (value(model.pe_i[i_j, j]) * value(model.d_i[i_j, j])
                       * self.time_series.get_n_trips(j, i_j) / value(model.eta_discharge_i[i_j]))

                nodes_info.append((e_j / prod_per_assign, prod_per_assign, ub_j, e_j))
                total_target_day += target

            nodes_sorted = sorted(nodes_info, key=lambda info: info[0])
            e_day_low += _greedy_min_cost_fractional(nodes_sorted, total_target_day)
            e_day_high += _greedy_min_cost_integer(nodes_sorted, total_target_day)

        e_year_low = e_day_low * value(model.scaling_factor_op_cost)
        e_year_high = e_day_high * value(model.scaling_factor_op_cost) * N_U_SAFETY_MARGIN

        n_slots_per_year = len(model.ZCHARGE_INDEX) * len(model.days) * len(model.time_intervals_set)
        max_energy_per_year = (n_slots_per_year * value(model.p_charger) * value(model.delta_t)
                                * value(model.scaling_factor_op_cost))

        N_L = max(math.floor(e_year_low / (n_elhd * B_U)), 0)
        N_U_prod = math.ceil(e_year_high / (n_elhd * B_L))
        N_U_charger = math.ceil(max_energy_per_year / (n_elhd * B_L))
        N_U = max(min(N_U_charger, N_U_prod), N_L)

        return N_L, N_U

    def _add_degradation_state(self, model):
        """Acople de degradación -- documento sec. 4 "Opción A", formulación
        EXACTA (no la aproximación CumS/gamma_lin que usa el monolítico).

        Con la convención "reemplazo al inicio del año", B_y (capacidad
        operable del año) depende solo de AN_ciclos_hat (heredado, un
        PARÁMETRO conocido en esta resolución) y R_y (binaria de este
        año) -- no de N_ciclos_y ni AN_ciclos_y propios. Eso rompe la
        circularidad que fuerza al monolítico a usar la expansión binaria
        completa (ver BoundRules.build_all_variables, bloque comentado:
        "b_bar depende de N_ciclos que depende de b_bar").

        AN_ciclos_hat se usa como PARÁMETRO directo (no como copia
        continua tipo "_prev") en las tres ecuaciones de abajo: así,
        b_bar_def y arrastre quedan lineales exactos (Param × Var), y
        energy_link queda lineal salvo un ÚNICO big-M (NB_y := R_y·N_ciclos_y,
        binaria × continua) -- exactamente "un único big-M sobre
        N^ciclos_y" que describe el documento.

        Como AN_ciclos_hat entra como COEFICIENTE (no como constante
        aditiva), no hay un dual directo que dé su sensibilidad para el
        corte de Benders: BendersCutManager.read_duals la calcula vía
        teorema de la envolvente, sumando los duales de b_bar_def/
        energy_link/arrastre ponderados por la derivada parcial analítica
        de cada restricción respecto de AN_ciclos_hat (ver conversación de
        diseño para la derivación completa)."""
        if self.mine_system.battery_degradation is None:
            return
        y = self.year

        N_L, N_U = self._compute_n_ciclos_bounds(model)
        an_ciclos_max = N_U * len(self.time_series.years)

        model.AN_ciclos_hat = pyo.Param(initialize=0.0, mutable=True)

        model.N_ciclos = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(N_L, N_U))
        model.AN_ciclos = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(0, an_ciclos_max))
        model.NB = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(0, N_U))

        # B_y = b_max - gamma*AN_ciclos_hat + gamma*AN_ciclos_hat*R_y --
        # Param x Var, lineal exacto (AN_ciclos_hat es coeficiente, no Var).
        model.b_bar_def = pyo.Constraint(expr=(
            model.b_bar[y] == model.b_max_fleet
            - model.gamma_coef * model.AN_ciclos_hat
            + model.gamma_coef * model.AN_ciclos_hat * model.R[y]
        ))

        # NB_y := R_y * N_ciclos_y -- unico big-M binaria x continua.
        model.nb_upper_R = pyo.Constraint(expr=model.NB[y] <= N_U * model.R[y])
        model.nb_lower_R = pyo.Constraint(expr=model.NB[y] >= N_L * model.R[y])
        model.nb_upper_cum = pyo.Constraint(expr=model.NB[y] <= model.N_ciclos[y] - N_L * (1 - model.R[y]))
        model.nb_lower_cum = pyo.Constraint(expr=model.NB[y] >= model.N_ciclos[y] - N_U * (1 - model.R[y]))

        # n_elhd * B_y * N_ciclos_y = S_y (forma producto para evitar la
        # division, ec. 20 reescrita) -- lineal en las variables de
        # decision (N_ciclos_y, NB_y) porque AN_ciclos_hat es coeficiente.
        model.energy_link = pyo.Constraint(expr=(
            model.n_elhd_bd * model.b_max_fleet * model.N_ciclos[y]
            - model.n_elhd_bd * model.gamma_coef * model.AN_ciclos_hat * model.N_ciclos[y]
            + model.n_elhd_bd * model.gamma_coef * model.AN_ciclos_hat * model.NB[y]
            == model.S[y]
        ))

        # AN_ciclos_y = (1-R_y)*AN_ciclos_hat + N_ciclos_y -- Param x Var,
        # lineal exacto.
        model.arrastre = pyo.Constraint(expr=(
            model.AN_ciclos[y] == model.AN_ciclos_hat * (1 - model.R[y]) + model.N_ciclos[y]
        ))

        self.state_links.append({
            "state": "AN_ciclos", "state_var": "AN_ciclos",
            "hat": "AN_ciclos_hat", "index_set": None,
            "kind": "degradation",
        })

    def _add_state_linking(self, model):
        # prev_bound: mismo tope fisico que ya usa la restriccion
        # operativa correspondiente (max_n_chargers/gen_max_units/
        # max_storage_units) -- necesario para que Strengthened Benders
        # quede bien planteado (ver docstring de _add_linear_state).
        self._add_linear_state(model, "N_chargers", "N_chargers",
                                model.Delta_N_chargers, model.N_chargers, model.stations_set,
                                prev_bound=model.max_bays_k)

        if len(list(model.gen_set)) > 0:
            self._add_linear_state(model, "G", "G_g", model.Delta_G_g, model.G_g, model.gen_set,
                                    prev_bound=model.g_max_g)

        if len(list(model.storage_set)) > 0:
            self._add_linear_state(model, "H", "H", model.Delta_H, model.H, None,
                                    prev_bound=model.h_max)

        self._add_degradation_state(model)

    def _add_cost_to_go(self, model):
        # Todos los terminos de costo (inversion, energia, reemplazo de
        # bateria) son no-negativos (documento/ObjectiveRules.total_cost),
        # por lo que Phi_{y+1} >= 0 siempre: alpha >= 0 es una cota inferior
        # trivial valida que evita que el forward pass sea no acotado antes
        # de que existan cortes (iteracion 1, lista de cortes vacia).
        model.alpha = pyo.Var(domain=pyo.NonNegativeReals)
        model.cuts = pyo.ConstraintList()
        if self.is_last_year:
            # Documento sec. 6.1: "alpha_Y = 0 fijado en el ultimo año (no
            # tiene futuro)". Sin fijar esto, las cotas quedan mal sin error
            # visible (documento sec. 10).
            model.alpha.fix(0.0)

    def set_heritage(self, values):
        """Actualiza los parametros heredados <estado>_hat con el estado
        optimo del año anterior (o los stocks iniciales si year==y1). El
        driver forward/backward llama esto antes de cada resolucion.

        :param values: dict {state_name: valor} para estados escalares
            (H, CumS), o {state_name: {idx: valor}} para estados indexados
            (N_chargers, G).
        """
        for link in self.state_links:
            state_name = link["state"]
            if state_name not in values:
                continue
            hat = getattr(self.model, link["hat"])
            new_value = values[state_name]
            if link["index_set"] is None:
                hat.set_value(float(new_value))
            else:
                for idx, v in new_value.items():
                    hat[idx].set_value(float(v))

    def extract_state(self):
        """Extrae x̂_y: el estado optimo de ESTE bloque ya resuelto (para
        alimentar set_heritage() del bloque year+1 en el forward pass, y
        como punto base x̂_{y,k} al construir un corte para el bloque
        anterior -- documento sec. 6.2). Formato identico al que espera
        set_heritage: {state_name: valor} o {state_name: {idx: valor}}."""
        result = {}
        for link in self.state_links:
            state_var = getattr(self.model, link["state_var"])
            if link["index_set"] is None:
                result[link["state"]] = value(state_var[self.year])
            else:
                result[link["state"]] = {
                    idx: value(state_var[idx, self.year]) for idx in link["index_set"]
                }
        return result

    def extract_full_solution(self):
        """Vuelca TODAS las variables de este bloque ya resuelto (no solo
        el estado -- ver extract_state) para reconstruir un reporte
        completo: Printer necesita las variables operativas (carga,
        asignacion, red, etc.), no solo N_chargers/G/H/CumS. Formato:
        {nombre_variable: {indice: valor}} o {nombre_variable: valor} si es
        escalar (sin indice). Los nombres son los mismos que usa el modelo
        monolitico para las variables "reales" (N_chargers, G_g, H, Z, Y,
        P, ...); las variables internas del bloque (alpha, *_hat, *_prev,
        W_s) no tienen equivalente monolitico y el llamador las descarta."""
        result = {}
        for var_comp in self.model.component_objects(pyo.Var, active=True):
            name = var_comp.name
            if var_comp.is_indexed():
                result[name] = {
                    idx: value(var_comp[idx])
                    for idx in var_comp
                    if var_comp[idx].value is not None
                }
            else:
                v = value(var_comp, exception=False)
                if v is not None:
                    result[name] = v
        return result
