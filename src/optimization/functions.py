import pyomo.environ as pyo
import math
import pandas as pd
import numpy as np
import time
import re
from pyomo.environ import quicksum, value

class OptRules(object):

    def __init__(self, mine_system,  time_series):
        self.mine_system = mine_system
        self.time_series = time_series
        self.time_series.get_node_assignment(mine_system.get_system_lhds())
        self.time_series.get_elhd_at_node(mine_system.get_system_nodes())
        self.time_series.get_station_assignment(mine_system.get_system_lhds())
        self.time_series.get_elhd_at_station(mine_system.get_system_stations())



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
    
    
    def _extract_lhd_numeric_suffix(self, lhd_name):
        """Return the trailing integer in an LHD name, or None if absent."""
        match = re.search(r"(\d+)$", str(lhd_name))
        return int(match.group(1)) if match else None

    def _get_pause_definitions(self):
        """Hardcoded pauses as (start_time, end_time, pause_type) in HH:MM.

        The optimization horizon starts at 08:00.
        Times strictly before 08:00 are interpreted as next day (e.g., 04:00).
        If end_time is earlier than start_time, the pause crosses midnight (e.g., 22:00 -> 00:30).
        """
        pauses = [
            # Mantenciones forzadas
            ("10:04", "12:26", "maintenance"),
            ("16:04", "17:34", "maintenance"),
            ("22:04", "00:26", "maintenance"),
            ("04:04", "05:34", "maintenance"),

            # Colación común para todas las tecnologías: ~60 min (delta_t = 8 min)
            ("14:04", "15:04", "meal"),
            ("02:04", "03:04", "meal"),
        ]

        return pauses

    def _split_contiguous_blocks(self, intervals):
        if not intervals:
            return []

        blocks = [[intervals[0]]]
        for t in intervals[1:]:
            if t == blocks[-1][-1] + 1:
                blocks[-1].append(t)
            else:
                blocks.append([t])
        return blocks

    def _split_meal_blocks(self, meal_intervals):
        meal_g1 = []
        meal_g2 = []

        for block in self._split_contiguous_blocks(meal_intervals):
            mid = len(block) // 2
            meal_g1.extend(block[:mid])
            meal_g2.extend(block[mid:])

        return meal_g1, meal_g2



    def _get_time_intervals_for_pause_type(self, pause_type):
        """
        Return sorted list of time-interval indices for a given pause_type.

        Assumes:
        - t=1 corresponds to [DAY_START, DAY_START + delta_t)
        - self.time_series.delta_t is in hours
        - pause definitions are (start_hhmm, end_hhmm, pause_type)
        """
        # Ajusta esto a tu inicio real del horizonte (09:00 según tu comentario)
        base_minutes = 9 * 60

        dt_minutes = int(round(self.time_series.delta_t * 60))
        if dt_minutes <= 0:
            raise ValueError("delta_t must be positive")
        if 1440 % dt_minutes != 0:
            raise ValueError("delta_t must divide 1440 minutes exactly")

        max_t = int(max(self.time_series.time_intervals))

        def _parse_hhmm(s: str) -> int:
            parts = s.strip().split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            return 60 * h + m

        indices = set()

        for start_str, end_str, ptype in self._get_pause_definitions():
            if ptype != pause_type:
                continue

            start_min = _parse_hhmm(start_str)
            end_min   = _parse_hhmm(end_str)

            # Interpretar horas < base como "día siguiente" dentro del ciclo 24h del horizonte
            if start_min < base_minutes:
                start_min += 24 * 60
            if end_min < base_minutes:
                end_min += 24 * 60

            # Si el fin quedó antes (o igual) que el inicio => cruza medianoche
            if end_min <= start_min:
                end_min += 24 * 60

            # Ventana en minutos desde el inicio del horizonte
            a = start_min - base_minutes
            b = end_min - base_minutes

            # Marcar intervalos t que se SOLAPAN con [a,b)
            for t in range(1, max_t + 1):
                s = (t - 1) * dt_minutes
                e = t * dt_minutes
                if max(s, a) < min(e, b):
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

         # Subsets de tiempo para pausas
        meal_intervals = self._get_time_intervals_for_pause_type("meal")
        meal_g1_intervals, meal_g2_intervals = self._split_meal_blocks(meal_intervals)

        model.time_intervals_meal_set = pyo.Set(
            initialize=meal_intervals
        )
        model.time_intervals_meal_g1_set = pyo.Set(
            initialize=meal_g1_intervals
        )
        model.time_intervals_meal_g2_set = pyo.Set(
            initialize=meal_g2_intervals
        )
        model.time_intervals_maintenance_set = pyo.Set(
            initialize=self._get_time_intervals_for_pause_type("maintenance")
        )



        
        # -----------------------------
        # Grupos de colación para todos los LHD:
        # Grupo 1 = sufijo par, Grupo 2 = sufijo impar.
        # Si algún nombre no trae sufijo numérico, se reparte en fallback para
        # no dejar equipos fuera de ambos grupos.
        # -----------------------------
        all_lhds = sorted(set(self.mine_system.get_system_lhds()))

        group1 = []
        group2 = []
        fallback = []

        for lhd_name in all_lhds:
            suffix = self._extract_lhd_numeric_suffix(lhd_name)
            if suffix is None:
                fallback.append(lhd_name)
            elif suffix % 2 == 0:
                group1.append(lhd_name)
            else:
                group2.append(lhd_name)

        split_fallback = (len(fallback) + 1) // 2
        group1.extend(fallback[:split_fallback])
        group2.extend(fallback[split_fallback:])

        model.meal_group1_set = pyo.Set(initialize=sorted(set(group1)))
        model.meal_group2_set = pyo.Set(initialize=sorted(set(group2)))


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
          # Índice esparso para carga: solo (station, elhd) válidas según StationAssignment
        def _init_ZCHARGE_INDEX(m):
            for elhd in m.elhd_set:
                station_list = self.time_series.mapper['Stations_per_elhd'].get(elhd, [])
                for k in station_list:
                    yield (k, elhd)
        model.ZCHARGE_INDEX = pyo.Set(dimen=2, initialize=_init_ZCHARGE_INDEX)   
        def _ZCHARGE_DAYS_TIME(m):
            for (k, i) in m.ZCHARGE_INDEX:
                for d in m.days:
                    for t in m.time_intervals_set:
                        yield (k, i, d, t)
        model.ZCHARGE_DAYS_TIME_INDEX = pyo.Set(dimen=4, initialize=_ZCHARGE_DAYS_TIME)
         # Y(i,j,d,t) solo si j ∈ Nodes_assigned_at_interval(d,t,i)
        def _init_Y_INDEX(m):
            for d in m.days:
                for t in m.time_intervals_set:
                    for i in m.lhd_set:
                        node_list = self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
                        for j in node_list:
                            yield (i, j, d, t)
        # Sets de índices (solo contienen tuplas válidas)
        model.Y_INDEX  = pyo.Set(dimen=4, initialize=_init_Y_INDEX)

        # Viaje completo de ida por nodo — SOLO en tuplas válidas
        model.Y = pyo.Var(model.Y_INDEX, domain=pyo.Binary)
        # Estado ON/OFF de cada LHD
        model.Z         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z, domain=pyo.Binary)
        # Indicador de carga eléctrica (por batería) en cada intervalo
        model.Z_charge  = pyo.Var(model.ZCHARGE_INDEX, model.days, model.time_intervals_set,
                                  bounds=self.Z_charge, domain=pyo.Binary)
        # Potencia de carga de batería b en (d,t)
        model.P         = pyo.Var(model.ZCHARGE_INDEX, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
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
        # Suma Z_charge solo sobre estaciones válidas para este elhd
        z_charge_sum = sum(model.Z_charge[k,i,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        return model.Z[i, d, t] + z_charge_sum + sum(model.Y[i, j, d, t] for j in nodes) == 1

    # (C5) Estado entre turnos: Zi,t = 1 for t in Tbs
    def between_shifts_elhd(self, model, i, d, t):
        # Suma Z_charge solo sobre estaciones válidas para este elhd
        z_charge_sum = sum(model.Z_charge[k,i,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        return model.Z[i, d, t] + z_charge_sum == 1

    def battery_soc(self, model, i, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        # Suma P solo sobre estaciones válidas para este elhd
        charge = sum(model.P[k, i, d, t] * (model.delta_t-model.man_time_k[k]-model.t_ttc_i[k,i]) for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        discharge = sum(
            model.Y[i, j, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
        )
        # Suma StartCharge solo sobre estaciones válidas para este elhd
        penalization_charge = 2*sum(model.StartCharge[k, i, d, t] * model.pk_i[k,i]*model.t_ttc_i[k,i] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        

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

        return term_de + model.F[j, d] >= target

    def max_production(self, model, d, j):
        target = model.m_j[j, d]
        # Solo sobre llaves EXISTENTES de Y para (d, j)
        term_de = sum(
            model.Y[i2, j, d, t2] * model.g_i[i2] * self.time_series.get_n_trips(j, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_de  <= target*2.5


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
        # Suma Z_charge solo sobre elhds válidos para esta estación
        return sum(model.Z_charge[k, i, d, t] for (k2, i) in model.ZCHARGE_INDEX if k2 == k) <= model.N_chargers[k]
    
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
    
    #  Sistemas distribución 
    def max_installed_capacity(self, model, k, d, t):
        # Suma P solo sobre elhds válidos para esta estación
        station_elhds = [i for (k2, i) in model.ZCHARGE_INDEX if k2 == k]
        if not station_elhds:
            return pyo.Constraint.Skip
        return sum(model.P[k, i, d, t] for i in station_elhds) <= model.p_max_k[k]

    def peak_power(self, model, d,t):
        # Suma P solo sobre tuplas válidas en ZCHARGE_INDEX
        return sum(model.P[k, i, d, t] for (k, i) in model.ZCHARGE_INDEX) <= model.p_peak
    
    #Condicion inicial estaciones
    def initial_condition_station(self, model):
        return sum(model.X[k] for k in model.stations_set) == 1
    
    # Detenciones 

     # Pausas: MEAL
    # --------------------------
    def meal_g1_no_travel_group1(self, model, i, d, t):
        """En meal_g1 el Grupo 1 no puede viajar."""
        if t not in model.time_intervals_meal_g1_set:
            return pyo.Constraint.Skip
        if i not in model.meal_group1_set:
            return pyo.Constraint.Skip

        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return sum(model.Y[i, j, d, t] for j in nodes) == 0

    def meal_g2_no_travel_group2(self, model, i, d, t):
        """En meal_g2 el Grupo 2 no puede viajar."""
        if t not in model.time_intervals_meal_g2_set:
            return pyo.Constraint.Skip
        if i not in model.meal_group2_set:
            return pyo.Constraint.Skip

        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return sum(model.Y[i, j, d, t] for j in nodes) == 0

    # --------------------------
    # Pausas: MAINTENANCE
    # --------------------------
    def maint_stop_all(self, model, i, d, t):
        """En intervalos de mantenimiento TODOS los LHD deben
        estar estacionados (Z = 1). En mantenimiento los ELHD no pueden estar cargando."""
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] == 1
    
    # Fijar cantidad de cargadores 
    def fixed_n_chargers(self, model, k):
        if k == "station_1":
            return model.N_chargers[k] == 1
        elif k == "station_2":
            return model.N_chargers[k] == 1
        elif k == "station_3":
            return model.N_chargers[k] == 1
        
    
    def build_all_constraints(self, model):
        model.state_unique_elhd                      = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.state_unique_elhd)
        model.between_shifts_elhd                    = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_between_shifts_set, rule=self.between_shifts_elhd)

        model.battery_soc                       = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_soc)
        model.battery_lower =                     pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_lower)
        model.battery_upper =                     pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.battery_upper)
        model.battery_boundary                  = pyo.Constraint(model.elhd_set, model.days, rule=self.battery_boundary)

        #nuevas
        model.max_n_chargers                     = pyo.Constraint(model.stations_set, rule=self.max_n_chargers)
        model.station_existence_constraint       = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.station_existence_constraint)
        model.charger_limit                      = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.charger_limit)
        model.charge_state                       = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.charge_state)
        model.max_power                          = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.max_power)

        model.max_installed_capacity             = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.max_installed_capacity)
        model.peak_power                         = pyo.Constraint(model.days, model.time_intervals_set, rule=self.peak_power)
        
        #Producción nuevas
        model.production         = pyo.Constraint(model.days, model.nodes_set, rule=self.production)
        model.daily_extraction_M= pyo.Constraint(model.elhd_set, model.nodes_set, model.days, rule=self.daily_extraction_M)

        # Penalización por tramos para F (piecewise lineal)
        model.F_piecewise_balance = pyo.Constraint(model.nodes_set, model.days, rule=self.F_piecewise_balance)
        model.F_piecewise_caps = pyo.Constraint(model.nodes_set, model.days, model.F_SEG,rule=self.F_piecewise_caps)

        #Detenciones 
        model.meal_g1_no_travel_group1 = pyo.Constraint(model.lhd_set, model.days, model.time_intervals_set, rule=self.meal_g1_no_travel_group1)
        model.meal_g2_no_travel_group2 = pyo.Constraint(model.lhd_set, model.days, model.time_intervals_set, rule=self.meal_g2_no_travel_group2)
        model.maintenance_stop_all = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.maint_stop_all)

        #fijar cantidad de cargadores
        model.fixed_n_chargers = pyo.Constraint(model.stations_set, rule=self.fixed_n_chargers)

class ObjectiveRules(OptRules):

    def lhd_charge_cost(self, model):
        # Coste de cargar baterías (electricidad)
        cost_el = sum(
            model.P[k, i, d, t] * model.costo_marginal[i, d, t] 
            for (k, i) in model.ZCHARGE_INDEX
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
    
    def op_cost_total(self, model):
        # Coste operativo total (sin inversión)
        return (self.lhd_charge_cost(model) + self.F_penalty_cost(model))/model.scaling_factor_op_cost
    
    #def production_total(self, model, j):
    #    def ntr(node,i):
    #        return self.time_series.get_n_trips(node,i)

        # extracción normal de mineral
    #    term_de = sum(
    #    model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
    #    for i in model.dlhd_set|model.elhd_set for t in model.time_intervals_set
    #    for d in model.days)
    #    return term_de*model.scaling_factor_op_cost

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






