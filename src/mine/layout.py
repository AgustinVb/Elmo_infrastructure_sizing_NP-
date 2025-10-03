from pandas import DataFrame, concat


class Layout(object):
    __slots__ = ['data', 'mapper']

    def __init__(self, data: DataFrame, cols_id):
        self.data = data.set_index(cols_id, drop=False).sort_index()
        self.mapper = dict()

    def get(self, col, keys):
        """ Generic getter of Any columns of defined data

        :param col: selected column of dataframe
        :param keys: defined id selected
        :return:
        """
        if keys:
            return self.data.loc[keys][col]
        return self.data[col]

    def get_name(self, keys=None):
        return self.get('name', keys)

    def get_discharge_node(self):
        return self.get('discharge_node', None)

    def get_distance_to_d_node_outbound(self, keys=None):
        return self.get('distance_to_d_node_outbound', keys)
    
    def get_distance_to_d_node_return(self, keys=None):
        return self.get('distance_to_d_node_return', keys)

    def get_quota(self, keys=None):
        return self.get('quota', keys)
    
    def get_tilt(self, keys=None):
        return self.get('tilt', keys)
    
    def get_street(self, keys=None):
        return self.get('street', keys)

