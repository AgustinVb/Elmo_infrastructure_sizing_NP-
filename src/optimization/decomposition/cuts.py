from pyomo.environ import value


class BendersCutManager(object):
    """Arma y agrega cortes de Benders entre bloques anuales consecutivos
    (implementacion_descomposicion_carga_ob.md, sec. 6.2):

        alpha_{y-1} >= Phi^LP_y + sum_familia mu_familia * (x_hat_{y-1} - x_{y-1})

    Mantiene un historial (iteracion, año) -> corte para debug/
    reproducibilidad (documento sec. 9.2, punto 7: "los cortes de
    iteraciones previas referencian el estado de aquella iteración")."""

    def __init__(self):
        self.history = []

    def read_duals(self, relaxed_model, year, state_links):
        """Lee mu para cada familia de estado de un modelo YA resuelto como
        LP relajado, con Suffix de duales importado. `year` es el año del
        bloque relajado (necesario para indexar R/N_ciclos/NB en el caso
        de degradacion). state_links: la lista `.state_links` de un
        YearBlockBuilder -- puede venir del modelo original o de un clon
        (mismos nombres de componente en ambos).

        Dos mecanismos segun 'kind':
        - "simple" (N_chargers/G/H): el heredado entra como constante
          aditiva en la restriccion de enlace <link_estado>; su dual ES
          directamente la sensibilidad (caso estandar de sensibilidad de
          LP respecto del RHS).
        - "degradation" (AN_ciclos): el heredado entra como COEFICIENTE en
          tres restricciones (b_bar_def/energy_link/arrastre), no como
          constante aditiva -- no hay un dual directo. Se usa el teorema
          de la envolvente (ver _degradation_sensitivity)."""
        mu = {}
        for link in state_links:
            state_name = link["state"]
            if link.get("kind") == "degradation":
                mu[state_name] = self._degradation_sensitivity(relaxed_model, year)
            else:
                link_con = getattr(relaxed_model, f"link_{state_name}")
                if link["index_set"] is None:
                    mu[state_name] = relaxed_model.dual[link_con]
                else:
                    mu[state_name] = {
                        idx: relaxed_model.dual[link_con[idx]] for idx in link["index_set"]
                    }
        return mu

    def _degradation_sensitivity(self, relaxed_model, year):
        """dPhi/d(AN_ciclos_hat) via teorema de la envolvente (derivacion
        completa en la conversacion de diseño; resumen: para
        min f(x) s.a. g_i(x,theta)=0, con multiplicadores λ_i en el optimo,
        dPhi/dtheta = sum_i λ_i * dg_i/dtheta). AN_ciclos_hat (theta) entra
        como coeficiente en b_bar_def, energy_link y arrastre
        (year_block.py::_add_degradation_state) -- se suman las tres
        contribuciones, cada una el dual de esa restriccion por su
        derivada parcial analitica respecto de AN_ciclos_hat, evaluada en
        el optimo primal de esta relajacion.

        Signo: verificado empiricamente por diferencias finitas (ver
        conversacion de diseño) contra la convencion real de duales de
        Pyomo/Gurobi para estas igualdades -- resulto ser el opuesto del
        que predice la derivacion KKT de libro (dPhi/dtheta = +sum λ·dg/dθ);
        la convencion efectiva de esta interfaz es
        dPhi/dtheta = -sum λ·dg/dθ (coherente con que los cortes de
        N_chargers/G/H, que usan el dual de 'prev==hat' sin negar, ya
        estaban validados en M2: para ese caso dg/dθ=-1, y el signo
        correcto ahi es +dual, no dual*(-1))."""
        y = year
        R_star = value(relaxed_model.R[y])
        n_ciclos_star = value(relaxed_model.N_ciclos[y])
        nb_star = value(relaxed_model.NB[y])
        n_elhd = value(relaxed_model.n_elhd_bd)
        gamma = value(relaxed_model.gamma_coef)

        dual_b_bar = relaxed_model.dual[relaxed_model.b_bar_def]
        dual_energy = relaxed_model.dual[relaxed_model.energy_link]
        dual_arrastre = relaxed_model.dual[relaxed_model.arrastre]

        d_b_bar_def = gamma * (1 - R_star)
        d_energy_link = n_elhd * gamma * (nb_star - n_ciclos_star)
        d_arrastre = -(1 - R_star)

        return -(dual_b_bar * d_b_bar_def
                 + dual_energy * d_energy_link
                 + dual_arrastre * d_arrastre)

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
            if link["index_set"] is None:
                expr += mu_fam * (x_hat_base[state_name] - state_var[y])
            else:
                for idx, mu_v in mu_fam.items():
                    expr += mu_v * (x_hat_base[state_name][idx] - state_var[idx, y])

        model.cuts.add(model.alpha >= expr)

        self.history.append({
            "iteration": iteration,
            "parent_year": y,
            "phi_lp": phi_lp,
            "mu": mu,
            "x_hat_base": x_hat_base,
        })
