from pandas import DataFrame


class Generators(object):
    __slots__ = ['data']

    def __init__(self, data: DataFrame, index_col: str = 'name'):
        self.data = data.set_index(index_col)

    def get_names(self) -> list:
        return list(self.data.index)

    def get_c_inv(self, name: str) -> float:
        return float(self.data.loc[name, 'c_inv'])

    def get_c_op(self, name: str) -> float:
        return float(self.data.loc[name, 'c_op'])

    def get_p_max(self, name: str) -> float:
        """Potencia nominal de una unidad [kW]."""
        return float(self.data.loc[name, 'p_max'])

    def get_g_max(self, name: str) -> int:
        """Cantidad máxima de unidades instalables."""
        return int(self.data.loc[name, 'g_max'])
