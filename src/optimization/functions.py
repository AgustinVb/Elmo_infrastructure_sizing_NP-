import pyomo.environ as pyo
import math
import pandas as pd
import numpy as np
import time
import re
from pyomo.environ import quicksum, value

class OptRules(object):

    def __init__(self, mine_system,  time_series, autonomous_mode=False,
                 mccormick_degradation=False):
        self.mine_system = mine_system
        self.time_series = time_series
        # Escenario DET: False (default) = modo normal, la colacion solo
        # permite hacer swap o estar detenido. True = modo autonomo, la
        # colacion ademas permite operar (viajar/extraer). El cambio de turno
        # (between_shifts) siempre restringe a swap o detenido, en ambos
        # modos.
        self.autonomous_mode = autonomous_mode
        # Camino A (McCormick, ver degradacion_descomposicion_mccormick.md
        # sec. 3 en la rama carga_ob_multiaño): si True, los dos bilineales
        # encadenados de la degradacion del pool de swap (n_total_def,
        # n_ciclos_link -- ver ConstraintRules.build_mccormick_degradation_block)
        # se reemplazan por su envolvente convexa en vez de resolverse como
        # restricciones cuadraticas no convexas (Gurobi NonConvex=2). Sin
        # efecto si el escenario no tiene hoja BatteryDegradation.
        self.mccormick_degradation = mccormick_degradation
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

    def _build_intervals_from_clock_windows(self, windows, start_hour=None):
        """Convert a list of (HH:MM, HH:MM) windows into interval indices.

        Uses self.time_series.delta_t (hours) and self.time_series.time_intervals.
        start_hour is the hour (can be fractional, e.g. 8.5) considered as
        interval 1 start. Defaults to self.time_series.base_hour (columna
        'base_hour' de la hoja Shifts del escenario).
        """
        if not hasattr(self.time_series, "time_intervals") or len(self.time_series.time_intervals) == 0:
            return []

        dt_minutes = int(round(self.time_series.delta_t * 60))
        if dt_minutes <= 0:
            return []

        if start_hour is None:
            start_hour = self.time_series.base_hour

        max_t = int(max(self.time_series.time_intervals))
        base_minutes = int(round(start_hour * 60))

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
                # Punto medio del intervalo (redondeo al mas cercano, en vez
                # del sesgo sistematico hacia arriba del solape) -- mismo
                # criterio que _get_time_intervals_for_pause_type.
                mid = (t - 1) * dt_minutes + dt_minutes / 2
                if a_rel <= mid < b_rel:
                    out.add(t)

        allowed = set(int(v) for v in self.time_series.time_intervals)
        return sorted(v for v in out if v in allowed)

    def _get_peak_intervals(self, windows: list = None, start_hour=None):
        """Return sorted list of interval indices considered peak-hours.

        If windows is None a sensible default is used. start_hour defaults
        to self.time_series.base_hour (hoja Shifts del escenario).
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

        Esquema correcto (meal/maintenance/road_clearing), portado desde
        carga_on_board. Reemplaza el esquema viejo (shift_change/"stops")
        que dejaba time_intervals_det_set practicamente vacio por un
        pause_type mal escrito ("stops" en vez de "maintenance").
        """

        pauses = [
            # --- Shift 1: 00:00 - 08:00 ---
            ("00:30", "00:46", "between_shifts"),
            ("00:46", "01:42", "meal"),
            ("02:54", "03:18", "maintenance"),
            ("04:30", "05:42", "road_clearing"),
            ("06:46", "07:34", "maintenance"),

            # --- Shift 2 (next day): 08:00 - 16:00 ---
            ("08:30", "08:46", "between_shifts"),
            ("08:46", "09:42", "meal"),
            ("10:54", "11:26", "maintenance"),
            ("12:30", "13:42", "road_clearing"),
            ("14:46", "15:34", "maintenance"),

            # -- Shift 3 (next day): 16:00 - 00:00 ---
            ("16:30", "16:46", "between_shifts"),
            ("16:46", "17:42", "meal"),
            ("18:54", "19:26", "maintenance"),
            ("20:30", "21:42", "road_clearing"),
            ("22:46", "23:34", "maintenance"),
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
        base_minutes = int(round(self.time_series.base_hour * 60))

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

            # Marcar t cuyo punto medio cae dentro de [a,b) (redondeo al mas
            # cercano, en vez del sesgo sistematico hacia arriba del solape).
            for t in range(1, max_t + 1):
                mid = (t - 1) * dt_minutes + dt_minutes / 2
                if a <= mid < b:
                    indices.add(t)

        return sorted(indices)

    def _build_intervals_from_clock_windows(self, windows, start_hour=None):
        """Convert a list of (HH:MM, HH:MM) windows into interval indices.

        Uses self.time_series.delta_t (hours) and self.time_series.time_intervals.
        start_hour is the hour (can be fractional, e.g. 8.5) considered as
        interval 1 start. Defaults to self.time_series.base_hour (columna
        'base_hour' de la hoja Shifts del escenario).
        """
        if not hasattr(self.time_series, "time_intervals") or len(self.time_series.time_intervals) == 0:
            return []

        dt_minutes = int(round(self.time_series.delta_t * 60))
        if dt_minutes <= 0:
            return []

        if start_hour is None:
            start_hour = self.time_series.base_hour

        max_t = int(max(self.time_series.time_intervals))
        base_minutes = int(round(start_hour * 60))

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
                # Punto medio del intervalo (redondeo al mas cercano, en vez
                # del sesgo sistematico hacia arriba del solape) -- mismo
                # criterio que _get_time_intervals_for_pause_type.
                mid = (t - 1) * dt_minutes + dt_minutes / 2
                if a_rel <= mid < b_rel:
                    out.add(t)

        allowed = set(int(v) for v in self.time_series.time_intervals)
        return sorted(v for v in out if v in allowed)

    def _get_peak_intervals(self, windows: list = None, start_hour=None):
        """Return sorted list of interval indices considered peak-hours.

        If windows is None a sensible default is used. start_hour defaults
        to self.time_series.base_hour (hoja Shifts del escenario).
        """
        if windows is None:
            # sensible default peak windows (adjust if needed)
            windows = [ ("18:00", "22:00")]
        return self._build_intervals_from_clock_windows(windows, start_hour=start_hour)
      
    
    
 

    def build_sets(self, model):
        model.lhd_set = pyo.Set(initialize=self.mine_system.get_system_lhds())
        model.elhd_set = pyo.Set(initialize=self.mine_system.get_electric_lhds())
        model.slhd_set = pyo.Set(initialize=self.mine_system.get_swap_lhds())
        model.nodes_set = pyo.Set(initialize=self.mine_system.get_system_nodes())
        model.time_intervals_set = pyo.Set(initialize=self.time_series.time_intervals)
        model.days = pyo.Set(initialize=self.time_series.days_within_year)
        model.years = pyo.Set(initialize=self.time_series.years)
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

        # DET (nuevo) detentions: build sets using the DET pause definitions.
        # Modo normal: la colacion tambien impide operar (solo swap o estar
        # detenido), igual que maintenance/road_clearing.
        # Modo autonomo: la colacion queda fuera de este set, por lo que el
        # LHD puede ademas operar (viajar/extraer) durante esa ventana; solo
        # queda restringido a swap-o-detenido durante between_shifts (ver
        # between_shifts_elhd_swap / time_intervals_between_shifts_det_set).
        det_pauses = self._get_pause_definitions_det()
        det_meal_intervals = self._get_time_intervals_for_pause_type("meal", pauses=det_pauses)
        det_maintenance_intervals = self._get_time_intervals_for_pause_type("maintenance", pauses=det_pauses)
        det_road_clearing_intervals = self._get_time_intervals_for_pause_type("road_clearing", pauses=det_pauses)
        det_between_shifts_intervals = self._get_time_intervals_for_pause_type("between_shifts", pauses=det_pauses)
        det_stop = sorted(set(det_maintenance_intervals) | set(det_road_clearing_intervals))

        model.time_intervals_meal_det_set = pyo.Set(
            initialize=sorted(det_meal_intervals)
        )
        model.time_intervals_maintenance_det_set = pyo.Set(
            initialize=sorted(det_maintenance_intervals)
        )
        # Road clearing DET: al igual que colacion, el LHD puede estar
        # detenido o haciendo swap (a diferencia de maintenance, donde debe
        # permanecer detenido).
        model.time_intervals_road_clearing_det_set = pyo.Set(
            initialize=sorted(det_road_clearing_intervals)
        )
        # Cambio de turno (between_shifts) DET: extraido directamente de la
        # lista de pausas DET (antes se inferia de los huecos entre turnos de
        # la hoja Shifts del Excel). En ambos modos (normal/autonomo) el LHD
        # solo puede hacer swap o estar detenido durante esta ventana.
        model.time_intervals_between_shifts_det_set = pyo.Set(
            initialize=sorted(det_between_shifts_intervals)
        )

        if self.autonomous_mode:
            model.time_intervals_det_set = pyo.Set(
                initialize=sorted(set(det_stop))
            )
        else:
            model.time_intervals_det_set = pyo.Set(
                initialize=sorted(set(det_meal_intervals) | set(det_stop))
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

        # Generadores renovables (vacío si no hay datos de generación)
        model.gen_set = pyo.Set(initialize=self.mine_system.get_system_generators())
        # Almacenamiento estacionario BESS (vacío si no hay hoja Storage)
        model.storage_set = pyo.Set(initialize=self.mine_system.get_system_storage())


class OptParameters(OptRules):

    def build_parameters(self, model):
        #Par�metros temporales
        model.delta_t = pyo.Param(initialize=self.time_series.delta_t, mutable=True)
        model.base_hour = pyo.Param(initialize=self.time_series.base_hour, mutable=True)
        model.t_ini = pyo.Param(initialize=self.time_series.get_time_intervals()[0], mutable=True)
        model.t_fin = pyo.Param(initialize=self.time_series.get_time_intervals()[-1], mutable=True)
        #Par�metros econ�micos
        model.m_j = pyo.Param(model.nodes_set, model.years, initialize={(j, y): self.time_series.get_extraction_goal(j, y) for j in model.nodes_set for y in model.years}, mutable=True)
        model.costo_electricidad = pyo.Param(model.years, model.days, model.time_intervals_set, initialize={(y, d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.chargers.get_energy_cost(), d, t) for y in model.years for d in model.days for t in model.time_intervals_set}, mutable=True)
        #Par�metros LHD
        # Par�metros de viaje por nodo
        model.d_i    = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_n_intervals_trip(j,i)       for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pe_i   = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_energy_consumption(j,i)     for i in model.lhd_set for j in model.nodes_set}, mutable=False)
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

        # Eficiencia de carga/descarga de la bateria (hoja LHD)
        model.eta_charge_i = pyo.Param(
        model.slhd_set,
        initialize={i: float(self.mine_system.elhd.get_charge_efficiency(i)) for i in model.slhd_set},
        mutable=False)

        model.eta_discharge_i = pyo.Param(
        model.slhd_set,
        initialize={i: float(self.mine_system.elhd.get_discharge_efficiency(i)) for i in model.slhd_set},
        mutable=False)
        # Capacidad de pala
        model.g_i    = pyo.Param(model.lhd_set,                   initialize={i: self.mine_system.elhd.get_load_capacity(i)       for i in model.lhd_set}, mutable=False)
        model.filling_factor = pyo.Param(model.lhd_set,        initialize={i: self.mine_system.elhd.get_filling_factor(i)      for i in model.lhd_set}, mutable=False)
         
        #Par�metros problema de inversi�n
        _p_charger_val = self.mine_system.chargers.get_charger_power()
        model.p_charger = pyo.Param(initialize=_p_charger_val, mutable=False)
        model.p_peak = pyo.Param(initialize=self.mine_system.chargers.get_p_peak_dist(), mutable=False)
        # Cantidad maxima de baterias cargando en paralelo equivalente a
        # p_peak, redondeada hacia abajo: usar esta cota entera directamente
        # sobre Sv (en vez de Sv*p_charger <= potencia) da una relajacion
        # lineal mas apretada cuando el cociente no es entero. p_max_k/
        # n_max_k (potencia/conteo maximo POR ESTACION) se eliminaron: esa
        # capacidad ahora es una decision de inversion (N_max_k mas abajo,
        # decidida UNA sola vez para todo el horizonte, sin cota fisica --
        # mismo patron que G_g/H).
        model.n_peak_max = pyo.Param(
            initialize=math.floor(self.mine_system.chargers.get_p_peak_dist() / _p_charger_val),
            mutable=False,
            within=pyo.NonNegativeIntegers,
        )
        model.c_inv_ssee_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_inv_ssee(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.charger_cost = pyo.Param(initialize=self.mine_system.chargers.get_charger_cost(), mutable=False)
        model.battery_cost = pyo.Param(initialize=self.mine_system.chargers.get_battery_cost(), mutable=False)
        # Cantidad de LHD swap efectivamente activos (con al menos un nodo
        # asignado) en cada año, segun NodeAssignment (fuente ya validada de
        # la rampa de flota -- ver hoja FleetByYear). Usado en n_battery_fleet_def
        # (ConstraintRules) para la flota fisica de baterias a degradar/
        # reemplazar: antes se usaba len(model.slhd_set) (roster completo,
        # constante en todos los años), lo que sobrestimaba las baterias
        # instaladas en los años en que todavia no esta activo todo el roster.
        _na_active = self.time_series.mapper['NodeAssignment']
        _slhd_names = set(model.slhd_set)
        model.n_active_slhd = pyo.Param(
            model.years,
            initialize={
                y: len(set(_na_active.loc[_na_active['year'] == y, 'elhd_name']) & _slhd_names)
                for y in model.years
            },
            within=pyo.NonNegativeIntegers,
            mutable=False,
        )
        model.scaling_factor_op_cost = pyo.Param(initialize=self.time_series.scaling_factor_op_cost, mutable=True)
        model.demand_charge_coef = pyo.Param(initialize=12 * 10, mutable=True)
        #Parametros estaciones de carga
        model.station_cost_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_station_cost(k) for k in model.stations_set}, mutable=False)
        model.c_bays_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_bays(k) for k in model.stations_set}, mutable=False)
        model.c_charger_space_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_charger_space(k) for k in model.stations_set}, mutable=False)
        model.c_battery_space_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_battery_space(k) for k in model.stations_set}, mutable=False)
        model.max_bays_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_bays(k) for k in model.stations_set}, mutable=False)
        model.max_batteries_per_bay_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_batteries_per_bay(k) for k in model.stations_set}, mutable=False)
        model.max_chargers_per_bay_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_chargers_per_bay(k) for k in model.stations_set}, mutable=False)
        model.c_crane_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_crane(k) for k in model.stations_set}, mutable=False)
        #model.t_charge = pyo.Param(model.lhd_set, initialize={i: self.mine_system.elhd.get_charge_time(i) for i in model.lhd_set}, mutable=False)
        # t_charge en intervalos con delta_t en horas:
        # ((model.bmax_b - model.bmax_b*model.bmin_b)/model.p_charger)/model.delta_t
        # Se usa solo el primer �ndice de slhd_set.
        first_slhd = next(iter(model.slhd_set), None)
        # eta_charge reduce la potencia efectiva que llega a la bateria, por lo
        # que se necesitan mas intervalos para completar la carga.
        model.t_charge = pyo.Param(initialize=int(math.floor(((((value(model.bmax_b[first_slhd]) - value(model.bmax_b[first_slhd]) * value(model.bmin_b[first_slhd])) / (value(model.p_charger) * value(model.eta_charge_i[first_slhd]))) / value(model.delta_t)) + 0.5))), mutable=False)

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
                model.gen_set, model.years, model.days, model.time_intervals_set,
                initialize={(g, y, d, t): self.time_series.get_alpha_g(g, d, t)
                            for g in model.gen_set
                            for y in model.years
                            for d in model.days
                            for t in model.time_intervals_set},
                mutable=False)

        # Parámetros de almacenamiento BESS (solo si existen unidades)
        if len(list(model.storage_set)) > 0:
            storage_names = list(model.storage_set)
            if len(storage_names) > 1:
                # El modelo de inversion multi-anio de BESS (H, sin indice h)
                # asume un unico cluster de almacenamiento candidato, ver
                # docs/restricciones_modelo.tex sec. Sistema de distribucion
                # electrica, generacion y almacenamiento.
                raise ValueError(
                    "El modelo de inversion multi-anio de BESS asume un unico "
                    "cluster de almacenamiento (una sola fila en la hoja "
                    f"Storage); se encontraron {len(storage_names)}: {storage_names}"
                )
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
            # Cota de unidades del cluster instalables en todo el horizonte
            # (H no tiene cota superior natural al ser entera).
            model.h_max = pyo.Param(initialize=stor.get_h_max(storage_names[0]), mutable=False)

        # Degradacion/reemplazo de baterias del pool de swap (global, solo
        # si la hoja BatteryDegradation existe)
        bd = self.mine_system.battery_degradation
        if bd is not None:
            model.gamma_coef = pyo.Param(initialize=bd.get_gamma_coef(), mutable=True)
            model.c_bat_replace = pyo.Param(initialize=bd.get_c_bat_replace(), mutable=True)
            model.min_capacity_fraction = pyo.Param(initialize=bd.get_min_capacity_fraction(), mutable=True)

            # Capacidad de referencia del pool: todas las baterias de swap
            # (slhd_set) deben compartir la misma capacidad nominal, porque
            # b_bar/R se modelan una vez para todo el pool, no por LHD/estacion.
            bmax_values = {round(float(self.mine_system.elhd.get_e_max(b)), 6) for b in model.slhd_set}
            if len(bmax_values) > 1:
                raise ValueError(
                    "Degradacion de bateria (pool global) requiere que todas las "
                    f"baterias de swap compartan la misma capacidad nominal (e_max); "
                    f"valores encontrados: {bmax_values}"
                )
            model.b_max_pool = pyo.Param(initialize=next(iter(bmax_values)), mutable=False)

        # Tasa de descuento: 0.0 (sin descuento, comportamiento actual) si no
        # hay datos de degradacion cargados o si la hoja no trae la columna.
        model.discount_rate = pyo.Param(initialize=(bd.get_discount_rate() if bd is not None else 0.0), mutable=True)

class BoundRules(OptRules):

    def Z(self, model, i, y, d, t):
        return 0, 1

    def Z_charge(self, model, k, i, y, d, t):
        return 0, 1

    def B(self, model, b, y, d, t):
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
                for y in m.years:
                    for d in m.days:
                        for t in m.time_intervals_set:
                            yield (k, i, y, d, t)
        model.ZSWAP_DAYS_TIME = pyo.Set(dimen=5, initialize=_ZSWAP_DAYS_TIME)
        def _init_Y_INDEX(m):
            for y in m.years:
                for d in m.days:
                    for t in m.time_intervals_set:
                        for i in m.lhd_set:
                            node_list = self.time_series.mapper['Nodes_assigned_at_interval'][(y, d, t, i)]
                            for j in node_list:
                                yield (i, j, y, d, t)
        # Sets de �ndices (solo contienen tuplas v�lidas)
        model.Y_INDEX = pyo.Set(dimen=5, initialize=_init_Y_INDEX)
        # Peak intervals set (for demand-charge constraints)
        def _init_peak_set(m):
            return tuple(self._get_peak_intervals())
        model.time_intervals_peak_set = pyo.Set(initialize=_init_peak_set)
        # Variable binaria que indica si hay penalidad en la producci�n por swap
        #model.Z_pen = pyo.Var(model.Y_INDEX, domain=pyo.NonNegativeReals)
        #Viaje completo de LHD i al nodo j en (d,t)
        model.Y = pyo.Var(model.Y_INDEX, domain=pyo.Binary)
        # Variable binaria que indica si el LHD esta inactivo o no
        model.Z         = pyo.Var(model.lhd_set, model.years, model.days, model.time_intervals_set, bounds=self.Z, domain=pyo.Binary)
        # Variable binaria que indica si el LHD est� cargando
        #model.Z_charge  = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set,                          bounds=self.Z_charge, domain=pyo.Binary)
        # Variable binaria que indica si el LHD reliza un swap
        model.Z_swap    = pyo.Var(model.ZSWAP_DAYS_TIME, domain=pyo.Binary)
        # Inicio/fin de una asignacion a extraccion (Y agregado sobre nodos),
        # para exigir minimo 2 intervalos consecutivos asignado a extraccion.
        model.StartAssign = pyo.Var(model.slhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
        model.EndAssign   = pyo.Var(model.slhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
        # Potencia de carga de bater�a b en (d,t)
        #model.P         = pyo.Var(model.stations_set,model.elhd_set, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        # SOC de bater�a b al final de (d,t)
        model.B         = pyo.Var(model.lhd_set, model.years, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        # Inversion multi-anio: X/N_bays/N_chargers/N_batteries pasan a ser el
        # STOCK acumulado por año (usado en el resto de las restricciones
        # operativas), y Delta_* es la decision de inversion propiamente tal
        # del año y (la que entra al costo). Ver restricciones de enlace
        # link_station_stock/link_bays_stock/link_charger_stock/
        # link_battery_stock mas abajo, que acumulan Delta_* año a año desde
        # un stock inicial cero (escenario greenfield). Sin cota manual
        # (1,12): la cota real viene de max_n_bays/max_chargers_per_bay_constr/
        # max_batteries_per_bay_constr (datos de la hoja stations).
        model.X                = pyo.Var(model.stations_set, model.years, domain=pyo.Binary)
        model.Delta_X          = pyo.Var(model.stations_set, model.years, domain=pyo.Binary)
        model.N_bays           = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        model.Delta_N_bays     = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        model.N_chargers       = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        model.Delta_N_chargers = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        # Potencia de subestacion, expresada como conteo entero de baterias
        # cargando en paralelo (mismo conteo que usa max_installed_capacity_
        # swap) en vez de kW -- decidida UNA sola vez para todo el horizonte
        # (sin indice de año, sin Delta), mismo patron que G_g/H, sin cota
        # fisica.
        model.N_max_k = pyo.Var(model.stations_set, domain=pyo.NonNegativeIntegers)
        #Inicio de una carga on-board
        #model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)
        # Indica si termina una carga en t
        #model.EndCharge = pyo.Var(model.stations_set, model.elhd_set, model.days, model.time_intervals_set, domain=pyo.Binary)
        # Cantidad de bater�as en estaci�n
        model.N_batteries       = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        model.Delta_N_batteries = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        # Variable que actualiza el estado de carga de la bater�a del LHD
        model.B_s = pyo.Var(model.slhd_set, model.years, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        # N�mero de bater�as cargadas en el intervalo t en la estaci�n k
        model.S = pyo.Var(model.stations_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # N�mero de bater�as que comienzan a cargar en a y siguen conectadas en t en la estaci�n k
        #
        # Sv[k,y,d,t,a] solo puede ser distinto de 0 si "a" (inicio de carga)
        # cae dentro de la ventana de duracion de carga que termina en t:
        # a en [max(t0, t-t_charge+1), t-1] -- fuera de esa ventana,
        # charging_duration_rule (mas abajo) lo fija en 0 de todas formas, y
        # Sv no aparece en la funcion objetivo (solo en restricciones <= y en
        # power_balance), asi que esas combinaciones nunca tienen incentivo a
        # ser > 0. Antes se declaraba sobre el producto cartesiano COMPLETO
        # time_intervals_set x time_intervals_set (t_charge suele ser una
        # fraccion pequeña de la cantidad total de intervalos del dia), lo que
        # generaba una cantidad de variables/restricciones triviales ("=0")
        # cuadratica en el numero de intervalos y dominaba el tiempo de
        # construccion del modelo. SV_INDEX solo contiene las combinaciones
        # (k,y,d,t,a) fisicamente posibles.
        def _init_SV_INDEX(m):
            intervals = sorted(int(t) for t in m.time_intervals_set)
            t0 = intervals[0]
            t_charge = int(value(m.t_charge))
            for k in m.stations_set:
                for y in m.years:
                    for d in m.days:
                        for t in intervals:
                            lo = max(t0, t - t_charge + 1)
                            hi = t - 1
                            for a in range(lo, hi + 1):
                                yield (k, y, d, t, a)
        model.SV_INDEX = pyo.Set(dimen=5, initialize=_init_SV_INDEX)
        model.Sv = pyo.Var(model.SV_INDEX, domain=pyo.NonNegativeIntegers)
        # N�mero de bater�as descargadas en el intervalo t en la estaci�n k
        model.X_dch = pyo.Var(model.stations_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # N�mero de bater�as que comienzan a cargar al inicio del intervalo t en la estaci�n k
        model.X_ini = pyo.Var(model.stations_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Demanda de bater�as en el intervalo t en la estaci�n k
        model.W = pyo.Var(model.stations_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeIntegers)
        # Variable de potencia pico contratada / demand charge
        model.P_pot = pyo.Var(model.years, domain=pyo.NonNegativeReals)
        #extracci�n total del equipo i en el d�a d.
        model.M = pyo.Var(model.slhd_set, model.nodes_set, model.years, model.days, domain=pyo.NonNegativeReals)
        # Potencia comprada a la red en (d,t) [kW] — siempre presente
        model.P_red = pyo.Var(model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        # Variables de generación renovable (solo si existen generadores)
        if len(list(model.gen_set)) > 0:
            # G_g = capacidad instalada (fraccion de p_max_g), decidida UNA
            # sola vez para todo el horizonte (sin indice de año, sin Delta)
            # -- mismo patron que la rama de horizonte unico battery_swapping.
            # Continua (relajacion del numero de unidades de generacion):
            # permite instalar capacidad fraccionaria de cada tecnologia.
            model.G_g = pyo.Var(model.gen_set, domain=pyo.NonNegativeReals)
            model.P_gen = pyo.Var(model.gen_set, model.years, model.days, model.time_intervals_set,
                                  domain=pyo.NonNegativeReals)
            # Curtailment: potencia renovable vertida en (d,t) [kW] — ec. 3.47
            model.Curt_g = pyo.Var(model.gen_set, model.years, model.days, model.time_intervals_set,
                                   domain=pyo.NonNegativeReals)
        # Variables de almacenamiento BESS (solo si existen unidades)
        if len(list(model.storage_set)) > 0:
            # Un unico cluster de almacenamiento (ver guardia de cardinalidad
            # en build_parameters): H = unidades instaladas del cluster,
            # decidida UNA sola vez para todo el horizonte (sin indice de
            # año, sin Delta) -- mismo patron que G_g. Sin indice h a
            # diferencia de la formulacion de horizonte unico (H_h binaria).
            # Continua (relajacion del numero de unidades del cluster BESS).
            model.H = pyo.Var(domain=pyo.NonNegativeReals)
            model.P_bat = pyo.Var(model.storage_set, model.years, model.days, model.time_intervals_set,
                                  domain=pyo.Reals)
            model.A_h = pyo.Var(model.storage_set, model.years, model.days, model.time_intervals_set_zero,
                                domain=pyo.NonNegativeReals)

        # Degradación de batería del pool de swap (global, fleet-wide), solo
        # si hay datos de degradación cargados. Modelo por ciclos con
        # recursión año a año, análogo a carga_ob_multiaño (ver B[y]/D[y]/
        # b_y_link/n_ciclos_link/d_y_fade en ConstraintRules): B[y] (=b_bar[y])
        # es la capacidad POR BATERÍA al inicio del año, D[y] al final. A
        # diferencia de on-board, la ec. de ciclos tiene un factor extra:
        # además de N_ciclos[y]*B[y] hay que multiplicar por
        # n_battery_fleet[y] (acá SÍ es una variable -- tamaño de la flota
        # física de baterías, pool + una por LHD -- a diferencia de on-board
        # donde n_elhd es una constante), así que es un producto de TRES
        # variables. Se reduce a grado 2 introduciendo una variable auxiliar
        # para UNO de los tres productos parciales y encadenando dos
        # restricciones cuadráticas (ver n_total_def/n_ciclos_link en
        # ConstraintRules), resueltas directo por Gurobi (NonConvex=2) -- ya
        # no hace falta el one-hot de n_battery_fleet que usaba la
        # alternativa lineal anterior para exactificar la división por ese
        # factor.
        #
        # De las 3 combinaciones posibles de variable auxiliar (B_total=B*
        # n_battery_fleet: N_ciclos[y]*B_total[y]=RHS; NB=N_ciclos*B:
        # NB[y]*n_battery_fleet[y]=RHS; N_total=N_ciclos*n_battery_fleet:
        # N_total[y]*B[y]=RHS), la relajación en la raíz (LP en el nodo 0)
        # dio IDÉNTICA en las tres (841,912.4) -- este trilineal es una
        # fracción mínima de un modelo de 150k filas/99k binarias dominado
        # por la programación de swaps, así que el pairing no mueve la cota
        # dual de partida. La diferencia real apareció aguas abajo, en el
        # comportamiento del branch-and-bound: probadas las 3 con el mismo
        # presupuesto de tiempo (200s, escenario 320kW, 5 años x 1 día), la
        # variante N_total=N_ciclos*n_battery_fleet fue la ÚNICA que encontró
        # soluciones factibles (10, mejor incumbente 2.62e6) -- B_total y NB
        # no encontraron ninguna en el mismo tiempo (Solution count 0), con
        # cotas duales similares o levemente peores. Se deja N_total activa
        # por eso (mejor comportamiento práctico de Gurobi con esta
        # instancia, no por una relajación matemáticamente más ajustada).
        if self.mine_system.battery_degradation is not None:
            # Flota de LHD swap ACTIVA por año (no el roster completo, ver
            # model.n_active_slhd en OptParameters) -- usada para las cotas
            # del tamaño físico de la flota de baterías por año.
            n_elhd_by_year = {y: int(value(model.n_active_slhd[y])) for y in model.years}
            n_elhd_min = min(n_elhd_by_year.values())
            n_elhd_max = max(n_elhd_by_year.values())

            # Inversion multi-año, arranque greenfield: N_batteries[k,y] parte
            # en 0 (sin piso de 1 por estación), por lo que N_min=0. La cota
            # superior N_max ya no es el literal 12 por estación (asociado a
            # los bounds fijos que tenía N_batteries antes de esta extensión)
            # sino la cota física real de datos: max_bays_k * max_batteries_per_bay_k,
            # sumada sobre todas las estaciones (ver eq. max_naves/
            # max_baterias_nave en docs/restricciones_modelo.tex).
            N_min = 0
            N_max = sum(
                int(value(model.max_bays_k[k])) * int(value(model.max_batteries_per_bay_k[k]))
                for k in model.stations_set
            )

            # N_batteries_total[y] = tamaño del pool en estación al año y
            # (suma de N_batteries[k,y] por estación) — NO es el total de
            # baterías físicas: cada LHD swap ACTIVO ese año tiene además su
            # propia batería instalada. La flota física completa a
            # degradar/reemplazar es
            # n_battery_fleet[y] = N_batteries_total[y] + n_active_slhd[y]
            # (flota swap ACTIVA ese año, no el roster completo -- ver
            # n_elhd_by_year arriba).
            model.N_batteries_total = pyo.Var(model.years, domain=pyo.NonNegativeIntegers, bounds=(N_min, N_max))

            NF_max = N_max + n_elhd_max
            model.n_battery_fleet = pyo.Var(
                model.years, domain=pyo.NonNegativeIntegers,
                bounds=lambda m, y: (N_min + n_elhd_by_year[y], N_max + n_elhd_by_year[y]),
            )
            # NF_max_param se sigue usando en z_repl_upper2 (costo de
            # reemplazo, sin relación con la ec. 3 de degradación). Es un
            # big-M global (max sobre años de NF_max), no necesita ser
            # ajustado por año -- solo debe ser una cota válida en todos.
            model.NF_max_param = pyo.Param(initialize=NF_max, mutable=False)

            b_max_val = value(model.b_max_pool)
            min_frac_val = value(model.min_capacity_fraction)
            B_L = min_frac_val * b_max_val
            B_U = b_max_val
            model.B_L = pyo.Param(initialize=B_L, mutable=False)
            model.B_U = pyo.Param(initialize=B_U, mutable=False)

            # Energía máxima cargable en un año por toda la flota (todas las
            # estaciones a su N_batteries máximo físico (max_bays_k *
            # max_batteries_per_bay_k, ya sumado en N_max arriba), todos los
            # días representativos del año, a potencia nominal del cargador).
            # SIN escalar a año completo todavía (mismas unidades que
            # EnergyConsumed[y], ver energy_consumed_def en ConstraintRules) --
            # se escala explícito con scaling_factor_op_cost donde haga falta
            # (ver n_ciclos_max abajo y n_ciclos_link).
            n_slots_per_year = N_max * len(model.days) * len(model.time_intervals_set)
            max_energy_per_year = n_slots_per_year * value(model.p_charger) * value(model.delta_t)

            # Cota (holgada) de N_ciclos[y]: peor caso = máxima energía anual
            # posible / mínima capacidad total instalada posible en CUALQUIER
            # año (N_min + n_elhd_min * B_L). Se usa el mínimo de flota activa
            # entre años (no un año fijo) para que la cota sea válida en
            # todos; se resguarda con max(.,1) por si algún año tuviera 0 LHD
            # swap activos (evita división por cero). Necesaria para que
            # Gurobi arme la relajación McCormick de los bilineales de
            # n_ciclos_link/n_total_def.
            nf_min_for_bound = N_min + max(n_elhd_min, 1)
            n_ciclos_max = (max_energy_per_year * value(model.scaling_factor_op_cost)) / (nf_min_for_bound * B_L)

            model.R        = pyo.Var(model.years, domain=pyo.Binary)
            model.b_bar    = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(B_L, B_U))
            model.D        = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(B_L, B_U))

            # Energía CONSUMIDA por los LHD (descarga real, misma expresión
            # que el término "discharge" de battery_soc_swap) en el año y --
            # se usa consumo en vez de energía cargada en estación (Sv)
            # porque esta última puede quedar desfasada del consumo real
            # dentro de un mismo día representativo (una batería swapeada
            # tarde en el día puede no terminar de cargar ese mismo día), lo
            # que subestimaba el desgaste — además así es comparable con
            # on-board, que mide energía en la misma batería que descarga.
            model.EnergyConsumed = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(0, max_energy_per_year))
            model.N_ciclos = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(0, n_ciclos_max))
            # N_total[y] = N_ciclos[y] * n_battery_fleet[y]: ciclos
            # equivalentes TOTALES de la flota de baterías en el año y (no
            # por batería) -- variable auxiliar elegida para la reducción de
            # grado del trilineal, ver nota de comparación empírica arriba.
            model.N_total = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(0, n_ciclos_max * NF_max))

            # Linealización exacta (big-M) de P_bbar_zagg[i,y,d,t] = b_bar[y] *
            # z_agg (z_agg = suma de Z_swap para el LHD i en (y,d,t)) — evita
            # el producto variable×variable b_bar[y]*z_agg dentro de las
            # fórmulas Big-M de battery_soc_swap_update_1/4 (y swap_soc_limit_30).
            # No tiene relación con la ec. 3 de degradación (restricción de
            # SOC aparte) -- se mantiene igual que antes.
            model.P_bbar_zagg = pyo.Var(model.slhd_set, model.years, model.days, model.time_intervals_set,
                                         domain=pyo.NonNegativeReals, bounds=(0, b_max_val))

            years_sorted = sorted(model.years)
            # B[y1] = b_max_pool fijo (ec. 5) y R[y1] = 0 fijo -- la batería
            # ya es nueva en el primer año, así que "reemplazarla" en y1 no
            # tiene efecto sobre la capacidad y solo agregaría costo evitable
            # (mismo patrón que carga_ob_multiaño). No se usa self._first_year()
            # acá (solo existe en ConstraintRules, no en BoundRules) -- pero
            # como esta rama no tiene descomposición por bloques anuales,
            # years_sorted[0] y el primer año GLOBAL del horizonte coinciden
            # siempre.
            first_year = years_sorted[0]
            model.b_bar[first_year].fix(b_max_val)
            model.R[first_year].fix(0)

            later_years = years_sorted[1:]
            if later_years:
                # b_y_link (ec. 2) aplica para todo año salvo el primero,
                # usando D[y-1] -- ver ConstraintRules.b_y_link.
                model.later_years_set = pyo.Set(initialize=later_years, within=model.years)

            # Z_repl[y] = R[y] * n_battery_fleet[y] (linealización big-M del
            # producto binario x entero, para el costo de reemplazo de TODA
            # la flota física: pool en estación + una batería por LHD). No
            # tiene relación con la ec. 3 (es el lado del COSTO) -- se
            # mantiene igual que antes.
            model.Z_repl = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(0, NF_max))

from src.optimization.functions import OptRules
import pyomo.environ as pyo

class ConstraintRules(OptRules):

    def _pool_capacity(self, model, i, y):
        """Capacidad de referencia de la batería del LHD/pool en el año y:
        b_bar[y] si hay datos de degradación cargados (capacidad que se
        degrada año a año), si no la capacidad fija bmax_b[i] (comportamiento
        original, sin degradación)."""
        if self.mine_system.battery_degradation is not None:
            return model.b_bar[y]
        return model.bmax_b[i]

    # ==========================================================
    # 1) Estado operacional del LHD (swap)
    # ==========================================================

    def state_unique_elhd_swap(self, model, i, y, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not nodes:
            return pyo.Constraint.Skip
        return model.Z[i, y, d, t] + sum(model.Y[i, j, y, d, t] for j in nodes) + sum(model.Z_swap[k, i, y, d, t] for k in valid_k_list) == 1

    def between_shifts_elhd_swap(self, model, i, y, d, t):
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        return model.Z[i, y, d, t] + sum(model.Z_swap[k, i, y, d, t] for k in valid_k_list) == 1

    def swap_only_meal_or_between_shifts(self, model, k, i, y, d, t):
        """Solo se permite hacer swap (Z_swap = 1) durante colación o entre turnos."""
        if t in model.time_intervals_meal_set or t in model.time_intervals_between_shifts_set:
            return pyo.Constraint.Skip
        return model.Z_swap[k, i, y, d, t] == 0

    def swap_only_meal_or_between_shifts_det(self, model, k, i, y, d, t):
        """Version DET de swap_only_meal_or_between_shifts.

        Se permite hacer swap (Z_swap = 1) durante colación DET
        (time_intervals_meal_det_set), road_clearing DET
        (time_intervals_road_clearing_det_set) o entre turnos. Durante
        maintenance el LHD debe permanecer detenido sin swap, por lo que
        esas ventanas no se incluyen aquí. Fuera de esas ventanas el swap
        queda prohibido, sin importar si el equipo está detenido o no.
        """
        if (
            t in model.time_intervals_meal_det_set
            or t in model.time_intervals_road_clearing_det_set
            or t in model.time_intervals_between_shifts_det_set
        ):
            return pyo.Constraint.Skip
        return model.Z_swap[k, i, y, d, t] == 0

    # Análogo a charge_state/min_charge_duration de la rama on-board, pero
    # para el estado agregado "asignado a extracción" (Assign = suma de Y
    # sobre todos los nodos j asignables al LHD i en (y,d,t)).
    def assign_state(self, model, i, y, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        assign_sum = sum(model.Y[i, j, y, d, t] for j in nodes)
        t0 = self.time_series.get_time_intervals()[0]
        if t > t0:
            nodes_prev = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t-1, i), [])
            assign_sum_prev = sum(model.Y[i, j, y, d, t-1] for j in nodes_prev)
            return assign_sum - assign_sum_prev == model.StartAssign[i, y, d, t] - model.EndAssign[i, y, d, t]
        else:
            return assign_sum == model.StartAssign[i, y, d, t] - model.EndAssign[i, y, d, t]

    # Si el LHD i arranca una asignación a extracción (a cualquier nodo j) en
    # t, debe mantenerse asignado (a algún nodo, no necesariamente el mismo)
    # en t+1 — mínimo 2 intervalos consecutivos.
    def min_assign_duration(self, model, i, y, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        t_fin = self.time_series.get_time_intervals()[-1]
        assign_sum_t = sum(model.Y[i, j, y, d, t] for j in nodes)
        if t == t_fin:
            return assign_sum_t == 0
        nodes_next = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t+1, i), [])
        assign_sum_next = sum(model.Y[i, j, y, d, t+1] for j in nodes_next)
        return assign_sum_t + assign_sum_next >= 2 * model.StartAssign[i, y, d, t]

    # ==========================================================
    # 2) Energ�a y SOC de bater�a (swap)
    # ==========================================================

    # Estado de energ�a considerando swap de bater�as
    def battery_soc_swap(self, model, i, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        discharge = sum(model.Y[i, j, y, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(y, d, t, i)])
        # El consumo de traccion se retira de la bateria amplificado por
        # 1/eta_discharge (perdidas internas de descarga, hoja LHD).
        discharge_eff = discharge / model.eta_discharge_i[i]
        if t >= t0:
            return model.B[i, y, d, t] == model.B_s[i, y, d, t-1] - discharge_eff

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
    
    def _z_agg_swap(self, model, i, y, d, t):
        """z_agg = suma de Z_swap sobre las estaciones válidas para el LHD i
        en (y,d,t). Efectivamente 0/1 (max_swaps limita la suma a <= 1)."""
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        return sum(model.Z_swap[k, i, y, d, t] for k in valid_k_list)

    # P_bbar_zagg[i,y,d,t] = b_bar[y] * z_agg, linealización big-M exacta
    # (b_bar[y] acotada en [0, b_max_pool], z_agg efectivamente binaria).
    # Solo se registran cuando hay degradación cargada (b_bar existe).
    def p_bbar_zagg_upper1(self, model, i, y, d, t):
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        return model.P_bbar_zagg[i, y, d, t] <= model.b_bar[y]

    def p_bbar_zagg_upper2(self, model, i, y, d, t):
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        z_agg = self._z_agg_swap(model, i, y, d, t)
        return model.P_bbar_zagg[i, y, d, t] <= value(model.b_max_pool) * z_agg

    def p_bbar_zagg_lower(self, model, i, y, d, t):
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        z_agg = self._z_agg_swap(model, i, y, d, t)
        return model.P_bbar_zagg[i, y, d, t] >= model.b_bar[y] - value(model.b_max_pool) * (1 - z_agg)

    def battery_soc_swap_update_1(self, model, i, y, d, t):
        """Convex-hull upper bound: B_s <= B + (U - L) * z_agg.
        Si hay degradación, U=b_bar[y] es variable: (U-L)*z_agg =
        (1-bmin_b[i]) * P_bbar_zagg (linealizado, ver p_bbar_zagg_*)."""
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        if self.mine_system.battery_degradation is not None:
            term = (1 - model.bmin_b[i]) * model.P_bbar_zagg[i, y, d, t]
        else:
            U = model.bmax_b[i]
            L = model.bmin_b[i] * U
            z_agg = self._z_agg_swap(model, i, y, d, t)
            term = (U - L) * z_agg
        return model.B_s[i, y, d, t-1] <= model.B[i, y, d, t-1] + term

    def battery_soc_swap_update_2(self, model, i, y, d, t):
        """Convex-hull lower bound (sharpened): B_s >= B"""
        return model.B_s[i, y, d, t-1] >= model.B[i, y, d, t-1]

    def battery_soc_swap_update_3(self, model, i, y, d, t):
        """Unconditional upper limit: B_s <= U"""
        return model.B_s[i, y, d, t-1] <= self._pool_capacity(model, i, y)

    def battery_soc_swap_update_4(self, model, i, y, d, t):
        """Convex-hull lower limit (dynamic): B_s >= L + (U - L) * z_agg.
        Misma linealización que battery_soc_swap_update_1 cuando hay
        degradación (U=b_bar[y] variable)."""
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        if self.mine_system.battery_degradation is not None:
            L = model.bmin_b[i] * model.b_bar[y]
            term = (1 - model.bmin_b[i]) * model.P_bbar_zagg[i, y, d, t]
            return model.B_s[i, y, d, t-1] >= L + term
        else:
            U = model.bmax_b[i]
            L = model.bmin_b[i] * U
            z_agg = self._z_agg_swap(model, i, y, d, t)
            return model.B_s[i, y, d, t-1] >= L + (U - L) * z_agg

    def swap_soc_limit_30(self, model, i, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        if t < t0:
            return pyo.Constraint.Skip

        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip

        # Tight Big-M: if swap_flag=1 => B <= 0.30*U; if swap_flag=0 => B <= U.
        # This uses M = U-0.30*U = 0.70*U (stronger than M = U in LP relaxation).
        # Si hay degradación, U=b_bar[y] es variable: U*swap_flag se
        # linealiza vía P_bbar_zagg (ver p_bbar_zagg_*).
        if self.mine_system.battery_degradation is not None:
            return model.B[i, y, d, t-1] <= model.b_bar[y] - 0.70 * model.P_bbar_zagg[i, y, d, t]
        else:
            U = model.bmax_b[i]
            swap_flag = self._z_agg_swap(model, i, y, d, t)
            return model.B[i, y, d, t-1] <= U - 0.70 * U * swap_flag

    # L�mite inferior de SOC bater�a
    def battery_lower(self, model, i, y, d, t):
        cap = self._pool_capacity(model, i, y)
        return model.B[i, y, d, t] >= model.bmin_b[i] * cap

    # L�mite superior de SOC bater�a
    def battery_upper(self, model, i, y, d, t):
        return model.B[i, y, d, t] <= self._pool_capacity(model, i, y)

    # Condici�n de borde SOC bater�a
    def battery_boundary_swap(self, model, i, y, d):
        tf = self.time_series.get_time_intervals()[-1]
        return model.B_s[i, y, d, 0] == model.B[i, y, d, 0]
    
    def battery_energy_conservation(self, model, i, y, d):
        # Use interval 0 (initial pre-interval) to enforce cycle with final interval
        tf = self.time_series.get_time_intervals()[-1]
        return model.B[i, y, d, 0] == model.B[i, y, d, tf]

    def battery_boundary_break_simmetry_lhds_start(self, model, i_low, i_high, y, d):
        return model.B[i_low, y, d, 0] <= model.B[i_high, y, d, 0]


    # ==========================================================
    # 3) Producci�n y penalizaciones operacionales
    # ==========================================================

    # Produccion

    def daily_production(self, model, y, d):
        """Production balance for the whole day d (sum over all nodes).

        Enforces: sum_{i,j,t} Y[i,j,d,t]*g_i*n_trips(j,i)*filling_factor[i] >= sum_j m_j[j,d]
        """
        total_target = sum(model.m_j[j, y] for j in model.nodes_set)

        term_de = sum(
            model.Y[i2, j2, y2, d2, t2] * model.g_i[i2] * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
            for (i2, j2, y2, d2, t2) in model.Y
            if y2 == y and d2 == d
        )

        return term_de >= total_target

    def production(self, model, y, d, j):
        """Production balance for node j on day d.

        Enforces:
            floor(m_j / prod_per_assign) <= sum_{i,t} Y[i,j,d,t] <= ceil(m_j / prod_per_assign)

        where prod_per_assign = g_i * n_trips(j,i) * filling_factor[i].
        All LHDs share the same model so n_trips is identical for all i at node j.
        """
        import math
        from pyomo.environ import value as pyo_value

        y_pairs = [(i2, t2) for (i2, j2, y2, d2, t2) in model.Y if j2 == j and y2 == y and d2 == d]
        if not y_pairs:
            return pyo.Constraint.Skip

        i_rep = y_pairs[0][0]
        prod_per_assign = (pyo_value(model.g_i[i_rep])
                           * self.time_series.get_n_trips(j, i_rep)
                           * pyo_value(model.filling_factor[i_rep]))

        target = pyo_value(model.m_j[j, y])
        lb = math.floor(target / prod_per_assign) 
        ub = math.ceil(target / prod_per_assign)  

        visits = sum(model.Y[i2, j, y, d, t2] for i2, t2 in y_pairs)

        return pyo.inequality(lb, visits, ub)

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
 

    def daily_extraction_M(self, model, i, j, y, d):
        """
        M[i,j,d] = extracci�n total del equipo i al nodo j en el d�a d,
        descontando la producci�n perdida por swaps simult�neos (Z_pen).
        Misma unidad que el t�rmino de producci�n (g_i * n_trips * f_i).
        """
        term = sum(
            model.Y[i2, j2, y2, d2, t2] * model.g_i[i2]
            * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
            for (i2, j2, y2, d2, t2) in model.Y_INDEX
            if i2 == i and y2 == y and d2 == d and j2 == j
        )
        #pen = sum(
        #    model.Z_pen[i2, j2, d2, t2] * model.g_i[i2]
        #    * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
        #    for (i2, j2, d2, t2) in model.Y_INDEX
        #    if i2 == i and d2 == d and j2 == j
        #)
        return model.M[i, j, y, d] == term #- pen

    # ==========================================================
    # 4) Infraestructura de estaciones y red el�ctrica
    # ==========================================================

    # Naves acotadas por máximo permitido en la estación
    def max_n_bays(self, model, k, y):
        return model.N_bays[k, y] <= model.max_bays_k[k] * model.X[k, y]

    # Swaps simultáneos no pueden superar las naves disponibles
    def bays_limit_swap(self, model, k, y, d, t):
        valid_i = [i for (k2, i) in model.ZSWAP_INDEX if k2 == k]
        if not valid_i:
            return pyo.Constraint.Skip
        return sum(model.Z_swap[k, i, y, d, t] for i in valid_i) <= model.N_bays[k, y]

    # Naves necesitan al menos un cargador cada una
    def bays_le_chargers(self, model, k, y):
        return model.N_bays[k, y] <= model.N_chargers[k, y]

    # Máximo de cargadores por bahía
    def max_chargers_per_bay_constr(self, model, k, y):
        return model.N_chargers[k, y] <= model.max_chargers_per_bay_k[k] * model.N_bays[k, y]

    # Máximo de baterías por bahía
    def max_batteries_per_bay_constr(self, model, k, y):
        return model.N_batteries[k, y] <= model.max_batteries_per_bay_k[k] * model.N_bays[k, y]

    # Cargadores no pueden superar la cantidad de baterías
    def chargers_le_batteries(self, model, k, y):
        return model.N_chargers[k, y] <= model.N_batteries[k, y]

    # ------------------------------------------------------------------ #
    # Inversion multi-anio: restricciones de enlace (linking constraints)
    # entre el stock acumulado y el incremento anual, para estaciones,
    # naves, cargadores, pool de baterias, generacion y almacenamiento. Ver
    # docs/restricciones_modelo.tex sec. "Inversion multi-año e
    # infraestructura acumulada". Escenario greenfield: stock inicial (antes
    # de y1) es cero para las cinco tecnologias, por lo que el caso y==y1
    # colapsa a stock[y1] == delta[y1].
    # ------------------------------------------------------------------ #
    def link_station_stock(self, model, k, y):
        if y == self._first_year():
            return model.X[k, y] == model.Delta_X[k, y]
        return model.X[k, y] == model.X[k, self._prev_year(y)] + model.Delta_X[k, y]

    def link_bays_stock(self, model, k, y):
        if y == self._first_year():
            return model.N_bays[k, y] == model.Delta_N_bays[k, y]
        return model.N_bays[k, y] == model.N_bays[k, self._prev_year(y)] + model.Delta_N_bays[k, y]

    def link_charger_stock(self, model, k, y):
        if y == self._first_year():
            return model.N_chargers[k, y] == model.Delta_N_chargers[k, y]
        return model.N_chargers[k, y] == model.N_chargers[k, self._prev_year(y)] + model.Delta_N_chargers[k, y]

    def link_battery_stock(self, model, k, y):
        if y == self._first_year():
            return model.N_batteries[k, y] == model.Delta_N_batteries[k, y]
        return model.N_batteries[k, y] == model.N_batteries[k, self._prev_year(y)] + model.Delta_N_batteries[k, y]

    def max_storage_units(self, model):
        return model.H <= model.h_max

    # Existencia de la estaci�n
    def station_existence_constraint_swap(self, model, k, i, y, d, t):
        return model.Z_swap[k, i, y, d, t] <= model.X[k, y]

    def _sv_a_window(self, model, t):
        """Ventana valida de 'a' (inicio de carga) para Sv[k,y,d,t,a] en el
        intervalo t -- misma formula que BoundRules.build_all_variables usa
        para construir SV_INDEX: [max(t0, t-t_charge+1), t-1]. Fuera de esa
        ventana Sv no existe como variable (ver comentario ahi), asi que las
        sumas sobre 'a' deben iterar solo esta ventana en vez de todo
        model.time_intervals_set."""
        t = int(t)
        t0 = int(self.time_series.get_time_intervals()[0])
        t_charge = int(value(model.t_charge))
        lo = max(t0, t - t_charge + 1)
        hi = t - 1
        return range(lo, hi + 1)

    def charger_limit_swap(self, model, k, y, d, t):
        # Para cada estaci�n k, d�a d y intervalo t, la suma de bater�as
        # conectadas (para todos los inicios a) en t no puede exceder los cargadores
        a_window = self._sv_a_window(model, t)
        if not a_window:
            # Ventana vacia (t = primer intervalo del dia): ningun Sv existe
            # todavia, la suma es 0 y la restriccion es trivialmente valida
            # (N_chargers >= 0) -- se omite para no comparar dos constantes.
            return pyo.Constraint.Skip
        return sum(model.Sv[k, y, d, t, a] for a in a_window) <= model.N_chargers[k, y]

    #  Sistemas distribuci�n
    def max_installed_capacity_swap(self, model, k, y, d, t):
        """Potencia maxima de la subestacion k, expresada como cota entera
        sobre la cantidad de baterias cargando en paralelo -- N_max_k[k]
        es una variable de decision (decidida UNA sola vez para todo el
        horizonte, ver substation_investment_cost), no un parametro fijo:
        misma restriccion en unidades de conteo de baterias que antes,
        pero ahora la capacidad se construye (y se paga) en vez de venir
        dada por p_max_ssee."""
        a_window = self._sv_a_window(model, t)
        if not a_window:
            return pyo.Constraint.Skip
        return sum(model.Sv[k, y, d, t, a] for a in a_window) <= model.N_max_k[k]

    def peak_power_swap(self, model, y, d, t):
        """Potencia peak de distribucion, expresada como cota entera sobre
        la cantidad total de baterias cargando en paralelo (n_peak_max =
        floor(p_peak/p_charger)) -- ver max_installed_capacity_swap."""
        a_window = self._sv_a_window(model, t)
        if not a_window:
            return pyo.Constraint.Skip
        return sum(
            model.Sv[k, y, d, t, a]
            for k in model.stations_set
            for a in a_window
        ) <= model.n_peak_max

    def power_peak_limit(self, model, y, d, t):
        """Demand-charge constraint: grid power during peak hours <= P_pot.

        Solo aplica entre abril y septiembre (meses de punta segun tarifa)."""
        if not (91 <= d <= 244):
            return pyo.Constraint.Skip
        return model.P_red[y, d, t] <= model.P_pot[y]

    # ==========================================================
    # 5) Inventario de bater�as en estaciones
    # ==========================================================

    # Demanda por swaps (bater�as requeridas)
    def total_swaps(self, model, k, y, d, t):
        valid_i_list = [i for (k2, i) in model.ZSWAP_INDEX if k2 == k]
        if not valid_i_list:
            return pyo.Constraint.Skip
        return model.W[k, y, d, t] == sum(model.Z_swap[k, i, y, d, t] for i in valid_i_list)
    
    def max_swaps(self, model, i, y, d, t):
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if not valid_k_list:
            return pyo.Constraint.Skip
        return sum(model.Z_swap[k, i, y, d, t] for k in valid_k_list) <= 1

    def swap_precedence_by_index(self, model, i_low, i_high, y, d, t):
        """Forces cumulative swaps of lower-index LHDs to be >= higher-index LHDs at each time t."""
        valid_k_low = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i_low]
        valid_k_high = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i_high]
        if not valid_k_low or not valid_k_high:
            return pyo.Constraint.Skip

        cumulative_low = sum(
            model.Z_swap[k, i_low, y, d, tau]
            for k in valid_k_low
            for tau in model.time_intervals_set
            if tau <= t
        )
        cumulative_high = sum(
            model.Z_swap[k, i_high, y, d, tau]
            for k in valid_k_high
            for tau in model.time_intervals_set
            if tau <= t
        )
        return cumulative_low >= cumulative_high
    
    # Inventario de bater�as descargadas
    def inventory_discharged_batteries_rule(self, model, k, y, d, t):
        t0= self.time_series.get_time_intervals()[0]
        if t == t0:
            return pyo.Constraint.Skip
        else:
            return model.X_dch[k, y, d, t] == model.X_dch[k, y, d, t-1] - model.X_ini[k, y, d, t-1] + model.W[k, y, d, t]

    # Inventario de bater�as cargadas
    def inventory_charged_batteries_rule(self, model, k, y, d, t):
        t0= self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        if t == tf: 
            return pyo.Constraint.Skip
        if t - (model.t_charge - 1) >= t0:
            return model.S[k, y, d, t+1] == model.S[k, y, d, t] + model.Sv[k, y, d, t, t - (model.t_charge - 1)] - model.W[k, y, d, t+1]
        elif t >= 0:
            return model.S[k, y, d, t+1] == model.S[k, y, d, t] - model.W[k, y, d, t+1]
        return pyo.Constraint.Skip

    # 4. Duraci�n de la carga (L�gica interna del cargador)
    def charging_duration_rule(self, model, k, y, d, t, a):
        """(k,y,d,t,a) recorre model.SV_INDEX (ver
        BoundRules.build_all_variables): a < t siempre (ventana valida), asi
        que solo quedan los dos casos no triviales de la version densa --
        el caso "a fuera de ventana => Sv=0" ya no hace falta porque esas
        combinaciones directamente no existen como variable."""
        t_prev = t - 1
        if a == t_prev:
            # Caso 3: la carga recien empez� en t_prev (a == t_prev).
            return model.Sv[k, y, d, t, a] == model.X_ini[k, y, d, t_prev]
        # Caso 2: la carga sigue en proceso, se mantiene el valor anterior.
        return model.Sv[k, y, d, t, a] == model.Sv[k, y, d, t_prev, a]

    # Condiciones de inventario c�clico
    def CI_S(self, model, k, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        return model.S[k, y, d, t0] == model.S[k, y, d, tf]
    
    def CI_X_dch(self, model, k, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        tf = self.time_series.get_time_intervals()[-1]
        return model.X_dch[k, y, d, t0] == model.X_dch[k, y, d, tf]
    
    def CB_general(self, model, k, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        return model.S[k, y, d, t0] + model.X_dch[k, y, d, t0] == model.N_batteries[k, y]
    

    # ==========================================================
    # 6) Pausas operacionales
    # ==========================================================

    # Pausas: MEAL
    # --------------------------
    def meal_g1_no_travel_group1(self, model, i, y, d, t):
        """En meal_g1 el Grupo 1 no puede viajar."""
        if t not in model.time_intervals_meal_g1_set:
            return pyo.Constraint.Skip
        if i not in model.meal_group1_set:
            return pyo.Constraint.Skip

        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return sum(model.Y[i, j, y, d, t] for j in nodes) == 0

    def meal_g2_no_travel_group2(self, model, i, y, d, t):
        """En meal_g2 el Grupo 2 no puede viajar."""
        if t not in model.time_intervals_meal_g2_set:
            return pyo.Constraint.Skip
        if i not in model.meal_group2_set:
            return pyo.Constraint.Skip

        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return sum(model.Y[i, j, y, d, t] for j in nodes) == 0


    # Pausas: MAINTENANCE
    def maint_stop_all(self, model, i, y, d, t):
        """En intervalos de mantenimiento TODOS los LHD deben
        estar estacionados (Z = 1). En mantenimiento los ELHD no pueden estar cargando."""
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z[i, y, d, t] == 1
    
    def det_stop_all(self, model, i, y, d, t):
        """En intervalos DET (meal + maintenance + road_clearing) todos los LHD deben estar estacionados (Z = 1) o haciendo swap (Z_swap = 1)."""
        valid_k_list = [k for (k, i2) in model.ZSWAP_INDEX if i2 == i]
        if t not in model.time_intervals_det_set:
            return pyo.Constraint.Skip
        return model.Z[i, y, d, t] + sum(model.Z_swap[k, i, y, d, t] for k in valid_k_list) == 1
    
    # Fijar baterias y cargadores
    # No registradas en build_all_constraints (dead code, ya lo era antes de
    # la inversion multi-año). Si se reactivan, N_chargers/N_batteries ahora
    # son N_chargers[k,y]/N_batteries[k,y] — actualizar la indexacion.
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
    def n_swaps_limit_1(self, model, i, y, d):
        return sum(model.Z_swap[k, i, y, d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) <= 5
    
    def n_swaps_limit_2(self, model, i, y, d):
        return sum(model.Z_swap[k, i, y, d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) >= 3

    # ==========================================================
    # 7) Balance de potencia y generación renovable / BESS
    # ==========================================================

    def power_balance(self, model, y, d, t):
        """Demanda de carga (estaciones) = red + generación local + BESS."""
        demand = sum(
            model.Sv[k, y, d, t, a] * model.p_charger
            for k in model.stations_set
            for a in self._sv_a_window(model, t)
        )
        gen = (sum(model.P_gen[g, y, d, t] for g in model.gen_set)
               if len(list(model.gen_set)) > 0 else 0)
        bess = (sum(model.P_bat[h, y, d, t] for h in model.storage_set)
                if len(list(model.storage_set)) > 0 else 0)
        return model.P_red[y, d, t] + gen + bess == demand

    def grid_limit(self, model, y, d, t):
        """Potencia de red acotada por capacidad de la subestación."""
        return model.P_red[y, d, t] <= model.p_peak

    def gen_limit(self, model, g, y, d, t):
        """Generación + curtailment = capacidad disponible — ec. 3.47."""
        return (model.P_gen[g, y, d, t] + model.Curt_g[g, y, d, t]
                == model.G_g[g] * model.p_max_g[g] * model.alpha_g[g, y, d, t])

    def gen_max_units(self, model, g):
        """Cantidad máxima de unidades instalables por tecnología."""
        return model.G_g[g] <= model.g_max_g[g]

    def bess_power_upper(self, model, h, y, d, t):
        """P_bat <= p_max_h * H (descarga máxima)."""
        return model.P_bat[h, y, d, t] <= model.p_max_h[h] * model.H

    def bess_power_lower(self, model, h, y, d, t):
        """P_bat >= -p_max_h * H (carga máxima)."""
        return model.P_bat[h, y, d, t] >= -model.p_max_h[h] * model.H

    def bess_soc_balance(self, model, h, y, d, t):
        """A_h,d,t = A_h,d,t-1 - (P_bat_h,d,t / eta_h) * delta_t."""
        t0 = self.time_series.get_time_intervals()[0]
        prev = model.A_h[h, y, d, 0] if t == t0 else model.A_h[h, y, d, t - 1]
        return model.A_h[h, y, d, t] == prev - (model.P_bat[h, y, d, t] / model.eta_h[h]) * model.delta_t

    def bess_soc_init(self, model, h, y, d):
        """Estado de energía inicial = 0 al comienzo de cada día."""
        return model.A_h[h, y, d, 0] == 0

    def bess_soc_upper(self, model, h, y, d, t):
        """A_h <= a_max_h * H."""
        return model.A_h[h, y, d, t] <= model.a_max_h[h] * model.H

    def bess_soc_lower(self, model, h, y, d, t):
        """A_h >= a_min_h * H."""
        return model.A_h[h, y, d, t] >= model.a_min_h[h] * model.H

    def bess_soc_cyclic(self, model, h, y, d):
        """SOC del primer intervalo igual al del último — ec. 3.53."""
        t_ini = self.time_series.get_time_intervals()[0]
        t_fin = self.time_series.get_time_intervals()[-1]
        return model.A_h[h, y, d, t_ini] == model.A_h[h, y, d, t_fin]

    # ==========================================================
    # Degradación de batería del pool de swap
    # ==========================================================

    def _prev_year(self, y):
        years_sorted = sorted(self.time_series.years)
        return years_sorted[years_sorted.index(y) - 1]

    def _first_year(self):
        return sorted(self.time_series.years)[0]

    def n_batteries_total_def(self, model, y):
        """N_batteries_total[y] = suma de N_batteries[k,y] por estación
        (stock acumulado, tamaño del pool en estación al año y — NO es la
        flota física completa, ver n_battery_fleet_def)."""
        return model.N_batteries_total[y] == sum(model.N_batteries[k, y] for k in model.stations_set)

    def n_battery_fleet_def(self, model, y):
        """n_battery_fleet[y] = N_batteries_total[y] (pool en estación) + una
        batería instalada en cada LHD swap ACTIVO ese año (n_active_slhd[y],
        ver OptParameters -- no el roster completo, que puede no estar
        totalmente activo aún según NodeAssignment/FleetByYear) = flota
        física total a degradar/reemplazar al año y."""
        return model.n_battery_fleet[y] == model.N_batteries_total[y] + model.n_active_slhd[y]

    def energy_consumed_def(self, model, y):
        """EnergyConsumed[y] = energía consumida (descarga real) por todos
        los LHD de swap en el/los día(s) representativo(s) del año y — misma
        expresión que el término "discharge" de battery_soc_swap. Se usa el
        consumo de los vehículos en vez de la energía cargada en estación
        (Sv) porque esta última puede quedar desfasada del consumo real
        dentro de un mismo día representativo (una batería swapeada tarde en
        el día puede no terminar de cargar ese mismo día), lo que subestimaba
        el desgaste — y así queda comparable con on-board, que mide sobre la
        misma batería que descarga. Expresión lineal (suma de variables por
        constantes)."""
        return model.EnergyConsumed[y] == sum(
            model.Y[i, j, y, d, t] * model.pe_i[i, j] * model.d_i[i, j] * self.time_series.get_n_trips(j, i)
            for i in model.slhd_set
            for d in model.days
            for t in model.time_intervals_set
            for j in self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        )

    # ------------------------------------------------------------------ #
    # Degradación de batería del pool de swap -- modelo por ciclos con
    # recursión año a año, análogo a carga_ob_multiaño. Ecuaciones (numeración
    # del modelo entregado):
    #   1) B[y] <= b_max_pool                    -> cota superior de b_bar (B_U)
    #   2) B[y] <= D[y-1] + 0.3*b_max_pool*R[y]   para y != primer año -> b_y_link
    #   3) N_ciclos[y]*B[y]*n_battery_fleet[y] == EnergyConsumed[y]*scaling_factor_op_cost
    #      -- trilineal, reducido a grado 2 vía N_total[y]=N_ciclos[y]*n_battery_fleet[y]
    #      (de las 3 combinaciones posibles, la que mejor se comportó en
    #      Gurobi -- ver nota de comparación empírica en BoundRules):
    #      3a) N_total[y] == N_ciclos[y]*n_battery_fleet[y]   -> n_total_def
    #      3b) N_total[y]*b_bar[y] == EnergyConsumed[y]*scaling_factor_op_cost -> n_ciclos_link
    #   4) D[y] = B[y] - gamma_coef*N_ciclos[y]   -> d_y_fade
    #   5) B[y1] = b_max_pool                     -> fijado en BoundRules (model.b_bar[y1].fix)
    # ------------------------------------------------------------------ #
    def n_total_def(self, model, y):
        """(ec. 3a) N_total[y] = N_ciclos[y] * n_battery_fleet[y]: ciclos
        equivalentes TOTALES de la flota de baterías en el año y -- bilineal
        (continua x entera), resuelto directo como restricción cuadrática no
        convexa (Gurobi NonConvex=2), sin linealizar."""
        return model.N_total[y] == model.N_ciclos[y] * model.n_battery_fleet[y]

    def n_ciclos_link(self, model, y):
        """(ec. 3b) N_total[y] * b_bar[y] == EnergyConsumed[y] *
        scaling_factor_op_cost -- segunda mitad de la reducción de grado del
        trilineal original (N_ciclos[y]*B[y]*n_battery_fleet[y]); junto con
        n_total_def reconstruye exactamente esa ecuación sin aproximar."""
        return model.N_total[y] * model.b_bar[y] == model.EnergyConsumed[y] * model.scaling_factor_op_cost

    def build_mccormick_degradation_block(self, model):
        """Camino A (McCormick, ver degradacion_descomposicion_mccormick.md
        sec. 3 en la rama carga_ob_multiaño) aplicado al pool de swap: la
        ec. 3 (N_ciclos[y]*b_bar[y]*n_battery_fleet[y] == EnergyConsumed[y]*
        scaling_factor_op_cost) es un TRILINEAL reducido a DOS bilineales
        encadenados (n_total_def, n_ciclos_link -- ver comentario en
        BoundRules y ConstraintRules.energy_consumed_def). Este bloque
        reemplaza AMBOS por su envolvente convexa de McCormick, dejando el
        modelo MILP puro (sin necesitar Gurobi NonConvex=2, ver guard en
        OptModel._configure_solver).

        Las cotas usadas son las YA declaradas en BoundRules para cada
        variable (N_ciclos, n_battery_fleet, N_total, b_bar) -- N_ciclos usa
        una cota GLOBAL (n_ciclos_max), no ajustada por año como
        compute_n_ciclos_bounds hace en carga_ob_multiaño para el caso
        on-board. Si el residuo de McCormick resulta grande, ajustar esa
        cota por año (mismo principio, sec. 3.5 del documento) es el
        siguiente paso natural."""
        NC_bounds = {y: (float(model.N_ciclos[y].lb), float(model.N_ciclos[y].ub)) for y in model.years}
        NF_bounds = {y: (float(model.n_battery_fleet[y].lb), float(model.n_battery_fleet[y].ub)) for y in model.years}
        NT_bounds = {y: (float(model.N_total[y].lb), float(model.N_total[y].ub)) for y in model.years}
        B_L = value(model.B_L)
        B_U = value(model.B_U)

        # --- Bilineal 1 (ec. 3a): N_total[y] ~= N_ciclos[y] * n_battery_fleet[y] ---
        def _mc_n_total_lb1(m, y):
            NC_L, _ = NC_bounds[y]
            NF_L, _ = NF_bounds[y]
            return m.N_total[y] >= NC_L * m.n_battery_fleet[y] + NF_L * m.N_ciclos[y] - NC_L * NF_L

        def _mc_n_total_lb2(m, y):
            _, NC_U = NC_bounds[y]
            _, NF_U = NF_bounds[y]
            return m.N_total[y] >= NC_U * m.n_battery_fleet[y] + NF_U * m.N_ciclos[y] - NC_U * NF_U

        def _mc_n_total_ub1(m, y):
            _, NC_U = NC_bounds[y]
            NF_L, _ = NF_bounds[y]
            return m.N_total[y] <= NC_U * m.n_battery_fleet[y] + NF_L * m.N_ciclos[y] - NC_U * NF_L

        def _mc_n_total_ub2(m, y):
            NC_L, _ = NC_bounds[y]
            _, NF_U = NF_bounds[y]
            return m.N_total[y] <= NC_L * m.n_battery_fleet[y] + NF_U * m.N_ciclos[y] - NC_L * NF_U

        model.mc_n_total_lb1 = pyo.Constraint(model.years, rule=_mc_n_total_lb1)
        model.mc_n_total_lb2 = pyo.Constraint(model.years, rule=_mc_n_total_lb2)
        model.mc_n_total_ub1 = pyo.Constraint(model.years, rule=_mc_n_total_ub1)
        model.mc_n_total_ub2 = pyo.Constraint(model.years, rule=_mc_n_total_ub2)

        # --- Bilineal 2 (ec. 3b): w_deg[y] ~= N_total[y] * b_bar[y] == EnergyConsumed[y]*scaling ---
        model.w_deg = pyo.Var(model.years, domain=pyo.NonNegativeReals)

        def _mc_w_deg_lb1(m, y):
            NT_L, _ = NT_bounds[y]
            return m.w_deg[y] >= NT_L * m.b_bar[y] + B_L * m.N_total[y] - NT_L * B_L

        def _mc_w_deg_lb2(m, y):
            _, NT_U = NT_bounds[y]
            return m.w_deg[y] >= NT_U * m.b_bar[y] + B_U * m.N_total[y] - NT_U * B_U

        def _mc_w_deg_ub1(m, y):
            _, NT_U = NT_bounds[y]
            return m.w_deg[y] <= NT_U * m.b_bar[y] + B_L * m.N_total[y] - NT_U * B_L

        def _mc_w_deg_ub2(m, y):
            NT_L, _ = NT_bounds[y]
            return m.w_deg[y] <= NT_L * m.b_bar[y] + B_U * m.N_total[y] - NT_L * B_U

        def _mc_w_deg_energy(m, y):
            return m.w_deg[y] == m.EnergyConsumed[y] * m.scaling_factor_op_cost

        model.mc_w_deg_lb1    = pyo.Constraint(model.years, rule=_mc_w_deg_lb1)
        model.mc_w_deg_lb2    = pyo.Constraint(model.years, rule=_mc_w_deg_lb2)
        model.mc_w_deg_ub1    = pyo.Constraint(model.years, rule=_mc_w_deg_ub1)
        model.mc_w_deg_ub2    = pyo.Constraint(model.years, rule=_mc_w_deg_ub2)
        model.mc_w_deg_energy = pyo.Constraint(model.years, rule=_mc_w_deg_energy)

    def d_y_fade(self, model, y):
        """(ec. 4) D[y] = B[y] - gamma_coef*N_ciclos[y]: capacidad POR
        BATERÍA al final del año, degradada por los ciclos equivalentes del
        propio año (sin arrastre acumulado explícito -- el arrastre entre
        años lo da D[y-1] vía b_y_link)."""
        return model.D[y] == model.b_bar[y] - model.gamma_coef * model.N_ciclos[y]

    def b_y_link(self, model, y):
        """(ec. 2) B[y] <= D[y-1] + 0.3*b_max_pool*R[y], para todo año salvo
        el primero (B[y1]=b_max_pool fijo, ec. 5). Si se reemplaza la
        batería (R[y]=1) se recupera hasta un 30% de capacidad nominal
        adicional sobre D[y-1]; si no, la capacidad de inicio de año queda
        acotada por la capacidad heredada del año anterior."""
        return model.b_bar[y] <= (
            model.D[self._prev_year(y)] + 0.3 * value(model.b_max_pool) * model.R[y]
        )

    def z_repl_upper1(self, model, y):
        """Z_repl[y] = R[y] * n_battery_fleet[y] (linealización big-M) — al
        reemplazar se reemplaza TODA la flota física (pool + instaladas)."""
        return model.Z_repl[y] <= model.n_battery_fleet[y]

    def z_repl_upper2(self, model, y):
        return model.Z_repl[y] <= model.NF_max_param * model.R[y]

    def z_repl_lower(self, model, y):
        return model.Z_repl[y] >= model.n_battery_fleet[y] - model.NF_max_param * (1 - model.R[y])

    def build_all_constraints(self, model):
        # 1) Energ�a / SOC de bater�as del LHD (swap)
        model.battery_soc_swap = pyo.Constraint(
            model.slhd_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap,
        )
        model.battery_soc_swap_update_1 = pyo.Constraint(
            model.slhd_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap_update_1,
        )
        model.battery_soc_swap_update_2 = pyo.Constraint(
            model.slhd_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap_update_2,
        )
        model.battery_soc_swap_update_3 = pyo.Constraint(
            model.slhd_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.battery_soc_swap_update_3,
        )
        model.battery_soc_swap_update_4 = pyo.Constraint(
            model.slhd_set,
            model.years,
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
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.battery_lower,
        )
        model.battery_upper = pyo.Constraint(
            model.slhd_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.battery_upper,
        )
        model.battery_boundary_swap = pyo.Constraint(model.slhd_set, model.years, model.days, rule=self.battery_boundary_swap)
        model.batter_energy_conservation = pyo.Constraint(model.slhd_set, model.years, model.days, rule=self.battery_energy_conservation)

        # 2) Infraestructura de estaciones y capacidad el�ctrica
        model.max_n_bays = pyo.Constraint(model.stations_set, model.years, rule=self.max_n_bays)
        model.bays_limit_swap = pyo.Constraint(model.stations_set, model.years, model.days, model.time_intervals_set, rule=self.bays_limit_swap)
        model.bays_le_chargers = pyo.Constraint(model.stations_set, model.years, rule=self.bays_le_chargers)
        model.max_chargers_per_bay_constr = pyo.Constraint(model.stations_set, model.years, rule=self.max_chargers_per_bay_constr)
        model.max_batteries_per_bay_constr = pyo.Constraint(model.stations_set, model.years, rule=self.max_batteries_per_bay_constr)
        model.chargers_le_batteries = pyo.Constraint(model.stations_set, model.years, rule=self.chargers_le_batteries)
        #model.fix_stations = pyo.Constraint(model.stations_set, rule=self.fix_stations)
        #model.fix_n_chargers = pyo.Constraint(model.stations_set, rule=self.fix_n_chargers)
        #model.fix_n_batteries = pyo.Constraint(model.stations_set, rule=self.fix_n_batteries)
        model.link_station_stock = pyo.Constraint(model.stations_set, model.years, rule=self.link_station_stock)
        model.link_bays_stock    = pyo.Constraint(model.stations_set, model.years, rule=self.link_bays_stock)
        model.link_charger_stock = pyo.Constraint(model.stations_set, model.years, rule=self.link_charger_stock)
        model.link_battery_stock = pyo.Constraint(model.stations_set, model.years, rule=self.link_battery_stock)
        model.station_existence_constraint_swap = pyo.Constraint(
            model.ZSWAP_DAYS_TIME,
            rule=self.station_existence_constraint_swap,
        )
        model.max_installed_capacity = pyo.Constraint(
            model.stations_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.max_installed_capacity_swap,
        )
        model.charger_limit_swap = pyo.Constraint(
            model.stations_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.charger_limit_swap,
        )
        model.peak_power_swap = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.peak_power_swap)
        model.power_peak_limit = pyo.Constraint(model.years, model.days, model.time_intervals_peak_set, rule=self.power_peak_limit)

        # 3) Operaci�n de LHD (estado, viajes y swaps)
        model.state_unique_elhd_swap = pyo.Constraint(
            model.slhd_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.state_unique_elhd_swap,
        )
        model.between_shifts_elhd_swap = pyo.Constraint(
            model.slhd_set,
            model.years,
            model.days,
            model.time_intervals_between_shifts_det_set,
            rule=self.between_shifts_elhd_swap,
        )
        #model.swap_only_meal_or_between_shifts = pyo.Constraint(
        #    model.ZSWAP_DAYS_TIME, rule=self.swap_only_meal_or_between_shifts
        #)
        model.swap_only_meal_or_between_shifts_det = pyo.Constraint(
            model.ZSWAP_DAYS_TIME, rule=self.swap_only_meal_or_between_shifts_det
        )
        #model.assign_state = pyo.Constraint(
        #    model.slhd_set,
        #    model.years,
        #    model.days,
        #    model.time_intervals_set,
        #    rule=self.assign_state,
        #)
        #model.min_assign_duration = pyo.Constraint(
        #    model.slhd_set,
        #    model.years,
        #    model.days,
        #    model.time_intervals_set,
        #    rule=self.min_assign_duration,
        #)
        model.total_swaps = pyo.Constraint(
            model.stations_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.total_swaps,
        )
        model.max_swaps = pyo.Constraint(
            model.slhd_set,
            model.years,
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
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.inventory_discharged_batteries_rule,
        )
        model.inventory_charged_batteries = pyo.Constraint(
            model.stations_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.inventory_charged_batteries_rule,
        )
        model.charging_duration = pyo.Constraint(
            model.SV_INDEX,
            rule=self.charging_duration_rule,
        )
        model.CI_S = pyo.Constraint(
            model.stations_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.CI_S,
        )
        model.CI_X_dch = pyo.Constraint(
            model.stations_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.CI_X_dch,
        )
        model.CB_general = pyo.Constraint(
            model.stations_set,
            model.years,
            model.days,
            model.time_intervals_set,
            rule=self.CB_general,
        )

        # 5) Producci�n y penalizaciones
        model.daily_extraction = pyo.Constraint(
            model.years,
            model.days,
            rule=self.daily_production,
        )
        model.production = pyo.Constraint(model.years, model.days, model.nodes_set, rule=self.production)
        model.daily_extraction_M = pyo.Constraint(
            model.slhd_set,
            model.nodes_set,
            model.years,
            model.days,
            rule=self.daily_extraction_M,
        )

        # 6) Pausas operacionales DET (esquema activo). DCH queda comentado
        # mas abajo, sin registrar.
        #model.meal_g1_no_travel_group1 = pyo.Constraint(model.lhd_set, model.years, model.days, model.time_intervals_set, rule=self.meal_g1_no_travel_group1)
        #model.meal_g2_no_travel_group2 = pyo.Constraint(model.lhd_set, model.years, model.days, model.time_intervals_set, rule=self.meal_g2_no_travel_group2)

        #model.maintenance_stop_all = pyo.Constraint(
        #    model.slhd_set,
        #    model.years,
        #    model.days,
        #    model.time_intervals_set,
        #    rule=self.maint_stop_all,
        #)
        model.det_stop_all = pyo.Constraint(model.slhd_set, model.years, model.days, model.time_intervals_set, rule=self.det_stop_all)

        # 7) Balance de potencia y generación / BESS
        model.power_balance = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.power_balance)
        model.grid_limit    = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.grid_limit)

        if len(list(model.gen_set)) > 0:
            model.gen_limit     = pyo.Constraint(model.gen_set, model.years, model.days, model.time_intervals_set, rule=self.gen_limit)
            model.gen_max_units = pyo.Constraint(model.gen_set, rule=self.gen_max_units)

        if len(list(model.storage_set)) > 0:
            model.max_storage_units  = pyo.Constraint(rule=self.max_storage_units)
            model.bess_power_upper = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_power_upper)
            model.bess_power_lower = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_power_lower)
            model.bess_soc_balance = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_soc_balance)
            model.bess_soc_init    = pyo.Constraint(model.storage_set, model.years, model.days, rule=self.bess_soc_init)
            model.bess_soc_upper   = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_soc_upper)
            model.bess_soc_lower   = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_soc_lower)
            model.bess_soc_cyclic  = pyo.Constraint(model.storage_set, model.years, model.days, rule=self.bess_soc_cyclic)

        # 8) Rotura simetr�a
        #model.battery_boundary_break_simmetry_lhds_start = pyo.Constraint(
        #    model.swap_precedence_pairs,
        #    model.years,
        #    model.days,
        #    rule=self.battery_boundary_break_simmetry_lhds_start,
        #)

        #model.swap_precedence_by_index = pyo.Constraint(
        #    model.swap_precedence_pairs,
        #    model.years,
        #    model.days,
        #    model.time_intervals_set,
        #    rule=self.swap_precedence_by_index,
        #)

        # 9) Degradación de batería del pool de swap (solo si hay datos cargados)
        if self.mine_system.battery_degradation is not None:
            model.n_batteries_total_def = pyo.Constraint(model.years, rule=self.n_batteries_total_def)
            model.n_battery_fleet_def = pyo.Constraint(model.years, rule=self.n_battery_fleet_def)
            model.energy_consumed_def = pyo.Constraint(model.years, rule=self.energy_consumed_def)

            if self.mccormick_degradation:
                # Camino A: reemplaza n_total_def + n_ciclos_link (bilineales
                # no convexos) por su envolvente convexa de McCormick -- ver
                # build_mccormick_degradation_block.
                self.build_mccormick_degradation_block(model)
            else:
                model.n_total_def   = pyo.Constraint(model.years, rule=self.n_total_def)
                model.n_ciclos_link = pyo.Constraint(model.years, rule=self.n_ciclos_link)
            model.d_y_fade      = pyo.Constraint(model.years, rule=self.d_y_fade)
            if hasattr(model, 'later_years_set'):
                model.b_y_link = pyo.Constraint(model.later_years_set, rule=self.b_y_link)

            model.z_repl_upper1 = pyo.Constraint(model.years, rule=self.z_repl_upper1)
            model.z_repl_upper2 = pyo.Constraint(model.years, rule=self.z_repl_upper2)
            model.z_repl_lower  = pyo.Constraint(model.years, rule=self.z_repl_lower)

            model.p_bbar_zagg_upper1 = pyo.Constraint(
                model.slhd_set, model.years, model.days, model.time_intervals_set, rule=self.p_bbar_zagg_upper1
            )
            model.p_bbar_zagg_upper2 = pyo.Constraint(
                model.slhd_set, model.years, model.days, model.time_intervals_set, rule=self.p_bbar_zagg_upper2
            )
            model.p_bbar_zagg_lower = pyo.Constraint(
                model.slhd_set, model.years, model.days, model.time_intervals_set, rule=self.p_bbar_zagg_lower
            )

class ObjectiveRules(OptRules):

    def year_position(self, y: int) -> int:
        """Posicion 1-indexada de y dentro del horizonte modelado (1, 2, 3, ...)."""
        return sorted(self.time_series.years).index(y) + 1

    def _discount_factor(self, model, y):
        """1/(1+r)^pos(y) si hay datos de degradacion/descuento cargados, si no 1
        (preserva el comportamiento original para escenarios sin esa hoja)."""
        if self.mine_system.battery_degradation is None:
            return 1
        return 1 / (1 + model.discount_rate) ** self.year_position(y)

    def lhd_charge_cost_bs(self, model):
        """Costo de electricidad comprada a la red (P_red * costo_electricidad),
        descontado a valor presente por año."""
        cost_el = sum(
            model.P_red[y, d, t] * model.costo_electricidad[y, d, t] * model.delta_t
            * self._discount_factor(model, y)
            for y in model.years
            for d in model.days
            for t in model.time_intervals_set
        )
        return cost_el * model.scaling_factor_op_cost

    def _one_time_discounted_yearly_sum(self, model, yearly_cost_fn):
        """sum_y yearly_cost_fn(y) * discount_factor(y): costo de inversion
        pagado UNA sola vez en el año de compra, descontado a valor presente
        (mismo patron que battery_replace_cost). yearly_cost_fn(y) debe usar
        las variables de INCREMENTO (Delta_*) del año y, no el stock
        acumulado, para no contar de nuevo cada año el costo de un activo ya
        construido."""
        return sum(yearly_cost_fn(y) * self._discount_factor(model, y) for y in model.years)

    def inversion_cost(self, model):
        def yearly(y):
            return sum(
                model.station_cost_k[k] * model.Delta_X[k, y]
                + model.c_bays_k[k] * model.Delta_N_bays[k, y]
                + model.c_crane_k[k] * model.Delta_N_bays[k, y]
                + (model.charger_cost + model.c_charger_space_k[k]) * model.Delta_N_chargers[k, y]
                + (model.battery_cost + model.c_battery_space_k[k]) * model.Delta_N_batteries[k, y]
                for k in model.stations_set
            )
        return self._one_time_discounted_yearly_sum(model, yearly)

    def substation_investment_cost(self, model):
        """Costo de inversión en potencia de subestación: N_max_k[k]
        (conteo entero de baterías cargando en paralelo, ver
        max_installed_capacity_swap) se decide una sola vez para todo el
        horizonte (mismo patron que G_g/H), asi que se paga una unica vez,
        descontado al primer año. Se multiplica por p_charger para expresar
        el costo en $/kW aunque la variable de decision sea un conteo
        entero de baterias."""
        return sum(
            model.c_inv_ssee_k[k] * model.p_charger * model.N_max_k[k]
            for k in model.stations_set
        ) * self._discount_factor(model, self._first_year())

    def gen_investment_cost(self, model):
        """Costo de inversión en generación: G_g[g] se decide una sola vez
        para todo el horizonte (ver BoundRules), asi que se paga una unica
        vez, descontado al primer año."""
        if len(list(model.gen_set)) == 0:
            return 0
        return sum(model.G_g[g] * model.c_inv_g[g] * model.p_max_g[g] for g in model.gen_set) \
            * self._discount_factor(model, self._first_year())

    def gen_op_cost(self, model):
        """Costo de O&M anual de generación: recurre cada año del horizonte
        sobre la capacidad fija G_g[g]."""
        if len(list(model.gen_set)) == 0:
            return 0
        return sum(
            model.G_g[g] * model.c_op_g[g] * model.p_max_g[g] * self._discount_factor(model, y)
            for g in model.gen_set for y in model.years
        )

    def bess_investment_cost(self, model):
        """Costo de inversión BESS: H se decide una sola vez para todo el
        horizonte, se paga una unica vez, descontado al primer año."""
        if len(list(model.storage_set)) == 0:
            return 0
        return sum(model.H * model.c_inv_h[h] for h in model.storage_set) \
            * self._discount_factor(model, self._first_year())

    def bess_op_cost(self, model):
        """Costo de O&M anual BESS: recurre cada año del horizonte sobre la
        capacidad fija H."""
        if len(list(model.storage_set)) == 0:
            return 0
        return sum(
            model.H * model.c_op_h[h] * self._discount_factor(model, y)
            for h in model.storage_set for y in model.years
        )

    def peak_power_cost(self, model):
        return sum(model.P_pot[y] * 12 * 10 * self._discount_factor(model, y) for y in model.years)

    def battery_replace_cost(self, model):
        """Costo de reemplazo de TODAS las baterías físicas del sistema:
        evento puntual del año y, descontado con _discount_factor (mismo
        patron que inversion_cost/gen_investment_cost/bess_investment_cost).
        c_bat_replace es el costo TOTAL de UNA sola
        batería; Z_repl[y] ya linealiza R[y] * n_battery_fleet[y] (n_battery_fleet[y]
        = N_batteries_total[y] + n_active_slhd[y], la flota física completa: pool de
        estación (stock acumulado al año y) + una batería siempre instalada
        en cada LHD swap ACTIVO ese año — ver
        z_repl_upper1/upper2/lower), así que no hace falta sumar nada más acá."""
        if self.mine_system.battery_degradation is None:
            return 0
        return sum(
            model.Z_repl[y] * model.c_bat_replace * self._discount_factor(model, y)
            for y in model.years
        )

    def total_cost(self, model):
        return (self.lhd_charge_cost_bs(model)
                + self.inversion_cost(model)
                + self.substation_investment_cost(model)
                + self.gen_investment_cost(model)
                + self.gen_op_cost(model)
                + self.bess_investment_cost(model)
                + self.bess_op_cost(model)
                + self.peak_power_cost(model)
                + self.battery_replace_cost(model))

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






 
