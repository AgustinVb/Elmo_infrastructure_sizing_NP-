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

    def _build_intervals_from_clock_windows(self, windows, start_hour: int = 9):
        """Convert a list of (HH:MM, HH:MM) windows into interval indices.

        Uses self.time_series.delta_t (hours) and self.time_series.time_intervals.
        start_hour is the hour (0-23) considered as interval 1 start (default 9).
        """
        if not hasattr(self.time_series, "time_intervals") or len(self.time_series.time_intervals) == 0:
            return []

        dt_minutes = int(round(self.time_series.delta_t * 60))
        if dt_minutes <= 0:
            return []

        max_t = int(max(self.time_series.time_intervals))
        base_minutes = int(start_hour) * 60

        def _parse_hhmm(s):
            hh, mm = s.strip().split(":")
            return int(hh) * 60 + int(mm)

        out = set()
        for start_str, end_str in windows:
            a = _parse_hhmm(start_str)
            b = _parse_hhmm(end_str)
            if a < base_minutes:
                a += 24 * 60
            if b < base_minutes:
                b += 24 * 60
            if b <= a:
                b += 24 * 60

            a_rel = a - base_minutes
            b_rel = b - base_minutes

            for t in range(1, max_t + 1):
                s = (t - 1) * dt_minutes
                e = t * dt_minutes
                if max(s, a_rel) < min(e, b_rel):
                    out.add(t)

        allowed = set(int(v) for v in self.time_series.time_intervals)
        return sorted(v for v in out if v in allowed)

    def _get_peak_intervals(self, windows: list = None, start_hour: int = 9):
        """Return sorted list of interval indices considered peak-hours.

        If windows is None a sensible default is used.
        """
        if windows is None:
            windows = [("18:00", "22:00")]
        return self._build_intervals_from_clock_windows(windows, start_hour=start_hour)

class OptSets(OptRules):
    def _extract_lhd_numeric_suffix(self, lhd_name):
        """Return the trailing integer in an LHD name, or None if absent."""
        match = re.search(r"(\d+)$", str(lhd_name))
        return int(match.group(1)) if match else None

    def _get_pause_definitions(self):
        """Detenciones DCH (legacy): pauses as (start_time, end_time, pause_type) in HH:MM.

        The optimization horizon starts at 09:00.
        Times strictly before 09:00 are interpreted as next day (e.g., 04:00).
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

    def _get_pause_definitions_det(self):
        """Detenciones DET (nuevo): pauses as (start_time, end_time, pause_type) in HH:MM.

        This contains the new, user-provided DET schedule.
        """
        pauses = [
            # --- Shift 2 (in progress): 08:00 - 16:00 ---
            # Shift change already started at 08:00, horizon captures from 09:00
            ("09:00", "09:40", "shift_change"),
            ("10:28", "12:58", "maintenance"),

            # --- Shift 3: 16:00 - 00:00 ---
            ("16:30", "17:40", "shift_change"),
            ("19:28", "21:58", "maintenance"),

            # --- Shift 1: 00:00 - 08:00 ---
            ("00:30", "01:40", "shift_change"),
            ("03:28", "05:58", "maintenance"),

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

        if pauses is None:
            pauses = self._get_pause_definitions()

        for start_str, end_str, ptype in pauses:
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

    def _build_intervals_from_clock_windows(self, windows, start_hour: int = 9):
        """Convert a list of (HH:MM, HH:MM) windows into interval indices.

        Uses self.time_series.delta_t (hours) and self.time_series.time_intervals.
        start_hour is the hour (0-23) considered as interval 1 start (default 9).
        """
        if not hasattr(self.time_series, "time_intervals") or len(self.time_series.time_intervals) == 0:
            return []

        dt_minutes = int(round(self.time_series.delta_t * 60))
        if dt_minutes <= 0:
            return []

        max_t = int(max(self.time_series.time_intervals))
        base_minutes = int(start_hour) * 60

        def _parse_hhmm(s):
            hh, mm = s.strip().split(":")
            return int(hh) * 60 + int(mm)

        out = set()
        for start_str, end_str in windows:
            a = _parse_hhmm(start_str)
            b = _parse_hhmm(end_str)
            if a < base_minutes:
                a += 24 * 60
            if b < base_minutes:
                b += 24 * 60
            if b <= a:
                b += 24 * 60

            a_rel = a - base_minutes
            b_rel = b - base_minutes

            for t in range(1, max_t + 1):
                s = (t - 1) * dt_minutes
                e = t * dt_minutes
                if max(s, a_rel) < min(e, b_rel):
                    out.add(t)

        allowed = set(int(v) for v in self.time_series.time_intervals)
        return sorted(v for v in out if v in allowed)

    def _get_peak_intervals(self, windows: list = None, start_hour: int = 9):
        """Return sorted list of interval indices considered peak-hours.

        If windows is None a sensible default is used.
        """
        if windows is None:
            # sensible default peak windows (adjust if needed)
            windows = [ ("18:00", "22:00")]
        return self._build_intervals_from_clock_windows(windows, start_hour=start_hour)
      
    
    
 

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
        det_stops = self._get_time_intervals_for_pause_type("stops", pauses=self._get_pause_definitions_det())
        model.time_intervals_det_set = pyo.Set(
            initialize=sorted(set(det_shift) | set(det_stops))
        )

        # Expose DET-specific subsets so they are serialized into parameters.json
        model.time_intervals_shift_change_det_set = pyo.Set(
            initialize=sorted(det_shift)
        )

        model.time_intervals_stops_set = pyo.Set(
            initialize=sorted(det_stops)
        )

        # DCH detentions (legacy) kept under a separate set name
        model.time_intervals_dch_det_set = pyo.Set(
            initialize=sorted(
                set(self._get_time_intervals_for_pause_type("shift_change"))
                | set(self._get_time_intervals_for_pause_type("fuel_delay"))
            )
        )



        
        # -----------------------------
        # Grupos de colaci�n para todos los LHD:
        # Grupo 1 = sufijo par, Grupo 2 = sufijo impar.
        # Si alg�n nombre no trae sufijo num�rico, se reparte en fallback para
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

        # Orden de precedencia para swaps: menor �ndice de LHD debe swapear antes.
        ordered_slhds = sorted(
            set(self.mine_system.get_swap_lhds()),
            key=lambda x: (
                self._extract_lhd_numeric_suffix(x) is None,
                self._extract_lhd_numeric_suffix(x) if self._extract_lhd_numeric_suffix(x) is not None else float("inf"),
                str(x),
            ),
        )
        model.swap_precedence_pairs = pyo.Set(
            dimen=2,
            initialize=[(ordered_slhds[idx], ordered_slhds[idx + 1]) for idx in range(len(ordered_slhds) - 1)],
        )

        # Tramos de penalizaci�n para d�ficit F (piecewise lineal)
        model.F_SEG = pyo.Set(initialize=[1, 2, 3, 4, 5])
        # Generadores renovables (vacío si no hay datos de generación)
        model.gen_set = pyo.Set(initialize=self.mine_system.get_system_generators())
        # Almacenamiento estacionario BESS (vacío si no hay hoja Storage)
        model.storage_set = pyo.Set(initialize=self.mine_system.get_system_storage())


class OptParameters(OptRules):

    def build_parameters(self, model):
        max_extraction_goal = max(
            (self.time_series.get_extraction_goal(j, d) for j in model.nodes_set for d in model.days),
            default=0.0
        )
        #Par�metros temporales
        model.delta_t = pyo.Param(initialize=self.time_series.delta_t, mutable=True)
        model.t_ini = pyo.Param(initialize=self.time_series.get_time_intervals()[0], mutable=True)
        model.t_fin = pyo.Param(initialize=self.time_series.get_time_intervals()[-1], mutable=True)
        #Par�metros econ�micos
        model.m_j = pyo.Param(model.nodes_set,model.days, initialize={(j, d): self.time_series.get_extraction_goal(j, d)for j in model.nodes_set for d in model.days},mutable=True)
        #model.costo_marginal = pyo.Param(model.slhd_set, model.days, model.time_intervals_set, initialize={(b, d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.elhd.get_energy_cost(b), d, t) for b in model.slhd_set for d in model.days for t in model.time_intervals_set}, mutable=True)
        model.costo_electricidad = pyo.Param(model.days, model.time_intervals_set, initialize={(d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.chargers.get_energy_cost(), d, t) for d in model.days for t in model.time_intervals_set}, mutable=True)
        #Par�metros LHD
        # Par�metros de viaje por nodo
        model.d_i    = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_n_intervals_trip(j,i)       for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pe_i   = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_energy_consumption(j,i)     for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pd_i = pyo.Param( model.lhd_set,model.nodes_set,initialize={(i, j): self.time_series.get_diesel_consumption(j,i) for i in model.lhd_set for j in model.nodes_set},mutable=False)
        # Par�metros de bater�a
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
         
        #Par�metros problema de inversi�n
        model.p_charger = pyo.Param(initialize=self.mine_system.chargers.get_charger_power(), mutable=False)
        model.p_max_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_p_max_ssee(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.p_peak = pyo.Param(initialize=self.mine_system.chargers.get_p_peak_dist(), mutable=False)
        model.charger_cost = pyo.Param(initialize=self.mine_system.chargers.get_charger_cost(), mutable=False)
        model.scaling_factor_op_cost = pyo.Param(initialize=self.time_series.scaling_factor_op_cost, mutable=True)
        model.demand_charge_coef = pyo.Param(initialize=12 * 10, mutable=True)
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
        # t_charge en intervalos con delta_t en horas:
        # ((model.bmax_b - model.bmax_b*model.bmin_b)/model.p_charger)/model.delta_t
        # Se usa solo el primer �ndice de slhd_set.
        first_slhd = next(iter(model.slhd_set), None)
        model.t_charge = pyo.Param(initialize=int(math.floor(((((value(model.bmax_b[first_slhd]) - value(model.bmax_b[first_slhd]) * value(model.bmin_b[first_slhd])) / value(model.p_charger)) / value(model.delta_t)) + 0.5))), mutable=False)

        # ---- Penalizaci�n por tramos para F (d�ficit) ----
        # Nota: para tramo 5 (100+) se usa Voll/0.1 (más caro).
        model.F_penalty_div = pyo.Param(model.F_SEG,initialize={1: 5000, 2: 1000, 3: 200, 4: 50, 5: 10},mutable=True)
        # Capacidad (longitud) de cada tramo
        model.F_penalty_cap = pyo.Param(model.F_SEG,initialize={1: 50, 2: 100, 3: 200, 4: 1000, 5: 0},mutable=True)
        model.Voll = pyo.Param(initialize=4*500, mutable=True)

        # Parámetros de generación renovable (solo si existen generadores)
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

        # Parámetros de almacenamiento BESS (solo si existen unidades)
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

    def B(self, model, b, d, t):
        return (0, model.bmax_b[b])

    def Bini(self, model, b, d):
        return (0, model.bmax_b[b])

    def build_all_variables(self, model):
        # Y(i,j,d,t) solo si j ? Nodes_assigned_at_interval(d,t,i)
        def _init_ZSWAP_INDEX(m):
            for slhd in m.slhd_set:
                station_list = self.time_series.mapper['Stations_per_elhd'].get(slhd, [])   
                for k in station_list:
                    yield (k,slhd)
        model.ZSWAP_INDEX = pyo.Set(dimen=2, initialize=_init_ZSWAP_INDEX)
        def _ZSWAP_DAYS_TIME(m):
            for (k,i) in m.ZSWAP_INDEX:
                for d in m.days:
                    for t in m.time_intervals_set:
                        yield (k, i, d, t)
        model.ZSWAP_DAYS_TIME = pyo.Set(dimen=4, initialize=_ZSWAP_DAYS_TIME)
        def _init_Y_INDEX(m):
            for d in m.days:
                for t in m.time_intervals_set:
                    for i in m.lhd_set:
                        node_list = self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)]
                        for j in node_list:
                            yield (i, j, d, t)
        # Sets de �ndices (solo contienen tuplas v�lidas)
        model.Y_INDEX  = pyo.Set(dimen=4, initialize=_init_Y_INDEX)
        # Peak intervals set (for demand-charge constraints)
        def _init_peak_set(m):
            return tuple(self._get_peak_intervals())
        model.time_intervals_peak_set = pyo.Set(initialize=_init_peak_set)
        # Variable binaria que indica si hay penalidad en la producci�n por swap
        #model.Z_pen = pyo.Var(model.Y_INDEX, domain=pyo.NonNegativeReals)
        #Viaje completo de LHD i al nodo j en (d,t)
        model.Y = pyo.Var(model.Y_INDEX, bounds=self.Y, domain=pyo.Binary)
        # Variable binaria que indica si el LHD esta inactivo o no
        model.Z         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set,bounds=self.Z, domain=pyo.Binary)
        # Variable binaria que indica si el LHD est� cargando
        #model.Z_charge  = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set,                          bounds=self.Z_charge, domain=pyo.Binary)
        # Variable binaria que indica si el LHD reliza un swap 
        model.Z_swap   = pyo.Var(model.ZSWAP_DAYS_TIME,domain=pyo.Binary)
        # Potencia de carga de bater�a b en (d,t)
        #model.P         = pyo.Var(model.stations_set,model.elhd_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        # SOC de bater�a b al final de (d,t)
        model.B         = pyo.Var(model.lhd_set, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        #Cantidad de cargadores
        model.N_chargers= pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers, bounds= (1, 12))
        #Elecci�n estaci�n de carga
        model.X = pyo.Var(model.stations_set, domain=pyo.Binary, bounds=(1,1))
        #Inicio de una carga on-board
        #model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)    
        # Indica si termina una carga en t
        #model.EndCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)
        # Cantidad de bater�as en estaci�n
        model.N_batteries = pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers, bounds=(1, 12))
        # Variable que actualiza el estado de carga de la bater�a del LHD
        model.B_s = pyo.Var(model.slhd_set, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        # N�mero de bater�as cargadas en el intervalo t en la estaci�n k
        model.S = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # N�mero de bater�as que comienzan a cargar en a y siguen conectadas en t en la estaci�n k
        model.Sv = pyo.Var(model.stations_set, model.days, model.time_intervals_set,  model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # N�mero de bater�as descargadas en el intervalo t en la estaci�n k
        model.X_dch = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # N�mero de bater�as que comienzan a cargar al inicio del intervalo t en la estaci�n k
        model.X_ini = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Demanda de bater�as en el intervalo t en la estaci�n k
        model.W = pyo.Var(model.stations_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Variable de potencia pico contratada / demand charge
        model.P_pot = pyo.Var(domain=pyo.NonNegativeReals)
        #extracci�n total del equipo i en el d�a d.
        model.M = pyo.Var(model.slhd_set, model.nodes_set, model.days, domain=pyo.NonNegativeReals)
        # Holgura producci�n
        model.F = pyo.Var(model.nodes_set, model.days,domain=pyo.NonNegativeReals)
        # Descomposici�n de F en tramos para costo piecewise lineal
        model.F_seg = pyo.Var(model.nodes_set, model.days, model.F_SEG,domain=pyo.NonNegativeReals)
        # Potencia comprada a la red en (d,t) [kW] — siempre presente
        model.P_red = pyo.Var(model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        # Variables de generación renovable (solo si existen generadores)
        if len(list(model.gen_set)) > 0:
            model.G_g = pyo.Var(model.gen_set, domain=pyo.NonNegativeIntegers)
            model.P_gen = pyo.Var(model.gen_set, model.days, model.time_intervals_set,
                                  domain=pyo.NonNegativeReals)
        # Variables de almacenamiento BESS (solo si existen unidades)
        if len(list(model.storage_set)) > 0:
            model.H_h = pyo.Var(model.storage_set, domain=pyo.Binary)
            model.P_bat = pyo.Var(model.storage_set, model.days, model.time_intervals_set,
                                  domain=pyo.Reals)
            model.A_h = pyo.Var(model.storage_set, model.days, model.time_intervals_set_zero,
                                domain=pyo.NonNegativeReals)

from src.optimization.functions import OptRules
import pyomo.environ as pyo

class ConstraintRules(OptRules):

    # ==========================================================
    # 1) Estado operacional del LHD (swap)
    # ==========================================================

    def state_unique_elhd_swap(self, model, i, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not nodes:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] + sum(model.Y[i, j, d, t] for j in nodes) + sum(model.Z_swap[k, i, d, t] for k in valid_k_list) == 1

    def between_shifts_elhd_swap(self, model, i, d, t):
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        return model.Z[i, d, t] + sum(model.Z_swap[k, i, d, t] for k in valid_k_list) == 1

    # ==========================================================
    # 2) Energ�a y SOC de bater�a (swap)
    # ==========================================================

    # Estado de energ�a considerando swap de bater�as
    def battery_soc_swap(self, model, i, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        discharge = sum(model.Y[i, j, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(d, t, i)])
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        penalization_charge = sum(model.Z_swap[k, i, d, t] * model.pk_i[k,i] for k in valid_k_list) * model.t_swap[i]
        
        if t >= t0:
            return model.B[i, d, t] ==  model.B_s[i, d, t-1] - discharge - penalization_charge
        
        else:
            return pyo.Constraint.Skip

    # Definici�n de B_s: actualizaci�n del estado de carga con swap (Convex-Hull Formulation)
    # New formulation: tighter Big-M coefficients for improved LP relaxation
    # z_agg = sum of Z_swap over all valid stations k for LHD i at (d,t)
    # U = b_max[i], L = b_min[i] * b_max[i]
    # Constraints:
    #   (1) B_s <= B + (U - L) * z_agg  (tighter upper bound when z=0: B_s <= B)
    #   (2) B_s >= B                    (sharpened lower bound)
    #   (3) B_s <= U                    (unconditional upper limit)
    #   (4) B_s >= L + (U - L) * z_agg  (dynamic lower limit: when z=0: B_s >= L impossible unless B >= L)
    
    def battery_soc_swap_update_1(self, model, i, d, t):
        """Convex-hull upper bound: B_s <= B + (U - L) * z_agg"""
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        U = model.bmax_b[i]
        L = model.bmin_b[i] * model.bmax_b[i]
        z_agg = sum(model.Z_swap[k, i, d, t] for k in valid_k_list)
        return model.B_s[i, d, t-1] <= model.B[i, d, t-1] + (U - L) * z_agg

    def battery_soc_swap_update_2(self, model, i, d, t):
        """Convex-hull lower bound (sharpened): B_s >= B"""
        return model.B_s[i, d, t-1] >= model.B[i, d, t-1]
    
    def battery_soc_swap_update_3(self, model, i, d, t):
        """Unconditional upper limit: B_s <= U"""
        return model.B_s[i, d, t-1] <= model.bmax_b[i]

    def battery_soc_swap_update_4(self, model, i, d, t):
        """Convex-hull lower limit (dynamic): B_s >= L + (U - L) * z_agg"""
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        U = model.bmax_b[i]
        L = model.bmin_b[i] * model.bmax_b[i]
        z_agg = sum(model.Z_swap[k, i, d, t] for k in valid_k_list)
        return model.B_s[i, d, t-1] >= L + (U - L) * z_agg

    def swap_soc_limit_30(self, model, i, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        if t < t0:
            return pyo.Constraint.Skip

        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip

        # Tight Big-M: if swap_flag=1 => B <= 0.30*U; if swap_flag=0 => B <= U.
        # This uses M = U-0.30*U = 0.70*U (stronger than M = U in LP relaxation).
        U = model.bmax_b[i]
        swap_flag = sum(model.Z_swap[k, i, d, t] for k in valid_k_list)
        return model.B[i, d, t-1] <= U - 0.70 * U * swap_flag

    # L�mite inferior de SOC bater�a
    def battery_lower(self, model, i, d, t):
        return model.B[i, d, t] >= model.bmin_b[i] * model.bmax_b[i]

    # L�mite superior de SOC bater�a
    def battery_upper(self, model, i, d, t):
        return model.B[i, d, t] <= model.bmax_b[i]

    # Condici�n de borde SOC bater�a
    def battery_boundary_swap(self, model, i, d):
        tf = self.time_series.get_time_intervals()[-1]
        return model.B_s[i, d, 0] == model.B[i, d, 0]
    
    def battery_energy_conservation(self, model, i, d):
        # Use interval 0 (initial pre-interval) to enforce cycle with final interval
        tf = self.time_series.get_time_intervals()[-1]
        return model.B[i, d, 0] == model.B[i, d, tf]

    def battery_boundary_break_simmetry_lhds_start(self, model, i_low, i_high, d):
        return model.B[i_low, d, 0] <= model.B[i_high, d, 0]


    # ==========================================================
    # 3) Producci�n y penalizaciones operacionales
    # ==========================================================

    # Producci�n m�nima
    def production_swap(self, model, d, j):
        target = model.m_j[j, d] 

        term_de = sum(
            model.Y[i, j, d, t] * self.time_series.get_n_trips(j, i)* model.g_i[i] * model.filling_factor[i]
            for (i, j2, d2, t) in model.Y_INDEX
            if j2 == j and d2 == d
        )

        return term_de + model.F[j, d] >= target
    
    def production_swap_max(self, model, d, j):
        # M�nimo de 10 viajes efectivos por punto de extracci�n y d�a.
        target = 0

        term_de = sum(
            model.Y[i, j, d, t] * self.time_series.get_n_trips(j, i)
            for (i, j2, d2, t) in model.Y_INDEX
            if j2 == j and d2 == d
        )

        #pen = sum(model.Z_pen[i, j, d, t] * model.g_i[i] * ntr(j, i) * model.filling_factor[i] * (model.t_swap[i] / model.delta_t)
        #          for (i, j2, d2, t) in model.Y_INDEX if j2 == j and d2 == d)

        return term_de  >= target
    
    def min_visits_per_node(self, model, j, d):
        """Garantiza que cada nodo `j` en el día `d` sea visitado al menos 1 intervalo.

        Suma `Y[i,j,d,t]` sobre todas las tuplas existentes en `model.Y` para (j,d).
        """
        term_visits = sum(
            model.Y[i2, j2, d2, t2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_visits >= 1

    def max_visits_node(self, model, j, d):
        """Limita el n�mero m�ximo de asignaciones (visitas) a un nodo `j` en el d�a `d`.

        """
        term_visits = sum(
            model.Y[i2, j2, d2, t2]
            for (i2, j2, d2, t2) in model.Y
            if j2 == j and d2 == d
        )

        return term_visits <= 4.

    def daily_production(self, model, d):
        """Production balance for the whole day d (sum over all nodes).

        Enforces: sum_{i,j,t} Y[i,j,d,t]*g_i*n_trips(j,i)*filling_factor[i] + sum_j F[j,d] <= sum_j m_j[j,d]
        """
        # Total target across all nodes
        total_target = 25000

        # Sum production term over all Y tuples for day d
        term_de = sum(
            model.Y[i2, j2, d2, t2] * model.g_i[i2] * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y
            if d2 == d
        )


        return  term_de <= total_target

    def aux_zpen_1(self, model, i, j, d, t):
        # Z_pen >= Z_swap + Y - 1  ?  fuerza Z_pen=1 cuando Y=1 y S Z_swap=1
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        return sum(model.Z_swap[k, i ,d, t] for k in valid_k_list) + model.Y[i, j ,d, t] - 1 <= model.Z_pen[i, j ,d, t]

    def aux_zpen_2(self, model, i, j, d, t):
        # Z_pen <= S Z_swap  ?  Z_pen=0 cuando no hay swap
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        return model.Z_pen[i, j ,d, t] <= sum(model.Z_swap[k, i ,d, t] for k in valid_k_list)

    def aux_zpen_3(self, model, i, j , d, t):
        # Z_pen <= Y  ?  Z_pen=0 cuando no hay viaje
        return model.Z_pen[i, j ,d, t] <= model.Y[i, j ,d, t]    
 

    def daily_extraction_M(self, model, i, j, d):
        """
        M[i,j,d] = extracci�n total del equipo i al nodo j en el d�a d,
        descontando la producci�n perdida por swaps simult�neos (Z_pen).
        Misma unidad que el t�rmino de producci�n (g_i * n_trips * f_i).
        """
        term = sum(
            model.Y[i2, j2, d2, t2] * model.g_i[i2]
            * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
            for (i2, j2, d2, t2) in model.Y_INDEX
            if i2 == i and d2 == d and j2 == j
        )
        #pen = sum(
        #    model.Z_pen[i2, j2, d2, t2] * model.g_i[i2]
        #    * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
        #    * (model.t_swap[i2] / model.delta_t)
        #    for (i2, j2, d2, t2) in model.Y_INDEX
        #    if i2 == i and d2 == d and j2 == j
        #)
        return model.M[i, j, d] == term #- pen

    # --------------------------
    # Penalizaci�n por tramos (piecewise) para F
    # --------------------------
    def F_piecewise_balance(self, model, j, d):
        # Sumatoria de tramos debe reconstruir F
        return sum(model.F_seg[j, d, s] for s in model.F_SEG) == model.F[j, d]

    def F_piecewise_caps(self, model, j, d, s):
        # Cada tramo tiene una "longitud" m�xima
        return model.F_seg[j, d, s] <= model.F_penalty_cap[s]

    # ==========================================================
    # 4) Infraestructura de estaciones y red el�ctrica
    # ==========================================================

    # Cantidad m�xima de cargadores
    def max_n_chargers(self, model, k):
        return model.N_chargers[k] <= model.max_chargers_k[k] * model.X[k]

    # Cantidad m�xima de bater�as
    def max_n_batteries(self, model, k):
        return model.N_batteries[k] <= model.nk_bat[k] * model.X[k]

    # Cantidad de cargadores no puede exceder la cantidad de bater�as en la estaci�n
    def chargers_le_batteries(self, model, k):
        return model.N_chargers[k] <= model.N_batteries[k]


    # Existencia de la estaci�n
    def station_existence_constraint_swap(self, model, k, i, d, t):
        return model.Z_swap[k,i, d, t] <= model.X[k]
    
    def charger_limit_swap(self, model, k, d, t):
        # Para cada estaci�n k, d�a d y intervalo t, la suma de bater�as
        # conectadas (para todos los inicios a) en t no puede exceder los cargadores
        return sum(model.Sv[k, d, t, a] for a in model.time_intervals_set) <= model.N_chargers[k]
    
    #  Sistemas distribuci�n 
    def max_installed_capacity_swap(self, model, k, d, t):
        return sum(model.Sv[k, d, t, a]*model.p_charger for a in model.time_intervals_set)  <= model.p_max_k[k]
    
    def peak_power_swap(self, model, d, t):
        return sum(model.Sv[k, d, t, a]*model.p_charger for k in model.stations_set for a in model.time_intervals_set) <= model.p_peak

    def power_peak_limit(self, model, d, t):
        """Demand-charge constraint: grid power during peak hours <= P_pot."""
        return model.P_red[d, t] <= model.P_pot

    # ==========================================================
    # 5) Inventario de bater�as en estaciones
    # ==========================================================

    # Demanda por swaps (bater�as requeridas)
    def total_swaps(self, model, k, d, t):
        valid_i_list = [i for (k2, i) in model.ZSWAP_INDEX if k2 == k]
        if not valid_i_list:
            return pyo.Constraint.Skip
        return model.W[k, d, t] == sum(model.Z_swap[k, i ,d, t] for i in valid_i_list)
    
    def max_swaps(self, model, i, d, t):
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        return sum(model.Z_swap[k, i ,d, t] for k in valid_k_list) <= 1

    def swap_precedence_by_index(self, model, i_low, i_high, d, t):
        """Forces cumulative swaps of lower-index LHDs to be >= higher-index LHDs at each time t."""
        valid_k_low = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i_low]
        valid_k_high = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i_high]
        if not valid_k_low or not valid_k_high:
            return pyo.Constraint.Skip

        cumulative_low = sum(
            model.Z_swap[k, i_low, d, tau]
            for k in valid_k_low
            for tau in model.time_intervals_set
            if tau <= t
        )
        cumulative_high = sum(
            model.Z_swap[k, i_high, d, tau]
            for k in valid_k_high
            for tau in model.time_intervals_set
            if tau <= t
        )
        return cumulative_low >= cumulative_high
    
    # Inventario de bater�as descargadas
    def inventory_discharged_batteries_rule(self, model, k, d, t):
        t0= self.time_series.get_time_intervals()[0]
        if t == t0:
            return pyo.Constraint.Skip
        else:
            return model.X_dch[k, d, t] == model.X_dch[k, d, t-1] - model.X_ini[k, d, t-1] + model.W[k, d, t]

    # Inventario de bater�as cargadas
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

    # 4. Duraci�n de la carga (L�gica interna del cargador)
    def charging_duration_rule(self, model, k, d, t, a):
        tf = self.time_series.get_time_intervals()[-1]
        if t == tf:
            return pyo.Constraint.Skip
        # Caso 1: La carga ya termin� (o no deber�a haber nada)
        if a <= t - model.t_charge + 1:
            return model.Sv[k, d, t+1, a] == 0
        
        # Caso 2: La carga est� en proceso (se mantiene el valor anterior)
        elif (t - model.t_charge + 1) <= a <= (t - 1):
            return model.Sv[k, d, t+1, a] == model.Sv[k, d, t, a]
        
        # Caso 3: La carga acaba de iniciar en este instante t (a==t)
        elif t == a:
            return model.Sv[k, d, t+1, a] == model.X_ini[k, d, t]
            
        return pyo.Constraint.Skip

    # Condiciones de inventario c�clico
    def CI_S(self, model, k, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        return model.S[k, d, t0] == model.S[k, d, tf]
    
    def CI_X_dch(self, model, k, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        return model.X_dch[k, d, t0] == model.X_dch[k, d, tf]
    
    def CB_general(self, model, k, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        return model.S[k, d, t0] + model.X_dch[k, d, t0] == model.N_batteries[k]
    

    # ==========================================================
    # 6) Pausas operacionales
    # ==========================================================

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


    # Pausas: MAINTENANCE
    def maint_stop_all(self, model, i, d, t):
        """En intervalos de mantenimiento TODOS los LHD deben
        estar estacionados (Z = 1). En mantenimiento los ELHD no pueden estar cargando."""
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z[i, d, t] == 1
    
    def det_stop_all(self, model, i, d, t):
        """En intervalos DET (shift_change + fuel_delay) todos los LHD deben estar estacionados (Z = 1)."""
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if t not in model.time_intervals_det_set:
            return sum(model.Z_swap[k, i, d, t] for k in valid_k_list) == 0
            #return pyo.Constraint.Skip
        return model.Z[i, d, t] + sum(model.Z_swap[k, i, d, t] for k in valid_k_list) == 1
    
    # Fijar baterias y cargadores
    def fix_n_chargers(self, model, k):
        if k == "station_1":
            return model.N_chargers[k] == 2
        elif k == "station_2":
            return model.N_chargers[k] == 2
        else:
            return model.N_chargers[k] == 2
        
    def fix_n_batteries(self, model, k):
        if k == "station_1":
            return model.N_batteries[k] == 2
        elif k == "station_2":
            return model.N_batteries[k] == 2
        else:
            return model.N_batteries[k] == 2
        
    #FORTALECIMIENTO 
    def n_swaps_limit_1(self, model, i, d):
        return sum(model.Z_swap[k, i ,d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) <= 5
    
    def n_swaps_limit_2(self, model, i, d):
        return sum(model.Z_swap[k, i ,d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) >= 3

    # ==========================================================
    # 7) Balance de potencia y generación renovable / BESS
    # ==========================================================

    def power_balance(self, model, d, t):
        """Demanda de carga (estaciones) = red + generación local + BESS."""
        demand = sum(
            model.Sv[k, d, t, a] * model.p_charger
            for k in model.stations_set
            for a in model.time_intervals_set
        )
        gen = (sum(model.P_gen[g, d, t] for g in model.gen_set)
               if len(list(model.gen_set)) > 0 else 0)
        bess = (sum(model.P_bat[h, d, t] for h in model.storage_set)
                if len(list(model.storage_set)) > 0 else 0)
        return model.P_red[d, t] + gen + bess == demand

    def grid_limit(self, model, d, t):
        """Potencia de red acotada por capacidad de la subestación."""
        return model.P_red[d, t] <= model.p_peak

    def gen_limit(self, model, g, d, t):
        """Potencia generada acotada por unidades instaladas y disponibilidad."""
        return model.P_gen[g, d, t] <= model.G_g[g] * model.p_max_g[g] * model.alpha_g[g, d, t]

    def gen_max_units(self, model, g):
        """Cantidad máxima de unidades instalables por tecnología."""
        return model.G_g[g] <= model.g_max_g[g]

    def bess_power_upper(self, model, h, d, t):
        """P_bat <= p_max_h * H_h (descarga máxima)."""
        return model.P_bat[h, d, t] <= model.p_max_h[h] * model.H_h[h]

    def bess_power_lower(self, model, h, d, t):
        """P_bat >= -p_max_h * H_h (carga máxima)."""
        return model.P_bat[h, d, t] >= -model.p_max_h[h] * model.H_h[h]

    def bess_soc_balance(self, model, h, d, t):
        """A_h,d,t = A_h,d,t-1 - (P_bat_h,d,t / eta_h) * delta_t."""
        t0 = self.time_series.get_time_intervals()[0]
        prev = model.A_h[h, d, 0] if t == t0 else model.A_h[h, d, t - 1]
        return model.A_h[h, d, t] == prev - (model.P_bat[h, d, t] / model.eta_h[h]) * model.delta_t

    def bess_soc_init(self, model, h, d):
        """Estado de energía inicial = 0 al comienzo de cada día."""
        return model.A_h[h, d, 0] == 0

    def bess_soc_upper(self, model, h, d, t):
        """A_h <= a_max_h * H_h."""
        return model.A_h[h, d, t] <= model.a_max_h[h] * model.H_h[h]

    def bess_soc_lower(self, model, h, d, t):
        """A_h >= a_min_h * H_h."""
        return model.A_h[h, d, t] >= model.a_min_h[h] * model.H_h[h]


    def build_all_constraints(self, model):
        # 1) Energ�a / SOC de bater�as del LHD (swap)
        model.battery_soc_swap = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap,
        )
        model.battery_soc_swap_update_1 = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap_update_1,
        )
        model.battery_soc_swap_update_2 = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap_update_2,
        )
        model.battery_soc_swap_update_3 = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap_update_3,
        )
        model.battery_soc_swap_update_4 = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap_update_4,
        )
        #model.swap_soc_limit_30 = pyo.Constraint(
        #    model.slhd_set,
        #    model.days,
        #    model.time_intervals_set,
        #    rule=self.swap_soc_limit_30,
        #)
        model.battery_lower = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.battery_lower,
        )
        model.battery_upper = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.battery_upper,
        )
        model.battery_boundary_swap = pyo.Constraint(model.slhd_set, model.days, rule=self.battery_boundary_swap)
        model.batter_energy_conservation = pyo.Constraint(model.slhd_set, model.days, rule=self.battery_energy_conservation)

        # 2) Infraestructura de estaciones y capacidad el�ctrica
        model.max_n_chargers = pyo.Constraint(model.stations_set, rule=self.max_n_chargers)
        model.max_n_batteries = pyo.Constraint(model.stations_set, rule=self.max_n_batteries)
        model.chargers_le_batteries = pyo.Constraint(model.stations_set, rule=self.chargers_le_batteries)
        #model.fix_stations = pyo.Constraint(model.stations_set, rule=self.fix_stations)
        #model.fix_n_chargers = pyo.Constraint(model.stations_set, rule=self.fix_n_chargers)
        #model.fix_n_batteries = pyo.Constraint(model.stations_set, rule=self.fix_n_batteries)
        model.station_existence_constraint_swap = pyo.Constraint(
            model.ZSWAP_DAYS_TIME,
            rule=self.station_existence_constraint_swap,
        )
        model.max_installed_capacity = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            rule=self.max_installed_capacity_swap,
        )
        model.charger_limit_swap = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            rule=self.charger_limit_swap,
        )
        model.peak_power_swap = pyo.Constraint(model.days, model.time_intervals_set, rule=self.peak_power_swap)
        model.power_peak_limit = pyo.Constraint(model.days, model.time_intervals_peak_set, rule=self.power_peak_limit)

        # 3) Operaci�n de LHD (estado, viajes y swaps)
        model.state_unique_elhd_swap = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.state_unique_elhd_swap,
        )
        model.between_shifts_elhd_swap = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_between_shifts_set,
            rule=self.between_shifts_elhd_swap,
        )
        model.total_swaps = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            rule=self.total_swaps,
        )
        model.max_swaps = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.max_swaps,
        )
        #model.n_swaps_limit_1 = pyo.Constraint(
        #    model.slhd_set,
        #    model.days,
        #    rule=self.n_swaps_limit_1,
        #)
        #model.n_swaps_limit_2 = pyo.Constraint(
        #   model.slhd_set,
        #   model.days,
        #   rule=self.n_swaps_limit_2,
        #)

        # 4) Inventario de bater�as en estaciones
        model.inventory_discharged_batteries = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            rule=self.inventory_discharged_batteries_rule,
        )
        model.inventory_charged_batteries = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            rule=self.inventory_charged_batteries_rule,
        )
        model.charging_duration = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            model.time_intervals_set,
            rule=self.charging_duration_rule,
        )
        model.CI_X_dch = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            rule=self.CI_X_dch,
        )
        model.CB_general = pyo.Constraint(
            model.stations_set,
            model.days,
            model.time_intervals_set,
            rule=self.CB_general,
        )

        # 5) Producci�n y penalizaciones
        model.production_swap = pyo.Constraint(model.days, model.nodes_set, rule=self.production_swap)
        #model.production_swap_max = pyo.Constraint(model.days, model.nodes_set, rule=self.production_swap_max)
        #model.aux_zpen_1 = pyo.Constraint(model.Y_INDEX, rule=self.aux_zpen_1)
        #model.aux_zpen_2 = pyo.Constraint(model.Y_INDEX, rule=self.aux_zpen_2)
        #model.aux_zpen_3 = pyo.Constraint(model.Y_INDEX, rule=self.aux_zpen_3)
        #model.daily_extraction = pyo.Constraint(
        #    model.days,
        #    rule=self.daily_production,
        #)
        model.daily_extraction_M = pyo.Constraint(
            model.slhd_set,
            model.nodes_set,
            model.days,
            rule=self.daily_extraction_M,
        )
        #model.min_visits_per_node = pyo.Constraint(model.nodes_set, model.days, rule=self.min_visits_per_node)
        #model.max_visits_node = pyo.Constraint(model.nodes_set, model.days, rule=self.max_visits_node)

        model.F_piecewise_balance = pyo.Constraint(
            model.nodes_set,
            model.days,
            rule=self.F_piecewise_balance,
        )
        model.F_piecewise_caps = pyo.Constraint(
            model.nodes_set,
            model.days,
            model.F_SEG,
            rule=self.F_piecewise_caps,
        )

        # 6) Pausas operacionales
        model.meal_g1_no_travel_group1 = pyo.Constraint(model.lhd_set, model.days, model.time_intervals_set, rule=self.meal_g1_no_travel_group1)
        model.meal_g2_no_travel_group2 = pyo.Constraint(model.lhd_set, model.days, model.time_intervals_set, rule=self.meal_g2_no_travel_group2)

        model.maintenance_stop_all = pyo.Constraint(
            model.slhd_set,
            model.days,
            model.time_intervals_set,
            rule=self.maint_stop_all,
        )
        #model.det_stop_all = pyo.Constraint(model.slhd_set, model.days, model.time_intervals_set, rule=self.det_stop_all)

        # 7) Balance de potencia y generación / BESS
        model.power_balance = pyo.Constraint(model.days, model.time_intervals_set, rule=self.power_balance)
        model.grid_limit    = pyo.Constraint(model.days, model.time_intervals_set, rule=self.grid_limit)

        if len(list(model.gen_set)) > 0:
            model.gen_limit     = pyo.Constraint(model.gen_set, model.days, model.time_intervals_set, rule=self.gen_limit)
            model.gen_max_units = pyo.Constraint(model.gen_set, rule=self.gen_max_units)

        if len(list(model.storage_set)) > 0:
            model.bess_power_upper = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_power_upper)
            model.bess_power_lower = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_power_lower)
            model.bess_soc_balance = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_soc_balance)
            model.bess_soc_init    = pyo.Constraint(model.storage_set, model.days, rule=self.bess_soc_init)
            model.bess_soc_upper   = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_soc_upper)
            model.bess_soc_lower   = pyo.Constraint(model.storage_set, model.days, model.time_intervals_set, rule=self.bess_soc_lower)

        # 8) Rotura simetr�a
        #model.battery_boundary_break_simmetry_lhds_start = pyo.Constraint(
        #    model.swap_precedence_pairs,
        #    model.days,
        #    rule=self.battery_boundary_break_simmetry_lhds_start,
        #)

        #model.swap_precedence_by_index = pyo.Constraint(
        #    model.swap_precedence_pairs,
        #    model.days,
        #    model.time_intervals_set,
        #    rule=self.swap_precedence_by_index,
        #)

class ObjectiveRules(OptRules):
    def lhd_charge_cost_bs(self, model):
        """Costo de electricidad comprada a la red (P_red * costo_electricidad)."""
        cost_el = sum(
            model.P_red[d, t] * model.costo_electricidad[d, t] * model.delta_t
            for d in model.days
            for t in model.time_intervals_set
        )
        return cost_el * model.scaling_factor_op_cost

    def penalization(self, model):
        F_penalty = sum(
            model.F_seg[j, d, s] * (model.Voll / model.F_penalty_div[s])
            for j in model.nodes_set
            for d in model.days
            for s in model.F_SEG
        )
        return F_penalty * model.scaling_factor_op_cost

    def inversion_cost(self, model):
        cost_inv = sum(
            model.station_cost_k[k] * model.X[k] + model.charger_cost * model.N_chargers[k]
            + model.battery_cost * model.N_batteries[k] for k in model.stations_set
        )
        return cost_inv

    def gen_investment_cost(self, model):
        if len(list(model.gen_set)) == 0:
            return 0
        return sum(model.G_g[g] * model.c_inv_g[g] * model.p_max_g[g] for g in model.gen_set)

    def gen_op_cost(self, model):
        if len(list(model.gen_set)) == 0:
            return 0
        return sum(model.G_g[g] * model.c_op_g[g] * model.p_max_g[g] for g in model.gen_set)

    def bess_investment_cost(self, model):
        if len(list(model.storage_set)) == 0:
            return 0
        return sum(model.H_h[h] * model.c_inv_h[h] for h in model.storage_set)

    def bess_op_cost(self, model):
        if len(list(model.storage_set)) == 0:
            return 0
        return sum(model.H_h[h] * model.c_op_h[h] for h in model.storage_set)

    def peak_power_cost(self, model):
        return model.P_pot * 12 * 10

    def total_cost(self, model):
        return (self.lhd_charge_cost_bs(model)
                + self.inversion_cost(model)
                + self.gen_investment_cost(model)
                + self.gen_op_cost(model)
                + self.bess_investment_cost(model)
                + self.bess_op_cost(model)
                + self.penalization(model)
                + self.peak_power_cost(model))

    def max_min_extraction(self, model):
        """Maximiza la cota inferior L de la extracci�n en todos los puntos."""
        return model.L

    def build_objective(self, model):
        model.obj = pyo.Objective(rule=self.total_cost, sense=pyo.minimize)

    #def build_objective(self, model):
    #    model.obj = pyo.Objective(rule=self.lhd_charge_cost_bs, sense=pyo.minimize)

class OutputManager(OptRules):

      def get_var(self, variable, index_names):
        """
        Devuelve un DataFrame con:
        - Una columna 'value'   (los valores num�ricos de la variable)
        - Una columna por cada �ndice (day, interval, lhd, battery, �)
        Esto deja el DF listo para las agrupaciones y filtros de printer.py
        """
        # Extrae valores de Pyomo ? dict {index_tuple: value}
        var_values = pd.DataFrame.from_dict(
            variable.extract_values(),
            orient='index',
            columns=['value']        # ? nombre est�ndar
        )

        # Convierte el dict-index en MultiIndex con nombres claros
        var_values.index = pd.MultiIndex.from_tuples(
            var_values.index,
            names=index_names
        )

        # �Clave!  ? pasa el MultiIndex a columnas
        var_values = var_values.reset_index()   #  ? a�ade day, interval, �

        return var_values






 
