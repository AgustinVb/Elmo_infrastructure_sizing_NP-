from pandas import DataFrame, concat
import numpy as np


class Battery(object):
    __slots__ = ["data", "batteries", "mapper"]

    def __init__(self, data: DataFrame, cols_id: str):
        self.data = data.set_index(cols_id, drop=False).sort_index()
        self.batteries = self.data.index
        self.mapper = {}  

    def _get(self, col: str, keys=None):
        """Acceso genérico a cualquier columna de la hoja Batteries."""
        if keys is not None:
            return self.data.loc[keys, col]
        return self.data[col]

    def get_pmax_charge(self, keys=None):
        return self._get("charge_power", keys)            # kW

    def get_e_max(self, keys=None):
        return self._get("battery_capacity", keys)        # kWh

    def get_soc_min(self, keys=None):
        return self._get("min_soc", keys)                 # p.u.

    def get_charge_efficiency(self, keys=None):
        return self._get("charge_efficiency", keys)       # p.u.

    def get_discharge_efficiency(self, keys=None):
        return self._get("discharge_efficiency", keys)    # p.u.

    def get_total_chargers(self, keys=None):
        return self._get("total_chargers", keys)          # unidades

    def get_energy_cost(self, keys=None):
        return self._get("energy_cost", keys)             # perfil / label

    def get_operation_mode(self, keys=None):
        return self._get("operation_mode", keys)
    
    def get_swap_time(self, keys=None):
        return self._get("swap_time", keys)



