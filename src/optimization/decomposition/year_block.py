import contextlib
import math

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
                 mccormick_degradation=True, free_charging=False,
                 free_maintenance=False, days_override=None):
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
        # days_override: sub-bloque de un solo dia representativo, con el MISMO
        # acople de estado que el bloque anual (N = prev + Delta, prev == hat,
        # globales fijadas al heredado). Lo usa decomposition/day_blocks.py para
        # el MIP start por dias. None = los cuatro dias, el bloque de siempre.
        self.days_override = list(days_override) if days_override is not None else None
        if self.days_override is not None:
            common["days_override"] = self.days_override
        # Se guardan para poder construir sub-bloques diarios "hermanos" de este
        # bloque con exactamente la misma configuracion (ver day_blocks.py).
        self.exogenous_stations = dict(exogenous_stations)
        self.autonomous_mode = autonomous_mode
        self.mccormick_degradation = mccormick_degradation
        self.free_charging = free_charging
        self.free_maintenance = free_maintenance
        # Regimenes de swap/mantenimiento: tienen que valer IGUAL en todos los
        # bloques y en el monolitico de reporte, si no la descomposicion estaria
        # resolviendo un problema distinto del que reporta.
        regimen = dict(free_charging=free_charging,
                       free_maintenance=free_maintenance)

        self.set_builder = OptSets(
            mine_system, time_series, autonomous_mode=autonomous_mode,
            **regimen, **common
        )
        self.param_rules = OptParameters(mine_system, time_series, **common)
        self.bound_rules = BoundRules(mine_system, time_series, **common)
        self.constraint_rules = ConstraintRules(
            mine_system, time_series, mccormick_degradation=mccormick_degradation,
            **regimen, **common
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

    # ------------------------------------------------------------------ #
    # Region de confianza (Box-step) para el forward estabilizado
    # ------------------------------------------------------------------ #
    # Estabilizacion a la Göke, Schmidt & Kendziorski (EJOR 316, 2024, sec.
    # 3.2.3): el forward busca la inversion de cada año dentro de una caja
    # alrededor del CENTRO de estabilidad (la mejor trayectoria conocida), para
    # que la trayectoria no salte de un extremo a otro entre iteraciones.
    # Box-step (norma l-infinito) y no la region cuadratica del paper: aca son
    # solo COTAS de variables -- el MILP no gana filas ni se vuelve MIQCP, y
    # ademas se achica.
    #
    # Se encajan las DECISIONES de inversion que este bloque toma: los stocks
    # N_bays/N_chargers/N_batteries del año, y n_ssee_k/G/H solo en el primer
    # año global (despues los fija la igualdad de enlace). D no: es resultado de
    # la degradacion, no una decision.
    #
    # La caja NUNCA debe llegar al backward ni a un corte de factibilidad: los
    # cortes tienen que valer para cualquier estado. Por eso passes.py la quita
    # apenas termina el solve del forward (clear_trust_region).

    def _trust_region_vardata(self):
        """[(state_name, idx, VarData)] de las decisiones que se encajan."""
        out = []
        for link in self.state_links:
            if link["state"] == "D":
                continue
            es_global = link.get("kind") == "global_once"
            if es_global and link["hat"] is not None:
                continue   # años siguientes: la fija link_<estado>, no se decide aca
            var = getattr(self.model, link["state_var"])
            if link["index_set"] is None:
                out.append((link["state"], None, var if es_global else var[self.year]))
            else:
                for idx in link["index_set"]:
                    out.append((link["state"], idx,
                                var[idx] if es_global else var[idx, self.year]))
        return out

    def set_trust_region(self, center, delta_int=1, frac_cont=0.25, min_cont=1.0):
        """Aprieta las cotas de las decisiones de inversion a una caja alrededor
        de `center` (mismo formato que extract_state()):

            enteras:   [c - delta_int, c + delta_int]
            continuas: [c - w, c + w],  w = max(frac_cont*|c|, min_cont)

        intersectada con las cotas originales. Para los stocks, la caja se
        estira hasta el valor heredado si hace falta: el stock no puede bajar
        del año anterior (Delta >= 0), y una caja que no lo contuviera volveria
        infactible el año por culpa de la estabilizacion y no del problema."""
        self.clear_trust_region()
        self._trust_region = dict(center=center, delta_int=delta_int,
                                  frac_cont=frac_cont, min_cont=min_cont)
        self._tr_saved = []
        for state, idx, vd in self._trust_region_vardata():
            if vd.fixed or state not in center:
                continue
            c = center[state] if idx is None else center[state].get(idx)
            if c is None:
                continue
            c = float(c)
            entera = not vd.is_continuous()
            w = delta_int if entera else max(frac_cont * abs(c), min_cont)
            lb0, ub0 = vd.lb, vd.ub
            lo, hi = c - w, c + w
            link = next(l for l in self.state_links if l["state"] == state)
            if link.get("kind") == "simple" and link["hat"] is not None:
                hat = getattr(self.model, link["hat"])
                heredado = value(hat if idx is None else hat[idx])
                hi = max(hi, heredado)
            if lb0 is not None:
                lo = max(lo, lb0)
            if ub0 is not None:
                hi = min(hi, ub0)
            if entera:
                lo, hi = math.ceil(lo - 1e-6), math.floor(hi + 1e-6)
            if lo > hi:
                continue   # sin interseccion con las cotas originales: no se encaja
            self._tr_saved.append((vd, lb0, ub0, lo, hi))
            vd.setlb(lo)
            vd.setub(hi)

    def trust_region_binding(self, tol=1e-6):
        """True si la solucion actual toca algun borde de la caja que sea mas
        apretado que la cota original: la caja esta limitando la decision."""
        for vd, lb0, ub0, lo, hi in getattr(self, "_tr_saved", []):
            x = value(vd, exception=False)
            if x is None:
                continue
            if (lb0 is None or lo > lb0 + tol) and x <= lo + tol:
                return True
            if (ub0 is None or hi < ub0 - tol) and x >= hi - tol:
                return True
        return False

    def clear_trust_region(self):
        for vd, lb0, ub0, _lo, _hi in getattr(self, "_tr_saved", []):
            vd.setlb(lb0)
            vd.setub(ub0)
        self._tr_saved = []
        self._trust_region = None

    # ------------------------------------------------------------------
    # Forward con level-set (alternativa a la caja)
    # ------------------------------------------------------------------
    # Pecci & Jenkins (IEEE TPWRS 2025): el proximo punto no es el optimo del
    # modelo aproximado sino un punto "central" de su conjunto de nivel
    #     { costo aproximado <= L + alpha (U - L) }.
    # Con enteras el punto interior pierde sentido (el mismo paper, etapa 2,
    # fija las enteras y centra solo las continuas), asi que aca:
    #   1. L: el bloque con la OPERACION RELAJADA (inversion entera, cortes
    #      alpha incluidos) -- barato, pocas enteras;
    #   2. enteras de inversion: las mas cercanas al centro en norma l1 dentro
    #      del nivel (local branching, Baena, Castro & Frangioni, Mgmt Sci 2020);
    #   3. continuas de estado (G_g, H, solo en el primer año): punto interior
    #      del nivel con esas enteras fijas (barrier sin crossover).
    # El forward fija despues esa inversion y resuelve el MILP real del año.

    def level_point(self, center, alpha, gap_rel, solver_options=None, verbose=True):
        """Inversion regularizada del año (formato de extract_state), o None
        si no se pudo calcular (el forward sigue sin regularizar)."""
        import pyomo.environ as pyo
        from pyomo.environ import SolverFactory
        from pyomo.opt import TerminationCondition
        from src.optimization.opt_model import VARS_OPERACIONALES

        m = self.model
        vardata = [(s, i, vd) for s, i, vd in self._trust_region_vardata() if not vd.fixed]
        if not vardata:
            return None
        guard_dom = []
        for nombre in VARS_OPERACIONALES:
            comp = getattr(m, nombre, None)
            if comp is None:
                continue
            for vd in comp.values():
                if not vd.is_continuous() and not vd.fixed:
                    guard_dom.append((vd, vd.domain, vd.lb, vd.ub))
                    lb, ub = vd.bounds
                    vd.domain = pyo.Reals
                    vd.setlb(lb)
                    vd.setub(ub)
        opt = SolverFactory("gurobi", solver_io="python")
        opt.options.update({"OutputFlag": 0, "MIPGap": 1e-4, "TimeLimit": 300})
        for k, v in (solver_options or {}).items():
            if k in ("Threads",):
                opt.options[k] = v
        enteras = [(s, i, vd) for s, i, vd in vardata if not vd.is_continuous()]
        continuas = [(s, i, vd) for s, i, vd in vardata if vd.is_continuous()]
        fijadas = []
        punto = None
        try:
            # 1) L con la operacion relajada
            r = opt.solve(m, load_solutions=False)
            if r.solver.termination_condition not in (TerminationCondition.optimal,
                                                      TerminationCondition.maxTimeLimit):
                return None
            m.solutions.load_from(r)
            L = value(m.obj)
            nivel = L + alpha * max(gap_rel, 0.0) * abs(L)
            # 2) enteras: l1 al centro dentro del nivel
            m._lvl_nivel = pyo.Constraint(expr=m.obj.expr <= nivel)
            m._lvl_d = pyo.Var(range(len(enteras)), domain=pyo.NonNegativeReals)
            m._lvl_dev = pyo.ConstraintList()
            terminos = 0
            for k, (s, i, vd) in enumerate(enteras):
                c = center.get(s)
                c = c if i is None or c is None else c.get(i)
                if c is None:
                    continue
                m._lvl_dev.add(m._lvl_d[k] >= vd - float(c))
                m._lvl_dev.add(m._lvl_d[k] >= float(c) - vd)
                terminos += 1
            m.obj.deactivate()
            m._lvl_obj = pyo.Objective(expr=sum(m._lvl_d[k] for k in range(len(enteras)))
                                       if terminos else 0.0)
            r = opt.solve(m, load_solutions=False)
            if r.solver.termination_condition not in (TerminationCondition.optimal,
                                                      TerminationCondition.maxTimeLimit):
                return None
            m.solutions.load_from(r)
            dist = value(m._lvl_obj)
            # 3) continuas: punto interior del nivel con TODAS las enteras fijas
            #    (las de estado y las que dependen de ellas: Delta_*, flota,
            #    reemplazo): asi es un LP y el barrier entrega un punto interior.
            for vd in m.component_data_objects(pyo.Var, active=True):
                if not vd.is_continuous() and not vd.fixed and vd.value is not None:
                    vd.fix(round(value(vd)))
                    fijadas.append(vd)
            if continuas:
                m._lvl_obj.deactivate()
                m._lvl_cero = pyo.Objective(expr=0.0)
                opt_b = SolverFactory("gurobi", solver_io="python")
                opt_b.options.update({"OutputFlag": 0, "Method": 2, "Crossover": 0})
                r = opt_b.solve(m, load_solutions=False)
                if r.solver.termination_condition == TerminationCondition.optimal:
                    m.solutions.load_from(r)
            punto = {}
            for s, i, vd in vardata:
                v = value(vd)
                if i is None:
                    punto[s] = v
                else:
                    punto.setdefault(s, {})[i] = v
            if verbose:
                cont = ", ".join(f"{s}{'' if i is None else f'[{i}]'}={value(vd):.3f}"
                                 for s, i, vd in continuas)
                print(f"[Level] año {self.year}: L(op. relajada)={L:,.2f}  nivel={nivel:,.2f} "
                      f"(alpha {alpha}, gap {gap_rel:.2%})  distancia l1 al centro={dist:.0f}"
                      + (f"  continuas interiores: {cont}" if cont else ""))
            return punto
        finally:
            for vd in fijadas:
                vd.unfix()
            for nombre in ("_lvl_nivel", "_lvl_dev", "_lvl_d", "_lvl_obj", "_lvl_cero"):
                if hasattr(m, nombre):
                    m.del_component(nombre)
            m.obj.activate()
            for vd, dom, lb, ub in guard_dom:
                vd.domain = dom
                vd.setlb(lb)
                vd.setub(ub)

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

        No se clona. clone() de Pyomo es un deepcopy del grafo de objetos, y el
        backward relaja una vez por anio y por iteracion, asi que ese costo se
        paga N_anios * N_iteraciones veces.

        Medicion honesta del ahorro: en un A/B sobre el mismo bloque (65k
        variables) el tramo relajado paso de 7.4 s clonando a 4.9 s en sitio --
        1.5x, con el clone costando 2.6 s. Sobre 11 anios y 20 iteraciones eso
        son ~10 minutos de puro clonado, mas la memoria que deja de duplicarse.

        OJO con una medicion anterior que circulo como justificacion: se observo
        un clone de MAS de 13 minutos sobre un bloque de 160k variables, y de ahi
        salio un supuesto factor 100x. Esa medicion se tomo con el proceso en
        2.8 GB y ~3 GB libres, o sea paginando: el numero era real pero no
        representativo. El ahorro tipico es 1.5x; los 13 minutos son lo que pasa
        cuando ademas falta memoria -- que es justamente el escenario que esto
        evita.

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
    def capacity_presolve_mode(self, station=None):
        """Convierte el bloque EN SITU en el problema auxiliar del presolve de
        capacidad (ver NestedBendersSolver._capacity_presolve) y lo restaura
        al salir:

            n*_y(k) = min { n_ssee_k[k] : (x, u) factible para el anio y,
                            con TODO el estado heredado libre }

        Se desacoplan las igualdades de fijacion link_<estado> (N_bays,
        N_chargers, N_batteries, n_ssee_k, G, H, D): el anio elige libremente
        con que llega, dentro de las cotas fisicas que ya tienen las copias.
        Eso hace del auxiliar una RELAJACION del anio en contexto -- en el
        horizonte completo su heredado esta ademas restringido por los anios
        previos --, asi que su minimo es cota inferior valida de la capacidad
        que el anio necesita de verdad, y como n_ssee_k se decide una sola vez
        para todo el horizonte, tambien de la que el anio 1 tiene que comprar.

        La minimizacion es POR NAVE: la potencia de cada nave acota solo a sus
        propios cargadores, y un minimo de la suma no diria cuanto necesita
        cada una (el anio 1 pondria los modulos en la nave mas barata y el
        corte de factibilidad volveria a saltar). Ojo que las naves SI se
        acoplan por la meta diaria total de produccion: al minimizar una con
        las otras libres, la nave produce su piso y las otras absorben el
        resto, asi que la cota es valida pero puede quedar floja (medido en
        carga on board: (1,1,1) contra (2,1,1) que termina comprando el
        forward).

        Los cortes de model.cuts se dejan activos: son desigualdades validas
        del problema completo (alpha queda libre, asi que los de optimalidad
        no atan), y al momento del presolve la lista esta vacia de todos modos.
        No se relaja la integralidad -- el auxiliar es un MILP y lo que se usa
        es su cota dual, valida aunque se corte por tiempo.

        station=None minimiza la SUMA sum_k n_ssee_k[k]: es el complemento de
        los minimos por nave, porque capta lo que ellos no pueden ver por el
        acople de la meta diaria -- "una u otra nave necesita un modulo mas"
        da minimo 0 en cada una por separado y +1 en la suma (medido en swap,
        160kW_2dias: el unico corte de factibilidad era exactamente
        n_2 + n_3 >= 1).
        """
        model = self.model
        if station is not None and station not in model.stations_set:
            raise ValueError(f"nave {station!r} no pertenece al bloque del anio {self.year}")
        desacoplados = []
        for link in self.state_links:
            if link["hat"] is None:
                continue
            con = getattr(model, "link_" + link["state"])
            if con.active:
                con.deactivate()
                desacoplados.append(con)
        model.obj.deactivate()
        objetivo = (model.n_ssee_k[station] if station is not None
                    else sum(model.n_ssee_k[k] for k in model.stations_set))
        model.presolve_obj = pyo.Objective(expr=objetivo, sense=pyo.minimize)
        try:
            yield model
        finally:
            # Restaurar SIEMPRE, o el forward resolveria el anio con el estado
            # desacoplado y sin su objetivo.
            model.del_component("presolve_obj")
            model.obj.activate()
            for con in desacoplados:
                con.activate()

    @contextlib.contextmanager
    def lagrangean_mode(self, mu):
        """Convierte el bloque EN SITU en su RELAJACION LAGRANGEANA respecto de
        las igualdades de fijacion del estado heredado, y lo restaura al salir.
        Es lo que hace falta para el Strengthened Benders cut (ec. 63 de Lara
        et al. 2018, EJOR 271:1037-1054).

            Phi^LR(mu) = min { f_y + alpha + mu^T (z - x_hat)
                               : (x, u, z) en X_y }

        Dos diferencias con relaxed_mode, y las dos son el punto:

          - la INTEGRALIDAD SE MANTIENE. Por eso la cota resultante vale para
            el casco entero y no para su relajacion lineal. Es lo que rompe el
            techo del backward: con cortes de LP, alpha_1 converge exactamente
            a la relajacion lineal del monolitico (medido en DET 4 dias:
            1.807.131, contra 2.212.229 que Gurobi prueba con sus cortes).

          - la igualdad z = x_hat se DESACOPLA y su violacion se paga en el
            objetivo con el multiplicador mu, en vez de imponerse.

        SIGNO. Se usa la convencion de BendersCutManager.read_duals, donde
        mu = -pi y el lagrangiano es L = f + mu^T (z - x_hat), de modo que
        Phi(x_hat) = g(mu) - mu^T x_hat con g(mu) = min {f + mu^T z}. El
        termino -mu^T x_hat NO es opcional: es lo que deja Phi^LR en la misma
        base que Phi^LP y hace que el corte quede anclado donde corresponde.
        Como x_hat vive en los Param mutables <estado>_hat, alcanza con escribir
        (z - hat) y el ancla viaja sola.

        Para cada familia, z es la copia heredada <estado>_prev cuando existe, y
        la variable real cuando el estado se decide una sola vez para todo el
        horizonte (global_once: n_ssee_k, G_g, H), que es contra quien esta
        escrita la igualdad en ese caso.

        Los cortes ya acumulados en model.cuts se dejan activos: acotan alpha y
        son parte de phi_{t,k} en la ec. (60) del paper.
        """
        model = self.model
        desacoplados = []
        termino_dual = 0.0

        for link in self.state_links:
            if link["hat"] is None:
                continue
            con = getattr(model, "link_" + link["state"])
            if con.active:
                con.deactivate()
                desacoplados.append(con)

            mu_fam = mu.get(link["state"])
            if mu_fam is None:
                continue
            # z: la copia heredada si la hay; si no, la variable real.
            z_comp = getattr(model, link["prev"]) if link["prev"] else \
                getattr(model, link["state_var"])
            hat = getattr(model, link["hat"])
            if link["index_set"] is None:
                termino_dual += mu_fam * (z_comp - hat)
            else:
                for idx, mu_v in mu_fam.items():
                    termino_dual += mu_v * (z_comp[idx] - hat[idx])

        model.obj.deactivate()
        model.lagrangean_obj = pyo.Objective(expr=model.obj.expr + termino_dual,
                                             sense=pyo.minimize)
        try:
            yield model
        finally:
            # Restaurar SIEMPRE: si no, el forward resolveria el anio con el
            # estado desacoplado y con un objetivo que no es el suyo.
            model.del_component("lagrangean_obj")
            model.obj.activate()
            for con in desacoplados:
                con.activate()

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
