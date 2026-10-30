from pandas import DataFrame


class FleetByYear(object):
    """Cantidad de LHD electricos activos por año (fleet-wide, supuesto de
    operacion simetrica -- todas las baterias on-board se degradan por
    igual dentro de un año, ver BatteryDegradation). Alimenta n_elhd_bd
    en el submodelo de degradacion (functions.py), en vez del fleet
    nominal fijo (len(elhd_set)).

    Hoja 'FleetByYear': una fila por año, columnas ['year', 'n_active_lhd'] y,
    opcional, 'opera_en_colacion' (0/1, ver opera_en_colacion).
    """
    __slots__ = ['data']

    def __init__(self, data: DataFrame, cols_id: str = 'year'):
        self.data = data.set_index(cols_id, drop=False).sort_index()

    def get_n_active(self, year) -> int:
        """LHD electricos activos en el año dado. Error explicito (sin
        fallback silencioso) si el año no esta en la hoja, para no
        enmascarar un typo/omision en la planilla."""
        y = int(year)
        if y not in self.data.index:
            raise KeyError(
                f"FleetByYear: no hay fila para year={y} en la hoja "
                f"'FleetByYear' -- agrega una fila explicita, no se usa "
                f"ningun valor por defecto."
            )
        return int(self.data.loc[y, 'n_active_lhd'])

    def opera_en_colacion(self, year) -> bool:
        """True si ese año los LHD pueden operar durante la colacion del
        esquema DCH (columna opcional 'opera_en_colacion'; sin la columna o
        sin la fila, False). Ver ConstraintRules.meal_g1_no_travel_group1."""
        y = int(year)
        if 'opera_en_colacion' not in self.data.columns or y not in self.data.index:
            return False
        valor = self.data.loc[y, 'opera_en_colacion']
        return bool(int(valor)) if valor == valor else False
