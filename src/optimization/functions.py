import pyomo.environ as pyo
import math
import pandas as pd
import numpy as np
import time
from pyomo.environ import quicksum, value

class OptRules(object):

    def __init__(self, mine_system,  time_series):
        self.mine_system = mine_system
        self.time_series = time_series
        self.time_series.get_node_assignment(mine_system.get_system_lhds())
        self.time_series.get_elhd_at_node(mine_system.get_system_nodes())
        self.time_series.get_battery_assignment(mine_system.get_system_lhds())

    def create_pyo_object(self, object_type, model, object_name, sets, rule, domain=pyo.Reals):
        start = time.time()
        if object_type == "Var":
            setattr(model, object_name, pyo.Var(sets, bounds=rule, domain=domain))
        else:
            if object_type == "Constraint":
                setattr(model, object_name, pyo.Constraint(sets, rule=rule))
        end = time.time()
        print(object_name, end - start)

class OptSets(OptRules):

    def build_sets(self, model):
        model.lhd_set = pyo.Set(initialize=self.mine_system.get_system_lhds())
        model.elhd_set = pyo.Set(initialize=self.mine_system.get_electric_lhds())
        model.dlhd_set = pyo.Set(initialize=self.mine_system.get_diesel_lhds())
        model.nodes_set = pyo.Set(initialize=self.mine_system.get_system_nodes())
        model.time_intervals_set = pyo.Set(initialize=self.time_series.time_intervals)
        model.days = pyo.Set(initialize=self.time_series.days)
        model.shifts = pyo.Set(initialize=self.time_series.shifts)
        model.time_intervals_set_zero = pyo.Set(initialize=[0] + list(self.time_series.time_intervals))
        model.time_intervals_between_shifts_set = pyo.Set(initialize=self.time_series.get_intervals_between_shifts())
        model.stations_set = pyo.Set(initialize=self.mine_system.get_system_stations())


class OptParameters(OptRules):

    def build_parameters(self, model):
        #Parámetros temporales
        model.delta_t = pyo.Param(initialize=self.time_series.delta_t, mutable=True)
        model.t_ini = pyo.Param(initialize=self.time_series.get_time_intervals()[0], mutable=True)
        model.t_fin = pyo.Param(initialize=self.time_series.get_time_intervals()[-1], mutable=True)
        #Parámetros económicos
        model.m_j = pyo.Param(model.nodes_set,model.days, initialize={(j, d): self.time_series.get_extraction_goal(j, d)for j in model.nodes_set for d in model.days},mutable=True)
        model.costo_diesel = pyo.Param(model.dlhd_set, model.days, model.time_intervals_set, initialize={(i, d, t): self.time_series.get_marginal_cost(self.mine_system.elhd.get_energy_cost(i), d, t) for i in model.dlhd_set for d in model.days for t in model.time_intervals_set}, mutable=True)
        model.costo_marginal = pyo.Param(model.elhd_set, model.days, model.time_intervals_set, initialize={(b, d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.elhd.get_energy_cost(b), d, t) for b in model.elhd_set for d in model.days for t in model.time_intervals_set}, mutable=True)
        #Parámetros LHD
        # Parámetros de viaje por nodo
        model.d_i    = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_n_intervals_trip(j,i)       for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pe_i   = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_energy_consumption(j,i)     for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pd_i = pyo.Param( model.lhd_set,model.nodes_set,initialize={(i, j): self.time_series.get_diesel_consumption(j,i) for i in model.lhd_set for j in model.nodes_set},mutable=False)
        # Parámetros de batería
        model.pmax_b = pyo.Param(model.elhd_set,               initialize={b: self.mine_system.elhd.get_pmax_charge(b)        for b in model.elhd_set}, mutable=False)
        
        model.bmin_b = pyo.Param(
        model.elhd_set,
        initialize={b: float(self.mine_system.elhd.get_soc_min(b)) for b in model.elhd_set},
        mutable=False    )

        model.bmax_b = pyo.Param(
        model.elhd_set,
        initialize={b: float(self.mine_system.elhd.get_e_max(b)) for b in model.elhd_set},
        mutable=False)
        # Parámetros de estanque diésel
        model.cmax_i = pyo.Param(model.dlhd_set, initialize={i: self.mine_system.elhd.get_tank_capacity(i)*60/self.mine_system.elhd.get_refueling_time(i) for i in model.dlhd_set}, mutable=False)
        model.e_min_i= pyo.Param(model.dlhd_set,                  initialize={i: self.mine_system.elhd.get_min_capacity(i)        for i in model.dlhd_set}, mutable=False)
        model.e_max_i= pyo.Param(model.dlhd_set,                  initialize={i: self.mine_system.elhd.get_tank_capacity(i)       for i in model.dlhd_set}, mutable=False)
        # Capacidad de pala
        model.g_i    = pyo.Param(model.lhd_set,                   initialize={i: self.mine_system.elhd.get_load_capacity(i)       for i in model.lhd_set}, mutable=False)
        #Parámetros de infraestructura
        model.n_chargers = pyo.Param(initialize=self.time_series.get_n_chargers(), mutable=True)
        model.p_grid = pyo.Param(initialize=self.time_series.get_grid_power(), mutable=True)
        #Parámetros problema de inversión
        model.p_charger =353
        model.p_max = 2100
        model.p_peak = 2100
        model.charger_cost = 47500
        model.scaling_factor_op_cost = pyo.Param(initialize=self.time_series.scaling_factor_op_cost, mutable=True)
        #Parametros estaciones de carga
        model.station_cost_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_station_cost(k) for k in model.stations_set}, mutable=False)
        model.distance_to_dn_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_distance_to_discharge_node(k) for k in model.stations_set}, mutable=False)
        model.max_chargers_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_chargers(k) for k in model.stations_set}, mutable=False)
        model.man_time_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_maneuvering_time(k) for k in model.stations_set}, mutable=False)


class BoundRules(OptRules):

    def Z(self, model, i, d, t):
        return 0, 1
    
    def Z_charge(self, model, k, i, d, t):
        return 0, 1
    
    def Y(self, model, i, j, d, t):
        node_list=self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
        assigned = j in node_list
        if(assigned):
            return 0, 1
        else:
            return 0, 0

    def P(self, model, k, i, d, t):
        return (0, model.pmax_b[i])

    def B(self, model, b, d, t):
        return (0, model.bmax_b[b])

    def Bini(self, model, b, d):
        return (0, model.bmax_b[b])

    def build_all_variables(self, model):
        # Estado ON/OFF de cada LHD
        model.Z         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z, domain=pyo.Binary)
        # Indicador de carga eléctrica (por batería) en cada intervalo
        model.Z_charge  = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z_charge, domain=pyo.Binary)
        # Viaje completo de ida por nodo
        model.Y         = pyo.Var(model.lhd_set, model.nodes_set, model.days, model.time_intervals_set,
                                  bounds=self.Y, domain=pyo.Binary)
        # Potencia de carga de batería b en (d,t)
        model.P         = pyo.Var(model.stations_set,model.elhd_set, model.days, model.time_intervals_set,
                                  bounds=self.P)
        # SOC de batería b al final de (d,t)
        model.B         = pyo.Var(model.elhd_set, model.days, model.time_intervals_set_zero,
                                  bounds=self.B)
        #Cantidad de cargadores
        model.N_chargers= pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers)
        #Elección estación de carga
        model.X = pyo.Var(model.stations_set, domain=pyo.Binary)
        #Inicio de una carga on-board
        model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)

        # Indica si termina una carga en t
        model.EndCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)


from src.optimization.functions import OptRules
import pyomo.environ as pyo

class ConstraintRules(OptRules):

    # (C1) Estado único de cada LHD: Zi,t + sum_j Yi,j,t == 1 
    def state_unique_elhd(self, model, i, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] + sum(model.Z_charge[k,i,d,t] for k in model.stations_set) + sum(model.Y[i, j, d, t] for j in nodes) == 1

    # (C5) Estado entre turnos: Zi,t = 1 for t in Tbs
    def between_shifts_elhd(self, model, i, d, t):
        return model.Z[i, d, t] + sum(model.Z_charge[k,i,d,t] for k in model.stations_set) == 1

    def battery_soc(self, model, i, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        charge = sum(model.P[k,i, d, t] * model.delta_t for k in model.stations_set)
        discharge = sum(
            model.Y[i, j, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
        )

        if t == t0:
            return model.B[i, d, t] == model.B[i, d, 0] + charge - discharge
        else:
            return model.B[i, d, t] == model.B[i, d, t - 1] + charge - discharge


     # (C8a) Límite inferior de SOC batería
    def battery_lower(self, model, i, d, t):
        return model.B[i, d, t] >= model.bmin_b[i] * model.bmax_b[i]

    # (C8b) Límite superior de SOC batería
    def battery_upper(self, model, i, d, t):
        return model.B[i, d, t] <= model.bmax_b[i]


    # (C9) Condición de borde SOC batería
    def battery_boundary(self, model, i, d):
        tf = self.time_series.get_time_intervals()[-1]
        return model.B[i, d, 0] == model.B[i, d, tf]


    # (C25) Producción mínima 
    def production(self, model, d, j):
        target = model.m_j[j,d]
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)

        # extracción normal de mineral
        term_de = sum(
        model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * 0.7
        for i in model.dlhd_set|model.elhd_set for t in model.time_intervals_set
        )
        return term_de >= target

    # Estaciones de carga
    
    #Cantidad máxima de cargadores
    def max_n_chargers(self, model, k):
        return model.N_chargers[k] <= model.max_chargers_k[k] * model.X[k]
    
    #Existencia de la estación
    def station_existence_constraint(self, model, k, i, d, t):
        return model.Z_charge[k,i, d, t] <= model.X[k]
    
    #Limite de cargadores
    def charger_limit(self, model,k, d, t):
        return sum(model.Z_charge[k, i, d, t] for i in model.elhd_set) <= model.N_chargers[k]
    
    #Inicio y termino de una carga on-board
    def charge_state(self, model ,k, i, d, t):
       t0 = self.time_series.get_time_intervals()[0]
       if t>t0:
            return model.Z_charge[k, i, d, t] - model.Z_charge[k, i, d, t-1] == model.StartCharge[k, i, d, t] - model.EndCharge[k, i, d, t]
       else:
           return model.Z_charge[k, i, d, t] == model.StartCharge[k, i, d, t]  - model.EndCharge[k, i, d, t]
       
    #Máxima potencia de carga on-board solo si esta en estacion
    def max_power(self, model, k, i, d, t):
        return model.P[k, i, d, t]  <= model.Z_charge[k, i, d, t] * model.p_charger
    
    #  Sitemas distribución 
    def max_installed_capacity(self, model):
        return sum(model.N_chargers[k] for k in model.stations_set) * model.p_charger  <= model.p_max
    
    def peak_power(self, model, d,t):
        return sum(model.P[k, i, d, t] for k in model.stations_set for i in model.elhd_set) <= model.p_peak

    def build_all_constraints(self, model):
        model.state_unique_elhd                      = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.state_unique_elhd)
        model.between_shifts_elhd                    = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_between_shifts_set, rule=self.between_shifts_elhd)

        model.battery_soc                       = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_soc)
        model.battery_lower = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_lower)
        model.battery_upper = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_upper)
        model.battery_boundary                  = pyo.Constraint(model.elhd_set, model.days, rule=self.battery_boundary)
    
        


        model.production                         = pyo.Constraint(model.days, model.nodes_set, rule=self.production)

        #nuevas
        model.max_n_chargers                     = pyo.Constraint(model.stations_set, rule=self.max_n_chargers)
        model.station_existence_constraint       = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.station_existence_constraint)
        model.charger_limit                   = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.charger_limit)
        model.charge_state                       = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.charge_state)
        model.max_power                          = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.max_power)

        model.max_installed_capacity             = pyo.Constraint(rule=self.max_installed_capacity)
        model.peak_power                         = pyo.Constraint(model.days, model.time_intervals_set, rule=self.peak_power)

class ObjectiveRules(OptRules):

    def lhd_charge_cost(self, model):
        # Coste de cargar baterías (electricidad)
        cost_el = sum(
            model.P[k, i, d, t] * model.costo_marginal[i, d, t] 
            for k in model.stations_set
            for i in model.elhd_set
            for d in model.days
            for t in model.time_intervals_set
        ) * model.delta_t

        return cost_el*model.scaling_factor_op_cost
    
    def inversion_cost(self, model):
        cost_inv = sum(
            model.station_cost_k[k] * model.X[k] + model.charger_cost * model.N_chargers[k]
            for k in model.stations_set
        )
        return cost_inv
    
    def total_cost(self, model):
        return self.lhd_charge_cost(model) + self.inversion_cost(model)

    def build_objective(self, model):
        model.obj = pyo.Objective(rule=self.total_cost, sense=pyo.minimize)

class OutputManager(OptRules):

      def get_var(self, variable, index_names):
        """
        Devuelve un DataFrame con:
        - Una columna 'value'   (los valores numéricos de la variable)
        - Una columna por cada índice (day, interval, lhd, battery, …)
        Esto deja el DF listo para las agrupaciones y filtros de printer.py
        """
        # Extrae valores de Pyomo → dict {index_tuple: value}
        var_values = pd.DataFrame.from_dict(
            variable.extract_values(),
            orient='index',
            columns=['value']        # ← nombre estándar
        )

        # Convierte el dict‐index en MultiIndex con nombres claros
        var_values.index = pd.MultiIndex.from_tuples(
            var_values.index,
            names=index_names
        )

        # ¡Clave!  ► pasa el MultiIndex a columnas
        var_values = var_values.reset_index()   #  ← añade day, interval, …

        return var_values






