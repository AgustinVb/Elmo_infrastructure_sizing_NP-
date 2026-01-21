from pandas import DataFrame, concat


class Chargers(object):
    __slots__ = ['data']

    def __init__(self, data: DataFrame, cols_id):
        # Asumimos que la hoja 'chargers' tiene un id==1 con los valores en la tercera fila
        # y la segunda fila contiene unidades. Simplificamos: siempre leemos la fila con id 1.
        self.data = data.set_index(cols_id, drop=False).sort_index()

    def _row1(self):
        # intenta obtener el registro con índice 1, si no existe devuelve la primera fila
        if 1 in self.data.index:
            row = self.data.loc[1]
        elif '1' in self.data.index:
            row = self.data.loc['1']
        else:
            row = self.data.iloc[0]
        # si row es una Serie con múltiples entradas (por multiindex), tomar la primera
        if hasattr(row, 'iloc'):
            return row
        return row

    def get_model(self) -> str:
        return self._row1()['charger_model']

    def get_charger_cost(self) -> float:
        return self._row1()['charger_cost']

    def get_charger_power(self) -> float:
        return self._row1()['charger_power']

    def get_num_connectors(self) -> int:
        return self._row1()['num_connectors']

    def get_p_peak_dist(self) -> float:
        return self._row1()['p_peak_dist']
