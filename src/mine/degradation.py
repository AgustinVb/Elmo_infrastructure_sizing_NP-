from pandas import DataFrame


class BatteryDegradation(object):
    """Datos fleet-wide de degradacion/reemplazo de baterias on-board.

    Hoja 'BatteryDegradation': fila unica (id=1) con columnas
    ['gamma_coef', 'c_bat_replace', 'min_capacity_fraction', 'discount_rate'].
    """
    __slots__ = ['data']

    def __init__(self, data: DataFrame, cols_id: str = 'id'):
        self.data = data.set_index(cols_id, drop=False).sort_index()

    def _row1(self):
        if 1 in self.data.index:
            return self.data.loc[1]
        elif '1' in self.data.index:
            return self.data.loc['1']
        return self.data.iloc[0]

    def get_gamma_coef(self) -> float:
        """Coeficiente f(p_m^c): perdida de capacidad por ciclo equivalente."""
        return float(self._row1()['gamma_coef'])

    def get_c_bat_replace(self) -> float:
        """Costo de reemplazar la bateria de la flota [USD]."""
        return float(self._row1()['c_bat_replace'])

    def get_min_capacity_fraction(self) -> float:
        """Piso de degradacion como fraccion de b_max (ej. 0.8)."""
        return float(self._row1()['min_capacity_fraction'])

    def get_discount_rate(self) -> float:
        """Tasa de descuento anual r. Default 0.0 (sin descuento) si la
        columna no esta en la hoja, para no romper escenarios existentes."""
        row = self._row1()
        if 'discount_rate' in row.index:
            return float(row['discount_rate'])
        return 0.0
