import pyomo.environ as pyo
import math
import pandas as pd
import numpy as np
import time
import re
from pyomo.environ import quicksum, value

class OptRules(object):

    def __init__(self, mine_system,  time_series, autonomous_mode=False):
        self.mine_system = mine_system
        self.time_series = time_series
        # Escenario DET autonomo: durante la colacion el LHD puede ademas
        # operar (no solo cargar o estar detenido). Ver OptSets.build_sets.
        self.autonomous_mode = autonomous_mode
        self.time_series.get_node_assignment(mine_system.get_system_lhds())
        self.time_series.get_elhd_at_node(mine_system.get_system_nodes())
        self.time_series.get_station_assignment(mine_system.get_system_lhds())
        self.time_series.get_elhd_at_station(mine_system.get_system_stations())



    def year_position(self, y: int) -> int:
        """Posicion 1-indexada de y dentro del horizonte modelado (1, 2, 3, ...),
        para exponente de descuento — desacoplado del valor real de y."""
        return sorted(self.time_series.years).index(y) + 1

    def annuity_factor_expr(self, model, y):
        """AF_y(r,Y) = sum_{n=pos(y)}^{|Y|} 1/(1+r)^n. Escalar (expresion en
        model.discount_rate) para convertir un costo anualizado recurrente que
        empieza a devengarse en el año y (y se mantiene hasta el fin del
        horizonte, sin retiro) en su NPV. AF_{y1}(r,Y) reproduce la anualidad
        original de horizonte-unico (pos(y1)=1)."""
        Y = len(self.time_series.years)
        pos_y = self.year_position(y)
        r = model.discount_rate
        return sum(1 / (1 + r) ** n for n in range(pos_y, Y + 1))

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
        model.days = pyo.Set(initialize=self.time_series.days_within_year)
        model.years = pyo.Set(initialize=self.time_series.years)
        model.shifts = pyo.Set(initialize=self.time_series.shifts)
        model.time_intervals_set_zero = pyo.Set(initialize=[0] + list(self.time_series.time_intervals))
        model.time_intervals_between_shifts_set = pyo.Set(initialize=self.time_series.get_intervals_between_shifts())
        base_minutes = int(round(self.time_series.base_hour * 60))
        model.time_intervals_peak_set = pyo.Set(
            initialize=[
                t for t in self.time_series.time_intervals
                if 18 * 60 <= ((base_minutes + (t - 1) * int(round(self.time_series.delta_t * 60))) % 1440) < 22 * 60
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
        model.p_max_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_p_max_ssee(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
        model.p_peak = pyo.Param(initialize=self.mine_system.chargers.get_p_peak_dist(), mutable=False)
        model.charger_cost = pyo.Param(initialize=self.mine_system.chargers.get_charger_cost(), mutable=False)
        model.scaling_factor_op_cost = pyo.Param(initialize=self.time_series.scaling_factor_op_cost, mutable=True)
        #Parametros estaciones de carga
        model.station_cost_k = pyo.Param(model.stations_set, initialize={k: self.mine_system.stations.get_station_cost(k) for k in model.stations_set}, mutable=False, within=pyo.Reals)
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
        model.X                 = pyo.Var(model.stations_set, model.years, domain=pyo.Binary)
        model.Delta_X           = pyo.Var(model.stations_set, model.years, domain=pyo.Binary)
        model.N_chargers        = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        model.Delta_N_chargers  = pyo.Var(model.stations_set, model.years, domain=pyo.NonNegativeIntegers)
        model.StartCharge = pyo.Var(model.stations_set, model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
        model.EndCharge   = pyo.Var(model.stations_set, model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)

        model.StartAssign = pyo.Var(model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
        model.EndAssign   = pyo.Var(model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)

        if len(list(model.gen_set)) > 0:
            # G_g = stock acumulado (fraccion de p_max_g) al año y; Delta_G_g =
            # capacidad instalada en el año y (ver link_gen_stock).
            model.G_g       = pyo.Var(model.gen_set, model.years, domain=pyo.NonNegativeIntegers)
            model.Delta_G_g = pyo.Var(model.gen_set, model.years, domain=pyo.NonNegativeIntegers)
            model.P_gen  = pyo.Var(model.gen_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)
            model.Curt_g = pyo.Var(model.gen_set, model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)

        model.P_red = pyo.Var(model.years, model.days, model.time_intervals_set, domain=pyo.NonNegativeReals)

        if len(list(model.storage_set)) > 0:
            # Un unico cluster de almacenamiento (ver guardia de cardinalidad en
            # build_parameters): H = unidades acumuladas del cluster al año y,
            # Delta_H = unidades construidas en el año y (ver link_storage_stock).
            # Sin indice h a diferencia de la formulacion de horizonte unico
            # (H_h binaria) — el indice h se mantiene solo en las restricciones
            # operativas (P_bat, A_h) por si en el futuro se admite mas de un
            # cluster candidato.
            model.H       = pyo.Var(model.years, domain=pyo.NonNegativeIntegers)
            model.Delta_H = pyo.Var(model.years, domain=pyo.NonNegativeIntegers)
            model.P_bat = pyo.Var(model.storage_set, model.years, model.days, model.time_intervals_set, domain=pyo.Reals)
            model.A_h   = pyo.Var(model.storage_set, model.years, model.days, model.time_intervals_set_zero, domain=pyo.NonNegativeReals)

        # Degradación de batería on-board (fleet-wide, solo si hay datos cargados)
        # Modelo de fade lineal: b_bar[y] = b_max - gamma_coef * CumEFC[y], donde
        # CumEFC[y] son los ciclos equivalentes (EFC) acumulados desde el último
        # reemplazo. El binario x continuo (1-R[y])*CumEFC[y-1] se linealiza con
        # big-M estándar (exacto). N_ciclos[y] = S[y] / (n_elhd * b_bar[y]) es un
        # producto entera x continua (N_ciclos ahora depende de b_bar[y], no de un
        # b_max_fleet fijo) y se linealiza de forma EXACTA vía expansión binaria de
        # N_ciclos[y] + big-M sobre cada bit (ver n_ciclos_bits/z_.../nb_def más abajo).
        if self.mine_system.battery_degradation is not None:
            b_max_val = value(model.b_max_fleet)
            min_frac_val = value(model.min_capacity_fraction)
            n_elhd = len(model.elhd_set)

            # Energía máxima cargable en un año por toda la flota (todas las
            # combinaciones estación-LHD-día-intervalo a potencia nominal del
            # cargador), ya escalada a año completo.
            n_slots_per_year = len(model.ZCHARGE_INDEX) * len(model.days) * len(model.time_intervals_set)
            max_energy_per_year = (n_slots_per_year * value(model.p_charger) * value(model.delta_t)
                                    * value(model.scaling_factor_op_cost))

            # Cotas de B_y = b_bar[y]: las mismas ya usadas para acotar b_bar.
            B_L = min_frac_val * b_max_val
            B_U = b_max_val

            # --- Cota inferior de N_ciclos[y]: producción mínima obligatoria -------
            # Cada nodo j tiene un único LHD asociado (estructura de
            # Nodes_assigned_at_interval); production() ya acota sus visitas entre
            # [lb_j, ub_j] a partir del target m_j[j,y]. Reusamos esa misma banda
            # para acotar la energía que la flota eléctrica está obligada a consumir
            # cada año (lb_j => cota inferior de energía; ub_j => cota superior,
            # alternativa a la basada en potencia de cargador).
            node_lhd_pairs = {}
            for (i2, j2, y2, d2, t2) in model.Y_INDEX:
                node_lhd_pairs.setdefault((y2, d2, j2), []).append(i2)

            E_year_low_by_y = {}
            E_year_high_by_y = {}
            for y in model.years:
                e_day_low = 0.0
                e_day_high = 0.0
                for d in model.days:
                    for j in model.nodes_set:
                        i_list = node_lhd_pairs.get((y, d, j))
                        if not i_list:
                            continue
                        elec_i_list = [i for i in i_list if i in model.elhd_set]
                        if not elec_i_list:
                            continue  # nodo servido solo por diesel: no aporta a N_ciclos
                        i_j = elec_i_list[0]

                        prod_per_assign = (value(model.g_i[i_j]) * self.time_series.get_n_trips(j, i_j)
                                            * value(model.filling_factor[i_j]))
                        target = value(model.m_j[j, y])
                        lb_j = max(math.floor(target / prod_per_assign) - 1, 0)
                        ub_j = math.ceil(target / prod_per_assign) + 1

                        e_j = (value(model.pe_i[i_j, j]) * value(model.d_i[i_j, j])
                               * self.time_series.get_n_trips(j, i_j) / value(model.eta_discharge_i[i_j]))

                        e_day_low += lb_j * e_j
                        e_day_high += ub_j * e_j

                E_year_low_by_y[y] = e_day_low * value(model.scaling_factor_op_cost)
                E_year_high_by_y[y] = e_day_high * value(model.scaling_factor_op_cost)

            # Cota de N_ciclos[y]: como el denominador es variable (B_y, no
            # b_max_fleet fijo), la cota superior usa el peor caso B_y = B_L
            # (batería más degradada => misma energía produce más EFC) y la
            # inferior usa el mejor caso B_y = B_U. N_L/N_U son globales (un solo
            # K para todos los años), así que se toma el más ajustado válido para
            # TODOS los años: min de los N_L por año, max de los N_U por año.
            N_L = min(math.floor(E_year_low_by_y[y] / (n_elhd * B_U)) for y in model.years)
            N_L = max(N_L, 0)

            N_U_prod = max(math.ceil(E_year_high_by_y[y] / (n_elhd * B_L)) for y in model.years)
            N_U_charger = math.ceil(max_energy_per_year / (n_elhd * B_L))
            N_U = min(N_U_charger, N_U_prod)

            cum_efc_max = N_U * len(model.years)

            K = math.ceil(math.log2(N_U - N_L + 1))
            model.K_bits = pyo.RangeSet(0, K - 1)

            model.N_L = pyo.Param(initialize=N_L, mutable=False)
            model.N_U = pyo.Param(initialize=N_U, mutable=False)
            model.B_L = pyo.Param(initialize=B_L, mutable=False)
            model.B_U = pyo.Param(initialize=B_U, mutable=False)
            model.n_elhd_bd = pyo.Param(initialize=n_elhd, mutable=False)

            model.R        = pyo.Var(model.years, domain=pyo.Binary)
            model.b_bar    = pyo.Var(model.years, domain=pyo.NonNegativeReals,
                                      bounds=(B_L, B_U))
            model.N_ciclos = pyo.Var(model.years, domain=pyo.NonNegativeIntegers, bounds=(N_L, N_U))
            model.CumEFC   = pyo.Var(model.years, domain=pyo.NonNegativeReals, bounds=(0, cum_efc_max))
            model.cum_efc_max = pyo.Param(initialize=cum_efc_max, mutable=False)

            # Expansión binaria exacta de N_ciclos[y] = N_L + sum_k 2^k * b_bit[y,k],
            # y linealización exacta (big-M) del producto b_bit[y,k] * b_bar[y] = z_bit[y,k],
            # que permite reconstruir NB[y] = N_ciclos[y] * b_bar[y] sin aproximación.
            model.S      = pyo.Var(model.years, domain=pyo.NonNegativeReals)
            model.b_bit  = pyo.Var(model.years, model.K_bits, domain=pyo.Binary)
            model.z_bit  = pyo.Var(model.years, model.K_bits, domain=pyo.NonNegativeReals, bounds=(0, B_U))
            model.NB     = pyo.Var(model.years, domain=pyo.NonNegativeReals)

            years_sorted = sorted(model.years)
            later_years = years_sorted[1:]
            if later_years:
                model.later_years_set = pyo.Set(initialize=later_years, within=model.years)
                # W[y] = (1-R[y]) * CumEFC[y_prev]: si se reemplaza (R[y]=1) el
                # arrastre de ciclos previos se anula; si no, se conserva integro.
                model.W = pyo.Var(model.later_years_set, domain=pyo.NonNegativeReals, bounds=(0, cum_efc_max))

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

    def link_gen_stock(self, model, g, y):
        if y == self._first_year():
            return model.G_g[g,y] == model.Delta_G_g[g,y]
        return model.G_g[g,y] == model.G_g[g, self._prev_year(y)] + model.Delta_G_g[g,y]

    def link_storage_stock(self, model, y):
        if y == self._first_year():
            return model.H[y] == model.Delta_H[y]
        return model.H[y] == model.H[self._prev_year(y)] + model.Delta_H[y]

    def max_storage_units(self, model, y):
        return model.H[y] <= model.h_max

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
        return sum(model.P[k,i,y,d,t] for i in station_elhds) <= model.p_max_k[k]

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
                == model.G_g[g,y] * model.p_max_g[g] * model.alpha_g[g,y,d,t])

    def gen_max_units(self, model, g, y):
        return model.G_g[g,y] <= model.g_max_g[g]

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
        return model.P_bat[h,y,d,t] <= model.p_max_h[h] * model.H[y]

    def bess_power_lower(self, model, h, y, d, t):
        return model.P_bat[h,y,d,t] >= -model.p_max_h[h] * model.H[y]

    def bess_soc_balance(self, model, h, y, d, t):
        t0 = self.time_series.get_time_intervals()[0]
        prev = model.A_h[h,y,d,0] if t == t0 else model.A_h[h,y,d,t-1]
        return model.A_h[h,y,d,t] == prev - (model.P_bat[h,y,d,t] / model.eta_h[h]) * model.delta_t

    def bess_soc_init(self, model, h, y, d):
        return model.A_h[h,y,d,0] == 0

    def bess_soc_upper(self, model, h, y, d, t):
        return model.A_h[h,y,d,t] <= model.a_max_h[h] * model.H[y]

    def bess_soc_lower(self, model, h, y, d, t):
        return model.A_h[h,y,d,t] >= model.a_min_h[h] * model.H[y]

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

    # ------------------------------------------------------------------ #
    # Degradación de batería on-board (fleet-wide)
    # ------------------------------------------------------------------ #
    def _prev_year(self, y):
        years_sorted = sorted(self.time_series.years)
        return years_sorted[years_sorted.index(y) - 1]

    # -- N_ciclos[y] = S[y] / (n_elhd * b_bar[y]), linealizado exacto -------- #
    # La relación original es no lineal (N_ciclos entero x b_bar continua en el
    # denominador). Se reescribe como n_elhd * N_ciclos[y] * b_bar[y] == S[y] y
    # se linealiza en 3 pasos: (1) suma lineal S[y]; (2) expansión binaria exacta
    # de N_ciclos[y] (entera, acotada [N_L,N_U]); (3) big-M exacto (binario x
    # continua) para reconstruir el producto NB[y] = N_ciclos[y] * b_bar[y].
    def s_def(self, model, y):
        """S[y] = energía cargada por la flota en el año y (kWh), escalada a año completo."""
        energy_repr_day = sum(
            model.P[k, i, y2, d, t] * model.delta_t
            for (k, i, y2, d, t) in model.ZCHARGE_DAYS_TIME_INDEX if y2 == y
        )
        return model.S[y] == energy_repr_day * model.scaling_factor_op_cost

    def n_ciclos_bits(self, model, y):
        """Expansión binaria exacta: N_ciclos[y] = N_L + sum_k 2^k * b_bit[y,k]."""
        return model.N_ciclos[y] == model.N_L + sum(
            (2 ** k) * model.b_bit[y, k] for k in model.K_bits
        )

    def z_upper1(self, model, y, k):
        return model.z_bit[y, k] <= model.B_U * model.b_bit[y, k]

    def z_lower1(self, model, y, k):
        return model.z_bit[y, k] >= model.B_L * model.b_bit[y, k]

    def z_upper2(self, model, y, k):
        return model.z_bit[y, k] <= model.b_bar[y] - model.B_L * (1 - model.b_bit[y, k])

    def z_lower2(self, model, y, k):
        return model.z_bit[y, k] >= model.b_bar[y] - model.B_U * (1 - model.b_bit[y, k])

    def nb_def(self, model, y):
        """NB[y] = N_ciclos[y] * b_bar[y], reconstruido exacto a partir de los z_bit."""
        return model.NB[y] == model.N_L * model.b_bar[y] + sum(
            (2 ** k) * model.z_bit[y, k] for k in model.K_bits
        )

    def n_ciclos_energy_link(self, model, y):
        """n_elhd * N_ciclos[y] * b_bar[y] == S[y], vía el producto exacto NB[y]."""
        return model.n_elhd_bd * model.NB[y] == model.S[y]

    def w_upper_R(self, model, y):
        return model.W[y] <= model.cum_efc_max * (1 - model.R[y])

    def w_upper_cum(self, model, y):
        return model.W[y] <= model.CumEFC[self._prev_year(y)]

    def w_lower(self, model, y):
        return model.W[y] >= model.CumEFC[self._prev_year(y)] - model.cum_efc_max * model.R[y]

    def cum_efc_def(self, model, y):
        """CumEFC[y] = ciclos equivalentes acumulados desde el último reemplazo:
        (1-R[y])*CumEFC[y_prev] + N_ciclos[y]. En el primer año no hay arrastre."""
        first_year = sorted(self.time_series.years)[0]
        if y == first_year:
            return model.CumEFC[y] == model.N_ciclos[y]
        return model.CumEFC[y] == model.W[y] + model.N_ciclos[y]

    def b_bar_fade(self, model, y):
        """Fade lineal: b_bar[y] = b_max - gamma_coef * CumEFC[y]."""
        return model.b_bar[y] == value(model.b_max_fleet) - model.gamma_coef * model.CumEFC[y]

    def build_all_constraints(self, model):
        model.state_unique_elhd         = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.state_unique_elhd)
        model.between_shifts_elhd       = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_between_shifts_set, rule=self.between_shifts_elhd)

        model.battery_soc               = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.battery_soc)
        model.battery_lower             = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.battery_lower)
        model.battery_upper             = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.battery_upper)
        model.battery_boundary          = pyo.Constraint(model.elhd_set, model.years, model.days, rule=self.battery_boundary)
        model.battery_soc_break_symmetry = pyo.Constraint(model.charge_precedence_pairs, model.years, model.days, rule=self.battery_soc_break_symmetry)

        model.max_n_chargers            = pyo.Constraint(model.stations_set, model.years, rule=self.max_n_chargers)
        model.station_existence_constraint = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.station_existence_constraint)
        model.charger_limit             = pyo.Constraint(model.stations_set, model.years, model.days, model.time_intervals_set, rule=self.charger_limit)
        model.link_station_stock        = pyo.Constraint(model.stations_set, model.years, rule=self.link_station_stock)
        model.link_charger_stock        = pyo.Constraint(model.stations_set, model.years, rule=self.link_charger_stock)
        model.charge_state              = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.charge_state)
        model.min_charge_duration       = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.min_charge_duration)
        model.assign_state              = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.assign_state)
        model.min_assign_duration       = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.min_assign_duration)
        model.max_power                 = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.max_power)
        model.max_installed_capacity    = pyo.Constraint(model.stations_set, model.years, model.days, model.time_intervals_set, rule=self.max_installed_capacity)
        model.power_balance             = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.power_balance)
        model.grid_limit                = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.grid_limit)
        model.power_cost_peak_limit     = pyo.Constraint(model.years, model.days, model.time_intervals_set, rule=self.power_cost_peak_limit)

        if len(list(model.gen_set)) > 0:
            model.gen_limit     = pyo.Constraint(model.gen_set, model.years, model.days, model.time_intervals_set, rule=self.gen_limit)
            model.gen_max_units = pyo.Constraint(model.gen_set, model.years, rule=self.gen_max_units)
            model.link_gen_stock = pyo.Constraint(model.gen_set, model.years, rule=self.link_gen_stock)

        if len(list(model.storage_set)) > 0:
            model.link_storage_stock = pyo.Constraint(model.years, rule=self.link_storage_stock)
            model.max_storage_units  = pyo.Constraint(model.years, rule=self.max_storage_units)
            model.bess_power_upper = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_power_upper)
            model.bess_power_lower = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_power_lower)
            model.bess_soc_balance = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_soc_balance)
            model.bess_soc_init    = pyo.Constraint(model.storage_set, model.years, model.days, rule=self.bess_soc_init)
            model.bess_soc_upper   = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_soc_upper)
            model.bess_soc_lower   = pyo.Constraint(model.storage_set, model.years, model.days, model.time_intervals_set, rule=self.bess_soc_lower)
            model.bess_soc_cyclic  = pyo.Constraint(model.storage_set, model.years, model.days, rule=self.bess_soc_cyclic)

        if self.mine_system.battery_degradation is not None:
            model.s_def               = pyo.Constraint(model.years, rule=self.s_def)
            model.n_ciclos_bits       = pyo.Constraint(model.years, rule=self.n_ciclos_bits)
            model.z_upper1            = pyo.Constraint(model.years, model.K_bits, rule=self.z_upper1)
            model.z_lower1            = pyo.Constraint(model.years, model.K_bits, rule=self.z_lower1)
            model.z_upper2            = pyo.Constraint(model.years, model.K_bits, rule=self.z_upper2)
            model.z_lower2            = pyo.Constraint(model.years, model.K_bits, rule=self.z_lower2)
            model.nb_def              = pyo.Constraint(model.years, rule=self.nb_def)
            model.n_ciclos_energy_link = pyo.Constraint(model.years, rule=self.n_ciclos_energy_link)
            model.cum_efc_def  = pyo.Constraint(model.years, rule=self.cum_efc_def)
            model.b_bar_fade   = pyo.Constraint(model.years, rule=self.b_bar_fade)
            if hasattr(model, 'later_years_set'):
                model.w_upper_R   = pyo.Constraint(model.later_years_set, rule=self.w_upper_R)
                model.w_upper_cum = pyo.Constraint(model.later_years_set, rule=self.w_upper_cum)
                model.w_lower     = pyo.Constraint(model.later_years_set, rule=self.w_lower)

        model.daily_production      = pyo.Constraint(model.years, model.days, rule=self.daily_production)
        model.production            = pyo.Constraint(model.years, model.days, model.nodes_set, rule=self.production)
        model.interval_extraction_M = pyo.Constraint(model.Y_INDEX, rule=lambda m, i, j, y, d, t: self.interval_extraction_M(m, i, j, y, d, t))

        model.meal_g1_no_travel_group1 = pyo.Constraint(model.lhd_set, model.years, model.days, model.time_intervals_set, rule=self.meal_g1_no_travel_group1)
        model.meal_g2_no_travel_group2 = pyo.Constraint(model.lhd_set, model.years, model.days, model.time_intervals_set, rule=self.meal_g2_no_travel_group2)
        model.maintenance_stop_all     = pyo.Constraint(model.elhd_set, model.years, model.days, model.time_intervals_set, rule=self.maint_stop_all)
        model.maint_no_charge          = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.maint_no_charge)
        model.charge_only_meal_or_shift_change = pyo.Constraint(model.ZCHARGE_DAYS_TIME_INDEX, rule=self.charge_only_meal_or_shift_change)

        # Esquema DET (redefinido, con base_hour y modo autonomo): definido pero
        # NO activo todavia en esta rama -- el esquema DCH de arriba sigue siendo
        # el que corre. Para activarlo, descomentar estas dos lineas y comentar
        # el bloque DCH (meal_g1/g2, maintenance_stop_all, maint_no_charge,
        # charge_only_meal_or_shift_change), igual que en carga_on_board.
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

    def _annuitized_yearly_sum(self, model, yearly_cost_fn):
        """sum_y AF_y(r,Y) * yearly_cost_fn(y) si hay datos de degradacion/
        descuento cargados; si no, suma cruda sin anualizar ni descontar
        (replica el comportamiento de un unico año representativo sin
        descuento). yearly_cost_fn(y) debe usar las variables de INCREMENTO
        (Delta_*) del año y, no el stock acumulado, para no contar de nuevo
        cada año el costo de un activo ya construido — ver
        docs/restricciones_modelo.tex ec. costo_total."""
        if self.mine_system.battery_degradation is not None:
            return sum(self.annuity_factor_expr(model, y) * yearly_cost_fn(y) for y in model.years)
        return sum(yearly_cost_fn(y) for y in model.years)

    def inversion_cost(self, model):
        def yearly(y):
            return sum(
                model.station_cost_k[k] * model.Delta_X[k,y]
                + (model.c_bays_k[k] + model.charger_cost + model.c_charger_space_k[k]) * model.Delta_N_chargers[k,y]
                for k in model.stations_set
            )
        return self._annuitized_yearly_sum(model, yearly)

    def gen_investment_cost(self, model):
        if len(list(model.gen_set)) == 0:
            return 0
        def yearly(y):
            return sum(model.Delta_G_g[g,y] * model.c_inv_g[g] * model.p_max_g[g] for g in model.gen_set)
        return self._annuitized_yearly_sum(model, yearly)

    def gen_op_cost(self, model):
        if len(list(model.gen_set)) == 0:
            return 0
        def yearly(y):
            return sum(model.Delta_G_g[g,y] * model.c_op_g[g] * model.p_max_g[g] for g in model.gen_set)
        return self._annuitized_yearly_sum(model, yearly)

    def bess_investment_cost(self, model):
        """Costo de inversión BESS: sum_h c_inv_h * Delta_H[y] — ec. costo_total."""
        if len(list(model.storage_set)) == 0:
            return 0
        def yearly(y):
            return sum(model.Delta_H[y] * model.c_inv_h[h] for h in model.storage_set)
        return self._annuitized_yearly_sum(model, yearly)

    def bess_op_cost(self, model):
        """Costo de O&M anual BESS: sum_h c_op_h * Delta_H[y] — ec. costo_total."""
        if len(list(model.storage_set)) == 0:
            return 0
        def yearly(y):
            return sum(model.Delta_H[y] * model.c_op_h[h] for h in model.storage_set)
        return self._annuitized_yearly_sum(model, yearly)

    def power_cost(self, model):
        return sum(model.P_pot[y] * 12 * 10 * self._discount_factor(model, y) for y in model.years)

    def battery_replace_cost(self, model):
        """Costo de reemplazo de batería: evento puntual del año y (no una
        anualidad recurrente, por eso no usa annuity_factor_expr). c_bat_replace
        es el costo TOTAL de UNA sola batería, por lo que se escala por la
        cantidad de LHD eléctricos (una batería por LHD) para obtener el costo
        de reemplazar la flota completa."""
        if self.mine_system.battery_degradation is None:
            return 0
        n_elhd = len(model.elhd_set)
        return sum(
            n_elhd * model.R[y] * model.c_bat_replace * self._discount_factor(model, y)
            for y in model.years
        )

    def total_cost(self, model):
        return (self.lhd_charge_cost(model)
                + self.inversion_cost(model)
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






