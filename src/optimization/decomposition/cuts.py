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
        LP relajado, con Suffix de duales importado. state_links: la lista
        `.state_links` de un YearBlockBuilder -- puede venir del modelo
        original o de un clon (mismos nombres de componente en ambos).

        Con la formulacion McCormick (degradacion_descomposicion_mccormick.
        md, Camino A) el estado de degradacion "D" entra en el acople
        exactamente igual que N_chargers/G/H (kind "simple": el heredado
        es una constante aditiva en <link_estado>, dual = sensibilidad
        directa de LP respecto del RHS) -- ya no requiere el teorema de la
        envolvente que necesitaba el esquema viejo AN_ciclos (heredado
        como coeficiente)."""
        mu = {}
        for link in state_links:
            state_name = link["state"]
            link_con = getattr(relaxed_model, f"link_{state_name}")
            if link["index_set"] is None:
                mu[state_name] = relaxed_model.dual[link_con]
            else:
                mu[state_name] = {
                    idx: relaxed_model.dual[link_con[idx]] for idx in link["index_set"]
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
