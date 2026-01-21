from pandas import DataFrame
import numpy as np

class ELHD(object):
    """
    Objeto ELHD (Electric / Diesel Load-Haul-Dump).

    Este módulo solo maneja los datos que vienen de la hoja “LHD” en el Excel.
    Todos los parámetros relacionados con baterías han sido movidos a `battery.py`.
    """

    __slots__ = [
        "data",         # DataFrame completo con índice en “name”
        "elhds",        # lista de índices (nombres) de cada LHD
        "mapper"        # diccionario opcional para alias
    ]

    # ------------------------------------------------------------------ #
    # Constructor & utilidades genéricas
    # ------------------------------------------------------------------ #
    def __init__(self, data: DataFrame, cols_id: str):
        """
        - data: DataFrame leído de la pestaña “LHD”
        - cols_id: nombre de la columna que identifica unívocamente cada LHD,
                   típicamente “name” o “id”
        """
        # Convertimos “cols_id” en índice, sin descartarlo (“drop=False”), y ordenamos
        self.data = data.set_index(cols_id, drop=False).sort_index()
        self.elhds = list(self.data.index)      # lista de todos los LHD (por nombre/id)
        self.mapper = {}                        # por si quieres añadir alias

    def _get(self, col: str, keys=None):
        """
        Acceso genérico a la columna “col”. Si keys es None, devuelve toda la serie;
        si keys es lista o nombre individual, filtra por índice.
        """
        if keys is not None:
            return self.data.loc[keys, col]
        return self.data[col]

    # ------------------------------------------------------------------ #
    # Getters para CADA columna de la pestaña “LHD”
    # ------------------------------------------------------------------ #

    def get_id(self, keys=None):
        return self._get("id", keys)

    def get_name(self, keys=None):
        return self._get("name", keys)

    def get_energy_cost(self, keys=None):
        return self._get("energy_cost", keys)
    
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
    
    def get_charge_time(self, keys=None):
        return self._get("charge_time", keys)

    def get_technology_type(self, keys=None):
        return self._get("technology_type", keys)

    def get_operation_mode(self, keys=None):
        return self._get("operation_mode", keys)

    def get_model(self, keys=None):
        return self._get("model", keys)

    def get_load_capacity(self, keys=None):
        return self._get("load_capacity", keys)          # [t]

    def get_filling_factor(self, keys=None):
        return self._get("filling_factor", keys)         # [p.u.]

    def get_loading_time(self, keys=None):
        return self._get("loading_time", keys)           # [h o min, según tu Excel]

    def get_discharging_time(self, keys=None):
        return self._get("discharging_time", keys)       # [h o min]

    def get_weight(self, keys=None):
        return self._get("weight", keys)                 # [t]

    def get_engine_power(self, keys=None):
        return self._get("engine_power", keys)           # [kW]

    def get_frontal_area(self, keys=None):
        return self._get("frontal_area", keys)           # [m²]

    def get_tank_capacity(self, keys=None):
        return self._get("tank_capacity", keys)          # [L o m³, según tu Excel]

    def get_min_capacity(self, keys=None):
        return self._get("min_capacity", keys)           # [L o m³]

    def get_fuel_consumption(self, keys=None):
        return self._get("fuel_consumption", keys)       # [L/h o similar]

    def get_refueling_time(self, keys=None):
        return self._get("refueling_time", keys)         # [h o min]

    def get_engine_efficiency(self, keys=None):
        return self._get("engine_efficiency", keys)      # [p.u.]

    def get_transmission_efficiency(self, keys=None):
        return self._get("transmission_efficiency", keys)# [p.u.]

    def get_speed(self, keys=None):
        return self._get("speed", keys)                  # [m/s o km/h]

    def get_acceleration(self, keys=None):
        return self._get("acceleration", keys)           # [m/s²]

    def get_hydraulic_power(self, keys=None):
        return self._get("hydraulic_power", keys)        # [kW]

    def get_aux_power(self, keys=None):
        return self._get("aux_power", keys)              # [kW]

    def get_total_trips_info(self, distance_outbound, distance_return, tilt, elhd_name, delta_t):
        travel_dur_sec, energy = self.get_per_trip_info(distance_outbound, distance_return, tilt, elhd_name)
        travel_dur=travel_dur_sec/3600

        return travel_dur, energy



    def get_per_trip_info(self, distance_outbound, distance_return, tilt, elhd_name):
        speed = self.get_speed(elhd_name) * (1000 / 3600)
        acceleration = self.get_acceleration(elhd_name)
        t_load = self.get_loading_time(elhd_name)
        t_discharge = self.get_discharging_time(elhd_name)

        # OUTBOUND (descargado)
        t_acc_out = (speed - 0) / acceleration
        t_dec_out = (speed - 0) / acceleration
        t_const_out = (distance_outbound - 0.5 * acceleration * t_acc_out**2 - 0.5 * acceleration * t_dec_out**2) / speed
        t_outbound = t_acc_out + t_const_out + t_dec_out

        # RETURN (cargado)
        t_acc_ret = (speed - 0) / acceleration
        t_dec_ret = (speed - 0) / acceleration
        t_const_ret = (distance_return - 0.5 * acceleration * t_acc_ret**2 - 0.5 * acceleration * t_dec_ret**2) / speed
        t_return = t_acc_ret + t_const_ret + t_dec_ret

        # ENERGY

        energy = self.engine_energy(elhd_name, tilt, t_acc_out, t_const_out, t_acc_ret, t_const_ret, t_load, t_discharge)

        return t_outbound + t_load + t_return + t_discharge, energy

    #Eliminé termino de loss_dec_term
    def aerodynamic_loss(self, elhd_name, t_acc_out, t_const_out, t_acc_ret, t_const_ret):
        acceleration = self.get_acceleration(elhd_name)
        speed = self.get_speed(elhd_name) * (1000 / 3600)
        frontal_area = self.get_frontal_area(elhd_name)
        rho = 1.225
        c_drag = 1.95
        constant = 0.5 * rho * frontal_area * c_drag

        def loss_term(t_acc, t_const):
            loss_acc = ((acceleration ** 3) * (t_acc ** 4)) / 4
            loss_cte = speed ** 3 * t_const
            return loss_acc + loss_cte

        return constant * (loss_term(t_acc_out, t_const_out) + loss_term(t_acc_ret, t_const_ret))


    def delta_kinetic_energy(self, elhd_name):
        constant_speed = self.get_speed(elhd_name) * (1000 / 3600)
        weight_out = self.get_weight(elhd_name) * 1000  # in kg
        weight_return = (self.get_weight(elhd_name) + self.get_load_capacity(elhd_name)) * 1000  # in kg
        e_outbound = 0.5 * weight_out * constant_speed ** 2
        e_return = 0.5 * weight_return * constant_speed ** 2
        return e_outbound + e_return  # assumes no regenerative brake
    
    ##Eliminé termino de e_decc_out_term
    def rolling_resistance_loss(self, elhd_name, tilt, t_acc_out, t_const_out, t_acc_ret, t_const_ret):
        g = 9.81
        cr = 1
        c1 = 0.000
        c2 = 0.03
        acceleration = self.get_acceleration(elhd_name)
        speed = self.get_speed(elhd_name) * (1000 / 3600)

        # OUTBOUND
        weight_out = self.get_weight(elhd_name) * 1000
        const_out = weight_out * g * np.cos(tilt) * cr
        e_acc_out = (c1 * acceleration**2 * t_acc_out**3 / 3 + c2 * acceleration * t_acc_out**2 / 2)
        e_cte_out = (c1 * speed + c2) * speed * t_const_out
        e_outbound = const_out * (e_acc_out + e_cte_out)

        # RETURN
        weight_ret = (self.get_weight(elhd_name) + self.get_load_capacity(elhd_name)) * 1000
        const_ret = weight_ret * g * np.cos(tilt) * cr
        e_acc_ret = (c1 * acceleration**2 * t_acc_ret**3 / 3 + c2 * acceleration * t_acc_ret**2 / 2)
        e_cte_ret = (c1 * speed + c2) * speed * t_const_ret
        e_return = const_ret * (e_acc_ret + e_cte_ret)

        return e_outbound + e_return


    #Eliminé termino de e_decc_out_term
    def gravitational_work(self, elhd_name, tilt, t_acc_out, t_const_out, t_acc_ret, t_const_ret):
        g = 9.81
        acceleration = self.get_acceleration(elhd_name)
        speed = self.get_speed(elhd_name) * (1000 / 3600)

        # OUTBOUND (descargado)
        weight_out = self.get_weight(elhd_name) * 1000
        cte_out = weight_out * g * np.sin(tilt)
        e_acc_out_term = acceleration * t_acc_out**2 / 2
        e_cte_out_term = speed * t_const_out
        e_outbound = cte_out * (e_acc_out_term + e_cte_out_term)

        # RETURN (cargado)
        weight_ret = (self.get_weight(elhd_name) + self.get_load_capacity(elhd_name)) * 1000
        cte_ret = weight_ret * g * np.sin(-tilt)
        e_acc_ret_term = acceleration * t_acc_ret**2 / 2
        e_cte_ret_term = speed * t_const_ret
        e_return = cte_ret * (e_acc_ret_term + e_cte_ret_term)

        return e_outbound + e_return


    def wheel_energy(self, elhd_name, tilt, t_acc, t_constant):
        return self.gravitational_work(elhd_name, tilt, t_acc, t_constant)+ \
               self.aerodynamic_loss( elhd_name, t_acc, t_constant)+ \
               self.delta_kinetic_energy(elhd_name)+ \
               self.rolling_resistance_loss(elhd_name, tilt, t_acc, t_constant) 
            

    def engine_energy(self, elhd_name, tilt, t_acc_out, t_const_out, t_acc_ret, t_const_ret, t_load, t_unload):
        aux_energy = 1000 * self.get_aux_power(elhd_name) * (t_acc_out + t_const_out + t_acc_ret + t_const_ret + t_load + t_unload)
        hydraulic_energy = 1000 * self.get_hydraulic_power(elhd_name) * (t_load + t_unload)
        
        wheel_energy = (
            self.gravitational_work(elhd_name, tilt, t_acc_out, t_const_out, t_acc_ret, t_const_ret)
            + self.aerodynamic_loss(elhd_name, t_acc_out, t_const_out, t_acc_ret, t_const_ret)
            + self.delta_kinetic_energy(elhd_name)
            + self.rolling_resistance_loss(elhd_name, tilt, t_acc_out, t_const_out, t_acc_ret, t_const_ret)
        )
        
        return (wheel_energy / (
            self.get_engine_efficiency(elhd_name) * self.get_transmission_efficiency(elhd_name)) + (aux_energy + hydraulic_energy)/self.get_transmission_efficiency(elhd_name)) / (1000 * 3600)


    def engine_energy_charge_travel(self, distance, elhd_name, tilt):
        speed = self.get_speed(elhd_name) * (1000/3600)
        acceleration = self.get_acceleration(elhd_name)
        t_acc = (speed - 0) / acceleration
        t_dec = (speed - 0) / acceleration
        t_constant = (distance - 0.5 * acceleration * (t_acc) ** 2 - 0.5 * acceleration * (t_dec) ** 2)/speed
        aux_energy = 1000 * self.get_aux_power(elhd_name) * 2*(t_acc + t_constant + t_dec)

        #gravitational_work ida y vuelta a estación de carga sin material
        g = 9.81
        acceleration = self.get_acceleration(elhd_name)
        speed = self.get_speed(elhd_name) * (1000 / 3600)
        weight_out = self.get_weight(elhd_name) * 1000
        cte_out = weight_out * g * np.sin(tilt)
        e_acc_out_term = acceleration * t_acc**2 / 2
        e_cte_out_term = speed * t_constant
        gravitational_work = 2*cte_out * (e_acc_out_term + e_cte_out_term) #ida y vuelta a estación de carga sin material

        #delta kinetic energy ida y vuelta a estación de carga sin material
        delta_kinetic_energy= 2*(0.5 * weight_out * speed ** 2)
        
        #rolling resistance loss ida y vuelta a estación de carga sin material
        g = 9.81
        cr = 1
        c1 = 0.000
        c2 = 0.03
        const_out = weight_out * g * np.cos(tilt) * cr
        e_acc_out = (c1 * acceleration**2 * t_acc**3 / 3 + c2 * acceleration * t_acc**2 / 2)
        e_cte_out = (c1 * speed + c2) * speed * t_constant
        rolling_resistance_loss = 2*const_out * (e_acc_out + e_cte_out)


        wheel_energy = (
            gravitational_work
            + self.aerodynamic_loss(elhd_name, t_acc, t_constant, t_acc, t_constant)
            + delta_kinetic_energy
            + rolling_resistance_loss
        )
        
        if distance==0:
            return 0
        return (wheel_energy / (
            self.get_engine_efficiency(elhd_name) * self.get_transmission_efficiency(elhd_name)) + (aux_energy)/self.get_transmission_efficiency(elhd_name)) / (1000 * 3600)
    
    def time_charge_station(self, distance, elhd_name):
        #travel time to charge station
        velocity = self.get_speed(elhd_name) * (1000 / 3600)
        acceleration = self.get_acceleration(elhd_name)
        t_acc = (velocity - 0) / acceleration
        t_dec = (velocity - 0) / acceleration
        t_constant = (distance - 0.5 * acceleration * (t_acc) ** 2 - 0.5 * acceleration * (t_dec) ** 2) / velocity
        #Total time in hours
        t_total = (t_acc + t_constant + t_dec)/3600
        if distance==0:
            return 0
        return t_total
