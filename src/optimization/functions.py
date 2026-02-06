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
        """Hardcoded pauses as (start_time, end_time, pause_type) in HH:MM.

        The optimization horizon starts at 08:00.
        Times strictly before 08:00 are interpreted as next day (e.g., 04:00).
        If end_time is earlier than start_time, the pause crosses midnight (e.g., 22:00 -> 00:30).
        """
        return [
            # Mantenciones (las que me dijiste)
            ("10:04", "12:26", "maintenance"),
            ("16:04", "17:26", "maintenance"),
            ("22:04", "00:26", "maintenance"),
            ("04:04", "05:26", "maintenance"),

            ("14:04", "14:56", "meal"),
            ("02:04", "02:56", "meal"),
        ]



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
        model.slhd_set = pyo.Set(initialize=self.mine_system.get_swap_lhds())
        model.nodes_set = pyo.Set(initialize=self.mine_system.get_system_nodes())
        model.time_intervals_set = pyo.Set(initialize=self.time_series.time_intervals)
        model.days = pyo.Set(initialize=self.time_series.days)
        model.shifts = pyo.Set(initialize=self.time_series.shifts)
        model.time_intervals_set_zero = pyo.Set(initialize=[0] + list(self.time_series.time_intervals))
        model.time_intervals_between_shifts_set = pyo.Set(initialize=self.time_series.get_intervals_between_shifts())
        model.stations_set = pyo.Set(initialize=self.mine_system.get_system_stations())
         # Nuevos subsets de tiempo para pausas de comida y mantenimiento
        model.time_intervals_meal_set = pyo.Set(initialize=self._get_time_intervals_for_pause_type("meal"))
        model.time_intervals_maintenance_set = pyo.Set(initialize=self._get_time_intervals_for_pause_type("maintenance"))
        # Tramos de penalización para déficit F (piecewise lineal)
        model.F_SEG = pyo.Set(initialize=[1, 2, 3, 4, 5])


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
        model.p_max_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_p_max_ssee(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
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
        #model.t_charge = pyo.Param(model.lhd_set, initialize={i: self.mine_system.elhd.get_charge_time(i) for i in model.lhd_set}, mutable=False)
        model.t_charge = 9

        # ---- Penalización por tramos para F (déficit) ----
        # Tramos: 0-5, 5-10, 10-50, 50-100, 100+
        # Costo unitario por tramo: Voll / divisor
        # Nota: para tramo 5 (100+) se usa Voll/0.1 (más caro).
        model.F_penalty_div = pyo.Param(
            model.F_SEG,
            initialize={1: 1000.0, 2: 100.0, 3: 10.0, 4: 1.0, 5: 0.1},
            mutable=True
        )

        # Capacidad (longitud) de cada tramo
        model.F_penalty_cap = pyo.Param(
            model.F_SEG,
            initialize={1: 5.0, 2: 5.0, 3: 40.0, 4: 50.0, 5: 1e18},
            mutable=True
        )

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

    def build_all_variables(self, model):
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
        # Variable binaria que indica si hay penalidad en la producción por swap
        model.Z_pen = pyo.Var(model.Y_INDEX, domain=pyo.NonNegativeReals)
        #Viaje completo de LHD i al nodo j en (d,t)
        model.Y = pyo.Var(model.Y_INDEX, bounds=self.Y, domain=pyo.Binary)
        # Variable binaria que indica si el LHD esta inactivo o no
        model.Z         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z, domain=pyo.Binary)
        # Variable binaria que indica si el LHD está cargando
        model.Z_charge  = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z_charge, domain=pyo.Binary)
        # Variable binaria que indica si el LHD reliza un swap 
        model.Z_swap   = pyo.Var(model.stations_set, model.slhd_set, model.days, model.time_intervals_set,
                             domain=pyo.Binary)
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

        model.M = pyo.Var(
            model.slhd_set,
            model.nodes_set,
            model.days,
            domain=pyo.NonNegativeReals
        )

        model.F = pyo.Var(
            model.nodes_set, model.days,
            domain=pyo.NonNegativeReals
        )


        # Descomposición de F en tramos para costo piecewise lineal
        model.F_seg = pyo.Var(
            model.nodes_set, model.days, model.F_SEG,
            domain=pyo.NonNegativeReals
        )
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

     # Definicion de B_s actualización del estado de carga con swap    
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
    def production_swap(self, model, d, j):
        target = model.m_j[j,d]
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)

        term_de = sum(model.Y[i, j, d, t] * model.g_i[i] * ntr(j, i) * model.filling_factor[i]
                  for (i, j2, d2, t) in model.Y_INDEX if j2 == j and d2 == d)
    
        pen = sum(model.Z_pen[i, j, d, t] * model.g_i[i] * ntr(j, i) * model.filling_factor[i] * (model.t_swap[i] / model.delta_t)
                  for (i, j2, d2, t) in model.Y_INDEX if j2 == j and d2 == d)
    
        return term_de - pen + model.F[j, d] >= target
    
    def production_max(self, model, d, j):
        target = model.m_j[j,d]
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)

        term_de = sum(model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
        for i in model.slhd_set for t in model.time_intervals_set)
        pen = sum(model.Z_pen[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i] * (model.t_swap[i] / model.delta_t)
        for i in model.slhd_set for t in model.time_intervals_set)
        return term_de - pen + model.F[j, d] <= target*1.3
    
    def aux_zpen_1(self, model, i, j, d, t):
        return sum(model.Z_swap[k, i ,d, t] for k in model.stations_set) - model.Y[i, j ,d, t] - 1 <= model.Z_pen[i, j ,d, t]

    def aux_zpen_2(self, model, i, j, d, t):
        return model.Z_pen[i, j ,d, t] <= sum(model.Z_swap[k, i ,d, t] for k in model.stations_set)

    def aux_zpen_3(self, model, i, j , d, t):
        return model.Z_pen[i, j ,d, t] <= model.Y[i, j ,d, t]    
 

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
    
    # --------------------------
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
    
    #Cantidad máxima de baterías
    def max_n_batteries(self, model, k):
        return model.N_batteries[k] <= model.nk_bat[k] * model.X[k]
    
    #def max_n_batteries(self, model, k):
    #    return model.N_batteries[k] == 2 * model.X[k]
    
    #Existencia de la estación
    def station_existence_constraint(self, model, k, i, d, t):
        return model.Z_charge[k,i, d, t] <= model.X[k]
    
    def station_existence_constraint_swap(self, model, k, i, d, t):
        return model.Z_swap[k,i, d, t] <= model.X[k]
    
    #Limite de cargadores
    def charger_limit(self, model,k, d, t):
        return sum(model.Z_charge[k, i, d, t] for i in model.elhd_set) <= model.N_chargers[k]
    
    def charger_limit_swap(self, model, k, d, t):
        # Para cada estación k, día d y intervalo t, la suma de baterías
        # conectadas (para todos los inicios a) en t no puede exceder los cargadores
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
    def max_installed_capacity_swap(self, model, k, d, t):
        return sum(model.Sv[k, d, t, a]*model.p_charger for a in model.time_intervals_set)  <= model.p_max_k[k]
    
    def peak_power(self, model, d, t):
        return sum(model.P[k, i, d, t] for k in model.stations_set for i in model.elhd_set) <= model.p_peak
    
    def peak_power_swap(self, model, d, t):
        return sum(model.Sv[k, d, t, a]*model.p_charger for k in model.stations_set for a in model.time_intervals_set) <= model.p_peak
        
    #Manejo de inventario de baterías            
    def total_swaps(self, model, k, d, t):
        return model.W[k, d, t] == sum(model.Z_swap[k, i ,d, t] for i in model.slhd_set)
    
    def c_b_charge_batteries(self, model, k, d):
        t0 = self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        return model.S[k, d, t0] == model.S[k, d ,tf]
    
    def batteries_inventory(self, model, d, t):
        batteries_charging = sum(model.Sv[k, d, t, a] for a in model.time_intervals_set for k in model.stations_set)
        return sum(model.S[k, d, t] for k in model.stations_set) + sum(model.X_dch[k, d, t] for k in model.stations_set) + batteries_charging == sum(model.N_batteries[k] for k in model.stations_set)
    
    def initial_batteries_discharged(self, model, k , d):
        t0 = self.time_series.get_time_intervals()[0]
        return model.X_dch[k,d,t0] == model.N_batteries[k]

    def max_swaps(self, model, i, d, t):
        return sum(model.Z_swap[k, i ,d, t] for k in model.stations_set) <= 1
    
    # Inventario de baterías descargadas
    def inventory_discharged_batteries_rule(self, model, k, d, t):
        t0= self.time_series.get_time_intervals()[0]
        if t == t0:
            return pyo.Constraint.Skip
        else:
            return model.X_dch[k, d, t] == model.X_dch[k, d, t-1] - model.X_ini[k, d, t-1] + model.W[k, d, t]

    # Inventario de baterías cargadas
    def inventory_charged_batteries_rule(self, model, k, d, t):
        t0= self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        if t == tf: 
            return pyo.Constraint.Skip
        if t - (model.t_charge - 1) >= t0:
            return model.S[k, d, t+1] == model.S[k, d, t] + model.Sv[k, d, t, t - (model.t_charge - 1)] - model.W[k, d, t+1]
        elif t >= 0:
            return model.S[k, d, t+1] == model.S[k, d, t] - model.W[k, d, t+1]
        return pyo.Constraint.Skip

    # 4. Duración de la carga (Lógica interna del cargador)
    def charging_duration_rule(self, model, k, d, t, a):
        tf = self.time_series.get_time_intervals()[-1]
        if t == tf:
            return pyo.Constraint.Skip
        # Caso 1: La carga ya terminó (o no debería haber nada)
        if a <= t - model.t_charge + 1:
            return model.Sv[k, d, t+1, a] == 0
        
        # Caso 2: La carga está en proceso (se mantiene el valor anterior)
        elif (t - model.t_charge + 1) <= a <= (t - 1):
            return model.Sv[k, d, t+1, a] == model.Sv[k, d, t, a]
        
        # Caso 3: La carga acaba de iniciar en este instante t (a==t)
        elif t == a:
            return model.Sv[k, d, t+1, a] == model.X_ini[k, d, t]
            
        return pyo.Constraint.Skip

    # No estan en el modelo en latex 
    def initial_charging_batteries(self, model, k , d):
        t0 = self.time_series.get_time_intervals()[0]
        return model.X_ini[k,d,t0] == 0
    
    def avaible_batteries_for_swap(self, model, k , d ,t):
        return sum(model.Z_swap[k, i ,d, t] for i in model.slhd_set) <= model.S[k,d,t]
    
      #Condiciones iniciales 
    def initial_condition_station(self, model):
        return sum(model.X[k] for k in model.stations_set) == 1
    
    def initial_condition_station_swap(self, model):
        k0 = list(model.stations_set)[0]
        return model.X[k0] == 1
    
    def initial_condition_chargers(self, model, k):
        k0 = list(model.stations_set)[0]
        return model.N_chargers[k0] == 1
    
     # Detenciones 
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
        estar estacionados (Z = 1). En mantenimiento los ELHD no pueden estar cargando."""
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] == 1

    def no_swap_outside_allowed_times(self, model, k, i, d, t):
        """Fuerza Z_swap a 0 fuera de intervalos meal y between_shifts."""
        if t in model.time_intervals_meal_set or t in model.time_intervals_between_shifts_set:
            return pyo.Constraint.Skip  # Permitir swap en estos intervalos
        else:
            return model.Z_swap[k, i, d, t] == 0  # Forzar a 0 fuera

    
    def build_all_constraints(self, model):
        model.battery_lower =              pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_lower)
        model.battery_upper =               pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_upper)
        model.battery_boundary =            pyo.Constraint(model.slhd_set, model.days, rule=self.battery_boundary)
       
        model.max_n_chargers                     = pyo.Constraint(model.stations_set, rule=self.max_n_chargers)
        model.max_installed_capacity             = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.max_installed_capacity_swap)
        
        #nuevas
        model.max_n_batteries                   = pyo.Constraint(model.stations_set, rule=self.max_n_batteries)
        model.station_existence_constraint_swap      = pyo.Constraint(model.stations_set, model.slhd_set, model.days, model.time_intervals_set, rule=self.station_existence_constraint_swap)
        model.charger_limit_swap                   = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.charger_limit_swap)
        model.battery_soc_swap                       = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap)
        model.battery_soc_swap_update_1                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_1)
        model.battery_soc_swap_update_2                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_2)
        model.battery_soc_swap_update_3                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_3)
        model.battery_soc_swap_update_4                  = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.battery_soc_swap_update_4)
        model.peak_power_swap                         = pyo.Constraint(model.days, model.time_intervals_set, rule=self.peak_power_swap)
        model.state_unique_elhd_swap                      = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.state_unique_elhd_swap)
        model.between_shifts_elhd_swap    = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_between_shifts_set, rule=self.between_shifts_elhd_swap)
        
        model.inventory_discharged_batteries = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.inventory_discharged_batteries_rule)
        model.inventory_charged_batteries   = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.inventory_charged_batteries_rule)
        model.charging_duration              = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, model.time_intervals_set, rule=self.charging_duration_rule)
        model.total_swaps                   = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.total_swaps)
        model.batteries_inventory = pyo.Constraint(model.days, model.time_intervals_set, rule=self.batteries_inventory)
        model.max_swaps                   = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.max_swaps)
        
        model.production_swap                         = pyo.Constraint(model.days, model.nodes_set, rule=self.production_swap)
        model.aux_zpen_1                         = pyo.Constraint(model.Y_INDEX, rule=self.aux_zpen_1)
        model.aux_zpen_2                         = pyo.Constraint(model.Y_INDEX, rule=self.aux_zpen_2)
        model.aux_zpen_3                         = pyo.Constraint(model.Y_INDEX, rule=self.aux_zpen_3)
        model.daily_extraction                   = pyo.Constraint(model.slhd_set, model.nodes_set, model.days, rule=self.daily_extraction_M)
        # Penalización por tramos para F (piecewise lineal)
        model.F_piecewise_balance = pyo.Constraint(model.nodes_set, model.days, rule=self.F_piecewise_balance)
        model.F_piecewise_caps = pyo.Constraint(model.nodes_set, model.days, model.F_SEG,rule=self.F_piecewise_caps)

        # no estan en el modelo latex
        model.initial_charging_batteries    = pyo.Constraint(model.stations_set, model.days, rule=self.initial_charging_batteries)
        model.avaible_batteries_for_swap    = pyo.Constraint(model.stations_set, model.days, model.time_intervals_set, rule=self.avaible_batteries_for_swap)
        model.initial_batteries_discharged = pyo.Constraint(model.stations_set, model.days, rule=self.initial_batteries_discharged)
        
        # Restricción: swap solo en meal y between_shifts
        model.no_swap_outside = pyo.Constraint(model.stations_set, model.slhd_set, model.days, model.time_intervals_set, rule=self.no_swap_outside_allowed_times)
        #condiciones iniciales
        model.initial_condition_station_swap = pyo.Constraint(rule=self.initial_condition_station_swap)
        #model.initial_condition_chargers = pyo.Constraint(model.stations_set, rule=self.initial_condition_chargers)
        #model.initial_condition_station = pyo.Constraint(rule=self.initial_condition_station)
        
          #Detenciones  
        model.meal_stop_all = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.meal_stop_all)
        model.maintenance_stop_all = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.maint_stop_all)

class ObjectiveRules(OptRules):
    def lhd_charge_cost_bs(self, model):
        # Coste de cargar baterías (electricidad) con tecnonología BS
        cost_el = sum(
            model.costo_electricidad[d, t] * model.Sv[k, d, t, a] * model.p_charger * model.delta_t
            for k in model.stations_set
            for d in model.days
            for t in model.time_intervals_set
            for a in model.time_intervals_set 
        ) 
        F_penalty = sum(
            model.F_seg[j, d, s] * (model.Voll / model.F_penalty_div[s])
            for j in model.nodes_set
            for d in model.days
            for s in model.F_SEG
        )
        return (cost_el + F_penalty) * model.scaling_factor_op_cost

    def inversion_cost(self, model):
        cost_inv = sum(
            model.station_cost_k[k] * model.X[k] + model.charger_cost * model.N_chargers[k] 
            + model.battery_cost * model.N_batteries[k]  for k in model.stations_set 
        ) 
        return cost_inv
    
    def production_total(self, model, j):
        def ntr(node,i):
            return self.time_series.get_n_trips(node,i)
        term_de = sum(model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
        for i in model.dlhd_set|model.slhd_set for t in model.time_intervals_set for d in model.days)
        pen = sum(model.Z_pen[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i] * (model.t_swap[i] / model.delta_t)
        for i in model.slhd_set|model.slhd_set for t in model.time_intervals_set for d in model.days)
        return (term_de - pen)*model.scaling_factor_op_cost
    
    def op_cost_total(self, model):
        cost_el = sum(
            model.costo_electricidad[d, t] * model.Sv[k, d, t, a] * model.p_charger * model.delta_t
            for k in model.stations_set
            for d in model.days
            for t in model.time_intervals_set
            for a in model.time_intervals_set 
        ) 
        F_penalty = sum(
            model.F_seg[j, d, s] * (model.Voll / model.F_penalty_div[s])
            for j in model.nodes_set
            for d in model.days
            for s in model.F_SEG
        )
        return cost_el + F_penalty
    
    def total_cost(self, model):
        return self.lhd_charge_cost_bs(model) + self.inversion_cost(model)

    def build_objective(self, model):
        model.obj = pyo.Objective(rule=self.op_cost_total, sense=pyo.minimize)

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






 