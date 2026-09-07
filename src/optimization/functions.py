import pyomo.environ as pyo
import math
import pandas as pd
import numpy as np
import time
import re
from pyomo.environ import quicksum, value

class OptRules(object):

    def __init__(self, mine_system, time_series, autonomous_mode=False,
                 years_override=None, exogenous_stations=None,
                 mccormick_degradation=False):
        self.mine_system = mine_system
        self.time_series = time_series
        # Camino A (McCormick) aplicado al MONOLITICO completo (ver
        # degradacion_descomposicion_mccormick.md sec. 3): si True, el
        # bilineal n_ciclos_link se reemplaza por su envolvente convexa
        # (mismo bloque que ya usa YearBlockBuilder para el descompuesto,
        # ver compute_n_ciclos_bounds mas abajo) en vez de resolverse como
        # restriccion cuadratica no convexa con Gurobi NonConvex=2. Sin
        # efecto si is_decomposed_block=True (year_block.py ya construye
        # su propio bloque McCormick, independiente de este flag) ni si el
        # escenario no tiene hoja BatteryDegradation.
        self.mccormick_degradation = mccormick_degradation
        # Escenario DET autonomo: durante la colacion el LHD puede ademas
        # operar (no solo cargar o estar detenido). Ver OptSets.build_sets.
        self.autonomous_mode = autonomous_mode
        # Descomposicion Nested Benders (ver
        # implementacion_descomposicion_carga_ob.md): years_override acota
        # model.years a los años de un bloque (p.ej. un solo año), sin tocar
        # time_series.years (que sigue siendo el horizonte completo, usado
        # por year_position para el descuento correcto de cada bloque). None
        # preserva el comportamiento monolitico de hoy (model.years = todo
        # el horizonte).
        self.model_years = list(years_override) if years_override is not None else list(time_series.years)
        self.is_decomposed_block = years_override is not None
        # exogenous_stations: dict {(k, y): 0/1} con X fijo desde afuera (solo
        # en modo descompuesto, donde X deja de ser estado — ver documento
        # sec. 2.1 "Fuera del estado"). None preserva X como Var libre.
        self.exogenous_stations = exogenous_stations
        self.time_series.get_node_assignment(mine_system.get_system_lhds())
        self.time_series.get_elhd_at_node(mine_system.get_system_nodes())
        self.time_series.get_station_assignment(mine_system.get_system_lhds())
        self.time_series.get_elhd_at_station(mine_system.get_system_stations())



    def year_position(self, y: int) -> int:
        """Posicion 1-indexada de y dentro del horizonte modelado (1, 2, 3, ...),
        para exponente de descuento — desacoplado del valor real de y."""
        return sorted(self.time_series.years).index(y) + 1

    def _first_year(self):
        return sorted(self.time_series.years)[0]

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
        # Hora real en que arranca el horizonte (t=1), parametrizada por
        # escenario via Shifts.base_hour (distinta en DET/DCH).
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

            # Marcar el intervalo t si su PUNTO MEDIO cae dentro de [a,b).
            # Redondea al intervalo mas cercano (error maximo dt/2 por borde)
            # en vez de "cualquier solape cuenta" (que redondea siempre hacia
            # arriba en ambos extremos e infla el tiempo bloqueado).
            for t in range(1, max_t + 1):
                mid = (t - 1) * dt_minutes + dt_minutes / 2
                if a <= mid < b:
                    indices.add(t)

        return sorted(indices)

    def build_sets(self, model):
        model.lhd_set = pyo.Set(initialize=self.mine_system.get_system_lhds())
        model.elhd_set = pyo.Set(initialize=self.mine_system.get_electric_lhds())
        model.nodes_set = pyo.Set(initialize=self.mine_system.get_system_nodes())
        model.time_intervals_set = pyo.Set(initialize=self.time_series.time_intervals)
        model.days = pyo.Set(initialize=self.time_series.days_within_year)
        model.years = pyo.Set(initialize=self.model_years)
        model.shifts = pyo.Set(initialize=self.time_series.shifts)
        model.time_intervals_set_zero = pyo.Set(initialize=[0] + list(self.time_series.time_intervals))
        model.time_intervals_between_shifts_set = pyo.Set(initialize=self.time_series.get_intervals_between_shifts())
        base_minutes = int(round(self.time_series.base_hour * 60))
        _peak_dt_minutes = int(round(self.time_series.delta_t * 60))
        model.time_intervals_peak_set = pyo.Set(
            initialize=[
                t for t in self.time_series.time_intervals
                # Punto medio del intervalo (mismo criterio que
                # _get_time_intervals_for_pause_type), no "inicio en ventana".
                if 18 * 60 <= ((base_minutes + (t - 1) * _peak_dt_minutes + _peak_dt_minutes / 2) % 1440) < 22 * 60
            ]
        )
        model.stations_set = pyo.Set(initialize=self.mine_system.get_system_stations())
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

        # DET (nuevo) detentions: build sets using the DET pause definitions.
        # Modo normal: la colacion tambien impide operar (solo cargar o estar
        # detenido), igual que maintenance/road_clearing.
        # Modo autonomo: la colacion queda fuera de este set, por lo que el
        # LHD puede ademas operar (viajar/extraer) durante esa ventana; solo
        # queda restringido a cargar-o-detenido durante between_shifts (ver
        # between_shifts_elhd / time_intervals_between_shifts_det_set).
        det_pauses = self._get_pause_definitions_det()
        det_meal_intervals = self._get_time_intervals_for_pause_type("meal", pauses=det_pauses)
        det_maintenance_intervals = self._get_time_intervals_for_pause_type("maintenance", pauses=det_pauses)
        det_road_clearing_intervals = self._get_time_intervals_for_pause_type("road_clearing", pauses=det_pauses)
        det_between_shifts_intervals = self._get_time_intervals_for_pause_type("between_shifts", pauses=det_pauses)
        det_stop = sorted(set(det_maintenance_intervals) | set(det_road_clearing_intervals))

        if self.autonomous_mode:
            model.time_intervals_det_set = pyo.Set(
                initialize=sorted(set(det_stop))
            )
        else:
            model.time_intervals_det_set = pyo.Set(
                initialize=sorted(set(det_meal_intervals) | set(det_stop))
            )

        # Expose DET-specific subsets so they are serialized into parameters.json
        model.time_intervals_meal_det_set = pyo.Set(
            initialize=sorted(det_meal_intervals)
        )
        model.time_intervals_maintenance_det_set = pyo.Set(
            initialize=sorted(det_maintenance_intervals)
        )
        # Road clearing DET: al igual que colacion, el LHD puede estar detenido
        # o cargando (a diferencia de maintenance, donde debe permanecer detenido).
        model.time_intervals_road_clearing_det_set = pyo.Set(
            initialize=sorted(det_road_clearing_intervals)
        )
        # Cambio de turno (between_shifts) DET: extraido directamente de la
        # lista de pausas DET. En ambos modos (normal/autonomo) el LHD solo
        # puede cargar o estar detenido durante esta ventana.
        model.time_intervals_between_shifts_det_set = pyo.Set(
            initialize=sorted(det_between_shifts_intervals)
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

        # Pares de precedencia por estación para rotura de simetría en SOC inicial.
        # Dentro de cada estación, los ELHDs se ordenan por sufijo numérico ascendente.
        # El de menor índice debe iniciar con menor o igual SOC (carga primero).
        from collections import defaultdict
        elhds_per_station = defaultdict(list)
        for elhd in self.mine_system.get_electric_lhds():
            for station in self.time_series.mapper['Stations_per_elhd'].get(elhd, []):
                elhds_per_station[station].append(elhd)

        def _sort_key(x):
            s = self._extract_lhd_numeric_suffix(x)
            return (s is None, s if s is not None else float("inf"), str(x))

        charge_pairs = []
        for elhds in elhds_per_station.values():
            ordered = sorted(elhds, key=_sort_key)
            for idx in range(len(ordered) - 1):
                pair = (ordered[idx], ordered[idx + 1])
                if pair not in charge_pairs:
                    charge_pairs.append(pair)

        model.charge_precedence_pairs = pyo.Set(dimen=2, initialize=charge_pairs)


class OptParameters(OptRules):

    def build_parameters(self, model):
        #ParÃ¡metros temporales
        model.delta_t = pyo.Param(initialize=self.time_series.delta_t, mutable=True)
        model.t_ini = pyo.Param(initialize=self.time_series.get_time_intervals()[0], mutable=True)
        model.t_fin = pyo.Param(initialize=self.time_series.get_time_intervals()[-1], mutable=True)
        #ParÃ¡metros econÃ³micos
        model.m_j = pyo.Param(model.nodes_set, model.years, initialize={(j, y): self.time_series.get_extraction_goal(j, y) for j in model.nodes_set for y in model.years}, mutable=True)
        model.costo_marginal = pyo.Param(model.elhd_set, model.years, model.days, model.time_intervals_set, initialize={(b, y, d, t): self.time_series.get_marginal_cost_scaled(self.mine_system.elhd.get_energy_cost(b), d, t) for b in model.elhd_set for y in model.years for d in model.days for t in model.time_intervals_set}, mutable=True)
        #ParÃ¡metros LHD
        # ParÃ¡metros de viaje por nodo
        model.d_i    = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_n_intervals_trip(j,i)       for i in model.lhd_set for j in model.nodes_set}, mutable=False)
        model.pe_i   = pyo.Param(model.lhd_set, model.nodes_set, initialize={(i,j): self.time_series.get_energy_consumption(j,i)     for i in model.lhd_set for j in model.nodes_set}, mutable=False)
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

        # Eficiencia de carga/descarga de la baterÃ­a on-board (hoja LHD)
        model.eta_charge_i = pyo.Param(
        model.elhd_set,
        initialize={i: float(self.mine_system.elhd.get_charge_efficiency(i)) for i in model.elhd_set},
        mutable=False)

        model.eta_discharge_i = pyo.Param(
        model.elhd_set,
        initialize={i: float(self.mine_system.elhd.get_discharge_efficiency(i)) for i in model.elhd_set},
        mutable=False)
        # Capacidad de pala
        model.g_i    = pyo.Param(model.lhd_set,                   initialize={i: self.mine_system.elhd.get_load_capacity(i)       for i in model.lhd_set}, mutable=False)
        model.filling_factor = pyo.Param(model.lhd_set,        initialize={i: self.mine_system.elhd.get_filling_factor(i)      for i in model.lhd_set}, mutable=False)
         
        #ParÃ¡metros problema de inversiÃ³n
        model.p_charger = pyo.Param(initialize=self.mine_system.chargers.get_charger_power(), mutable=False)
        model.p_peak = pyo.Param(initialize=self.mine_system.chargers.get_p_peak_dist(), mutable=False)
        model.charger_cost = pyo.Param(initialize=self.mine_system.chargers.get_charger_cost(), mutable=False)
        model.scaling_factor_op_cost = pyo.Param(initialize=self.time_series.scaling_factor_op_cost, mutable=True)
        #Parametros estaciones de carga
        model.station_cost_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_station_cost(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.c_inv_ssee_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_inv_ssee(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.c_bays_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_bays(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.c_charger_space_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_c_charger_space(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.max_bays_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_bays(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.max_chargers_per_bay_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_max_chargers_per_bay(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.max_chargers_k = pyo.Param(model.stations_set, initialize={k: value(model.max_bays_k[k]) * value(model.max_chargers_per_bay_k[k]) for k in model.stations_set}, mutable=False, within=pyo.Reals)

        # Costo de energia de la red por (y,d,t): usa el primer ELHD como referencia del contrato
        ref_elhd = list(model.elhd_set)[0]
        model.costo_red = pyo.Param(
            model.years, model.days, model.time_intervals_set,
            initialize={(y, d, t): self.time_series.get_marginal_cost_scaled(
                            self.mine_system.elhd.get_energy_cost(ref_elhd), d, t)
                        for y in model.years for d in model.days for t in model.time_intervals_set},
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
                model.gen_set, model.years, model.days, model.time_intervals_set,
                initialize={(g, y, d, t): self.time_series.get_alpha_g(g, d, t)
                            for g in model.gen_set
                            for y in model.years
                            for d in model.days
                            for t in model.time_intervals_set},
                mutable=False)

        # Parametros de almacenamiento BESS (solo si existen unidades de almacenamiento)
        if len(list(model.storage_set)) > 0:
            storage_names = list(model.storage_set)
            if len(storage_names) > 1:
                # El modelo de inversion multi-anio de BESS (H, sin indice h)
                # asume un unico cluster de almacenamiento candidato, ver
                # docs/restricciones_modelo.tex sec. Infraestructura de
                # generacion y almacenamiento.
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
            # (H no tiene cota superior natural al ser entera, a diferencia de
            # X que es binaria): ver eq. max_storage_units.
            model.h_max = pyo.Param(initialize=stor.get_h_max(storage_names[0]), mutable=False)

        # Degradación de batería on-board (fleet-wide, solo si la hoja existe)
        bd = self.mine_system.battery_degradation
        if bd is not None:
            model.gamma_coef = pyo.Param(initialize=bd.get_gamma_coef(), mutable=True)
            model.c_bat_replace = pyo.Param(initialize=bd.get_c_bat_replace(), mutable=True)
            model.min_capacity_fraction = pyo.Param(initialize=bd.get_min_capacity_fraction(), mutable=True)

            # Fleet-wide: todos los ELHD deben compartir la misma capacidad nominal,
            # porque b_bar/R se modelan una vez para toda la flota, no por LHD.
            bmax_values = {round(float(self.mine_system.elhd.get_e_max(b)), 6) for b in model.elhd_set}
            if len(bmax_values) > 1:
                raise ValueError(
                    "Degradación de batería (fleet-wide) requiere que todos los ELHD "
                    f"compartan la misma capacidad nominal (e_max); valores encontrados: {bmax_values}"
                )
            model.b_max_fleet = pyo.Param(initialize=next(iter(bmax_values)), mutable=False)

        # Tasa de descuento: 0.0 (sin descuento, comportamiento actual) si no hay
        # datos de degradación cargados o si la hoja no trae la columna.
        model.discount_rate = pyo.Param(initialize=(bd.get_discount_rate() if bd is not None else 0.0), mutable=True)


def compute_n_ciclos_bounds(model, year, time_series):
    """Cotas [N_L, N_U] de N_ciclos_y PARA UN AÑO DADO, validas tanto para
    un bloque descompuesto (model.years = {year}) como para el monolitico
    completo (model.years = todo el horizonte) -- ninguno de los sets que
    usa (ZCHARGE_INDEX, Y_INDEX, days, time_intervals_set) necesita
    filtrado adicional: ZCHARGE_INDEX no esta indexado por año (solo
    (estacion, elhd)) y Y_INDEX/nodes_set se filtran explicitamente por
    `year` mas abajo. N_L es la cobertura minima fraccionaria del target
    de produccion (cota valida para CUALQUIER solucion factible); N_U es
    la cobertura entera greedy con margen de seguridad. Usada tanto por
    year_block.py (Camino A descompuesto) como por ConstraintRules
    (Camino A aplicado al monolitico completo, ver
    build_mccormick_degradation_block) -- recalcular por año, no reusar
    una cota global, o el big-M/envolvente de McCormick queda flojo (ver
    degradacion_descomposicion_mccormick.md sec. 10)."""
    y = year
    B_L = value(model.B_L)
    B_U = value(model.B_U)
    n_elhd = value(model.n_elhd_bd[year])
    N_U_SAFETY_MARGIN = 1.20

    def _greedy_min_cost_fractional(nodes_sorted, total_target):
        remaining = total_target
        energy = 0.0
        for ratio, ppa, ub_j, e_j in nodes_sorted:
            if remaining <= 0:
                break
            tomar = min(ub_j * ppa, remaining)
            energy += tomar * ratio
            remaining -= tomar
        return energy

    def _greedy_min_cost_integer(nodes_sorted, total_target):
        remaining = total_target
        energy = 0.0
        for ratio, ppa, ub_j, e_j in nodes_sorted:
            if remaining <= 0:
                break
            visits = min(ub_j, math.ceil(remaining / ppa))
            energy += visits * e_j
            remaining -= visits * ppa
        return energy

    node_lhd_pairs = {}
    for (i2, j2, y2, d2, t2) in model.Y_INDEX:
        node_lhd_pairs.setdefault((y2, d2, j2), []).append(i2)

    e_day_low = 0.0
    e_day_high = 0.0
    for d in model.days:
        nodes_info = []
        total_target_day = 0.0
        for j in model.nodes_set:
            i_list = node_lhd_pairs.get((y, d, j))
            if not i_list:
                continue
            elec_i_list = [i for i in i_list if i in model.elhd_set]
            if not elec_i_list:
                continue
            i_j = elec_i_list[0]

            prod_per_assign = (value(model.g_i[i_j]) * time_series.get_n_trips(j, i_j)
                                * value(model.filling_factor[i_j]))
            target = value(model.m_j[j, y])
            ub_j = math.ceil(target / prod_per_assign) + 1

            e_j = (value(model.pe_i[i_j, j]) * value(model.d_i[i_j, j])
                   * time_series.get_n_trips(j, i_j) / value(model.eta_discharge_i[i_j]))

            nodes_info.append((e_j / prod_per_assign, prod_per_assign, ub_j, e_j))
            total_target_day += target

        nodes_sorted = sorted(nodes_info, key=lambda info: info[0])
        e_day_low += _greedy_min_cost_fractional(nodes_sorted, total_target_day)
        e_day_high += _greedy_min_cost_integer(nodes_sorted, total_target_day)

    e_year_low = e_day_low * value(model.scaling_factor_op_cost)
    e_year_high = e_day_high * value(model.scaling_factor_op_cost) * N_U_SAFETY_MARGIN

    n_slots_per_year = len(model.ZCHARGE_INDEX) * len(model.days) * len(model.time_intervals_set)
    max_energy_per_year = (n_slots_per_year * value(model.p_charger) * value(model.delta_t)
                            * value(model.scaling_factor_op_cost))

    N_L = max(math.floor(e_year_low / (n_elhd * B_U)), 0)
    N_U_prod = math.ceil(e_year_high / (n_elhd * B_L))
    N_U_charger = math.ceil(max_energy_per_year / (n_elhd * B_L))
    N_U = max(min(N_U_charger, N_U_prod), N_L)

    return N_L, N_U


class BoundRules(OptRules):

    def Z(self, model, i, y, d, t):
        return 0, 1

    def Z_charge(self, model, k, i, y, d, t):
        return 0, 1

    def P_pot(self, model, y):
        return (0, None)

    def B(self, model, b, y, d, t):
        return (0, model.bmax_b[b])
    
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
                for y in m.years:
                    for d in m.days:
                        for t in m.time_intervals_set:
                            yield (k, i, y, d, t)
        model.ZCHARGE_DAYS_TIME_INDEX = pyo.Set(dimen=5, initialize=_ZCHARGE_DAYS_TIME)

        def _init_Y_INDEX(m):
            for y in m.years:
                for d in m.days:
                    for t in m.time_intervals_set:
                        for i in m.lhd_set:
                            node_list = self.time_series.mapper['Nodes_assigned_at_interval'][(y, d, t, i)]
                            for j in node_list:
                                yield (i, j, y, d, t)
        model.Y_INDEX = pyo.Set(dimen=5, initialize=_init_Y_INDEX)

        model.Y         = pyo.Var(model.Y_INDEX, domain=pyo.Binary)
        model.Z         = pyo.Var(model.lhd_set, model.years, model.days, model.time_intervals_set,
                                  bounds=self.Z, domain=pyo.Binary)
        model.Z_charge  = pyo.Var(model.ZCHARGE_INDEX, model.years, model.days, model.time_intervals_set,
                                  bounds=self.Z_charge, domain=pyo.Binary)
        model.P         = pyo.Var(model.ZCHARGE_INDEX, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
        model.P_pot     = pyo.Var(model.years, domain=pyo.NonNegativeReals)
        model.B         = pyo.Var(model.elhd_set, model.years, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)
        # Inversion multi-anio: X/N_chargers pasan a ser el STOCK acumulado por
        # año (usado en el resto de las restricciones operativas), y Delta_X/
        # Delta_N_chargers son la decision de inversion propiamente tal del año
        # y (la que entra al costo). Ver restricciones de enlace link_station_stock
        # / link_charger_stock mas abajo, que acumulan Delta_* año a año desde
        # un stock inicial cero (escenario greenfield).
        if self.exogenous_stations is None:
            model.X          = pyo.Var(model.stations_set, model.years, domain=pyo.Binary)
            model.Delta_X    = pyo.Var(model.stations_set, model.years, domain=pyo.Binary)
        else:
            # Modo descompuesto: X es exogeno (parametro fijo), sin ΔX ni
            # link_station_stock asociado — ver documento sec. 2.1 "Fuera del
            # estado". exogenous_stations: dict {(k, y): 0/1}.
            model.X = pyo.Param(
                model.stations_set, model.years,
                initialize=lambda m, k, y: self.exogenous_stations[(k, y)],
                within=pyo.Binary, mutable=False,
            )
        model.N_chargers        = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        model.Delta_N_chargers  = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        # Potencia de subestacion (kW): decidida UNA sola vez para todo el
        # horizonte (sin indice de año, sin Delta) -- mismo patron que G_g/H.
        # Sin cota fisica (igual que antes): el costo es lo unico que limita
        # cuanto se construye.
        model.P_max_k        = pyo.Var(model.stations_set, domain=pyo.NonNegativeReals)
        model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
        model.EndCharge   = pyo.Var(model.stations_set, model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)

        model.StartAssign = pyo.Var(model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
        model.EndAssign   = pyo.Var(model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)

        if len(list(model.gen_set)) > 0:
            # G_g = capacidad instalada (fraccion de p_max_g), decidida UNA
            # sola vez para todo el horizonte (sin indice de año, sin Delta) —
            # mismo patron que la rama de horizonte unico battery_swapping.
            model.G_g    = pyo.Var(model.gen_set, domain=pyo.NonNegativeReals)
            model.P_gen  = pyo.Var(model.gen_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
            model.Curt_g = pyo.Var(model.gen_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)

        model.P_red = pyo.Var(model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)

        if len(list(model.storage_set)) > 0:
            # Un unico cluster de almacenamiento (ver guardia de cardinalidad en
            # build_parameters): H = unidades instaladas del cluster, decidida
            # UNA sola vez para todo el horizonte (sin indice de año, sin
            # Delta) -- mismo patron que G_g. Sin indice h a diferencia de la
            # formulacion de horizonte unico (H_h binaria) — el indice h se
            # mantiene solo en las restricciones operativas (P_bat, A_h) por
            # si en el futuro se admite mas de un cluster candidato.
            model.H     = pyo.Var(domain=pyo.NonNegativeReals)
            model.P_bat = pyo.Var(model.storage_set, model.years, model.days, model.time_intervals_set, domain=pyo.Reals)
            model.A_h   = pyo.Var(model.storage_set, model.years, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)

        # Degradación de batería on-board (fleet-wide, solo si hay datos cargados)
        # Modelo por ciclos con recursión año a año (sin acumulador explícito
        # tipo CumEFC/CumS): B[y] = capacidad operable al INICIO del año y,
        # D[y] = capacidad al FINAL del año y. El arrastre entre años lo da
        # D[y-1] directamente (ver b_y_link en ConstraintRules), no un
        # contador de ciclos acumulados. La ecuación de ciclos
        # (N_ciclos[y] * B[y] == S[y]/n_elhd) es bilineal (producto de dos
        # variables continuas) y se deja SIN linealizar: se resuelve como
        # restricción cuadrática no convexa directamente en Gurobi
        # (requiere opt.options['NonConvex'] = 2, ver opt_model.py), a
        # diferencia de las alternativas descartadas (expansión binaria
        # exacta / aproximación lineal por energía acumulada).
        if self.mine_system.battery_degradation is not None:
            b_max_val = value(model.b_max_fleet)
            min_frac_val = value(model.min_capacity_fraction)
            n_elhd = len(model.elhd_set)

            # Cotas de B[y]/D[y]: capacidad nominal como techo, piso mínimo
            # operable (min_capacity_fraction) como el resto del modelo ya
            # usaba para b_bar.
            B_L = min_frac_val * b_max_val
            B_U = b_max_val

            model.B_L = pyo.Param(initialize=B_L, mutable=False)
            model.B_U = pyo.Param(initialize=B_U, mutable=False)
            # n_elhd_bd por año: si existe la hoja 'FleetByYear' (flota
            # activa variable, ej. ramp-up), se usa esa; si no, se
            # mantiene el fleet nominal completo para todos los años
            # (comportamiento previo a esta hoja). Year-indexado porque un
            # fleet activo mas chico implica MAS ciclos equivalentes por
            # unidad para la misma energia S[y] -- ver n_ciclos_link.
            if self.mine_system.fleet_by_year is not None:
                n_elhd_by_year = {y: self.mine_system.fleet_by_year.get_n_active(y) for y in model.years}
            else:
                n_elhd_by_year = {y: n_elhd for y in model.years}
            model.n_elhd_bd = pyo.Param(model.years, initialize=n_elhd_by_year, mutable=False)
            # Fracción de capacidad nominal que se recupera al reemplazar la
            # batería (0.3 = 30%, constante del modelo, no un dato cargado).
            model.replace_capacity_fraction = pyo.Param(initialize=0.3, mutable=True)

            # Energía máxima cargable en un año por toda la flota (todas las
            # combinaciones estación-LHD-día-intervalo a potencia nominal del
            # cargador), ya escalada a año completo. Se usa solo para acotar
            # N_ciclos[y] por arriba (cota holgada, necesaria para que Gurobi
            # pueda construir la relajación McCormick del término bilineal
            # N_ciclos[y]*B[y] en n_ciclos_link) -- no requiere el cálculo
            # greedy ajustado que usaban las alternativas linealizadas,
            # porque aquí no se traduce en ningún bit/expansión binaria.
            # Year-indexada por la misma razón que n_elhd_bd: con fleet
            # activo variable, un año con menos LHD admite MAS ciclos por
            # unidad dentro de la misma energia maxima cargable -- una cota
            # global unica (con el fleet nominal completo) quedaria
            # demasiado ajustada para los años de fleet chico (mismo tipo
            # de bug que N_U_SAFETY_MARGIN, ver compute_n_ciclos_bounds).
            n_slots_per_year = len(model.ZCHARGE_INDEX) * len(model.days) * len(model.time_intervals_set)
            max_energy_per_year = (n_slots_per_year * value(model.p_charger) * value(model.delta_t)
                                    * value(model.scaling_factor_op_cost))
            n_ciclos_max_by_year = {y: max_energy_per_year / (n_elhd_by_year[y] * B_L) for y in model.years}

            model.R        = pyo.Var(model.years, domain=pyo.Binary)
            model.b_bar    = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(B_L, B_U))
            model.D        = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(B_L, B_U))
            model.S        = pyo.Var(model.years, domain=pyo.NonNegativeReals)
            model.N_ciclos = pyo.Var(model.years, domain=pyo.NonNegativeReals,
                                      bounds=lambda m, y: (0, n_ciclos_max_by_year[y]))

            if self.mccormick_degradation and not self.is_decomposed_block:
                # Camino A aplicado al monolitico completo: recalcular
                # [N_L, N_U] por año (greedy, compute_n_ciclos_bounds) en
                # vez de dejar la cota global n_ciclos_max de arriba --
                # necesario para que la envolvente de McCormick de
                # ConstraintRules.build_mccormick_degradation_block quede
                # ajustada (documento sec. 10: "recalcular con el B_y del
                # año, o el big-M queda flojo"). El descompuesto ya hace
                # esto por su cuenta (year_block.py), de ahi el guard
                # not is_decomposed_block.
                for y in model.years:
                    N_L, N_U = compute_n_ciclos_bounds(model, y, self.time_series)
                    model.N_ciclos[y].setlb(N_L)
                    model.N_ciclos[y].setub(N_U)

            # B[y1] = b_max fijo (ec. 5: la batería parte nueva) y R[y1] = 0
            # fijo (reemplazar en y1 no tiene efecto sobre la capacidad y solo
            # agregaría costo evitable). Se fijan las variables directamente
            # en vez de registrar una restricción de un solo índice. Usa el
            # primer año del horizonte GLOBAL (self._first_year()), no
            # min(model.years), por consistencia con el resto del archivo
            # (ver link_station_stock) de cara a una futura descomposición.
            first_year = self._first_year()
            if first_year in model.years:
                model.b_bar[first_year].fix(b_max_val)
                model.R[first_year].fix(0)

            years_sorted = sorted(model.years)
            later_years = [y for y in years_sorted if y != first_year]
            if later_years:
                # b_y_link (ec. 2) aplica para todo año salvo el primero,
                # usando D[y-1] -- ver ConstraintRules.b_y_link.
                model.later_years_set = pyo.Set(initialize=later_years, within=model.years)

        model.M = pyo.Var(model.Y_INDEX, domain=pyo.NonNegativeReals)

        #AsignaciÃ³n estaciÃ³n por macrobloque
        #model.U = pyo.Var(model.stations_set, model.elhd_set, domain=pyo.Binary)

        #Límite inferior producción por punto
        #model.L = pyo.Var(domain=pyo.NonNegativeReals)

from src.optimization.functions import OptRules
import pyomo.environ as pyo

class ConstraintRules(OptRules):

    # (C1) Estado único de cada LHD
    def state_unique_elhd(self, model, i, y, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        z_charge_sum = sum(model.Z_charge[k,i,y,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        return model.Z[i,y,d,t] + z_charge_sum + sum(model.Y[i,j,y,d,t] for j in nodes) == 1

    # (C5) Estado entre turnos
    def between_shifts_elhd(self, model, i, y, d, t):
        z_charge_sum = sum(model.Z_charge[k,i,y,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        return model.Z[i,y,d,t] + z_charge_sum == 1

    def battery_soc(self, model, i, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        charge = sum(model.P[k,i,y,d,t] * model.delta_t for (k, i2) in model.ZCHARGE_INDEX if i2 == i)
        discharge = sum(
            model.Y[i,j,y,d,t] * model.pe_i[i,j] * model.d_i[i,j] * self.time_series.get_n_trips(j, i)
            for j in self.time_series.mapper['Nodes_assigned_at_interval'][(y, d, t, i)]
        )
        # Energia efectivamente almacenada/retirada de la baterÃ­a, afectada por
        # las eficiencias de carga y descarga de la hoja LHD (charge_efficiency,
        # discharge_efficiency): la carga entra atenuada por eta_charge y el
        # consumo de tracciÃ³n se retira de la baterÃ­a amplificado por 1/eta_discharge.
        charge_eff = charge * model.eta_charge_i[i]
        discharge_eff = discharge / model.eta_discharge_i[i]

        if t == t0:
            return model.B[i,y,d,t] == model.B[i,y,d,0] + charge_eff - discharge_eff
        else:
            return model.B[i,y,d,t] == model.B[i,y,d,t-1] + charge_eff - discharge_eff

    def battery_lower(self, model, i, y, d, t):
        cap = model.b_bar[y] if self.mine_system.battery_degradation is not None else model.bmax_b[i]
        return model.B[i,y,d,t] >= model.bmin_b[i] * cap

    def battery_upper(self, model, i, y, d, t):
        cap = model.b_bar[y] if self.mine_system.battery_degradation is not None else model.bmax_b[i]
        return model.B[i,y,d,t] <= cap

    def battery_boundary(self, model, i, y, d):
        tf = self.time_series.get_time_intervals()[-1]
        return model.B[i,y,d,0] == model.B[i,y,d,tf]

    def battery_soc_break_symmetry(self, model, i_low, i_high, y, d):
        return model.B[i_high,y,d,0] <= model.B[i_low,y,d,0]

    def daily_production(self, model, y, d):
        total_target = sum(model.m_j[j, y] for j in model.nodes_set)
        term_de = sum(
            model.Y[i2,j2,y2,d2,t2] * model.g_i[i2] * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]
            for (i2, j2, y2, d2, t2) in model.Y
            if y2 == y and d2 == d
        )
        return term_de >= total_target

    def production(self, model, y, d, j):
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
        lb = math.floor(target / prod_per_assign) - 1
        ub = math.ceil(target / prod_per_assign) + 1
        visits = sum(model.Y[i2, j, y, d, t2] for i2, t2 in y_pairs)
        return pyo.inequality(lb, visits, ub)

    def interval_extraction_M(self, model, i, j, y, d, t):
        return model.M[i,j,y,d,t] == (
            model.Y[i,j,y,d,t] * model.g_i[i] * self.time_series.get_n_trips(j, i) * model.filling_factor[i]
        )

    def max_n_chargers(self, model, k, y):
        return model.N_chargers[k,y] <= model.max_bays_k[k] * model.X[k,y]

    def station_existence_constraint(self, model, k, i, y, d, t):
        return model.Z_charge[k,i,y,d,t] <= model.X[k,y]

    def charger_limit(self, model, k, y, d, t):
        return sum(model.Z_charge[k,i,y,d,t] for (k2, i) in model.ZCHARGE_INDEX if k2 == k) <= model.N_chargers[k,y]

    # ------------------------------------------------------------------ #
    # Inversion multi-anio: restricciones de enlace (linking constraints)
    # entre el stock acumulado y el incremento anual, para estaciones,
    # cargadores, generacion y almacenamiento. Ver
    # docs/restricciones_modelo.tex sec. "Inversion multi-año e
    # infraestructura acumulada". Escenario greenfield: stock inicial (antes
    # de y1) es cero para las cuatro tecnologias, por lo que el caso y==y1
    # colapsa a stock[y1] == delta[y1].
    # ------------------------------------------------------------------ #
    def link_station_stock(self, model, k, y):
        if y == self._first_year():
            return model.X[k,y] == model.Delta_X[k,y]
        return model.X[k,y] == model.X[k, self._prev_year(y)] + model.Delta_X[k,y]

    def link_charger_stock(self, model, k, y):
        if y == self._first_year():
            return model.N_chargers[k,y] == model.Delta_N_chargers[k,y]
        return model.N_chargers[k,y] == model.N_chargers[k, self._prev_year(y)] + model.Delta_N_chargers[k,y]

    def max_storage_units(self, model):
        return model.H <= model.h_max

    def charge_state(self, model, k, i, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        if t > t0:
            return model.Z_charge[k,i,y,d,t] - model.Z_charge[k,i,y,d,t-1] == model.StartCharge[k,i,y,d,t] - model.EndCharge[k,i,y,d,t]
        else:
            return model.Z_charge[k,i,y,d,t] == model.StartCharge[k,i,y,d,t] - model.EndCharge[k,i,y,d,t]

    def min_charge_duration(self, model, k, i, y, d, t):
        t_fin = self.time_series.get_time_intervals()[-1]
        if t == t_fin:
            return model.Z_charge[k,i,y,d,t] == 0
        return model.Z_charge[k,i,y,d,t] + model.Z_charge[k,i,y,d,t+1] >= 2 * model.StartCharge[k,i,y,d,t]

    # AnÃ¡logo a charge_state, pero para el estado agregado "asignado a extracciÃ³n"
    # (Assign = suma de Y sobre todos los nodos j asignables al LHD i en (y,d,t)).
    def assign_state(self, model, i, y, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        assign_sum = sum(model.Y[i,j,y,d,t] for j in nodes)
        t0 = self.time_series.get_time_intervals()[0]
        if t > t0:
            nodes_prev = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t-1, i), [])
            assign_sum_prev = sum(model.Y[i,j,y,d,t-1] for j in nodes_prev)
            return assign_sum - assign_sum_prev == model.StartAssign[i,y,d,t] - model.EndAssign[i,y,d,t]
        else:
            return assign_sum == model.StartAssign[i,y,d,t] - model.EndAssign[i,y,d,t]

    # AnÃ¡logo a min_charge_duration: si el LHD i arranca una asignaciÃ³n a
    # extracciÃ³n (a cualquier nodo j) en t, debe mantenerse asignado (a algÃºn
    # nodo, no necesariamente el mismo) en t+1.
    def min_assign_duration(self, model, i, y, d, t):
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        t_fin = self.time_series.get_time_intervals()[-1]
        assign_sum_t = sum(model.Y[i,j,y,d,t] for j in nodes)
        if t == t_fin:
            return assign_sum_t == 0
        nodes_next = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t+1, i), [])
        assign_sum_next = sum(model.Y[i,j,y,d,t+1] for j in nodes_next)
        return assign_sum_t + assign_sum_next >= 2 * model.StartAssign[i,y,d,t]

    def max_power(self, model, k, i, y, d, t):
        return model.P[k,i,y,d,t] <= model.Z_charge[k,i,y,d,t] * model.p_charger

    def max_installed_capacity(self, model, k, y, d, t):
        station_elhds = [i for (k2, i) in model.ZCHARGE_INDEX if k2 == k]
        if not station_elhds:
            return pyo.Constraint.Skip
        return sum(model.P[k,i,y,d,t] for i in station_elhds) <= model.P_max_k[k]

    def power_cost_peak_limit(self, model, y, d, t):
        if not (91 <= d <= 244):
            return pyo.Constraint.Skip
        if t not in model.time_intervals_peak_set:
            return pyo.Constraint.Skip
        return model.P_red[y,d,t] <= model.P_pot[y]

    def power_balance(self, model, y, d, t):
        demand = sum(model.P[k,i,y,d,t] for (k, i) in model.ZCHARGE_INDEX)
        gen  = (sum(model.P_gen[g,y,d,t] for g in model.gen_set)  if len(list(model.gen_set))   > 0 else 0)
        bess = (sum(model.P_bat[h,y,d,t] for h in model.storage_set) if len(list(model.storage_set)) > 0 else 0)
        return model.P_red[y,d,t] + gen + bess == demand

    def grid_limit(self, model, y, d, t):
        return model.P_red[y,d,t] <= model.p_peak

    def gen_limit(self, model, g, y, d, t):
        return (model.P_gen[g,y,d,t] + model.Curt_g[g,y,d,t]
                == model.G_g[g] * model.p_max_g[g] * model.alpha_g[g,y,d,t])

    def gen_max_units(self, model, g):
        return model.G_g[g] <= model.g_max_g[g]

    def initial_condition_station(self, model):
        # No registrada en build_all_constraints (dead code, ya lo era antes
        # de la inversion multi-año). Si se reactiva, X ahora es X[k,y] —
        # actualizar la indexacion antes de usarla.
        return sum(model.X[k] for k in model.stations_set) == 1

    def meal_g1_no_travel_group1(self, model, i, y, d, t):
        if t not in model.time_intervals_meal_g1_set:
            return pyo.Constraint.Skip
        if i not in model.meal_group1_set:
            return pyo.Constraint.Skip
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return sum(model.Y[i,j,y,d,t] for j in nodes) == 0

    def meal_g2_no_travel_group2(self, model, i, y, d, t):
        if t not in model.time_intervals_meal_g2_set:
            return pyo.Constraint.Skip
        if i not in model.meal_group2_set:
            return pyo.Constraint.Skip
        nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
        if not nodes:
            return pyo.Constraint.Skip
        return sum(model.Y[i,j,y,d,t] for j in nodes) == 0

    def bess_power_upper(self, model, h, y, d, t):
        return model.P_bat[h,y,d,t] <= model.p_max_h[h] * model.H

    def bess_power_lower(self, model, h, y, d, t):
        return model.P_bat[h,y,d,t] >= -model.p_max_h[h] * model.H

    def bess_soc_balance(self, model, h, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        prev = model.A_h[h,y,d,0] if t == t0 else model.A_h[h,y,d,t-1]
        return model.A_h[h,y,d,t] == prev - (model.P_bat[h,y,d,t] / model.eta_h[h]) * model.delta_t

    def bess_soc_init(self, model, h, y, d):
        return model.A_h[h,y,d,0] == 0

    def bess_soc_upper(self, model, h, y, d, t):
        return model.A_h[h,y,d,t] <= model.a_max_h[h] * model.H

    def bess_soc_lower(self, model, h, y, d, t):
        return model.A_h[h,y,d,t] >= model.a_min_h[h] * model.H

    def bess_soc_cyclic(self, model, h, y, d):
        t_ini = self.time_series.get_time_intervals()[0]
        t_fin = self.time_series.get_time_intervals()[-1]
        return model.A_h[h,y,d,t_ini] == model.A_h[h,y,d,t_fin]

    def maint_stop_all(self, model, i, y, d, t):
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z[i,y,d,t] == 1

    def maint_no_charge(self, model, k, i, y, d, t):
        if t not in model.time_intervals_maintenance_set:
            return pyo.Constraint.Skip
        return model.Z_charge[k,i,y,d,t] == 0

    def charge_only_meal_or_shift_change(self, model, k, i, y, d, t):
        if t in model.time_intervals_meal_set or t in model.time_intervals_between_shifts_set:
            return pyo.Constraint.Skip
        return model.Z_charge[k,i,y,d,t] == 0

    def det_stop_all(self, model, i, y, d, t):
        if t not in model.time_intervals_det_set:
            return pyo.Constraint.Skip
        return model.Z[i,y,d,t] + sum(model.Z_charge[k,i,y,d,t] for (k, i2) in model.ZCHARGE_INDEX if i2 == i) == 1

    def charge_only_meal_or_between_shifts_det(self, model, k, i, y, d, t):
        """Version DET de charge_only_meal_or_shift_change.

        Se permite cargar (Z_charge = 1) durante colacion DET
        (time_intervals_meal_det_set), road_clearing DET
        (time_intervals_road_clearing_det_set) o entre turnos DET
        (time_intervals_between_shifts_det_set). Durante maintenance el LHD
        debe permanecer detenido sin cargar, por lo que esa ventana no se
        incluye aqui. Fuera de esas ventanas la carga queda prohibida.

        Definida pero no registrada en build_all_constraints (igual que
        det_stop_all): el esquema activo hoy en esta rama sigue siendo DCH.
        """
        if (
            t in model.time_intervals_meal_det_set
            or t in model.time_intervals_road_clearing_det_set
            or t in model.time_intervals_between_shifts_det_set
        ):
            return pyo.Constraint.Skip
        return model.Z_charge[k,i,y,d,t] == 0

    def fixed_n_chargers(self, model, k):
        # No registrada en build_all_constraints (dead code, ya lo era antes
        # de la inversion multi-año). Si se reactiva, N_chargers ahora es
        # N_chargers[k,y] — actualizar la indexacion antes de usarla.
        if k == "station_1":
            return model.N_chargers[k] == 4
        elif k == "station_2":
            return model.N_chargers[k] == 2
        elif k == "station_3":
            return model.N_chargers[k] == 2

    def _prev_year(self, y):
        years_sorted = sorted(self.time_series.years)
        return years_sorted[years_sorted.index(y) - 1]

    # ------------------------------------------------------------------ #
    # Degradación de batería on-board (fleet-wide). Modelo por ciclos con
    # recursión año a año: B[y] = capacidad al INICIO del año, D[y] =
    # capacidad al FINAL del año (ver BoundRules.build_all_variables para
    # las variables/cotas). Ecuaciones (numeración del modelo entregado):
    #   1) B[y] <= b_max            -> cota superior de la Var b_bar (B_U)
    #   2) B[y] <= D[y-1] + 0.3*b_max*R[y]   para y != primer año -> b_y_link
    #   3) N_ciclos[y] * B[y] == S[y]/n_elhd                      -> n_ciclos_link
    #   4) D[y] = B[y] - gamma_coef * N_ciclos[y]                 -> d_y_fade
    #   5) B[y1] = b_max             -> fijado en BoundRules (model.b_bar[y1].fix)
    # ------------------------------------------------------------------ #
    def s_def(self, model, y):
        """S[y] = energía cargada por la flota en el año y (kWh), escalada a año completo."""
        energy_repr_day = sum(
            model.P[k, i, y2, d, t] * model.delta_t
            for (k, i, y2, d, t) in model.ZCHARGE_DAYS_TIME_INDEX if y2 == y
        )
        return model.S[y] == energy_repr_day * model.scaling_factor_op_cost

    def n_ciclos_link(self, model, y):
        """(ec. 3) N_ciclos[y] * B[y] == S[y] / n_elhd -- bilineal (producto
        de dos variables continuas), resuelto directo como restricción
        cuadrática no convexa (Gurobi NonConvex=2), sin linealizar."""
        return model.N_ciclos[y] * model.b_bar[y] == model.S[y] / model.n_elhd_bd[y]

    def build_mccormick_degradation_block(self, model):
        """Camino A (McCormick, degradacion_descomposicion_mccormick.md
        sec. 3.3) aplicado al MONOLITICO completo: reemplaza la ec. 3
        (n_ciclos_link, bilineal no convexa) por su envolvente convexa,
        para TODOS los años del horizonte en una sola pasada -- mismo
        principio que year_block.py::_add_degradation_state usa por
        bloque, generalizado con Vars/Constraints indexadas por
        model.years en vez de un componente escalar por año. Con esto el
        modelo completo queda MILP puro: opt_model.py._configure_solver
        ya no necesita activar NonConvex=2 (ver guard
        self.mccormick_degradation ahi)."""
        B_L = value(model.B_L)
        B_U = value(model.B_U)
        bounds_by_year = {
            y: compute_n_ciclos_bounds(model, y, self.time_series) for y in model.years
        }

        # w_deg[y] ~= b_bar[y] * N_ciclos[y] -- variable auxiliar de la
        # envolvente de McCormick (las cotas [N_L,N_U] de N_ciclos ya
        # quedaron tightened por año en BoundRules.build_all_variables).
        model.w_deg = pyo.Var(model.years, domain=pyo.NonNegativeReals)

        def _mccormick_lb1(m, y):
            N_L, _ = bounds_by_year[y]
            return m.w_deg[y] >= N_L * m.b_bar[y] + B_L * m.N_ciclos[y] - N_L * B_L

        def _mccormick_lb2(m, y):
            _, N_U = bounds_by_year[y]
            return m.w_deg[y] >= N_U * m.b_bar[y] + B_U * m.N_ciclos[y] - N_U * B_U

        def _mccormick_ub1(m, y):
            _, N_U = bounds_by_year[y]
            N_L, _ = bounds_by_year[y]
            return m.w_deg[y] <= N_U * m.b_bar[y] + B_L * m.N_ciclos[y] - N_U * B_L

        def _mccormick_ub2(m, y):
            N_L, N_U = bounds_by_year[y]
            return m.w_deg[y] <= N_L * m.b_bar[y] + B_U * m.N_ciclos[y] - N_L * B_U

        def _mccormick_energy(m, y):
            # Version lineal (McCormick) de n_ciclos_link.
            return m.n_elhd_bd[y] * m.w_deg[y] == m.S[y]

        model.mccormick_lb1    = pyo.Constraint(model.years, rule=_mccormick_lb1)
        model.mccormick_lb2    = pyo.Constraint(model.years, rule=_mccormick_lb2)
        model.mccormick_ub1    = pyo.Constraint(model.years, rule=_mccormick_ub1)
        model.mccormick_ub2    = pyo.Constraint(model.years, rule=_mccormick_ub2)
        model.mccormick_energy = pyo.Constraint(model.years, rule=_mccormick_energy)

    def d_y_fade(self, model, y):
        """(ec. 4) D[y] = B[y] - gamma_coef * N_ciclos[y]: capacidad al
        final del año, degradada por los ciclos equivalentes del propio año
        (sin arrastre acumulado explícito -- el arrastre entre años lo da
        D[y-1] vía b_y_link)."""
        return model.D[y] == model.b_bar[y] - model.gamma_coef * model.N_ciclos[y]

    def b_y_link(self, model, y):
        """(ec. 2) B[y] <= D[y-1] + 0.3*b_max*R[y], para todo año salvo el
        primero (B[y1]=b_max fijo, ec. 5). Si se reemplaza la batería
        (R[y]=1) se recupera hasta un 30% de capacidad nominal adicional
        sobre D[y-1]; si no, la capacidad de inicio de año queda acotada por
        la capacidad heredada del año anterior."""
        return model.b_bar[y] <= (
            model.D[self._prev_year(y)]
            + model.replace_capacity_fraction * model.b_max_fleet * model.R[y]
        )

    def build_all_constraints(self, model):
        model.state_unique_elhd         = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.state_unique_elhd)
        # Esquema DCH activo: usa el set de entre-turnos legacy
        # (time_intervals_between_shifts_set), no el especifico DET (ver
        # carga_on_board commit 9943fdc7a / build_all_constraints con
        # pause_scheme='dch').
        model.between_shifts_elhd       = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_between_shifts_set, rule=self.between_shifts_elhd)

        model.battery_soc               = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.battery_soc)
        model.battery_lower             = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.battery_lower)
        model.battery_upper             = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.battery_upper)
        model.battery_boundary          = pyo.Constraint(model.elhd_set, model.years, model.days, rule=self.battery_boundary)
        model.battery_soc_break_symmetry = pyo.Constraint(model.charge_precedence_pairs, model.years, model.days, rule=self.battery_soc_break_symmetry)

        model.max_n_chargers            = pyo.Constraint(model.stations_set, model.years, rule=self.max_n_chargers)
        model.station_existence_constraint = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.station_existence_constraint)
        model.charger_limit             = pyo.Constraint(model.stations_set, model.years, model.days, model.time_intervals_set, rule=self.charger_limit)
        if self.exogenous_stations is None:
            model.link_station_stock     = pyo.Constraint(model.stations_set, model.years, rule=self.link_station_stock)
        if not self.is_decomposed_block:
            # Descomposicion: la acumulacion entre años del stock de
            # cargadores la arma YearBlockBuilder via el parametro heredado
            # N_chargers_hat + la copia continua N_chargers_prev (ver
            # implementacion_descomposicion_carga_ob.md sec. 3) — no existe
            # model.N_chargers[k, y-1] dentro de un bloque de un solo año.
            model.link_charger_stock     = pyo.Constraint(model.stations_set, model.years, rule=self.link_charger_stock)
        model.charge_state              = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.charge_state)
        model.min_charge_duration       = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.min_charge_duration)
        # Desactivadas: min_assign_duration (min. 2 intervalos consecutivos por
        # asignacion Y, analogo a min_charge_duration) choca con el esquema DET
        # (15 detenciones/dia vs 6 en DCH, huecos libres mas fragmentados) --
        # confirmado via IIS de Gurobi que infactibiliza LH518B_7/año4/día196
        # (state_unique_elhd + det_stop_all + production). No existen activas
        # en carga_on_board (el baseline factible de referencia); quedan
        # definidas pero sin registrar, igual que alli.
        #model.assign_state              = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.assign_state)
        #model.min_assign_duration       = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.min_assign_duration)
        model.max_power                 = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.max_power)
        model.max_installed_capacity    = pyo.Constraint(model.stations_set, model.years, model.days, model.time_intervals_set, rule=self.max_installed_capacity)
        model.power_balance             = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.power_balance)
        model.grid_limit                = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.grid_limit)
        model.power_cost_peak_limit     = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.power_cost_peak_limit)

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

        if self.mine_system.battery_degradation is not None:
            model.s_def         = pyo.Constraint(model.years, rule=self.s_def)
            model.d_y_fade      = pyo.Constraint(model.years, rule=self.d_y_fade)
            if not self.is_decomposed_block:
                # Modo descompuesto: n_ciclos_link (bilineal, mismo año) se
                # reemplaza por la relajacion de McCormick en YearBlockBuilder
                # (ver degradacion_descomposicion_mccormick.md sec. 3.3), y
                # b_y_link (que referencia model.D[y-1], inexistente en un
                # bloque de un solo año) se reemplaza por la restriccion
                # local b_y_link_local sobre el parametro heredado D_hat
                # (ver year_block.py::_add_degradation_state).
                if self.mccormick_degradation:
                    # Camino A aplicado al monolitico completo (ver
                    # build_mccormick_degradation_block): reemplaza
                    # n_ciclos_link por su envolvente convexa, para TODOS
                    # los años. b_y_link no cambia -- sigue referenciando
                    # model.D[y-1], que SI existe en el monolitico (todos
                    # los años estan en model.years).
                    self.build_mccormick_degradation_block(model)
                else:
                    model.n_ciclos_link = pyo.Constraint(model.years, rule=self.n_ciclos_link)
                if hasattr(model, 'later_years_set'):
                    model.b_y_link = pyo.Constraint(model.later_years_set, rule=self.b_y_link)

        model.daily_production      = pyo.Constraint(model.years, model.days, rule=self.daily_production)
        model.production            = pyo.Constraint(model.years, model.days, model.nodes_set, rule=self.production)
        model.interval_extraction_M = pyo.Constraint(model.Y_INDEX, rule=lambda m, i, j, y, d, t: self.interval_extraction_M(m, i, j, y, d, t))

        # Esquema DCH (legacy), activo para el caso data/Escenarios_Gx (ver
        # meal_g1_no_travel_group1/meal_g2_no_travel_group2/maint_stop_all/
        # maint_no_charge/charge_only_meal_or_shift_change).
        model.meal_g1_no_travel_group1 = pyo.Constraint(model.lhd_set, model.years, model.days, model.time_intervals_set, rule=self.meal_g1_no_travel_group1)
        model.meal_g2_no_travel_group2 = pyo.Constraint(model.lhd_set, model.years, model.days, model.time_intervals_set, rule=self.meal_g2_no_travel_group2)
        model.maintenance_stop_all     = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.maint_stop_all)
        model.maint_no_charge          = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.maint_no_charge)
        model.charge_only_meal_or_shift_change = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.charge_only_meal_or_shift_change)

        # Esquema DET (redefinido, con base_hour y modo autonomo): reemplazado
        # por el esquema DCH de arriba para el caso data/Escenarios_Gx (ver
        # det_stop_all / charge_only_meal_or_between_shifts_det).
        #model.det_stop_all = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.det_stop_all)
        #model.charge_only_meal_or_between_shifts_det = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.charge_only_meal_or_between_shifts_det)

      
class ObjectiveRules(OptRules):

    def _discount_factor(self, model, y):
        """1/(1+r)^pos(y) si hay datos de degradación/descuento cargados, si no 1
        (preserva el comportamiento original para escenarios sin esa hoja)."""
        if self.mine_system.battery_degradation is None:
            return 1
        return 1 / (1 + model.discount_rate) ** self.year_position(y)

    def lhd_charge_cost(self, model):
        cost_el = sum(
            model.P_red[y, d, t] * model.costo_red[y, d, t] * self._discount_factor(model, y)
            for y in model.years
            for d in model.days
            for t in model.time_intervals_set
        ) * model.delta_t
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
        # Modo descompuesto: X es exogeno y no acarrea costo de inversion
        # propio (ΔX = 0, documento sec. 5 "Costo del año f_y") — el costo de
        # las estaciones ya fue asumido/consignado fuera de este bloque.
        station_cost_active = self.exogenous_stations is None

        def yearly(y):
            station_term = (
                sum(model.station_cost_k[k] * model.Delta_X[k, y] for k in model.stations_set)
                if station_cost_active else 0
            )
            return station_term + sum(
                (model.c_bays_k[k] + model.charger_cost + model.c_charger_space_k[k]) * model.Delta_N_chargers[k,y]
                for k in model.stations_set
            )
        return self._one_time_discounted_yearly_sum(model, yearly)

    def substation_investment_cost(self, model):
        """Costo de inversión en potencia de subestación: P_max_k[k] se
        decide una sola vez para todo el horizonte (mismo patron que G_g/H,
        ver BoundRules), asi que se paga una unica vez, descontado al primer
        año. Mismo guard `first_year in model.years` que gen_investment_cost/
        bess_investment_cost, y por la misma razon (evitar sobre-contar en
        modo descompuesto, donde ObjectiveRules.total_cost se evalua una vez
        por bloque-año)."""
        first_year = self._first_year()
        if first_year not in model.years:
            return 0
        return sum(model.c_inv_ssee_k[k] * model.P_max_k[k] for k in model.stations_set) \
            * self._discount_factor(model, first_year)

    def gen_investment_cost(self, model):
        """Costo de inversión en generación: G_g[g] se decide una sola vez
        para todo el horizonte (ver BoundRules), asi que se paga una unica
        vez, descontado al primer año.

        Guard `first_year in model.years`: en modo descompuesto, ObjectiveRules.
        total_cost se evalua UNA VEZ POR BLOQUE-AÑO (ver YearBlockBuilder._build),
        cada uno con su propia copia de G_g[g] (libre en el bloque origen, fijada
        por igualdad en los demas -- ver _add_global_once_state en year_block.py).
        Sin este guard, CADA bloque cobraria el costo de inversion completo (no
        solo el bloque origen), sobre-contando por un factor de n_years en el
        UB/LB de la descomposicion. En el monolitico (model.years = horizonte
        completo) el guard nunca se activa, sin cambio de comportamiento."""
        if len(list(model.gen_set)) == 0:
            return 0
        first_year = self._first_year()
        if first_year not in model.years:
            return 0
        return sum(model.G_g[g] * model.c_inv_g[g] * model.p_max_g[g] for g in model.gen_set) \
            * self._discount_factor(model, first_year)

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
        horizonte, se paga una unica vez, descontado al primer año. Mismo
        guard `first_year in model.years` que gen_investment_cost, y por
        la misma razon (evitar sobre-contar en modo descompuesto)."""
        if len(list(model.storage_set)) == 0:
            return 0
        first_year = self._first_year()
        if first_year not in model.years:
            return 0
        return sum(model.H * model.c_inv_h[h] for h in model.storage_set) \
            * self._discount_factor(model, first_year)

    def bess_op_cost(self, model):
        """Costo de O&M anual BESS: recurre cada año del horizonte sobre la
        capacidad fija H."""
        if len(list(model.storage_set)) == 0:
            return 0
        return sum(
            model.H * model.c_op_h[h] * self._discount_factor(model, y)
            for h in model.storage_set for y in model.years
        )

    def power_cost(self, model):
        return sum(model.P_pot[y] * 12 * 10 * self._discount_factor(model, y) for y in model.years)

    def battery_replace_cost(self, model):
        """Costo de reemplazo de batería: evento puntual del año y, descontado
        con _discount_factor (mismo patron que inversion_cost/gen_investment_
        cost/bess_investment_cost). c_bat_replace es el costo TOTAL de UNA sola
        batería, por lo que se escala por la
        cantidad de LHD eléctricos ACTIVOS ese año (n_elhd_bd[y], una batería
        por LHD) para obtener el costo de reemplazar la flota -- no el fleet
        nominal completo, para no cobrar el reemplazo de baterías de LHD
        que ese año no están operando (ver FleetByYear)."""
        if self.mine_system.battery_degradation is None:
            return 0
        return sum(
            model.n_elhd_bd[y] * model.R[y] * model.c_bat_replace * self._discount_factor(model, y)
            for y in model.years
        )

    def total_cost(self, model):
        return (self.lhd_charge_cost(model)
                + self.inversion_cost(model)
                + self.substation_investment_cost(model)
                + self.gen_investment_cost(model)
                + self.gen_op_cost(model)
                + self.bess_investment_cost(model)
                + self.bess_op_cost(model)
                + self.power_cost(model)
                + self.battery_replace_cost(model))
    
    def op_cost_total(self, model):
        # Coste operativo total (sin inversiÃ³n)
        return self.lhd_charge_cost(model)/model.scaling_factor_op_cost
    
    def max_mineral(self, model):
        """Maximiza L: la extracción mínima garantizada en todos los puntos-días."""
        return model.L
    
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






