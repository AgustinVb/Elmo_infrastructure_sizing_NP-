import math


class BendersCutManager(object):
    """Arma y agrega los cortes de Benders entre bloques anuales consecutivos:

        alpha_{y-1} >= Phi^LP_y + sum_familia mu_familia * (x_hat_{y-1} - x_{y-1})

    Mantiene un historial (iteracion, año) -> corte para debug y
    reproducibilidad: los cortes de iteraciones previas referencian el estado de
    aquella iteracion, no el actual.
    """

    def __init__(self):
        self.history = []

    def read_duals(self, relaxed_model, year, state_links, prefix="link_"):
        """Lee mu para cada familia de estado de un modelo YA resuelto como LP
        relajado, con el Suffix de duales importado. `state_links` es la lista
        `.state_links` de un YearBlockBuilder -- puede venir del modelo original
        o de un clon, porque los nombres de componente son los mismos.

        SIGNO: se devuelve -pi, NO el dual crudo pi del solver. El corte de
        add_cut usa mu en la convencion del lagrangiano L = f + mu^T (z - x_hat),
        con la que

            Phi(x_hat) = g(mu*) - mu*^T x_hat,   g(mu) = min {f + mu^T z}

        y, evaluando esa misma cota en cualquier otro x -- g no depende de x_hat,
        que es lo que hace global al corte --

            Phi(x) >= g(mu*) - mu*^T x = Phi(x_hat) + mu*^T (x_hat - x)

        es decir mu = -dPhi/dx_hat. El dual que reporta el solver para una
        igualdad escrita con el heredado en el lado derecho (<estado>_prev ==
        <estado>_hat, con _hat un Param) es la sensibilidad del optimo respecto
        de ese lado derecho, pi = +dPhi/dx_hat, o sea mu = -pi.

        Usar pi sin invertir el signo da un corte con la pendiente opuesta:
        penaliza invertir en vez de premiarlo, sobreestima el costo futuro fuera
        del punto ancla -- en el ancla los dos signos coinciden, por eso un
        chequeo en un solo punto no lo detecta -- y produce cotas inferiores por
        encima del optimo real (sintoma: LB > UB). Ver tests/test_cut_sign.py en
        la rama carga_ob_multiaño, donde esto esta verificado numericamente
        sobre este mismo codigo.

        `prefix` permite leer los duales del clon elastico que genera los cortes
        de factibilidad, donde las igualdades se llaman feas_link_<estado> (ver
        YearBlockBuilder.build_feasibility_model).
        """
        mu = {}
        for link in state_links:
            state_name = link["state"]
            link_con = getattr(relaxed_model, f"{prefix}{state_name}", None)
            if link_con is None:
                # Estado que este bloque decide (primer año): no hay igualdad de
                # fijacion y por lo tanto no hay multiplicador que leer.
                continue
            if link["index_set"] is None:
                mu[state_name] = -relaxed_model.dual[link_con]
            else:
                mu[state_name] = {
                    idx: -relaxed_model.dual[link_con[idx]] for idx in link["index_set"]
                }
        return mu

    def add_cut(self, parent_block, phi_lp, mu, x_hat_base, iteration=None):
        """Agrega a parent_block.model.cuts el corte que acota alpha del año
        padre con el costo (relajado) del año siguiente, evaluado en la
        sensibilidad mu respecto del estado heredado. `x_hat_base` es el estado
        optimo propio de `parent_block` en ESTA iteracion del forward pass: es
        el punto donde el corte es exacto."""
        model = parent_block.model
        y = parent_block.year
        expr = phi_lp

        for link in parent_block.state_links:
            state_name = link["state"]
            if state_name not in mu:
                continue
            state_var = getattr(model, link["state_var"])
            mu_fam = mu[state_name]
            # "global_once" (n_ssee_k, G, H): la variable real no tiene indice de
            # año, se referencia directo sin agregar el año del bloque padre.
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
    # entero. Los duales de un objetivo elastico l1 valen +-1 exactos, asi que
    # no hace falta nada mas fino.
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

        que es el corte. No lleva alpha: no acota el costo futuro, acota el
        estado. Vive en la misma ConstraintList que los de optimalidad.

        FORTALECIMIENTO ENTERO. Tal cual, ese corte es valido pero puede quedar
        casi vacio, porque el objetivo elastico l1 mide el deficit en la
        RELAJACION del hijo. Medido sobre este modelo: al anio 2 le faltaba
        n_ssee_k y el LP lo cubria con 0.028 unidades fraccionarias, cuando
        n_ssee_k es entera y hace falta una unidad completa -- el corte pedia 35
        veces menos de lo necesario. Si todos los terminos con multiplicador no
        nulo son variables ENTERAS, con el mismo coeficiente y ancla entera,
        entonces el lado izquierdo es entero y el derecho se puede redondear
        hacia arriba (Chvatal-Gomory):

            sum_j (x_j - x_hat_j) >= ceil(v_hat / c)

        En el caso medido eso convierte "n_ssee_k[2] + n_ssee_k[3] >= 0.028" en
        "n_ssee_k[2] + n_ssee_k[3] >= 1", que es la condicion verdadera. El corte
        se agrega al bloque del padre, cuya relajacion lineal produce la cota
        inferior: sigue siendo valido alli porque no elimina ninguna solucion
        entera factible del problema original.
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
            # "global_once" (n_ssee_k, G, H): la variable real no tiene indice de
            # anio, se referencia directo sin agregar el anio del bloque padre.
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
            kind = "feasibility"
            rhs = None
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
        """Devuelve ceil(v_hat / c) si el corte admite el fortalecimiento
        entero, o None si no. Hace falta que TODOS los terminos compartan el
        mismo coeficiente positivo c, que sus variables sean enteras y que el
        ancla tambien lo sea: solo asi el lado izquierdo esta garantizado
        entero y redondear el derecho es valido."""
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
