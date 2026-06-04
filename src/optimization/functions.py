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
        """Detenciones DCH (legacy): pauses as (start_time, end_time, pause_type) in HH:MM.

        This is the original scheme (DCH) kept for backward compatibility.
        """
        pauses = [
            # --- Shift 2 (in progress): 08:00 - 16:00 ---
            # Shift change at 08:00 already happened before the horizon.
            # Fuel delay + cleaning starts before horizon and overlaps from 09:00.
            ("09:00", "10:12", "maintenance"),

            # --- Shift 3: 16:00 - 00:00 ---
            ("16:00", "17:04", "shift_change"),
            ("17:04", "18:16", "maintenance"),

            # --- Shift 1: 00:00 - 08:00 ---
            ("00:00", "01:04", "shift_change"),
            ("01:04", "02:16", "maintenance"),

            # --- Shift 2 (next day): 08:00 - 16:00 ---
            ("08:00", "09:04", "shift_change"),
        ]

        return pauses

    def _get_pause_definitions_det(self):
        """Detenciones DET (nuevo): pauses as (start_time, end_time, pause_type) in HH:MM.

        This contains the new, user-provided DET schedule.
        """
        pauses = [
            # --- Shift 2 (in progress): 08:00 - 16:00 ---
            # Shift change already started at 08:00, horizon captures from 09:00
            ("09:00", "09:40", "shift_change"),
            ("10:28", "12:58", "stop"),

            # --- Shift 3: 16:00 - 00:00 ---
            ("16:30", "17:40", "shift_change"),
            ("19:28", "21:58", "stop"),

            # --- Shift 1: 00:00 - 08:00 ---
            ("00:30", "01:40", "shift_change"),
            ("03:28", "05:58", "stop"),

            # --- Shift 2 (next day): 08:00 - 16:00 ---
            # Interpreted as next day since 08:30 < 09:00
            ("08:30", "09:00", "shift_change"),
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



    def _get_time_intervals_for_pause_type(self, pause_type, pauses=None):
        """
        Return sorted list of time-interval indices for a given pause_type.

        Assumes:
        - t=1 corresponds to [DAY_START, DAY_START + delta_t)
        - self.time_series.delta_t is in hours
        - pause definitions are (start_hhmm, end_hhmm, pause_type)
        """
        # Ajusta esto a tu inicio real del horizonte (09:00 segÃºn tu comentario)
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

        if pauses is None:
            pauses = self._get_pause_definitions()

        for start_str, end_str, ptype in pauses:
            if ptype != pause_type:
                continue

            start_min = _parse_hhmm(start_str)
            end_min   = _parse_hhmm(end_str)

            # Interpretar horas < base como "dÃ­a siguiente" dentro del ciclo 24h del horizonte
            if start_min < base_minutes:
                start_min += 24 * 60
            if end_min < base_minutes:
                end_min += 24 * 60

            # Si el fin quedÃ³ antes (o igual) que el inicio => cruza medianoche
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
        model.years = pyo.Set(initialize=self.time_series.years)
        model.shifts = pyo.Set(initialize=self.time_series.shifts)
        model.time_intervals_set_zero = pyo.Set(initialize=[0] + list(self.time_series.time_intervals))
        model.time_intervals_between_shifts_set = pyo.Set(initialize=self.time_series.get_intervals_between_shifts())
        model.time_intervals_peak_set = pyo.Set(
            initialize=[
                t for t in self.time_series.time_intervals
                if 18 * 60 <= ((9 * 60 + (t - 1) * int(round(self.time_series.delta_t * 60))) % 1440) < 22 * 60
            ]
        )
        model.stations_set = pyo.Set(initialize=self.mine_system.get_system_stations())
        # Tramos de penalizaciÃ³n para dÃ©ficit F (piecewise lineal)
        model.F_SEG = pyo.Set(initialize=[1, 2, 3, 4, 5])
        # Generadores renovables (vacio si no hay datos de generacion)
        model.gen_set = pyo.Set(initialize=self.mine_system.get_system_generators())
        # Almacenamiento estacionario BESS (vacio si no hay hoja Storage)
        model.storage_set = pyo.Set(initialize=self.mine_system.get_system_storage())

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
        # Shift-change and fuel-delay sets for the legacy DCH scheme
        model.time_intervals_shift_change_set = pyo.Set(
            initialize=self._get_time_intervals_for_pause_type("shift_change")
        )
        model.time_intervals_fuel_delay_set = pyo.Set(
            initialize=self._get_time_intervals_for_pause_type("fuel_delay")
        )

        # DET (nuevo) detentions: build sets using the DET pause definitions
        det_shift = self._get_time_intervals_for_pause_type("shift_change", pauses=self._get_pause_definitions_det())
        det_stop = self._get_time_intervals_for_pause_type("stop", pauses=self._get_pause_definitions_det())
        model.time_intervals_det_set = pyo.Set(
            initialize=sorted(set(det_shift) | set(det_stop))
        )

        # Expose DET-specific subsets so they are serialized into parameters.json
        model.time_intervals_shift_change_det_set = pyo.Set(
            initialize=sorted(det_shift)
        )

        model.time_intervals_forced_detention_set = pyo.Set(
            initialize=sorted(det_stop)
        )

        # DCH detentions (legacy) kept under a separate set name
        model.time_intervals_dch_det_set = pyo.Set(
            initialize=sorted(
                set(self._get_time_intervals_for_pause_type("shift_change"))
                | set(self._get_time_intervals_for_pause_type("fuel_delay"))
            )
        )



        
        # -----------------------------
        # Grupos de colaciÃ³n para todos los LHD:
        # Grupo 1 = sufijo par, Grupo 2 = sufijo impar.
        # Si algÃºn nombre no trae sufijo numÃ©rico, se reparte en fallback para
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
        max_extraction_goal = max(
            (self.time_series.get_extraction_goal(j, d) for j in model.nodes_set for d in model.days),
            default=0.0
        )
        #ParÃ¡metros temporales
        model.delta_t = pyo.Param(initialize=self.time_series.delta_t, mutable=True)
        model.t_ini = pyo.Param(initialize=self.time_series.get_time_intervals()[0], mutable=True)
        model.t_fin = pyo.Param(initialize=self.time_series.get_time_intervals()[-1], mutable=True)
        model.year_of_day = pyo.Param(
            model.days,
            initialize={d: self.time_series.get_year_of_day(d) for d in model.days},
            mutable=False
        )
        model.F_max_global = pyo.Param(initialize=max_extraction_goal, mutable=True)
        #ParÃ¡metros econÃ³micos
        model.m_j = pyo.Param(model.nodes_set,model.days, initialize={(j, d): self.time_series.get_extraction_goal(j, d)for j in model.nodes_set for d in model.days},mutable=True)
        model.costo_marginal = pyo.Param(model.elhd_set, model.days, model.time_intervals_set, initialize={(b, d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.elhd.get_energy_cost(b), d, t) for b in model.elhd_set for d in model.days for t in model.time_intervals_set}, mutable=True)
        #ParÃ¡metros LHD
        # ParÃ¡metros de viaje por nodo
        model.d_i    = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_n_intervals_trip(j,i)       for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pe_i   = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_energy_consumption(j,i)     for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pd_i = pyo.Param( model.lhd_set,model.nodes_set,initialize={(i, j): self.time_series.get_diesel_consumption(j,i) for i in model.lhd_set for j in model.nodes_set},mutable=False)
        # ParÃ¡metros de baterÃ­a
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
         
        #ParÃ¡metros problema de inversiÃ³n
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

         # ---- PenalizaciÃ³n por tramos para F (dÃ©ficit) ----
        # Tramos: 0-5, 5-10, 10-50, 50-100, 100+
        # Costo unitario por tramo: Voll / divisor
        # Nota: para tramo 5 (100+) se usa Voll/0.1 (mÃ¡s caro).
        model.F_penalty_div = pyo.Param(model.F_SEG,initialize={1: 5000, 2: 1000, 3: 200, 4: 50, 5: 10},mutable=True)
        # Capacidad (longitud) de cada tramo
        model.F_penalty_cap = pyo.Param(model.F_SEG,initialize={1: 5, 2: 0, 3: 0, 4: 0, 5: 0},mutable=True)
        model.Voll = pyo.Param(initialize=500, mutable=True)

        # Costo de energia de la red por (d,t): usa el primer ELHD como referencia del contrato
        ref_elhd = list(model.elhd_set)[0]
        model.costo_red = pyo.Param(
            model.days, model.time_intervals_set,
            initialize={(d, t): self.time_series.get_marginal_cost_scaled(
                            self.mine_system.elhd.get_energy_cost(ref_elhd), d, t)
                        for d in model.days for t in model.time_intervals_set},
            mutable=True)

        # Parametros de generacion renovable (solo si existen generadores)
        if len(list(model.gen_set)) > 0:
            gen = self.mine_system.generators
            model.c_inv_g = pyo.Param(
                model.gen_set,
                initialize={g: gen.get_c_inv(g) for g in model.gen_set},
                mutable=False)
            model.c_op_g = pyo.Param(
                model.gen_set,
                initialize={g: gen.get_c_op(g) for g in model.gen_set},
                mutable=False)
            model.p_max_g = pyo.Param(
                model.gen_set,
                initialize={g: gen.get_p_max(g) for g in model.gen_set},
                mutable=False)
            model.g_max_g = pyo.Param(
                model.gen_set,
                initialize={g: gen.get_g_max(g) for g in model.gen_set},
                mutable=False)
            model.alpha_g = pyo.Param(
                model.gen_set, model.days, model.time_intervals_set,
                initialize={(g, d, t): self.time_series.get_alpha_g(g, d, t)
                            for g in model.gen_set
                            for d in model.days
                            for t in model.time_intervals_set},
                mutable=False)

        # Parametros de almacenamiento BESS (solo si existen unidades de almacenamiento)
        if len(list(model.storage_set)) > 0:
            stor = self.mine_system.storage
            model.c_inv_h = pyo.Param(
                model.storage_set,
                initialize={h: stor.get_c_inv(h) for h in model.storage_set},
                mutable=False)
            model.c_op_h = pyo.Param(
                model.storage_set,
                initialize={h: stor.get_c_op(h) for h in model.storage_set},
                mutable=False)
            model.p_max_h = pyo.Param(
                model.storage_set,
                initialize={h: stor.get_p_max(h) for h in model.storage_set},
                mutable=False)
            model.eta_h = pyo.Param(
                model.storage_set,
                initialize={h: stor.get_eta(h) for h in model.storage_set},
                mutable=False)
            model.a_min_h = pyo.Param(
                model.storage_set,
                initialize={h: stor.get_a_min(h) for h in model.storage_set},
                mutable=False)
            model.a_max_h = pyo.Param(
                model.storage_set,
                initialize={h: stor.get_a_max(h) for h in model.storage_set},
                mutable=False)


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

    def P_pot(self, model, y):
        return (0, None)

    def B(self, model, b, d, t):
        return (0, model.bmax_b[b])

    def Bini(self, model, b, d):
        return (0, model.bmax_b[b])
    
    def F(self, model, j, d):
        return (0, model.F_max_global)

    def build_all_variables(self, model):
          # Ãndice esparso para carga: solo (station, elhd) vÃ¡lidas segÃºn StationAssignment
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
         # Y(i,j,d,t) solo si j âˆˆ Nodes_assigned_at_interval(d,t,i)
        def _init_Y_INDEX(m):
            for d in m.days:
                for t in m.time_intervals_set:
                    for i in m.lhd_set:
                        node_list = self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
                        for j in node_list:
                            yield (i, j, d, t)
        # Sets de Ã­ndices (solo contienen tuplas vÃ¡lidas)
        model.Y_INDEX  = pyo.Set(dimen=4, initialize=_init_Y_INDEX)

        # Viaje completo de ida por nodo â€” SOLO en tuplas vÃ¡lidas
        model.Y = pyo.Var(model.Y_INDEX, domain=pyo.Binary)
        # Estado ON/OFF de cada LHD
        model.Z         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set,
                                  bounds=self.Z, domain=pyo.Binary)
        # Indicador de carga elÃ©ctrica (por baterÃ­a) en cada intervalo
        model.Z_charge  = pyo.Var(model.ZCHARGE_INDEX, model.days, model.time_intervals_set,
                                  bounds=self.Z_charge, domain=pyo.Binary)
        # Potencia de carga de baterÃ­a b en (d,t)
        model.P         = pyo.Var(model.ZCHARGE_INDEX, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        # Maxima potencia consumida en periodos de punta por anio
        model.P_pot = pyo.Var(model.years, domain=pyo.NonNegativeReals)
        # SOC de baterÃ­a b al final de (d,t)
        model.B         = pyo.Var(model.elhd_set, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        #Cantidad de cargadores
        model.N_chargers= pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers)
        #ElecciÃ³n estaciÃ³n de carga
        model.X = pyo.Var(model.stations_set, domain=pyo.Binary)
        #Inicio de una carga on-board
        model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)    
        # Indica si termina una carga en t
        model.EndCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)
        
        ## NUEVAS VARIABLES

        # Variables de generacion renovable (solo si existen generadores)
        if len(list(model.gen_set)) > 0:
            # Inversion: cantidad de unidades de generacion tipo g
            model.G_g = pyo.Var(model.gen_set, domain=pyo.NonNegativeIntegers)
            # Operacion: potencia generada por g en (d,t) [kW]
            model.P_gen = pyo.Var(model.gen_set, model.days, model.time_intervals_set,
                                  domain=pyo.NonNegativeReals)
        # Potencia comprada a la red en (d,t) [kW] — siempre presente
        model.P_red = pyo.Var(model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)

        # Variables de almacenamiento BESS (solo si existen unidades)
        if len(list(model.storage_set)) > 0:
            # Decisión binaria de inversión en unidad h
            model.H_h = pyo.Var(model.storage_set, domain=pyo.Binary)
            # Potencia extraída (+) / inyectada (-) de la batería h en (d,t) [kW]
            model.P_bat = pyo.Var(model.storage_set, model.days, model.time_intervals_set,
                                  domain=pyo.Reals)
            # Estado de energía de la batería h al final de (d,t) [kWh], incluye t=0
            model.A_h = pyo.Var(model.storage_set, model.days, model.time_intervals_set_zero,
                                 domain=pyo.NonNegativeReals)

        # ExtracciÃ³n por intervalo, consistente con Y_INDEX.
        model.M = pyo.Var(model.Y_INDEX, domain=pyo.NonNegativeReals)
        
        # Holgura producciÃ³n 
        model.F = pyo.Var(model.nodes_set, model.days, bounds=self.F)
        
        # DescomposiciÃ³n de F en tramos para costo piecewise lineal
        model.F_seg = pyo.Var(model.nodes_set, model.days, model.F_SEG, domain=pyo.NonNegativeReals)

        #AsignaciÃ³n estaciÃ³n por macrobloque
        #model.U = pyo.Var(model.stations_set, model.elhd_set, domain=pyo.Binary)

        #Límite inferior producción por punto
        #model.L = pyo.Var(domain=pyo.NonNegativeReals)

from src.optimization.functions import OptRules
import pyomo.environ as pyo

class ConstraintRules(OptRules):

    # (C1) Estado Ãºnico de cada LHD: Zi,t + sum_j Yi,j,t == 1 
    def state_unique_elhd(self, model, i, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        # Suma Z_charge solo sobre estaciones vÃ¡lidas para este elhd
        z_charge_sum = sum(model.Z_charge[k,i,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        return model.Z[i, d, t] + z_charge_sum + sum(model.Y[i, j, d, t] for j in nodes) == 1

    # (C5) Estado entre turnos: Zi,t = 1 for t in Tbs
    def between_shifts_elhd(self, model, i, d, t):
        # Suma Z_charge solo sobre estaciones vÃ¡lidas para este elhd
        z_charge_sum = sum(model.Z_charge[k,i,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        return model.Z[i, d, t] + z_charge_sum == 1

    def battery_soc(self, model, i, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        # Suma P solo sobre estaciones vÃ¡lidas para este elhd
        charge = sum(model.P[k, i, d, t] * (model.delta_t-model.man_time_k[k]-model.t_ttc_i[k,i]) for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        discharge = sum(
            model.Y[i, j, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
        )
        # Suma StartCharge solo sobre estaciones vÃ¡lidas para este elhd
        penalization_charge = 2*sum(model.StartCharge[k, i, d, t] * model.pk_i[k,i]*model.t_ttc_i[k,i] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        

        if t == t0:
            return model.B[i, d, t] == model.B[i, d, 0] + charge - discharge - penalization_charge
        else:
            return model.B[i, d, t] == model.B[i, d, t - 1] + charge - discharge - penalization_charge
        
     # (C8a) LÃ­mite inferior de SOC baterÃ­a
    def battery_lower(self, model, i, d, t):
        return model.B[i, d, t] >= model.bmin_b[i] * model.bmax_b[i]

    # (C8b) LÃ­mite superior de SOC baterÃ­a
    def battery_upper(self, model, i, d, t):
        return model.B[i, d, t] <= model.bmax_b[i]


    # (C9) CondiciÃ³n de borde SOC baterÃ­a
    def battery_boundary(self, model, i, d):
        tf = self.time_series.get_time_intervals()[-1]
        return model.B[i, d, 0] == model.B[i, d, tf]
    
    
    def min_visits_per_node(self, model, j, d):
        """Garantiza que cada nodo `j` en el dÃ­a `d` sea visitado al menos 2 intervalos.

        Suma `Y[i,j,d,t]` sobre todas las tuplas existentes en `model.Y` para (j,d).
        """
        term_visits = sum(
            model.Y[i2, j2, d2, t2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_visits >= 1

    def max_visits_node(self, model, j, d):
        """Limita el número máximo de asignaciones (visitas) a un nodo `j` en el día `d`.

        """
        term_visits = sum(
            model.Y[i2, j2, d2, t2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_visits <= 4
    
    def production_max(self, model, d, j):
        target = 400
        # Solo sobre llaves EXISTENTES de Y para (d, j)
        term_de = sum(
            model.Y[i2, j, d, t2] * model.g_i[i2] * self.time_series.get_n_trips(j, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_de <= target
    
    

    def daily_production(self, model, d):
        """Production balance for the whole day d (sum over all nodes).

        Enforces: sum_{i,j,t} Y[i,j,d,t]*g_i*n_trips(j,i)*filling_factor[i] + sum_j F[j,d] <= sum_j m_j[j,d]
        """
        # Total target across all nodes
        total_target = 32991

        # Sum production term over all Y tuples for day d
        term_de = sum(
            model.Y[i2, j2, d2, t2] * model.g_i[i2] * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if d2 == d
        )


        return  term_de >= total_target
    
    def production(self, model, d, j):
        """Production balance for node j on day d.

        Enforces: sum_i,t Y[i,j,d,t]*g_i*n_trips(j,i)*filling_factor[i] + F[j,d] == m_j[j,d]
        """
        # Target for node j on day d
        target = model.m_j[j, d]

        # Sum production term over all Y tuples for (d, j)
        term_de = sum(
            model.Y[i2, j, d, t2] * model.g_i[i2] * self.time_series.get_n_trips(j, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_de + model.F[j, d] >= target


    def interval_extraction_M(self, model, i, j, d, t):
        """
        M[i,j,d,t] = extracciÃ³n del equipo i en el nodo j
        durante el intervalo t del dÃ­a d.
        Misma unidad que el tÃ©rmino de producciÃ³n (g_i * n_trips * f_i).
        """
        return model.M[i, j, d, t] == (
            model.Y[i, j, d, t]
            * model.g_i[i]
            * self.time_series.get_n_trips(j, i)
            * model.filling_factor[i]
        )

     # PenalizaciÃ³n por tramos (piecewise) para F
    # --------------------------
    def F_piecewise_balance(self, model, j, d):
        # Sumatoria de tramos debe reconstruir F
        return sum(model.F_seg[j, d, s] for s in model.F_SEG) == model.F[j, d]

    def F_piecewise_caps(self, model, j, d, s):
        # Cada tramo tiene una "longitud" mÃ¡xima
        return model.F_seg[j, d, s] <= model.F_penalty_cap[s]

    # Estaciones de carga
    #Cantidad mÃ¡xima de cargadores
    def max_n_chargers(self, model, k):
        return model.N_chargers[k] <= model.max_chargers_k[k] * model.X[k]
    
    #Existencia de la estaciÃ³n
    def station_existence_constraint(self, model, k, i, d, t):
        return model.Z_charge[k,i, d, t] <= model.X[k]
    
    #Limite de cargadores
    def charger_limit(self, model,k, d, t):
        # Suma Z_charge solo sobre elhds vÃ¡lidos para esta estaciÃ³n
        return sum(model.Z_charge[k, i, d, t] for (k2, i) in model.ZCHARGE_INDEX if k2 == k) <= model.N_chargers[k]
    
    #Inicio y termino de una carga on-board
    def charge_state(self, model ,k, i, d, t):
       t0 = self.time_series.get_time_intervals()[0]
       if t>t0:
            return model.Z_charge[k, i, d, t] - model.Z_charge[k, i, d, t-1] == model.StartCharge[k, i, d, t] - model.EndCharge[k, i, d, t]
       else:
           return model.Z_charge[k, i, d, t] == model.StartCharge[k, i, d, t]  - model.EndCharge[k, i, d, t]
       
    #MÃ¡xima potencia de carga on-board solo si esta en estacion
    def max_power(self, model, k, i, d, t):
        return model.P[k, i, d, t]  <= model.Z_charge[k, i, d, t] * model.p_charger
    
    #  Sistemas distribuciÃ³n 
    def max_installed_capacity(self, model, k, d, t):
        # Suma P solo sobre elhds vÃ¡lidos para esta estaciÃ³n
        station_elhds = [i for (k2, i) in model.ZCHARGE_INDEX if k2 == k]
        if not station_elhds:
            return pyo.Constraint.Skip
        return sum(model.P[k, i, d, t] for i in station_elhds) <= model.p_max_k[k]

    def peak_power(self, model, d, t):
        # REEMPLAZADA por power_balance + grid_limit — se mantiene comentada como referencia
        return sum(model.P[k, i, d, t] for (k, i) in model.ZCHARGE_INDEX) <= model.p_peak

    def power_cost_peak_limit(self, model, d, t):
        # Potencia peak facturable = lo que se toma de la red en horas punta
        if t not in model.time_intervals_peak_set:
            return pyo.Constraint.Skip
        return model.P_red[d, t] <= model.P_pot[model.year_of_day[d]]

    # ------------------------------------------------------------------
    # Balance de potencia y restricciones de generacion renovable
    # ------------------------------------------------------------------

    def power_balance(self, model, d, t):
        """Suma de cargas = potencia de red + generación local + almacenamiento (ec. 3.48)."""
        demand = sum(model.P[k, i, d, t] for (k, i) in model.ZCHARGE_INDEX)
        gen = (sum(model.P_gen[g, d, t] for g in model.gen_set)
               if len(list(model.gen_set)) > 0 else 0)
        bess = (sum(model.P_bat[h, d, t] for h in model.storage_set)
                if len(list(model.storage_set)) > 0 else 0)
        return model.P_red[d, t] + gen + bess == demand

    def grid_limit(self, model, d, t):
        """Potencia de red acotada por capacidad de la subestacion."""
        return model.P_red[d, t] <= model.p_peak

    def gen_limit(self, model, g, d, t):
        """Potencia generada acotada por unidades instaladas y disponibilidad."""
        return model.P_gen[g, d, t] <= model.G_g[g] * model.p_max_g[g] * model.alpha_g[g, d, t]

    def gen_max_units(self, model, g):
        """Cantidad maxima de unidades instalables por tecnologia."""
        return model.G_g[g] <= model.g_max_g[g]
    
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

    # ------------------------------------------------------------------
    # Almacenamiento BESS (ec. 3.50 – 3.53)
    # ------------------------------------------------------------------

    def bess_power_upper(self, model, h, d, t):
        """P_bat <= p_max_h * H_h (descarga máxima) — ec. 3.50."""
        return model.P_bat[h, d, t] <= model.p_max_h[h] * model.H_h[h]

    def bess_power_lower(self, model, h, d, t):
        """P_bat >= -p_max_h * H_h (carga máxima) — ec. 3.50."""
        return model.P_bat[h, d, t] >= -model.p_max_h[h] * model.H_h[h]

    def bess_soc_balance(self, model, h, d, t):
        """Balance de energía entre intervalos — ec. 3.51.
        A_h,d,t = A_h,d,t-1 - (P_bat_h,d,t / eta_h) * delta_t
        """
        t0 = self.time_series.get_time_intervals()[0]
        prev = model.A_h[h, d, 0] if t == t0 else model.A_h[h, d, t - 1]
        return model.A_h[h, d, t] == prev - (model.P_bat[h, d, t] / model.eta_h[h]) * model.delta_t

    def bess_soc_init(self, model, h, d):
        """Estado de energía inicial = 0 al comienzo de cada día — ec. 3.52."""
        return model.A_h[h, d, 0] == 0

    def bess_soc_upper(self, model, h, d, t):
        """A_h <= a_max_h * H_h — ec. 3.53."""
        return model.A_h[h, d, t] <= model.a_max_h[h] * model.H_h[h]

    def bess_soc_lower(self, model, h, d, t):
        """A_h >= a_min_h * H_h — ec. 3.53."""
        return model.A_h[h, d, t] >= model.a_min_h[h] * model.H_h[h]

    # --------------------------
    # Pausas: MAINTENANCE
    # --------------------------
    def maint_stop_all(self, model, i, d, t):
        """En intervalos de mantenimiento todos los LHD deben estar estacionados (Z = 1)."""
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] == 1

    def maint_no_charge(self, model, k, i, d, t):
        """Prohíbe Z_charge durante mantenimiento.
        Necesario porque state_unique_elhd se saltea cuando nodes está vacío,
        dejando Z_charge sin restricción aunque Z == 1.
        """
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z_charge[k, i, d, t] == 0

    def det_stop_all(self, model, i, d, t):
        """En intervalos DET (shift_change + fuel_delay) todos los LHD deben estar estacionados (Z = 1)."""
        if t not in model.time_intervals_det_set:
            return pyo.Constraint.Skip
            # return sum(model.Z_charge[k,i,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i) == 0
        return model.Z[i, d, t] + sum(model.Z_charge[k,i,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i) == 1
    
    # Fijar cantidad de cargadores 
    def fixed_n_chargers(self, model, k):
        if k == "station_1":
            return model.N_chargers[k] == 4
        elif k == "station_2":
            return model.N_chargers[k] == 2
        elif k == "station_3":
            return model.N_chargers[k] == 2
        
     # --------------------------
    # Metas de extracción por punto
    # --------------------------
    
    def production_per_node(self, model, j, d):
        """L es menor o igual a la extracción total en el nodo j para el día d."""
        return model.L <= sum(
            model.M[i2, j2, d2, t2]
            for (i2, j2, d2, t2) in model.M
            if j2 == j and d2 == d
        )
    
    
    
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
        model.power_balance                      = pyo.Constraint(model.days, model.time_intervals_set, rule=self.power_balance)
        model.grid_limit                         = pyo.Constraint(model.days, model.time_intervals_set, rule=self.grid_limit)
        model.power_cost_peak_limit              = pyo.Constraint(model.days, model.time_intervals_set, rule=self.power_cost_peak_limit)
        # Restricciones de generacion renovable (solo si gen_set no vacio)
        if len(list(model.gen_set)) > 0:
            model.gen_limit      = pyo.Constraint(model.gen_set, model.days, model.time_intervals_set, rule=self.gen_limit)
            model.gen_max_units  = pyo.Constraint(model.gen_set, rule=self.gen_max_units)

        # Restricciones de almacenamiento BESS (solo si storage_set no vacio)
        if len(list(model.storage_set)) > 0:
            model.bess_power_upper = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_power_upper)
            model.bess_power_lower = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_power_lower)
            model.bess_soc_balance = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_soc_balance)
            model.bess_soc_init    = pyo.Constraint(model.storage_set, model.days, rule=self.bess_soc_init)
            model.bess_soc_upper   = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_soc_upper)
            model.bess_soc_lower   = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_soc_lower)
        
        #ProducciÃ³n nuevas
        #model.production_min         = pyo.Constraint(model.days, model.nodes_set, rule=self.production_min)
        #model.min_visits_per_node    = pyo.Constraint(model.nodes_set, model.days, rule=self.min_visits_per_node)
        #model.max_visits_per_node    = pyo.Constraint(model.nodes_set, model.days, rule=self.max_visits_node)
        #model.production_max     = pyo.Constraint(model.days, model.nodes_set, rule=self.production_max)
        #model.daily_production   = pyo.Constraint(model.days, rule=self.daily_production)
        model.production         = pyo.Constraint( model.days, model.nodes_set, rule=self.production)
        model.interval_extraction_M = pyo.Constraint(model.Y_INDEX, rule=lambda m, i, j, d, t: self.interval_extraction_M(m, i, j, d, t))

        # PenalizaciÃ³n por tramos para F (piecewise lineal)
        model.F_piecewise_balance = pyo.Constraint(model.nodes_set, model.days, rule=self.F_piecewise_balance)
        model.F_piecewise_caps = pyo.Constraint(model.nodes_set, model.days, model.F_SEG,rule=self.F_piecewise_caps)

        #Detenciones 
        model.meal_g1_no_travel_group1 = pyo.Constraint(model.lhd_set, model.days, model.time_intervals_set, rule=self.meal_g1_no_travel_group1)
        model.meal_g2_no_travel_group2 = pyo.Constraint(model.lhd_set, model.days, model.time_intervals_set, rule=self.meal_g2_no_travel_group2)
        model.maintenance_stop_all = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.maint_stop_all)
        model.maint_no_charge      = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.maint_no_charge)
        #
        # 
        #model.det_stop_all = pyo.Constraint(model.elhd_set, model.days, model.time_intervals_set, rule=self.det_stop_all)

        #fijar cantidad de cargadores
        #model.fixed_n_chargers = pyo.Constraint(model.stations_set, rule=self.fixed_n_chargers)
        
        # Restricción: L <= extracción en cada punto-día (L es el mínimo)
        #model.production_per_node = pyo.Constraint(model.nodes_set, model.days, rule=self.production_per_node)
        #model.fixed_n_chargers = pyo.Constraint(model.stations_set, rule=self.fixed_n_chargers)

class ObjectiveRules(OptRules):

    def lhd_charge_cost(self, model):
        # Costo de energia comprada a la red (P_red * costo contrato)
        cost_el = sum(
            model.P_red[d, t] * model.costo_red[d, t]
            for d in model.days
            for t in model.time_intervals_set
        ) * model.delta_t
        return cost_el * model.scaling_factor_op_cost

    def inversion_cost(self, model):
        cost_inv = sum(
            model.station_cost_k[k] * model.X[k] + model.charger_cost * model.N_chargers[k]
            for k in model.stations_set
        )
        return cost_inv

    def gen_investment_cost(self, model):
        if len(list(model.gen_set)) == 0:
            return 0
        return sum(model.G_g[g] * model.c_inv_g[g]* model.p_max_g[g] for g in model.gen_set)

    def gen_op_cost(self, model):
        if len(list(model.gen_set)) == 0:
            return 0
        return sum(model.G_g[g] * model.c_op_g[g] * model.p_max_g[g] for g in model.gen_set)

    def bess_investment_cost(self, model):
        """Costo de inversión BESS: sum_h c_inv_h * H_h — ec. 3.1."""
        if len(list(model.storage_set)) == 0:
            return 0
        return sum(model.H_h[h] * model.c_inv_h[h] for h in model.storage_set)

    def bess_op_cost(self, model):
        """Costo de O&M anual BESS: sum_h c_op_h * H_h — ec. 3.1."""
        if len(list(model.storage_set)) == 0:
            return 0
        return sum(model.H_h[h] * model.c_op_h[h] for h in model.storage_set)

    def power_cost(self, model):
        return sum(model.P_pot[y] * 12 * 10 for y in model.years)

    def F_penalty_cost(self, model):
        cost_F = sum(
            model.F_seg[j, d, s] * (4 * model.Voll / model.F_penalty_div[s])
            for j in model.nodes_set
            for d in model.days
            for s in model.F_SEG
        )
        return cost_F * model.scaling_factor_op_cost

    def total_cost(self, model):
        return (self.lhd_charge_cost(model)
                + self.inversion_cost(model)
                + self.gen_investment_cost(model)
                + self.gen_op_cost(model)
                + self.bess_investment_cost(model)
                + self.bess_op_cost(model)
                + self.F_penalty_cost(model)
                + self.power_cost(model))
    
    def op_cost_total(self, model):
        # Coste operativo total (sin inversiÃ³n)
        return (self.lhd_charge_cost(model) + 4*self.F_penalty_cost(model))/model.scaling_factor_op_cost
    
    def max_mineral(self, model):
        """Maximiza L: la extracción mínima garantizada en todos los puntos-días."""
        return model.L
    
    #def production_total(self, model, j):
    #    def ntr(node,i):
    #        return self.time_series.get_n_trips(node,i)

        # extracciÃ³n normal de mineral
    #    term_de = sum(
    #    model.Y[i,j,d,t] * model.g_i[i] * ntr(j,i) * model.filling_factor[i]
    #    for i in model.dlhd_set|model.elhd_set for t in model.time_intervals_set
    #    for d in model.days)
    #    return term_de*model.scaling_factor_op_cost

    def build_objective(self, model):
        model.obj = pyo.Objective(rule=self.total_cost, sense=pyo.minimize)
        #model.obj = pyo.Objective(rule=self.op_cost_total, sense=pyo.minimize)

class OutputManager(OptRules):

      def get_var(self, variable, index_names):
        """
        Devuelve un DataFrame con:
        - Una columna 'value'   (los valores numÃ©ricos de la variable)
        - Una columna por cada Ã­ndice (day, interval, lhd, battery, â€¦)
        Esto deja el DF listo para las agrupaciones y filtros de printer.py
        """
        # Extrae valores de Pyomo â†’ dict {index_tuple: value}
        var_values = pd.DataFrame.from_dict(
            variable.extract_values(),
            orient='index',
            columns=['value']        # â† nombre estÃ¡ndar
        )

        # Convierte el dictâ€index en MultiIndex con nombres claros
        var_values.index = pd.MultiIndex.from_tuples(
            var_values.index,
            names=index_names
        )

        # Â¡Clave!  â–º pasa el MultiIndex a columnas
        var_values = var_values.reset_index()   #  â† aÃ±ade day, interval, â€¦

        return var_values






