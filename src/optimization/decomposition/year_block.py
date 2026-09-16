import contextlib

import pyomo.environ as pyo
from pyomo.core.base import Suffix
from pyomo.environ import TransformationFactory, value

from src.optimization.functions import (
    OptSets,
    OptParameters,
    BoundRules,
    ConstraintRules,
    ObjectiveRules,
)


class YearBlockBuilder(object):
    """Construye el subproblema Pyomo de un solo año `y` para la descomposicion
    Nested Benders del modelo de battery swapping.

    Reutiliza las mismas clases OptSets/OptParameters/BoundRules/
    ConstraintRules/ObjectiveRules que arma el modelo monolitico
    (src/optimization/functions.py), instanciadas con years_override=[y]. Todas
    las restricciones y costos intra-año (swap, red, BESS, produccion,
    degradacion intra-año) quedan identicas al monolitico porque esas clases ya
    operan sobre model.years sin mirar y-1.

    Lo unico que este builder agrega por su cuenta es el acople entre años. Dos
    mecanismos, segun como se decide cada estado en el monolitico:

    - Stock+Delta acumulado año a año (N_bays, N_chargers, N_batteries), via
      `_add_linear_state`: declara un parametro heredado mutable `<estado>_hat`
      (que el driver actualiza entre iteraciones) y una copia CONTINUA
      `<estado>_prev` ligada a el por igualdad. Esa igualdad es la que produce,
      via su dual, el corte de Benders -- la copia se declara continua aunque el
      estado original sea entero, porque es lo que hace que el multiplicador
      quede bien definido al relajar. Ademas agrega la acumulacion local
      stock = prev + delta, que en el monolitico son las link_*_stock (alli
      miran model.X[k, y-1], un indice que no existe en un bloque de un solo
      año: por eso ConstraintRules las omite cuando is_decomposed_block).

    - Decision unica para todo el horizonte (n_ssee_k, G_g, H), via
      `_add_global_once_state`: el bloque del PRIMER año del horizonte global es
      el unico que la decide (variable libre, sin hat/prev); los bloques
      siguientes fijan la variable real al valor heredado, sin Delta ni
      acumulacion que mantener aparte.

    `X` (apertura de naves) no es estado aca: en modo descompuesto es exogeno
    (un Param, ver BoundRules), porque la asignacion equipo-nave es estatica.
    Su costo de apertura queda fuera del objetivo del bloque y hay que sumarlo
    aparte al comparar contra el monolitico.

    Degradacion del pool de swap (formulacion B/D): el estado es D_y, la
    capacidad al FINAL del año, y entra en el acople con el mismo mecanismo
    stock+Delta (kind "simple") porque en b_y_link aparece como constante
    aditiva, no como coeficiente. La reduccion del trilineal de la ec. 3 a dos
    bilineales y su envolvente de McCormick ya las arma ConstraintRules dentro
    del año (build_mccormick_degradation_block), asi que aca no hay que
    repetirlas: basta con la copia local de D y con reescribir b_y_link sobre
    ella.
    """

    def __init__(self, mine_system, time_series, year, is_last_year,
                 exogenous_stations, autonomous_mode=False,
                 mccormick_degradation=True):
        """
        :param year: año de este bloque (debe pertenecer a time_series.years).
        :param is_last_year: True si `year` es el ultimo año del horizonte
            GLOBAL -- fija alpha=0 (el ultimo año no tiene costo futuro).
        :param exogenous_stations: dict {k: 0/1} con X[k, year] fijo para este
            año. Requerido: en modo descompuesto X siempre es exogeno.
        :param mccormick_degradation: default True, a diferencia del monolitico.
            El backward pass resuelve la relajacion LINEAL de este bloque para
            leer los duales del corte, asi que el bloque tiene que ser MILP
            puro; con los bilineales exactos habria que resolver un MIQCP no
            convexo y los duales no estarian definidos.
        """
        self.mine_system = mine_system
        self.time_series = time_series
        self.year = year
        self.is_last_year = is_last_year

        if exogenous_stations is None:
            raise ValueError(
                "YearBlockBuilder requiere exogenous_stations: en modo "
                "descompuesto X no es estado, es un parametro fijado por fuera."
            )
        # Las reglas de functions.py esperan exogenous_stations indexado por
        # (k, y); years_override=[year] asi que solo necesitan la clave del
        # propio año.
        self._exogenous_stations_for_rules = {
            (k, year): v for k, v in exogenous_stations.items()
        }

        common = dict(years_override=[year],
                      exogenous_stations=self._exogenous_stations_for_rules)

        self.set_builder = OptSets(
            mine_system, time_series, autonomous_mode=autonomous_mode, **common
        )
        self.param_rules = OptParameters(mine_system, time_series, **common)
        self.bound_rules = BoundRules(mine_system, time_series, **common)
        self.constraint_rules = ConstraintRules(
            mine_system, time_series, mccormick_degradation=mccormick_degradation, **common
        )
        self.objective_rules = ObjectiveRules(mine_system, time_series, **common)

        # Registro de los estados que cruzan años: nombre logico, componente
        # Pyomo real, parametro heredado, copia local e index_set. Es la unica
        # interfaz que consumen cuts.py/passes.py/driver.py.
        self.state_links = []
        # Las holguras del modo elastico se crean una sola vez, a pedido.
        self._elastic_ready = False

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
            # f_y + costo-to-go del resto del horizonte. ObjectiveRules.
            # total_cost ya es exactamente f_y porque model.years = {year}.
            return self.objective_rules.total_cost(m) + m.alpha
        model.obj = pyo.Objective(rule=_obj, sense=pyo.minimize)

        model.dual = Suffix(direction=Suffix.IMPORT)

        return model

    def _first_year(self):
        return sorted(self.time_series.years)[0]

    def _add_linear_state(self, model, state_name, state_var_name, delta_var,
                          accum_var, index_set):
        """Acople 'stock = copia_continua_del_año_anterior + incremento', para
        las familias con acumulacion simple año a año (N_bays, N_chargers,
        N_batteries).

        La copia `prev` queda sin cota superior propia: en el forward y en el
        backward la igualdad de enlace siempre la fuerza al valor heredado, asi
        que ninguna cota ataria. (En la rama carga_ob_multiaño esa cota si hace
        falta, pero solo para el corte Strengthened Benders, que aca no se
        porta.)
        """
        y = self.year
        hat_name = f"{state_name}_hat"
        prev_name = f"{state_name}_prev"

        setattr(model, hat_name, pyo.Param(index_set, initialize=0.0, mutable=True))
        setattr(model, prev_name, pyo.Var(index_set, domain=pyo.NonNegativeReals))
        hat = getattr(model, hat_name)
        prev = getattr(model, prev_name)

        setattr(model, f"link_{state_name}",
                pyo.Constraint(index_set, rule=lambda m, *idx: prev[idx] == hat[idx]))
        setattr(model, f"accum_{state_name}",
                pyo.Constraint(index_set,
                               rule=lambda m, *idx: accum_var[idx + (y,)] == prev[idx] + delta_var[idx + (y,)]))

        self.state_links.append({
            "state": state_name, "state_var": state_var_name,
            "hat": hat_name, "prev": prev_name, "index_set": index_set,
            "kind": "simple",
        })

    def _add_global_once_state(self, model, state_name, state_var_name, index_set):
        """Acople para un estado que se decide UNA sola vez para todo el
        horizonte (n_ssee_k, G_g, H): no hay Delta ni acumulacion.

        Bloque del PRIMER año global: no agrega nada -- la variable real, ya
        creada libre por BoundRules, es la unica decision. Solo se registra en
        state_links (sin hat/prev) para que extract_state() reporte su valor y
        el driver lo propague.

        Años siguientes: se fuerza state_var == <estado>_hat directo sobre la
        variable real, sin copia intermedia, porque aca no hay ninguna
        aritmetica de acumulacion que mantener aparte: el heredado ES el valor.
        El dual de esa igualdad sirve igual para el corte.
        """
        state_var = getattr(model, state_var_name)

        if self.year == self._first_year():
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

    def _add_degradation_state(self, model):
        """Acople de la degradacion del pool: D_y (capacidad al final del año).

        En el primer año global no hay nada que enlazar -- BoundRules ya fija
        b_bar[y1]=b_max_pool y R[y1]=0 (condicion de borde, ec. 5) --, pero D si
        se registra en state_links para que extract_state() lo reporte: el año
        siguiente lo necesita como heredado.

        Para los demas años se agrega la copia continua D_prev ligada por
        igualdad al parametro heredado D_hat, y b_y_link reescrita sobre esa
        copia (en el monolitico es b_y_link, que mira model.D[y-1] -- un indice
        que no existe aca; ver el guard is_decomposed_block en
        ConstraintRules.build_all_constraints).
        """
        if self.mine_system.battery_degradation is None:
            return
        y = self.year

        if y == self._first_year():
            self.state_links.append({
                "state": "D", "state_var": "D", "hat": None, "prev": None,
                "index_set": None, "kind": "simple",
            })
            return

        B_U = value(model.B_U)
        model.D_hat = pyo.Param(initialize=0.0, mutable=True)
        model.D_prev = pyo.Var(domain=pyo.NonNegativeReals, bounds=(0, B_U))
        model.link_D = pyo.Constraint(expr=model.D_prev == model.D_hat)
        # Misma ecuacion que ConstraintRules.b_y_link, con D_prev en lugar de
        # D[y-1]: el 0.3 es la fraccion de capacidad nominal que recupera un
        # reemplazo, igual que alla.
        model.b_y_link_local = pyo.Constraint(expr=(
            model.b_bar[y] <= model.D_prev + 0.3 * value(model.b_max_pool) * model.R[y]
        ))

        self.state_links.append({
            "state": "D", "state_var": "D",
            "hat": "D_hat", "prev": "D_prev", "index_set": None,
            "kind": "simple",
        })

    def _add_state_linking(self, model):
        self._add_linear_state(model, "N_bays", "N_bays",
                               model.Delta_N_bays, model.N_bays, model.stations_set)
        self._add_linear_state(model, "N_chargers", "N_chargers",
                               model.Delta_N_chargers, model.N_chargers, model.stations_set)
        self._add_linear_state(model, "N_batteries", "N_batteries",
                               model.Delta_N_batteries, model.N_batteries, model.stations_set)

        # Potencia de subestacion: decidida una sola vez para todo el horizonte
        # (conteo entero de baterias en paralelo, sin indice de año).
        # El estado es el conteo de MODULOS de subestacion (n_ssee_k, entera):
        # la capacidad instalada es P_SSEE_STEP * n_ssee_k kW. Que sea entera es
        # lo que habilita el redondeo de Chvatal-Gomory del corte de
        # factibilidad (ver BendersCutManager._redondeo_entero).
        self._add_global_once_state(model, "n_ssee_k", "n_ssee_k", model.stations_set)

        if len(list(model.gen_set)) > 0:
            self._add_global_once_state(model, "G", "G_g", model.gen_set)

        if len(list(model.storage_set)) > 0:
            self._add_global_once_state(model, "H", "H", None)

        self._add_degradation_state(model)

    def _add_cost_to_go(self, model):
        # Todos los terminos de costo son no negativos, por lo que Phi_{y+1} >= 0
        # siempre: alpha >= 0 es una cota inferior trivial valida que evita que
        # el forward quede no acotado en la primera iteracion, cuando todavia no
        # hay ningun corte.
        model.alpha = pyo.Var(domain=pyo.NonNegativeReals)
        model.cuts = pyo.ConstraintList()
        if self.is_last_year:
            # El ultimo año no tiene futuro. Sin fijar esto las cotas quedan mal
            # sin ningun error visible.
            model.alpha.fix(0.0)

    def set_heritage(self, values):
        """Actualiza los parametros heredados <estado>_hat con el estado optimo
        del año anterior. El driver lo llama antes de cada resolucion.

        :param values: dict {state_name: valor} para estados escalares (H, D) o
            {state_name: {idx: valor}} para los indexados (N_bays, G, ...).
        """
        for link in self.state_links:
            state_name = link["state"]
            if state_name not in values or link["hat"] is None:
                # hat None = estado que este bloque decide (primer año): no hay
                # parametro heredado que actualizar.
                continue
            hat = getattr(self.model, link["hat"])
            new_value = values[state_name]
            if link["index_set"] is None:
                hat.set_value(float(new_value))
            else:
                for idx, v in new_value.items():
                    hat[idx].set_value(float(v))

    def extract_state(self):
        """Extrae x̂_y: el estado optimo de ESTE bloque ya resuelto, para
        alimentar set_heritage() del bloque y+1 en el forward y como punto ancla
        del corte. Mismo formato que espera set_heritage.

        Los estados "global_once" no tienen indice de año en el componente real
        (se deciden una sola vez), asi que se leen directo.
        """
        result = {}
        for link in self.state_links:
            state_var = getattr(self.model, link["state_var"])
            is_global_once = link.get("kind") == "global_once"
            if link["index_set"] is None:
                result[link["state"]] = (value(state_var) if is_global_once
                                         else value(state_var[self.year]))
            else:
                result[link["state"]] = {
                    idx: (value(state_var[idx]) if is_global_once
                          else value(state_var[idx, self.year]))
                    for idx in link["index_set"]
                }
        return result

    def _ensure_elastic_components(self):
        """Crea UNA sola vez las holguras y las igualdades elasticas del corte
        de factibilidad. Nacen inertes -- holguras fijadas en 0 y restricciones
        desactivadas --, asi que mientras no se entre en modo elastico el bloque
        resuelve exactamente el mismo MILP de siempre.
        """
        if self._elastic_ready:
            return
        model = self.model
        slack_terms = []
        for link in self.state_links:
            if link["hat"] is None:
                continue
            name = link["state"]
            hat = getattr(model, link["hat"])
            # Para "simple" el lado izquierdo es la copia local; para
            # "global_once" es la variable real, que alli se fija directo.
            lhs = getattr(model, link["prev"] if link["prev"] else link["state_var"])

            slack_name = "feas_slack_" + name
            if link["index_set"] is None:
                setattr(model, slack_name, pyo.Var(domain=pyo.NonNegativeReals,
                                                   initialize=0.0))
                s = getattr(model, slack_name)
                setattr(model, "feas_link_" + name,
                        pyo.Constraint(expr=lhs - s == hat))
                slack_terms.append(s)
            else:
                index_set = link["index_set"]
                setattr(model, slack_name, pyo.Var(index_set,
                                                   domain=pyo.NonNegativeReals,
                                                   initialize=0.0))
                s = getattr(model, slack_name)
                setattr(model, "feas_link_" + name,
                        pyo.Constraint(index_set,
                                       rule=lambda m, *idx: lhs[idx] - s[idx] == hat[idx]))
                slack_terms.extend(s[idx] for idx in index_set)
            s.fix(0.0)
            getattr(model, "feas_link_" + name).deactivate()

        if slack_terms:
            model.feas_obj = pyo.Objective(expr=sum(slack_terms), sense=pyo.minimize)
            model.feas_obj.deactivate()
        self._elastic_ready = True

    @contextlib.contextmanager
    def relaxed_mode(self):
        """Relaja la integralidad EN SITU y la restaura al salir.

        No se clona. clone() de Pyomo es un deepcopy del grafo de objetos y en
        un bloque de este tamanio (~160k variables) tarda dos ordenes de
        magnitud MAS que construirlo entero desde cero: medido, mas de 13
        minutos contra 8 segundos. Como el backward relaja una vez por anio y
        por iteracion, clonar hacia inviable el esquema completo.

        Ojo: al resolver se sobreescriben los valores de las variables del
        bloque con los del LP. Es seguro porque el forward ya extrajo su
        solucion a diccionarios antes de que corra el backward, y el forward de
        la iteracion siguiente vuelve a resolver el MILP.
        """
        relax = TransformationFactory("core.relax_integer_vars")
        relax.apply_to(self.model)
        try:
            yield self.model
        finally:
            relax.apply_to(self.model, undo=True)

    @contextlib.contextmanager
    def elastic_mode(self):
        """Pone el bloque en modo elastico EN SITU, para generar un corte de
        FACTIBILIDAD cuando el anio resulta infactible con el estado recibido.

        Mide cuanto estado le habria faltado: cada igualdad de fijacion z = x_hat
        se reemplaza por z - s = x_hat con s >= 0, y el objetivo pasa a ser
        sum(s). El resto del modelo queda intacto.

            v(x_hat) = min { sum(s) : z - s = x_hat, (x,u,z) en F }

        v = 0 significa que el anio si es factible con lo que recibio; v > 0 es
        exactamente la cantidad de estado que le falto, y su dual dice en que
        familias. La holgura va SOLO en las igualdades de fijacion: el resto de
        las restricciones son del propio anio y relajarlas no diria nada sobre
        el estado heredado.

        Se relaja la integralidad porque el corte necesita duales. Ojo con la
        consecuencia: si el anio es infactible por integralidad y no por falta
        de estado, este LP da v = 0 y el corte queda vacio -- el llamador tiene
        que detectarlo (ver ForwardPass) en vez de girar en falso.

        Las holguras de las familias con acumulacion (N_bays/N_chargers/
        N_batteries) nunca van a atar, porque el anio siempre puede comprar mas
        con su propio Delta; las que importan son las de las decisiones unicas
        del horizonte (n_ssee_k, G, H), que el anio no puede ampliar, y la de la
        degradacion.
        """
        self._ensure_elastic_components()
        model = self.model
        if not self._elastic_links():
            raise RuntimeError(
                "El bloque del anio " + str(self.year) + " no hereda ningun "
                "estado, asi que no hay corte de factibilidad que generar."
            )

        relax = TransformationFactory("core.relax_integer_vars")
        relax.apply_to(model)
        conmutados = []
        try:
            for link in self._elastic_links():
                name = link["state"]
                getattr(model, "link_" + name).deactivate()
                getattr(model, "feas_link_" + name).activate()
                getattr(model, "feas_slack_" + name).unfix()
                conmutados.append(name)
            model.obj.deactivate()
            model.feas_obj.activate()
            yield model
        finally:
            # Restaurar SIEMPRE: si el bloque quedara en modo elastico, los
            # forwards siguientes resolverian un problema distinto sin avisar.
            model.feas_obj.deactivate()
            model.obj.activate()
            for name in conmutados:
                getattr(model, "feas_link_" + name).deactivate()
                getattr(model, "link_" + name).activate()
                getattr(model, "feas_slack_" + name).fix(0.0)
            relax.apply_to(model, undo=True)

    def _elastic_links(self):
        return [link for link in self.state_links if link["hat"] is not None]


    def extract_full_solution(self):
        """Vuelca TODAS las variables del bloque ya resuelto (no solo el
        estado), para poder reconstruir un reporte completo: Printer necesita
        las variables operativas, no solo los stocks. Formato
        {nombre_variable: {indice: valor}}, o {nombre: valor} si es escalar.

        Los nombres son los del modelo monolitico; las variables internas del
        bloque (alpha, *_hat, *_prev) no tienen equivalente alla y el llamador
        las descarta.
        """
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
