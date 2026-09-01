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

    def get_station_name(self, keys=None):
        return self.get('station_name', keys)

    def get_station_cost(self, keys=None):
        return self.get('c_fixed', keys)

    def get_c_bays(self, keys=None):
        return self.get('c_bays', keys)

    def get_c_charger_space(self, keys=None):
        return self.get('c_charger_space', keys)

    def get_c_battery_space(self, keys=None):
        return self.get('c_battery_space', keys)

    def get_max_bays(self, keys=None):
        return self.get('max_bays', keys)

    def get_max_batteries_per_bay(self, keys=None):
        return self.get('max_batteries_per_bay', keys)

    def get_max_chargers_per_bay(self, keys=None):
        return self.get('max_chargers_per_bay', keys)

    def get_p_max_ssee(self, keys=None):
        return self.get('p_max_ssee', keys)

    def get_c_inv_ssee(self, keys=None):
        return self.get('c_inv_ssee', keys)

    def get_c_crane(self, keys=None):
        return self.get('c_crane', keys)
