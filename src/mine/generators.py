from pandas import DataFrame, isna


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

    def get_vida_util(self, name: str) -> float:
        """Vida útil [años] para el valor residual lineal al final del horizonte.
        0 (sin valor residual) si la hoja no trae la columna 'vida_util' o la
        celda está vacía."""
        if 'vida_util' not in self.data.columns or isna(self.data.loc[name, 'vida_util']):
            aviso_sin_vida_util('Generators', name)
            return 0.0
        return float(self.data.loc[name, 'vida_util'])


_SIN_VIDA_UTIL_AVISADOS = set()


def aviso_sin_vida_util(hoja: str, name: str) -> None:
    """Avisa UNA vez por proceso que un activo queda sin valor residual (el
    modelo se arma muchas veces: un bloque por año y por dia)."""
    if (hoja, name) not in _SIN_VIDA_UTIL_AVISADOS:
        _SIN_VIDA_UTIL_AVISADOS.add((hoja, name))
        print(f"[valor residual] AVISO: '{name}' (hoja {hoja}) sin vida_util: "
              "se cobra la inversion completa, sin valor residual", flush=True)
