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
        return self.get('station_cost', keys)
    
    def get_distance_to_discharge_node(self, keys=None):
        return self.get('distance_to_dn', keys)
    
    def get_max_chargers(self, keys=None):
        return self.get('max_chargers', keys)
    
    def get_maneuvering_time(self, keys=None):
        return self.get('man_time', keys)
