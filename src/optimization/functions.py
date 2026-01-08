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
        model.slhd_set = pyo.Set(initialize=self.mine_system.get_swap_lhds())
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
        model.costo_marginal = pyo.Param(model.slhd_set, model.days, model.time_intervals_set, initialize={(b, d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.elhd.get_energy_cost(b), d, t) for b in model.slhd_set for d in model.days for t in model.time_intervals_set}, mutable=True)
        model.costo_electricidad = pyo.Param(model.days, model.time_intervals_set, initialize={(d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.chargers.get_energy_cost(), d, t) for d in model.days for t in model.time_intervals_set}, mutable=True)
        #Parámetros LHD
        # Parámetros de viaje por nodo
        model.d_i    = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_n_intervals_trip(j,i)       for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pe_i   = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_energy_consumption(j,i)     for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pd_i = pyo.Param( model.lhd_set,model.nodes_set,initialize={(i, j): self.time_series.get_diesel_consumption(j,i) for i in model.lhd_set for j in model.nodes_set},mutable=False)
        # Parámetros de batería
        model.pmax_b = pyo.Param(model.slhd_set,               initialize={b: self.mine_system.elhd.get_pmax_charge(b)        for b in model.slhd_set}, mutable=False)
        
        model.bmin_b = pyo.Param(
        model.slhd_set,
        initialize={b: float(self.mine_system.elhd.get_soc_min(b)) for b in model.slhd_set},
        mutable=False    )

        model.bmax_b = pyo.Param(
        model.slhd_set,
        initialize={b: float(self.mine_system.elhd.get_e_max(b)) for b in model.slhd_set},
        mutable=False)
        # Capacidad de pala
        model.g_i    = pyo.Param(model.lhd_set,                   initialize={i: self.mine_system.elhd.get_load_capacity(i)       for i in model.lhd_set}, mutable=False)
        model.filling_factor = pyo.Param(model.lhd_set,        initialize={i: self.mine_system.elhd.get_filling_factor(i)      for i in model.lhd_set}, mutable=False)
         
        #Parámetros problema de inversión
        model.p_charger = pyo.Param(initialize=self.mine_system.chargers.get_charger_power(), mutable=False)
        model.p_max = pyo.Param(initialize=self.mine_system.chargers.get_p_max_dist(), mutable=False)
        model.p_peak = pyo.Param(initialize=self.mine_system.chargers.get_p_peak_dist(), mutable=False)
        model.charger_cost = pyo.Param(initialize=self.mine_system.chargers.get_charger_cost(), mutable=False)
        model.scaling_factor_op_cost = pyo.Param(initialize=self.time_series.scaling_factor_op_cost, mutable=True)
        model.battery_cost = pyo.Param(initialize=self.mine_system.chargers.get_battery_cost(), mutable=False)
        #Parametros estaciones de carga
        model.station_cost_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_station_cost(k) for k in model.stations_set}, mutable=False)
        model.distance_to_dn_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_distance_to_discharge_node(k) for k in model.stations_set}, mutable=False)
        model.max_chargers_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_chargers(k) for k in model.stations_set}, mutable=False)
        model.man_time_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_maneuvering_time(k) for k in model.stations_set}, mutable=False)
        model.pk_i   = pyo.Param( model.stations_set, model.slhd_set, initialize={(k,i):self.mine_system.elhd.engine_energy_charge_travel(self.mine_system.stations.get_distance_to_discharge_node(k),i,0) for k in model.stations_set for i in model.slhd_set}, mutable=False)
        model.t_ttc_i   = pyo.Param( model.stations_set, model.slhd_set, initialize={(k,i):self.mine_system.elhd.time_charge_station(self.mine_system.stations.get_distance_to_discharge_node(k),i) for k in model.stations_set for i in model.slhd_set}, mutable=False)
        model.nk_bat = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_batteries(k) for k in model.stations_set}, mutable=False)
        model.t_swap = pyo.Param(model.lhd_set, initialize={i: self.mine_system.elhd.get_swap_time(i) for i in model.lhd_set}, mutable=False)
        model.t_charge = pyo.Param(model.lhd_set, initialize={i: self.mine_system.elhd.get_charge_time(i) for i in model.lhd_set}, mutable=False)
        # Scalar fallback for station-level logic (use first LHD charge time)
        first_elhd = self.mine_system.elhd.elhds[0]
        model.t_charge_scalar = pyo.Param(initialize=self.mine_system.elhd.get_charge_time(first_elhd), mutable=False)
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
        # Variable binaria que indica si el LHD esta inactivo o no
        model.Z         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z, domain=pyo.Binary)
        # Variable binaria que indica si el LHD está cargando
        model.Z_charge  = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z_charge, domain=pyo.Binary)
        # Variable binaria que indica si el LHD reliza un swap 
        model.Z_swap   = pyo.Var(model.stations_set, model.slhd_set, model.days, model.time_intervals_set,
                             domain=pyo.Binary)
        # Variable binaria que indica si hay penalidad en la producción por swap
        model.Z_pen   = pyo.Var(model.slhd_set, model.nodes_set, model.days, model.time_intervals_set,
                                domain=pyo.Binary)
        # Viaje completo de ida por nodo
        model.Y         = pyo.Var(model.lhd_set, model.nodes_set, model.days, model.time_intervals_set,
                                  bounds=self.Y, domain=pyo.Binary)
        # Potencia de carga de batería b en (d,t)
        model.P         = pyo.Var(model.stations_set,model.elhd_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        # SOC de batería b al final de (d,t)
        model.B         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        #Cantidad de cargadores
        model.N_chargers= pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers)
        #Elección estación de carga
        model.X = pyo.Var(model.stations_set, domain=pyo.Binary)
        #Inicio de una carga on-board
        model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)    
        # Indica si termina una carga en t
        model.EndCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)
        # Cantidad de baterías en estación
        model.N_batteries = pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers)
        # Variable que actualiza el estado de carga de la batería del LHD
        model.B_s = pyo.Var(model.slhd_set, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        # Número de baterías cargadas en el intervalo t en la estación k
        model.S = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Número de baterías que comienzan a cargar en a y siguen conectadas en t en la estación k
        model.Sv = pyo.Var(model.stations_set, model.days, model.time_intervals_set,  model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Número de baterías descargadas en el intervalo t en la estación k
        model.X_dch = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Número de baterías que comienzan a cargar al inicio del intervalo t en la estación k
        model.X_ini = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Demanda de baterías en el intervalo t en la estación k
        model.W = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)

from src.optimization.functions import OptRules
import pyomo.environ as pyo

class ConstraintRules(OptRules):

    # Estado único de cada LHD: Zi,t + sum_j Yi,j,t == 1 
    def state_unique_elhd(self, model, i, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] + sum(model.Z_charge[k,i,d,t] for k in model.stations_set) + sum(model.Y[i, j, d, t] for j in nodes) == 1
    
    def state_unique_elhd_swap(self, model, i, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] + sum(model.Y[i, j, d, t] for j in nodes) == 1

    # Estado entre turnos: Zi,t = 1 for t in Tbs
    def between_shifts_elhd(self, model, i, d, t):
        return model.Z[i, d, t] + sum(model.Z_charge[k,i,d,t] for k in model.stations_set) == 1
    
    def between_shifts_elhd_swap(self, model, i, d, t):
        return model.Z[i, d, t]  == 1

    def battery_soc(self, model, i, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        # Safely get assigned nodes for (d,t,i); skip if none
        nodes = self.time_series.mapper.get('Nodes_assigned_at_interval', {}).get((d, t, i), [])
        charge = sum(model.P[k, i, d, t] * (model.delta_t - model.man_time_k[k] - model.t_ttc_i[k, i]) for k in model.stations_set)
        discharge = sum(
            model.Y[i, j, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in nodes
        )
        penalization_charge = 2 * sum(model.StartCharge[k, i, d, t] * model.pk_i[k, i] * model.t_ttc_i[k, i] for k in model.stations_set)
        
        if t >= t0:
            return model.B[i, d, t] == model.B[i, d, t - 1] + charge - discharge - penalization_charge
        else:
            return pyo.Constraint.Skip
    
    # Estado de energia considerando swap de baterías
    def battery_soc_swap(self, model, i, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        discharge = sum(model.Y[i, j, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)])
        penalization_charge = sum(model.Z_swap[k, i, d, t] * model.pk_i[k,i] for k in model.stations_set)
        
        if t > t0:
            return model.B[i, d, t] ==  model.B_s[i, d, t-1] - discharge - penalization_charge
        
        else:
            return pyo.Constraint.Skip

     # Definicion de B_s actualización del estado de carga con swap de
     
    def battery_soc_swap_update_1(self, model, i, d, t):
        return model.B_s[i,d,t-1] <= model.B[i,d,t-1] + sum(model.Z_swap[k, i ,d, t] for k in model.stations_set) * model.bmax_b[i]

    def battery_soc_swap_update_2(self, model, i, d, t):
        return model.B_s[i,d,t-1] >= model.B[i,d,t-1] - sum(model.Z_swap[k, i ,d, t] for k in model.stations_set) * model.bmax_b[i]
    
    def battery_soc_swap_update_3(self, model, i, d, t):
        return model.B_s[i,d,t-1] <= model.bmax_b[i]

    def battery_soc_swap_update_4(self, model, i, d, t):
        return model.B_s[i,d,t-1] >= sum(model.Z_swap[k, i ,d, t] for k in model.stations_set) * model.bmax_b[i]

     # Límite inferior de SOC batería
    def battery_lower(self, model, i, d, t):
        return model.B[i, d, t] >= model.bmin_b[i] * model.bmax_b[i]

    # Límite superior de SOC batería
    def battery_upper(self, model, i, d, t):
        return model.B[i, d, t] <= model.bmax_b[i]


    # Condición de borde SOC batería
    def battery_boundary(self, model, i, d):
        tf = self.time_series.get_time_intervals()[-1]
        return model.B[i, d, 0] == model.B[i, d, tf]

    def battery_boundary_swap(self, model, i, d):
        tf = self.time_series.get_time_intervals()[-1]
        return model.B_s[i, d, 0] == model.B[i, d, 0]
    
    # Producción mínima 
    def production(self, model, d, j):
        target = model.m_j[j,d]
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)

        # extracción normal de mineral
        term_de = sum(
        model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
        for i in model.dlhd_set|model.elhd_set for t in model.time_intervals_set
        )
        return term_de >= target
    
    def production_swap(self, model, d, j):
        target = model.m_j[j,d]
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)

        term_de = sum(model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
        for i in model.dlhd_set|model.slhd_set for t in model.time_intervals_set)
        pen = sum(model.Z_pen[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i] * (model.t_swap[i] / model.delta_t)
        for i in model.slhd_set|model.slhd_set for t in model.time_intervals_set)
        return term_de - pen >= target
    
    def aux_zpen_1(self, model, i, j, d, t):
        return sum(model.Z_swap[k, i ,d, t] for k in model.stations_set) - model.Y[i, j ,d, t] - 1 <= model.Z_pen[i, j ,d, t]

    def aux_zpen_2(self, model, i, j, d, t):
        return model.Z_pen[i, j ,d, t] <= sum(model.Z_swap[k, i ,d, t] for k in model.stations_set)

    def aux_zpen_3(self, model, i, j , d, t):
        return model.Z_pen[i, j ,d, t] <= model.Y[i, j ,d, t]    
 

    def production_max(self, model, d, j):
        target = model.m_j[j,d]
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)

        # extracción normal de mineral
        term_de = sum(
        model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
        for i in model.dlhd_set|model.elhd_set for t in model.time_intervals_set
        )
        return term_de <= target *1.3
    
    # Estaciones de carga
    
    #Cantidad máxima de cargadores
    def max_n_chargers(self, model, k):
        return model.N_chargers[k] <= model.max_chargers_k[k] * model.X[k]
    
    #Cantidad máxima de baterías
    def max_n_batteries(self, model, k):
        return model.N_batteries[k] <= model.nk_bat[k] * model.X[k]
    
    #Existencia de la estación
    def station_existence_constraint(self, model, k, i, d, t):
        return model.Z_charge[k,i, d, t] <= model.X[k]
    
    def station_existence_constraint_swap(self, model, k, i, d, t):
        return model.Z_swap[k,i, d, t] <= model.X[k]
    
    #Limite de cargadores
    def charger_limit(self, model,k, d, t):
        return sum(model.Z_charge[k, i, d, t] for i in model.elhd_set) <= model.N_chargers[k]
    
    def charger_limit_swap(self, model,k, d, t, a):
        return sum(model.Sv[k, d, t, a] for a in model.time_intervals_set) <= model.N_chargers[k]
    
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
    
    #def max_power_0_75C(self, model, k, i, d, t):
    #    return model.P[k, i, d, t]  <= model.Z_charge[k, i, d, t] * 353*0.7
    
    #  Sistemas distribución 
    def max_installed_capacity(self, model):
        return sum(model.N_chargers[k] for k in model.stations_set) * model.p_charger  <= model.p_max
    
    def peak_power(self, model, d, t):
        return sum(model.P[k, i, d, t] for k in model.stations_set for i in model.elhd_set) <= model.p_peak
    
    def peak_power_swap(self, model, d, t):
        return sum(model.Sv[k, d, t, a]*model.p_charger for k in model.stations_set for a in model.time_intervals_set) <= model.p_peak
    
    #Condicion inicial estaciones
    def initial_condition_station(self, model):
        return sum(model.X[k] for k in model.stations_set) == 1
    
    # Manejo inventario de baterías para battery swapping
    #def disch_batteries(self, model, k , d ,t):
    #    t0 = self.time_series.get_time_intervals()[0]
    #    tf = self.time_series.get_time_intervals()[-1]
    #    if t<tf:
    #        return  model.X_dch[k,d,t+1] == model.X_dch[k,d,t] - model.X_ini[k,d,t+1] + model.W[k,d,t+1]
    #    else:
    #        return pyo.Constraint.Skip
        
        
    def total_swaps(self, model, k , d ,t):
        return model.W[k,d,t] == sum(model.Z_swap[k, i, d, t] for i in model.slhd_set)
            
    #def charge_batteries(self, model, k, d, t):
    #    tf = self.time_series.get_time_intervals()[-1]
    #    if t<tf:
    #        # Compute the start time for the charging window and map it
    #        # to the nearest valid time-interval in the model.time_intervals_set
    #        charge_offset = float(t - value(model.t_charge_scalar) + 1)
    #        try:
    #            selected_a = min(model.time_intervals_set, key=lambda a: abs(a - charge_offset))
    #        except Exception:
    #            # Fallback: if something goes wrong selecting, skip constraint
    #            return pyo.Constraint.Skip

    #        return model.S[k, d, t+1] == model.S[k, d, t] + model.Sv[k, d, t, selected_a] - model.W[k, d, t+1]
    #    else:
    #        return pyo.Constraint.Skip
        

        

    #def charging_batteries(self, model, k, d, t, a):
    #    tf = self.time_series.get_time_intervals()[-1]
    #    t0 = self.time_series.get_time_intervals()[0]
    #    if t==t0:
    #        return model.Sv[k, d, t0, a] == 0
    #    if t<tf and t>t0:
    #        if a<= t - value(model.t_charge_scalar):
    #            return model.Sv[k, d, t+1, a] == 0
    #        if t - value(model.t_charge_scalar) + 1 <= a and a<=t-1:
    #            return model.Sv[k, d, t+1, a] == model.Sv[k, d, t, a]
    #        if a==t:
    #            return model.Sv[k, d, t+1, a] ==  model.X_ini[k, d, t]
    #        if a>t:
    #            return model.Sv[k, d, t+1, a] == 0 
    #    if t==t0:
    #        return model.Sv[k, d, t0, a] == 0
    #    else:
    #        return pyo.Constraint.Skip    

    #def charging_batteries(self, model, k, d, t, a):
    #    tf = self.time_series.get_time_intervals()[-1]
    #    t0 = self.time_series.get_time_intervals()[0]
    #    Tc = value(model.t_charge_scalar)

    #    if t == t0:
    #        return model.Sv[k, d, t, a] == 0
    #    if t0 <= t < tf:
    #        if a == t:
    #            return model.Sv[k, d, t+1, a] == model.X_ini[k, d, t]
    #        elif t - Tc + 1 <= a <= t - 1:
    #            return model.Sv[k, d, t+1, a] == model.Sv[k, d, t, a]
    #        else: 
    #            return model.Sv[k, d, t+1, a] == 0
    #    return pyo.Constraint.Skip

    # 1. BALANCE DE BATERÍAS DESCARGADAS
    # Corrección: Usar W[t] y X_ini[t] (todo ocurre en el intervalo actual)
    def disch_batteries(self, model, k, d, t):
        tf = self.time_series.get_time_intervals()[-1]
        
        if t < tf:
            # Lógica física:
            # Stock mañana = Stock hoy + (Llegan de swaps) - (Se van a cargar)
            # Antes tenías W[t+1], eso causaba el desajuste de masa.
            return model.X_dch[k,d,t+1] == model.X_dch[k,d,t] + model.W[k,d,t] - model.X_ini[k,d,t]
        else:
            return pyo.Constraint.Skip

    def charge_batteries(self, model, k, d, t):
        tf = self.time_series.get_time_intervals()[-1]
        
        if t < tf:
            # 1. Calculamos cuándo debió empezar a cargar la batería que terminaría HOY.
            #    Fórmula: t_inicio = t_actual - tiempo_carga + 1
            target_start_time = t - value(model.t_charge_scalar) + 1
            
            # 2. BÚSQUEDA ROBUSTA (Fix para el Infeasible):
            #    Buscamos en el set de tiempos si existe ese 'target_start_time' 
            #    usando una tolerancia de 0.001 para ignorar errores de decimales.
            valid_a = [a for a in model.time_intervals_set if abs(a - target_start_time) < 0.001]
            
            if valid_a:
                # Si encontramos el tiempo válido (ej: a=2.0), recuperamos esas baterías
                # Usamos valid_a[0] que es el valor real del set (ej: 2.0)
                finished_charging = model.Sv[k, d, t, valid_a[0]]
            else:
                # Si el tiempo calculado no existe (ej: t=1, target=-2), nadie termina.
                finished_charging = 0

            # 3. Balance de Masa:
            #    Stock(t+1) = Stock(t) + (Las que terminaron) - (Las que se llevan los camiones)
            return model.S[k, d, t+1] == model.S[k, d, t] + finished_charging - model.W[k, d, t]
            
        else:
            return pyo.Constraint.Skip

    # 3. LÓGICA INTERNA DEL CARGADOR (Sv)
    # Corrección: Asegurar que X_ini[t] entra a Sv en el siguiente paso
    def charging_batteries(self, model, k, d, t, a):
        tf = self.time_series.get_time_intervals()[-1]
        t0 = self.time_series.get_time_intervals()[0]
        Tc = value(model.t_charge_scalar)

        # Condición inicial
        if t == t0:
            return model.Sv[k, d, t, a] == 0

        # Paso recursivo
        if t0 <= t < tf:
            if a == t:
                # Caso: Batería acaba de entrar al cargador (viene de X_ini)
                return model.Sv[k, d, t+1, a] == model.X_ini[k, d, t]
            
            elif (t - Tc + 1) <= a <= (t - 1):
                # Caso: Batería sigue cargando (se mantiene)
                return model.Sv[k, d, t+1, a] == model.Sv[k, d, t, a]
            
            else:
                # Caso: Ya terminó o nunca existió
                return model.Sv[k, d, t+1, a] == 0
                
        return pyo.Constraint.Skip
    
    def c_b_charge_batteries(self, model, k, d):
        t0 = self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        return model.S[k, d, t0] == model.S[k, d ,tf]
    
    #def  batteries_inventory(self, model, k , d):
    #    t0 = self.time_series.get_time_intervals()[0]
    #    return model.S[k, d, t0] + model.X_dch[k, d ,t0] == model.N_batteries[k]
    
    def batteries_inventory(self, model, k, d, t):
        # Sumamos todas las baterías que están "encerradas" cargando en este instante 't'
        # (Son todas las que empezaron en algún 'a' y siguen conectadas)
        batteries_charging = sum(model.Sv[k, d, t, a] for a in model.time_intervals_set)
        
        # La suma física total en el instante 't' debe ser IGUAL al total comprado
        return model.S[k, d, t] + model.X_dch[k, d, t] + batteries_charging == model.N_batteries[k]
    
    def max_swaps(self, model, i, d, t):
        return sum(model.Z_swap[k, i ,d, t] for k in model.stations_set) <= 1
    
    # No estan en el modelo en latex 
    def initial_charging_batteries(self, model, k , d):
        t0 = self.time_series.get_time_intervals()[0]
        return model.X_ini[k,d,t0] == 0
    
    def avaible_batteries_for_swap(self, model, k , d ,t):
        return sum(model.Z_swap[k, i ,d, t] for i in model.slhd_set) <= model.S[k,d,t]

    # Link entre baterías descargadas y baterías que inician carga
    def x_ini_availability(self, model, k, d, t):
        return model.X_ini[k, d, t] <= model.X_dch[k, d, t]

    # No se pueden iniciar más cargas simultáneas que cargadores disponibles
    def x_ini_charger_limit(self, model, k, d, t):
        return model.X_ini[k, d, t] <= model.N_chargers[k]

    # Si la estación existe (X[k]=1) debe tener al menos 1 cargador
    def min_n_chargers(self, model, k):
        return model.N_chargers[k] >= model.X[k]

    #pruebsa debug
    def force_zswap_debug(self, model, k, i, d, t):
        """
        Restricción DEBUG: Fuerza swaps en momentos específicos para 
        probar la lógica de inventario (Sv, S, X_ini) de forma aislada.
        """
        # 1. DEFINIR ESCENARIO DE PRUEBA
        # Seleccionamos un camión para la prueba (por ejemplo, los primeros de cada set)
        target_lhd = model.slhd_set.first()   
        target_day = model.days.first()
        
        # 2. DEFINIR MOMENTOS DE SWAP (Time Steps)
        # Ejemplo: Un swap en t=5, otro en t=15 y otro en t=30
        swap_times = [5, 15]

        # 3. APLICAR LÓGICA
        if  i == target_lhd and d == target_day:
            if t in swap_times:
                return model.Z_swap[k, i, d, t] == 1
            else:
                return model.Z_swap[k, i, d, t] == 0
        else:
            # Para cualquier otro camión/estación/día, no hay swaps (limpiamos el ruido)
            return model.Z_swap[k, i, d, t] == 0
    
    def build_all_constraints(self, model):
        model.battery_lower =              pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_lower)
        model.battery_upper =               pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_upper)
        model.battery_boundary =            pyo.Constraint(model.slhd_set, model.days, rule=self.battery_boundary)
       
        model.max_n_chargers                     = pyo.Constraint(model.stations_set, rule=self.max_n_chargers)
        model.max_installed_capacity             = pyo.Constraint(rule=self.max_installed_capacity)
        
        #prueba
        model.initial_condition_station          = pyo.Constraint(rule=self.initial_condition_station)

        #nuevas
        model.max_n_batteries                   = pyo.Constraint(model.stations_set, rule=self.max_n_batteries)
        model.station_existence_constraint_swap      = pyo.Constraint(model.stations_set, model.slhd_set, model.days, model.time_intervals_set, rule=self.station_existence_constraint_swap)
        model.charger_limit_swap                   = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, model.time_intervals_set, rule=self.charger_limit_swap)
        model.battery_soc_swap                       = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap)
        model.battery_soc_swap_update_1                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_1)
        model.battery_soc_swap_update_2                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_2)
        model.battery_soc_swap_update_3                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_3)
        model.battery_soc_swap_update_4                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_4)
        model.peak_power_swap                         = pyo.Constraint(model.days, model.time_intervals_set, rule=self.peak_power_swap)
        model.state_unique_elhd_swap                      = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.state_unique_elhd_swap)
        model.between_shifts_elhd_swap    = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_between_shifts_set, rule=self.between_shifts_elhd_swap)
        
        model.disch_batteries             = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.disch_batteries)
        model.total_swaps                 = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.total_swaps)
        model.charge_batteries            = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.charge_batteries)
        model.charging_batteries           = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, model.time_intervals_set, rule=self.charging_batteries)
        #model.c_b_charge_batteries        = pyo.Constraint(model.stations_set, model.days, rule=self.c_b_charge_batteries)
        #model.batteries_inventory         = pyo.Constraint(model.stations_set, model.days, rule=self.batteries_inventory)
        model.batteries_inventory = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.batteries_inventory)
        model.max_swaps                   = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.max_swaps)
        
        model.production_swap                         = pyo.Constraint(model.days, model.nodes_set, rule=self.production_swap)
        model.aux_zpen_1                         = pyo.Constraint(model.slhd_set, model.nodes_set, model.days, model.time_intervals_set, rule=self.aux_zpen_1)
        model.aux_zpen_2                         = pyo.Constraint(model.slhd_set, model.nodes_set, model.days, model.time_intervals_set, rule=self.aux_zpen_2)
        model.aux_zpen_3                         = pyo.Constraint(model.slhd_set, model.nodes_set, model.days, model.time_intervals_set, rule=self.aux_zpen_3)

        # no estan en el modelo latex
        model.initial_charging_batteries    = pyo.Constraint(model.stations_set, model.days, rule=self.initial_charging_batteries)
        model.avaible_batteries_for_swap    = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.avaible_batteries_for_swap)

        # Restricciones nuevas para enlazar inicios de carga, descargadas y cargadores
        model.x_ini_availability = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.x_ini_availability)
        model.x_ini_charger_limit = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.x_ini_charger_limit)
        model.min_n_chargers = pyo.Constraint(model.stations_set, rule=self.min_n_chargers)

        #prueba debug
        #model.force_zswap_debug = pyo.Constraint(model.stations_set, model.slhd_set, model.days, model.time_intervals_set, rule=self.force_zswap_debug)

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
    
    def lhd_charge_cost_bs(self, model):
        # Coste de cargar baterías (electricidad) con tecnonología BS
        cost_el = sum(
            model.costo_electricidad[d, t] * model.Sv[k, d, t, a] * model.p_charger * model.delta_t
            for k in model.stations_set
            for d in model.days
            for t in model.time_intervals_set
            for a in model.time_intervals_set 
        ) 

        return cost_el*model.scaling_factor_op_cost

    def inversion_cost(self, model):
        cost_inv = sum(
            model.station_cost_k[k] * model.X[k] + model.charger_cost * model.N_chargers[k] 
            + model.battery_cost * model.N_batteries[k]  for k in model.stations_set 
        ) 
        return cost_inv
    
    def total_cost(self, model):
        return self.lhd_charge_cost_bs(model) + self.inversion_cost(model)

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






 