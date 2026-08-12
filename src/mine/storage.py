from pandas import DataFrame


class StorageSystems(object):
    __slots__ = ['data']

    def __init__(self, data: DataFrame, index_col: str = 'name'):
        self.data = data.set_index(index_col)

    def get_names(self) -> list:
        return list(self.data.index)

    def get_c_inv(self, name: str) -> float:
        """Costo de inversión por unidad [USD]."""
        return float(self.data.loc[name, 'c_inv'])

    def get_c_op(self, name: str) -> float:
        """Costo de O&M anual por unidad [USD/año]."""
        return float(self.data.loc[name, 'c_op'])

    def get_p_max(self, name: str) -> float:
        """Potencia máxima de inyección/extracción [kW]."""
        return float(self.data.loc[name, 'p_max'])

    def get_eta(self, name: str) -> float:
        """Eficiencia de carga/descarga [p.u.]."""
        return float(self.data.loc[name, 'eta'])

    def get_a_min(self, name: str) -> float:
        """Estado de energía mínimo [kWh]."""
        return float(self.data.loc[name, 'a_min'])

    def get_a_max(self, name: str) -> float:
        """Estado de energía máximo [kWh]."""
        return float(self.data.loc[name, 'a_max'])

    def get_h_max(self, name: str) -> int:
        """Número máximo de unidades del cluster instalables en todo el horizonte."""
        return int(self.data.loc[name, 'h_max'])
