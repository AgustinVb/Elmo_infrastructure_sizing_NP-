import math

def _hint_value(hint, state_name, idx, fallback):
    """Valor conocido de un estado que este bloque no controla. Si no hay
    ninguno, devuelve `fallback` (el propio punto ancla del corte), con lo que
    ese termino del corte se anula en vez de introducir un valor inventado."""
    fam = hint.get(state_name)
    if fam is None:
        return fallback
    if idx is None:
        return fam
    if isinstance(fam, dict):
        return fam.get(idx, fallback)
    return fallback


class BendersCutManager(object):
    """Arma y agrega cortes de Benders entre bloques anuales consecutivos
    (implementacion_descomposicion_carga_ob.md, sec. 6.2):

        alpha_{y-1} >= Phi^LP_y + sum_familia mu_familia * (x_hat_{y-1} - x_{y-1})

    Mantiene un historial (iteracion, año) -> corte para debug/
    reproducibilidad (documento sec. 9.2, punto 7: "los cortes de
    iteraciones previas referencian el estado de aquella iteración")."""

    def __init__(self):
        self.history = []

    def read_duals(self, relaxed_model, year, state_links, prefix="link_"):
        """Lee mu para cada familia de estado de un modelo YA resuelto como
        LP relajado, con Suffix de duales importado. state_links: la lista
        `.state_links` de un YearBlockBuilder -- puede venir del modelo
        original o de un clon (mismos nombres de componente en ambos).

        Con la formulacion McCormick (degradacion_descomposicion_mccormick.
        md, Camino A) el estado de degradacion "D" entra en el acople
        exactamente igual que N_chargers/G/H (kind "simple": el heredado
        es una constante aditiva en <link_estado>, dual = sensibilidad
        directa de LP respecto del RHS) -- ya no requiere el teorema de la
        envolvente que necesitaba el esquema viejo AN_ciclos (heredado
        como coeficiente).

        SIGNO: se devuelve -pi, NO el dual crudo pi del solver. El corte de
        add_cut (y la formula del documento) usa mu en la convencion del
        lagrangiano L = f + mu^T (z - x_hat), con la que

            Phi(x_hat) = g(mu*) - mu*^T x_hat,  g(mu) = min {f + mu^T z}

        y, evaluando esa misma cota en cualquier otro x,

            Phi(x) >= g(mu*) - mu*^T x = Phi(x_hat) + mu*^T (x_hat - x)

        es decir mu = -dPhi/dx_hat. El dual que reporta el solver para una
        igualdad escrita con el heredado en el lado derecho (<estado>_prev
        == <estado>_hat, con _hat un Param) es la sensibilidad del optimo
        respecto de ese lado derecho, pi = +dPhi/dx_hat, o sea mu = -pi.
        Usar pi sin invertir el signo da un corte con la pendiente opuesta:
        penaliza invertir en vez de premiarlo, sobreestima el costo futuro
        fuera del punto ancla y produce cotas inferiores mayores que el
        optimo real (sintoma: LB > UB).

        Verificado numericamente sobre este mismo codigo con una instancia
        de dos anios y una estacion (anio 1: N>=3 a costo 10; anio 2: N>=7 a
        costo 25 sobre el incremento; optimo 70 con N1=7): con el dual crudo
        el corte evaluado en N1=7 da 200 cuando el costo futuro real es 0, y
        LB converge a 130 > 70; con -pi el corte es exacto y LB = 70.

        La convencion aqui elegida es tambien la que ya esperan los caminos
        Lagrangeanos de passes.py, que penalizan con +mu*prev en el objetivo
        (ver _build_lagrangian_relaxation): reciben este mu como mu_init y
        devuelven mu_best en la misma convencion."""
        mu = {}
        for link in state_links:
            state_name = link["state"]
            # `prefix` permite leer los duales del clon elastico que genera los
            # cortes de factibilidad, donde las igualdades se llaman
            # feas_link_<estado> (ver YearBlockBuilder.elastic_mode). El default
            # None salta los estados que ESTE bloque decide (primer anio): alli
            # no hay igualdad de fijacion y por lo tanto no hay multiplicador.
            link_con = getattr(relaxed_model, f"{prefix}{state_name}", None)
            if link_con is None:
                continue
            if link["index_set"] is None:
                mu[state_name] = -relaxed_model.dual[link_con]
            else:
                mu[state_name] = {
                    idx: -relaxed_model.dual[link_con[idx]] for idx in link["index_set"]
                }
        return mu

    def add_cut(self, parent_block, phi_lp, mu, x_hat_base, iteration=None):
        """Agrega a parent_block.model.cuts el corte que acota alpha_parent
        con el costo (relajado) del año siguiente, evaluado en la
        sensibilidad mu respecto del estado heredado. `x_hat_base` es el
        estado óptimo propio de `parent_block` en ESTA iteración del forward
        pass (documento sec. 6.2, x̂_{y,k})."""
        model = parent_block.model
        y = parent_block.year
        expr = phi_lp

        for link in parent_block.state_links:
            state_name = link["state"]
            if state_name not in mu:
                continue
            state_var = getattr(model, link["state_var"])
            mu_fam = mu[state_name]
            # "global_once" (G, H): la variable real no tiene indice de año
            # (decidida una sola vez para todo el horizonte, ver
            # year_block.py::_add_global_once_state) -- se referencia
            # directo, sin agregar el año del bloque padre al indice.
            is_global_once = link.get("kind") == "global_once"
            if link["index_set"] is None:
                state_val = state_var if is_global_once else state_var[y]
                expr += mu_fam * (x_hat_base[state_name] - state_val)
            else:
                for idx, mu_v in mu_fam.items():
                    state_val = state_var[idx] if is_global_once else state_var[idx, y]
                    expr += mu_v * (x_hat_base[state_name][idx] - state_val)

        model.cuts.add(model.alpha >= expr)

        self.history.append({
            "iteration": iteration,
            "parent_year": y,
            "phi_lp": phi_lp,
            "mu": mu,
            "x_hat_base": x_hat_base,
        })

    # Tolerancia para decidir si un multiplicador es no nulo y si un valor es
    # entero. Los duales de un objetivo elastico l1 valen +-1 exactos.
    _TOL = 1e-6

    def add_feasibility_cut(self, parent_block, v_hat, mu, x_hat_base, iteration=None):
        """Agrega al anio padre un corte de FACTIBILIDAD: prohibe los estados
        desde los que el anio siguiente no tiene continuacion.

        Sea v(x_hat) el minimo de violacion del anio hijo cuando recibe x_hat,
        medido como la cantidad de estado que le habria faltado (holgura en las
        igualdades de fijacion, ver YearBlockBuilder.elastic_mode). Como x_hat
        entra solo en el lado derecho de esas igualdades, vale el mismo
        argumento que para el corte de optimalidad:

            v(x) >= v(x_hat) + mu^T (x_hat - x)     para todo x

        con mu = -dv/dx_hat, la misma convencion de signo que read_duals. El
        anio hijo es factible en x si y solo si v(x) = 0, y como v >= 0 siempre,
        imponer v(x) <= 0 da

            v_hat + mu^T (x_hat - x) <= 0

        No lleva alpha: no acota el costo futuro, acota el estado. Vive en la
        misma ConstraintList que los de optimalidad.

        FORTALECIMIENTO ENTERO. Tal cual, ese corte puede quedar casi vacio,
        porque el objetivo elastico l1 mide el deficit en la RELAJACION del
        hijo: si el estado que falta es entero, el LP se conforma con una
        fraccion. Si todos los terminos con multiplicador no nulo son variables
        ENTERAS, con el mismo coeficiente y ancla entera, el lado izquierdo es
        entero y el derecho se puede redondear hacia arriba (Chvatal-Gomory):

            sum_j (x_j - x_hat_j) >= ceil(v_hat / c)

        Medido en la rama battery_swapping_multianio, eso convirtio un corte
        que pedia 0.028 unidades de subestacion en uno que exige 1 -- la
        condicion verdadera, 35 veces mayor. El corte va al bloque del padre,
        cuya relajacion lineal produce la cota inferior: sigue siendo valido
        alli porque no elimina ninguna solucion entera factible del problema.
        """
        model = parent_block.model
        y = parent_block.year

        terminos = []
        for link in parent_block.state_links:
            state_name = link["state"]
            if state_name not in mu:
                continue
            state_var = getattr(model, link["state_var"])
            mu_fam = mu[state_name]
            is_global_once = link.get("kind") == "global_once"
            if link["index_set"] is None:
                if abs(mu_fam) > self._TOL:
                    var = state_var if is_global_once else state_var[y]
                    terminos.append((mu_fam, var, x_hat_base[state_name]))
            else:
                for idx, mu_v in mu_fam.items():
                    if abs(mu_v) <= self._TOL:
                        continue
                    var = state_var[idx] if is_global_once else state_var[idx, y]
                    terminos.append((mu_v, var, x_hat_base[state_name][idx]))

        if not terminos:
            raise RuntimeError(
                "El corte de factibilidad hacia el anio " + str(y) + " quedaria "
                "sin ningun termino: todos los multiplicadores son nulos."
            )

        redondeo = self._redondeo_entero(terminos, v_hat)
        if redondeo is None:
            expr = v_hat
            for mu_v, var, x_hat_j in terminos:
                expr += mu_v * (x_hat_j - var)
            model.cuts.add(expr <= 0)
            kind, rhs = "feasibility", None
        else:
            rhs = redondeo
            model.cuts.add(sum(var - round(x_hat_j) for _, var, x_hat_j in terminos) >= rhs)
            kind = "feasibility-entero"

        self.history.append({
            "iteration": iteration,
            "parent_year": y,
            "kind": kind,
            "v_hat": v_hat,
            "rhs_redondeado": rhs,
            "n_terminos": len(terminos),
            "mu": mu,
            "x_hat_base": x_hat_base,
        })

    def _redondeo_entero(self, terminos, v_hat):
        """ceil(v_hat / c) si el corte admite el fortalecimiento entero, o None.
        Hace falta que TODOS los terminos compartan el mismo coeficiente
        positivo c, que sus variables sean enteras y que el ancla tambien lo
        sea: solo asi el lado izquierdo esta garantizado entero."""
        coef = terminos[0][0]
        if coef <= self._TOL:
            return None
        if any(abs(mu_v - coef) > self._TOL for mu_v, _, _ in terminos):
            return None
        for _, var, x_hat_j in terminos:
            if not var.is_integer():
                return None
            if abs(x_hat_j - round(x_hat_j)) > self._TOL:
                return None
        return math.ceil(v_hat / coef - self._TOL)


    def add_year_cuts_to_macroblock(self, mb_block, state_hint=None):
        """Replica los cortes acumulados del año en el bloque de UN
        macrobloque (fase forward con descomposicion por macrobloque).

        El corte del año esta escrito sobre el estado COMPLETO del año
        (N_chargers de todas las naves, P_max_k, G, H, D), pero un macrobloque
        solo decide su propia parte. Los terminos de los estados que este
        bloque no controla se evaluan como constantes: con el valor conocido
        del año si `state_hint` lo trae (el estado de la iteracion anterior),
        y si no con el propio punto ancla del corte, con lo que ese termino se
        anula. Asi el corte conserva la pendiente respecto de lo que el
        macrobloque SI decide, que es lo que necesita el forward para no ser
        miope.

        El resultado es una guia, no una cota valida: el reparto de recursos
        ya hace del bloque un problema restringido. La validez de las cotas
        del algoritmo no depende de esto -- UB resta alpha (ver ForwardPass) y
        LB sale del backward, que siempre resuelve el año completo sin
        repartir.
        """
        model = mb_block.model
        y = mb_block.year
        station = mb_block.macroblock["station"]
        hint = state_hint or {}
        n_added = 0

        for cut in self.history:
            if cut["parent_year"] != y:
                continue
            expr = cut["phi_lp"]
            for state_name, mu_fam in cut["mu"].items():
                x_hat = cut["x_hat_base"][state_name]
                link = next(
                    (l for l in mb_block.state_links if l["state"] == state_name), None
                )
                if isinstance(mu_fam, dict):
                    for idx, mu_v in mu_fam.items():
                        own = link is not None and (idx == station or link.get("kind") == "global_once")
                        if own:
                            state_var = getattr(model, link["state_var"])
                            value_or_var = (state_var[idx] if link.get("kind") == "global_once"
                                            else state_var[idx, y])
                        else:
                            value_or_var = _hint_value(hint, state_name, idx, x_hat[idx])
                        expr += mu_v * (x_hat[idx] - value_or_var)
                else:
                    if link is not None:
                        state_var = getattr(model, link["state_var"])
                        value_or_var = (state_var if link.get("kind") == "global_once"
                                        else state_var[y])
                    else:
                        value_or_var = _hint_value(hint, state_name, None, x_hat)
                    expr += mu_fam * (x_hat - value_or_var)
            model.cuts.add(model.alpha >= expr)
            n_added += 1

        return n_added
