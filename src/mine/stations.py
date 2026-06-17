from pandas import DataFrame, concat


class Stations(object):
    __slots__ = ['data', 'mapper']

    def __init__(self, data: DataFrame, cols_id):
        self.data = data.set_index(cols_id, drop=False).sort_index()
        self.mapper = dict()

    def get(self, col, keys):
        """ Generic getter of Any columns of defined data

        :param col: selected column of dataframe
        :param keys: defined id selected (station_name o lista de stations)
        :return:
        """
        if keys:
            return self.data.loc[keys][col]
        return self.data[col]

    def _get_optional(self, col, keys=None, default=0):
        if col in self.data.columns:
            return self.get(col, keys)
        return default

    def get_station_name(self, keys=None):
        return self.get('station_name', keys)

    def get_station_cost(self, keys=None):
        return self._get_optional('c_fixed', keys, default=self._get_optional('station_cost', keys, default=0))

    def get_c_bays(self, keys=None):
        return self._get_optional('c_bays', keys, default=0)

    def get_c_charger_space(self, keys=None):
        return self._get_optional('c_charger_space', keys, default=0)
    
    def get_max_chargers(self, keys=None):
        return self._get_optional('max_chargers', keys, default=self._get_optional('max_chargers_per_bay', keys, default=0))

    def get_max_bays(self, keys=None):
        return self._get_optional('max_bays', keys, default=self.get_max_chargers(keys))

    def get_max_chargers_per_bay(self, keys=None):
        return self._get_optional('max_chargers_per_bay', keys, default=self.get_max_chargers(keys))
    
    def get_p_max_ssee(self, keys=None):
        return self.get('p_max_ssee', keys)