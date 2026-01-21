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

    def _get_pause_definitions(self):
        """Hardcoded definition of pauses (start time, duration, pause_type).

        Times are interpreted in HH:MM:SS, with the optimization horizon
        starting at 08:00. Any start time strictly before 08:00 is assumed
        to belong to the following day (e.g. 02:00, 04:00).
        """
        # start, duration, pause_type
        return [
            ("10:00:00", "2:30:00", "maintenance"),
            ("14:00:00", "1:00:00", "meal"),
            ("16:00:00", "1:30:00", "maintenance"),
            ("22:00:00", "2:30:00", "maintenance"),
            ("2:00:00",  "1:00:00", "meal"),
            ("4:00:00",  "1:30:00", "maintenance"),
        ]

    def _get_time_intervals_for_pause_type(self, pause_type):
        """Return sorted list of time-interval indices for a given pause_type.

        The mapping uses self.time_series.delta_t (in hours) and assumes that
        time interval 0 corresponds to 08:00. Interval t represents the
        half-open period [t, t+1) in index space, i.e. the t-th delta_t slot
        after 08:00.
        """
        # Base time: 08:00
        base_minutes = 8 * 60
        dt_minutes = int(round(self.time_series.delta_t * 60))
        if dt_minutes <= 0:
            raise ValueError("delta_t must be positive")

        # Max index in the time horizon
        max_t = max(self.time_series.time_intervals)

        def _parse_hhmmss(s):
            parts = s.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            return 60 * h + m

        indices = set()
        for start_str, dur_str, ptype in self._get_pause_definitions():
            if ptype != pause_type:
                continue

            start_min = _parse_hhmmss(start_str)
            dur_min = _parse_hhmmss(dur_str)

            # Times before 08:00 are interpreted as next day
            if start_min < base_minutes:
                start_min += 24 * 60

            offset = start_min - base_minutes
            if offset < 0:
                continue

            # t = 1 corresponds to [08:00, 08:00 + delta_t)
            start_idx = int(offset // dt_minutes) + 1
            n_intervals = int(math.ceil(float(dur_min) / dt_minutes))

            for t in range(start_idx, start_idx + n_intervals):
                if 1 <= t <= max_t:
                    indices.add(t)

        return sorted(indices)

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
        # Tramos de penalización para déficit F (piecewise lineal)
        model.F_SEG = pyo.Set(initialize=[1, 2, 3, 4, 5])

         # Nuevos subsets de tiempo para pausas de comida y mantenimiento
        model.time_intervals_meal_set = pyo.Set(
            initialize=self._get_time_intervals_for_pause_type("meal")
        )
        model.time_intervals_maintenance_set = pyo.Set(
            initialize=self._get_time_intervals_for_pause_type("maintenance")
        )


class OptParameters(OptRules):

    def build_parameters(self, model):
        #Parámetros temporales
        model.delta_t = pyo.Param(initialize=self.time_series.delta_t, mutable=True)
        model.t_ini = pyo.Param(initialize=self.time_series.get_time_intervals()[0], mutable=True)
        model.t_fin = pyo.Param(initialize=self.time_series.get_time_intervals()[-1], mutable=True)
        #Parámetros económicos
        model.m_j = pyo.Param(model.nodes_set,model.days, initialize={(j, d): self.time_series.get_extraction_goal(j, d)for j in model.nodes_set for d in model.days},mutable=True)
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
        # Capacidad de pala
        model.g_i    = pyo.Param(model.lhd_set,                   initialize={i: self.mine_system.elhd.get_load_capacity(i)       for i in model.lhd_set}, mutable=False)
        model.filling_factor = pyo.Param(model.lhd_set,        initialize={i: self.mine_system.elhd.get_filling_factor(i)      for i in model.lhd_set}, mutable=False)
         
        #Parámetros problema de inversión
        model.p_charger = pyo.Param(initialize=self.mine_system.chargers.get_charger_power(), mutable=False)
        model.p_max_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_p_max_ssee(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.p_peak = pyo.Param(initialize=self.mine_system.chargers.get_p_peak_dist(), mutable=False)
        model.charger_cost = pyo.Param(initialize=self.mine_system.chargers.get_charger_cost(), mutable=False)
        model.scaling_factor_op_cost = pyo.Param(initialize=self.time_series.scaling_factor_op_cost, mutable=True)
        #Parametros estaciones de carga
        model.station_cost_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_station_cost(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.distance_to_dn_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_distance_to_discharge_node(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.max_chargers_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_chargers(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.man_time_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_maneuvering_time(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.pk_i   = pyo.Param( model.stations_set, model.elhd_set, initialize={(k,i):self.mine_system.elhd.engine_energy_charge_travel(self.mine_system.stations.get_distance_to_discharge_node(k),i,0) for k in model.stations_set for i in model.elhd_set}, mutable=False)
        model.t_ttc_i   = pyo.Param( model.stations_set, model.elhd_set, initialize={(k,i):self.mine_system.elhd.time_charge_station(self.mine_system.stations.get_distance_to_discharge_node(k),i) for k in model.stations_set for i in model.elhd_set}, mutable=False)

         # ---- Penalización por tramos para F (déficit) ----
        # Tramos: 0-5, 5-10, 10-50, 50-100, 100+
        # Costo unitario por tramo: Voll / divisor
        # Nota: para tramo 5 (100+) se usa Voll/0.1 (más caro).
        model.F_penalty_div = pyo.Param(model.F_SEG,initialize={1: 1000.0, 2: 100.0, 3: 10.0, 4: 1.0, 5: 0.1},mutable=True)
        # Capacidad (longitud) de cada tramo
        model.F_penalty_cap = pyo.Param(model.F_SEG,initialize={1: 5.0, 2: 5.0, 3: 40.0, 4: 50.0, 5: 1e18},mutable=True)
        model.Voll = pyo.Param(initialize=1000, mutable=True)

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
    
    def F(self, model, j, d):
        return (0, None)

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
        model.P         = pyo.Var(model.stations_set,model.elhd_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        # SOC de batería b al final de (d,t)
        model.B         = pyo.Var(model.elhd_set, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        #Cantidad de cargadores
        model.N_chargers= pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers)
        #Elección estación de carga
        model.X = pyo.Var(model.stations_set, domain=pyo.Binary)
        #Inicio de una carga on-board
        model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)    
        # Indica si termina una carga en t
        model.EndCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)
        
        ## NUEVAS VARIABLES 

        #extracción total del equipo i en el día d.
        model.M = pyo.Var(model.lhd_set, model.nodes_set, model.days, domain=pyo.NonNegativeReals)
        
        # Holgura producción 
        model.F = pyo.Var(model.nodes_set, model.days, bounds=self.F)
        
        # Descomposición de F en tramos para costo piecewise lineal
        model.F_seg = pyo.Var(model.nodes_set, model.days, model.F_SEG, domain=pyo.NonNegativeReals)

        #Asignación estación por macrobloque
        #model.U = pyo.Var(model.stations_set, model.elhd_set, domain=pyo.Binary)

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
        charge = sum(model.P[k, i, d, t] * (model.delta_t-model.man_time_k[k]-model.t_ttc_i[k,i]) for k in model.stations_set)
        discharge = sum(
            model.Y[i, j, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
        )
        penalization_charge = 2*sum(model.StartCharge[k, i, d, t] * model.pk_i[k,i]*model.t_ttc_i[k,i] for k in model.stations_set)
        

        if t == t0:
            return model.B[i, d, t] == model.B[i, d, 0] + charge - discharge - penalization_charge
        else:
            return model.B[i, d, t] == model.B[i, d, t - 1] + charge - discharge - penalization_charge
        
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
    
    # Producción
    def production(self, model, d, j):
        target = model.m_j[j, d]
        # Solo sobre llaves EXISTENTES de Y para (d, j)
        term_de = sum(
            model.Y[i2, j, d, t2] * model.g_i[i2] * self.time_series.get_n_trips(j, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_de  >= target

    def max_production(self, model, d, j):
        target = model.m_j[j, d]
        # Solo sobre llaves EXISTENTES de Y para (d, j)
        term_de = sum(
            model.Y[i2, j, d, t2] * model.g_i[i2] * self.time_series.get_n_trips(j, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_de + model.F[j, d] <= target*2


    def daily_extraction_M(self, model, i, j, d):
        """
        M[i,d] = extracción total del equipo i en el día d.
        Misma unidad que el término de producción (g_i * n_trips * f_i).
        """
        term = sum(
            model.Y[i2, j2, d2, t2] * model.g_i[i2]
            * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if i2 == i and d2 == d and j2 == j
        )
        return model.M[i, j, d] == term

     # Penalización por tramos (piecewise) para F
    # --------------------------
    def F_piecewise_balance(self, model, j, d):
        # Sumatoria de tramos debe reconstruir F
        return sum(model.F_seg[j, d, s] for s in model.F_SEG) == model.F[j, d]

    def F_piecewise_caps(self, model, j, d, s):
        # Cada tramo tiene una "longitud" máxima
        return model.F_seg[j, d, s] <= model.F_penalty_cap[s]


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
    
    #Asignación estaciones de carga por macrobloque
    def stations_assignment(self, model, k, i, d, t):
        return model.Z_charge[k, i ,d ,t] <= model.U[k,i]
    
    def one_station_per_lhd(self, model, i):
        return sum(model.U[k,i] for k in model.stations_set) == 1
    
    def assignation_station_only_existing(self, model, k, i):
        return model.U[k,i] <= model.X[k]
    
    def macroblock_station_assignment(self, model, k, i):
        import re 
        import pyomo.environ as pyo

        # 1. IDENTIFICAR ID DEL LHD (i es string "LH518B_1")
        match_lhd = re.search(r'(\d+)$', str(i))
        if match_lhd:
            lhd_id = int(match_lhd.group(1)) # Extrae el 1
        else:
            return pyo.Constraint.Skip

        # 2. IDENTIFICAR ID DE LA ESTACIÓN (k es string "station_1")
        station_id = k 
        if isinstance(k, str):
            match_station = re.search(r'(\d+)$', k)
            if match_station:
                station_id = int(match_station.group(1)) # Extrae el 1
        
        # 3. LOGICA
        target_station = None
        if lhd_id in [1, 2, 3, 4]:
            target_station = 1
        elif lhd_id in [5, 6, 7, 8]:
            target_station = 2
        elif lhd_id in [9, 10, 11, 12]:
            target_station = 3
            
        # 4. RESTRICCIÓN
        if target_station is not None:
            if station_id == target_station:
                return model.U[k, i] == 1
            else:
                return model.U[k, i] == 0
        
        return pyo.Constraint.Skip
    
    #  Sistemas distribución 
    def max_installed_capacity(self, model, k, d, t):
        return sum(model.P[k, i, d, t] for i in model.elhd_set)   <= model.p_max_k[k]

    def peak_power(self, model, d,t):
        return sum(model.P[k, i, d, t] for k in model.stations_set for i in model.elhd_set) <= model.p_peak
    
    #Condicion inicial estaciones
    def initial_condition_station(self, model):
        return sum(model.X[k] for k in model.stations_set) == 1
    
    # Detenciones 

     # Pausas: MEAL
    # --------------------------
    def meal_stop_all(self, model, i, d, t):
        """En intervalos MEAL todos los LHD deben estar detenidos
        (estacionados, cargando o swapeando según tecnología)."""
        if t not in model.time_intervals_meal_set:
            return pyo.Constraint.Skip

        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip

        # Prohibimos viajes: solo Z / Z_charge / Swap quedan posibles
        return sum(model.Y[i, j, d, t] for j in nodes) == 0

    # --------------------------
    # Pausas: MAINTENANCE
    # --------------------------
    def maint_stop_all(self, model, i, d, t):
        """En intervalos de mantenimiento TODOS los LHD deben
        estar estacionados (Z = 1)."""
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip

        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip

        # Sin viajes → por state_unique_* esto fuerza Z = 1
        return sum(model.Y[i, j, d, t] for j in nodes) == 0

    def maint_no_charge_elhd(self, model, k, i, d, t):
        """En mantenimiento los ELHD no pueden estar cargando."""
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z_charge[k, i, d, t] == 0
    
    def build_all_constraints(self, model):
        model.state_unique_elhd                      = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.state_unique_elhd)
        model.between_shifts_elhd                    = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_between_shifts_set, rule=self.between_shifts_elhd)

        model.battery_soc                       = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_soc)
        model.battery_lower = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_lower)
        model.battery_upper = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_upper)
        model.battery_boundary                  = pyo.Constraint(model.elhd_set, model.days, rule=self.battery_boundary)

        #nuevas
        model.max_n_chargers                     = pyo.Constraint(model.stations_set, rule=self.max_n_chargers)
        model.station_existence_constraint       = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.station_existence_constraint)
        model.charger_limit                   = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.charger_limit)
        model.charge_state                       = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.charge_state)
        model.max_power                          = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.max_power)

        model.max_installed_capacity             = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.max_installed_capacity)
        model.peak_power                         = pyo.Constraint(model.days, model.time_intervals_set, rule=self.peak_power)
        
        #Producción nuevas
        model.production         = pyo.Constraint(model.days, model.nodes_set, rule=self.production)
        model.daily_extraction_M= pyo.Constraint(model.elhd_set, model.nodes_set, model.days, rule=self.daily_extraction_M)

        # Penalización por tramos para F (piecewise lineal)
        model.F_piecewise_balance = pyo.Constraint(model.nodes_set, model.days, rule=self.F_piecewise_balance)
        model.F_piecewise_caps = pyo.Constraint(model.nodes_set, model.days, model.F_SEG,rule=self.F_piecewise_caps)

        # Asignación estación por macrobloque
        #model.stations_assignment = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.stations_assignment)
        #model.one_station_per_lhd = pyo.Constraint(model.elhd_set, rule=self.one_station_per_lhd)
        #model.assignation_station_only_existing = pyo.Constraint(model.stations_set, model.elhd_set, rule=self.assignation_station_only_existing)
        #model.macroblock_station_assignment = pyo.Constraint(model.stations_set, model.elhd_set, rule=self.macroblock_station_assignment)
        
        #Detenciones 
        model.meal_stop_all = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.meal_stop_all)
        model.maintenance_stop_all = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.maint_stop_all)
        model.maintenance_no_charge_elhd = pyo.Constraint(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, rule=self.maint_no_charge_elhd)

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
    
    def F_penalty_cost(self, model):
        cost_F = sum(
            model.F_seg[j, d, s] * (model.Voll / model.F_penalty_div[s])
            for j in model.nodes_set
            for d in model.days
            for s in model.F_SEG
        )
        return cost_F*model.scaling_factor_op_cost
    
    def total_cost(self, model):
        return self.lhd_charge_cost(model) + self.inversion_cost(model)+ self.F_penalty_cost(model)
    
    def production_total(self, model, j):
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)

        # extracción normal de mineral
        term_de = sum(
        model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
        for i in model.dlhd_set|model.elhd_set for t in model.time_intervals_set
        for d in model.days)
        return term_de*model.scaling_factor_op_cost

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






