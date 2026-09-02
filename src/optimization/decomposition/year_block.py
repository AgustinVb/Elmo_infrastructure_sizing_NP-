import pyomo.environ as pyo
from pyomo.core.base import Suffix
from pyomo.environ import value

from src.optimization.functions import (
    OptSets,
    OptParameters,
    BoundRules,
    ConstraintRules,
    ObjectiveRules,
    compute_n_ciclos_bounds,
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
    (sec. 3 del documento). Dos mecanismos, segun como se decide cada
    estado en el monolitico (ver functions.py):

    - Stock+Delta acumulado año a año (N_chargers, y D con matices -- ver
      mas abajo), via `_add_linear_state`: declara un parametro heredado
      mutable `<estado>_hat` (actualizado entre iteraciones por el driver
      forward/backward) y una copia continua `<estado>_prev` ligada a el
      por igualdad — esa igualdad es la que produce, via su dual, el corte
      de Benders (sec. 2.3: "la copia se declara continua aunque el estado
      original sea entero, para que el dual este bien definido"), mas la
      restriccion de acumulacion local stock=prev+delta.
    - Decision unica para todo el horizonte (G, H, P_max_k -- desde que se
      eliminaron Delta_G_g/Delta_H/Delta_P_max_k del monolitico), via
      `_add_global_once_state`: el bloque del primer año del horizonte
      GLOBAL es el unico que decide el valor (variable libre, sin
      hat/prev); los bloques siguientes fijan la variable real == el
      heredado directo (sin Delta ni acumulacion que mantener aparte).

    Degradacion de bateria (formulacion B_y/D_y, ver
    degradacion_descomposicion_mccormick.md): aca el estado D_y (capacidad
    al FINAL del año) entra en el acople con el mecanismo stock+Delta
    (kind "simple", igual que N_chargers, no "global_once") --
    D_prev == D_hat, aditivo -- porque el unico termino no lineal del
    bloque (N_ciclos_y * b_bar_y == S_y, PRODUCTO DE DOS VARIABLES DEL
    MISMO AÑO, no cruza años) se resuelve aparte con la relajacion de
    McCormick (Camino A del documento, sec. 3) directamente en el modelo
    de este bloque. Eso deja el subproblema anual como MILP puro y
    permite que el mecanismo generico de duales/cortes (cuts.py) trate
    "D" exactamente igual que "N_chargers" -- ya no hace falta el teorema
    de la envolvente que
    requeria el esquema viejo AN_ciclos (donde el heredado entraba como
    COEFICIENTE, no como constante aditiva).
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
        local', para las familias de estado con acumulacion simple año a
        año (N_chargers) -- ver documento sec. 3, ecuaciones adyacentes
        (y-1 -> y). G/H/P_max_k usan en cambio _add_global_once_state
        (decision unica para todo el horizonte, sin Delta). CumS tiene su
        propia linealizacion (ver _add_degradation_state) porque incluye
        el big-M de reemplazo R_y.

        state_name: nombre logico del estado (usado para <estado>_hat/_prev
        y para la clave del corte). state_var_name: nombre del componente
        Pyomo real que guarda el stock (p.ej. "N_chargers" para el estado
        "N_chargers").

        prev_bound: cota superior fisica de "prev" (mismo Param que ya
        acota el stock acumulado en otra restriccion del modelo, p.ej.
        max_bays_k para N_chargers) -- no cambia nada en el forward/backward
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

    def _add_global_once_state(self, model, state_name, state_var_name, index_set):
        """Acople para un estado que se decide UNA sola vez para todo el
        horizonte (G_g, H, P_max_k desde que se eliminaron Delta_G_g/
        Delta_H/Delta_P_max_k del monolitico -- ver ObjectiveRules.
        gen_investment_cost/bess_investment_cost/substation_investment_cost
        en functions.py): a diferencia de _add_linear_state no hay Delta ni
        acumulacion stock=prev+delta.

        Bloque del PRIMER año del horizonte GLOBAL (origen): no hace nada
        extra -- state_var (model.G_g/model.H/model.P_max_k, ya creada libre
        por BoundRules) queda como la UNICA variable de decision; solo se
        registra la entrada en state_links sin hat/prev (mismo patron de
        borde que usa "D" en el primer año, ver _add_degradation_state),
        para que extract_state() reporte el valor optimo y el driver lo
        propague a los años siguientes.

        Años siguientes: se fuerza state_var == <estado>_hat directo sobre
        la variable real -- sin una copia "prev" separada (a diferencia de
        N_chargers/D) porque aca no hay ninguna aritmetica de acumulacion
        que mantener aparte del valor heredado: el heredado ES el valor de
        la variable, punto. El dual de esa igualdad es igual de valido
        para el corte de Benders."""
        first_year = self.bound_rules._first_year()
        state_var = getattr(model, state_var_name)

        if self.year == first_year:
            self.state_links.append({
                "state": state_name, "state_var": state_var_name,
                "hat": None, "prev": None, "index_set": index_set,
                "kind": "global_once",
            })
            return

        hat_name = f"{state_name}_hat"
        if index_set is None:
            setattr(model, hat_name, pyo.Param(initialize=0.0, mutable=True))
            hat = getattr(model, hat_name)
            setattr(model, f"link_{state_name}", pyo.Constraint(expr=state_var == hat))
        else:
            setattr(model, hat_name, pyo.Param(index_set, initialize=0.0, mutable=True))
            hat = getattr(model, hat_name)
            setattr(model, f"link_{state_name}",
                    pyo.Constraint(index_set, rule=lambda m, *idx: state_var[idx] == hat[idx]))

        self.state_links.append({
            "state": state_name, "state_var": state_var_name,
            "hat": hat_name, "prev": None, "index_set": index_set,
            "kind": "global_once",
        })

    def _compute_n_ciclos_bounds(self, model):
        """Cotas [N_L, N_U] de N_ciclos_y para ESTE año -- ver
        compute_n_ciclos_bounds en functions.py (funcion compartida con el
        Camino A aplicado al monolitico completo)."""
        return compute_n_ciclos_bounds(model, self.year, self.time_series)

    def _add_degradation_state(self, model):
        """Acople de degradación -- Camino A (McCormick) de
        degradacion_descomposicion_mccormick.md, secs. 2-3.

        Estado de acople: D_y (capacidad al FINAL del año), no AN_ciclos.
        d_y_fade (D_y = b_bar_y - gamma*N_ciclos_y) y s_def (S_y = energia
        cargada) ya quedaron registradas por ConstraintRules.
        build_all_constraints (funciones intra-año, sin cruce de años,
        validas tal cual para un bloque de un solo año). Lo que este
        metodo agrega:

        1. Acople y-1 -> y (solo si `year` no es el primer año del
           horizonte GLOBAL): copia continua D_prev ligada por igualdad al
           parametro heredado D_hat (mismo patron que N_chargers/G/H,
           kind "simple" -- ver docstring de la clase), y la restriccion
           b_y_link_local: b_bar_y <= D_prev + replace_capacity_fraction *
           b_max * R_y (doc sec. 3.3, ecuacion (2) reescrita con D_prev en
           vez de model.D[y-1] -- ese indice no existe en un bloque de un
           solo año, ver guard is_decomposed_block en
           ConstraintRules.build_all_constraints).
           Para el primer año, b_bar/R ya quedan fijados por BoundRules
           (b_bar[y1]=b_max, R[y1]=0): no hay heredado que enlazar.

        2. El bilineal n_ciclos_link (n_elhd*b_bar_y*N_ciclos_y = S_y),
           SIEMPRE (incluye el primer año -- D_y=d_y_fade lo necesita para
           poder heredarse al año 2), reemplazado por la envolvente de
           McCormick de la variable auxiliar w_y ~= b_bar_y*N_ciclos_y
           (doc sec. 3.3). Las cotas [N_L,N_U] se recalculan por bloque
           (_compute_n_ciclos_bounds, greedy) y se usan tanto como
           coeficientes de la envolvente como para re-acotar la propia Var
           N_ciclos -- doc sec. 10 / implementacion_descomposicion_carga_
           ob.md sec. 10: "recalcular la cota con el B_y del año, o el
           big-M queda flojo y debilita los cortes".

        Con esto el subproblema anual es MILP puro (doc sec. 3.3, ultimo
        parrafo): el mecanismo generico de duales de cuts.py trata "D"
        igual que "H", sin necesidad de teorema de la envolvente."""
        if self.mine_system.battery_degradation is None:
            return
        y = self.year

        N_L, N_U = self._compute_n_ciclos_bounds(model)
        model.N_ciclos[y].setlb(N_L)
        model.N_ciclos[y].setub(N_U)

        B_L = value(model.B_L)
        B_U = value(model.B_U)

        # w_deg ~= b_bar[y] * N_ciclos[y] -- envolvente convexa de
        # McCormick (doc sec. 3.3), reemplaza el producto bilineal exacto.
        model.w_deg = pyo.Var(domain=pyo.NonNegativeReals)
        model.mccormick_lb1 = pyo.Constraint(expr=(
            model.w_deg >= N_L * model.b_bar[y] + B_L * model.N_ciclos[y] - N_L * B_L
        ))
        model.mccormick_lb2 = pyo.Constraint(expr=(
            model.w_deg >= N_U * model.b_bar[y] + B_U * model.N_ciclos[y] - N_U * B_U
        ))
        model.mccormick_ub1 = pyo.Constraint(expr=(
            model.w_deg <= N_U * model.b_bar[y] + B_L * model.N_ciclos[y] - N_U * B_L
        ))
        model.mccormick_ub2 = pyo.Constraint(expr=(
            model.w_deg <= N_L * model.b_bar[y] + B_U * model.N_ciclos[y] - N_L * B_U
        ))
        # n_elhd * w_deg = S_y -- version lineal (McCormick) de n_ciclos_link.
        model.mccormick_energy = pyo.Constraint(expr=(
            model.n_elhd_bd[y] * model.w_deg == model.S[y]
        ))

        first_year = self.bound_rules._first_year()
        if y == first_year:
            # Condicion de borde (doc sec. 2, "Con condición de borde en
            # y_1"): b_bar[y1]/R[y1] ya fijados por BoundRules, no hay
            # D_hat que enlazar. Igual se registra "D" en state_links (sin
            # hat/prev) para que extract_state() reporte D[y1] -- el año 2
            # lo necesita como D_hat heredado (ver set_heritage).
            self.state_links.append({
                "state": "D", "state_var": "D", "hat": None, "prev": None,
                "index_set": None, "kind": "simple",
            })
            return

        model.D_hat = pyo.Param(initialize=0.0, mutable=True)
        model.D_prev = pyo.Var(domain=pyo.NonNegativeReals, bounds=(0, B_U))
        model.link_D = pyo.Constraint(expr=model.D_prev == model.D_hat)
        model.b_y_link_local = pyo.Constraint(expr=(
            model.b_bar[y] <= model.D_prev + model.replace_capacity_fraction * model.b_max_fleet * model.R[y]
        ))

        self.state_links.append({
            "state": "D", "state_var": "D",
            "hat": "D_hat", "prev": "D_prev", "index_set": None,
            "kind": "simple",
        })

    def mccormick_residual(self):
        """Validacion obligatoria (doc sec. 3.4): residuo del bilineal
        EXACTO evaluado en la solucion optima de la relajacion McCormick --
        N_ciclos_y*b_bar_y - S_y/n_elhd. Cero si McCormick resulto exacto
        en el optimo (caso tipico: uno de b_bar_y/N_ciclos_y queda pegado a
        una de sus cotas); un residuo grande indica que conviene refinar
        (piecewise McCormick, doc sec. 3.5) o pasar al Camino B
        (Lagrangeano)."""
        if self.mine_system.battery_degradation is None:
            return None
        y = self.year
        n_ciclos_val = value(self.model.N_ciclos[y], exception=False)
        b_bar_val = value(self.model.b_bar[y], exception=False)
        s_val = value(self.model.S[y], exception=False)
        if n_ciclos_val is None or b_bar_val is None or s_val is None:
            return None  # bloque aun no resuelto
        n_elhd = value(self.model.n_elhd_bd[y])
        return n_ciclos_val * b_bar_val - s_val / n_elhd

    def _add_state_linking(self, model):
        # prev_bound: mismo tope fisico que ya usa la restriccion operativa
        # correspondiente (max_n_chargers) -- necesario para que
        # Strengthened Benders quede bien planteado (ver docstring de
        # _add_linear_state).
        self._add_linear_state(model, "N_chargers", "N_chargers",
                                model.Delta_N_chargers, model.N_chargers, model.stations_set,
                                prev_bound=model.max_bays_k)

        # Potencia de subestacion: decidida UNA sola vez para todo el
        # horizonte (ya no Delta/stock año a año, ver BoundRules en
        # functions.py) -- usa el acople "global_once", igual que G_g/H.
        self._add_global_once_state(model, "P_max_k", "P_max_k", model.stations_set)

        if len(list(model.gen_set)) > 0:
            # G_g ya no tiene Delta/indice de año (decidida una sola vez
            # para todo el horizonte, ver BoundRules) -- usa el acople
            # "global_once", no el de stock+Delta.
            self._add_global_once_state(model, "G", "G_g", model.gen_set)

        if len(list(model.storage_set)) > 0:
            self._add_global_once_state(model, "H", "H", None)

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
        set_heritage: {state_name: valor} o {state_name: {idx: valor}}.

        Estados "global_once" (G, H) no tienen indice de año en el
        componente Pyomo real (decidida una sola vez para todo el
        horizonte, ver _add_global_once_state) -- se leen directo, sin
        agregar self.year al indice."""
        result = {}
        for link in self.state_links:
            state_var = getattr(self.model, link["state_var"])
            is_global_once = link.get("kind") == "global_once"
            if link["index_set"] is None:
                result[link["state"]] = value(state_var if is_global_once else state_var[self.year])
            else:
                result[link["state"]] = {
                    idx: value(state_var[idx] if is_global_once else state_var[idx, self.year])
                    for idx in link["index_set"]
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
