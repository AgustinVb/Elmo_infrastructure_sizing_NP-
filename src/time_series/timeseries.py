
import math
import json
import os
import pandas as pd
from pandas import DataFrame, concat
import numpy as np
import itertools


class Timeseries(object):

    def __init__(self, series, days, delta_t):
        self.series = series
        self.days = sorted(days)
        self.delta_t = delta_t
        self.time, self.mapper = dict(hourly=1, daily=24, weekly=168), {}
        self.day_to_year = {int(day): self.get_year_of_day(day) for day in self.days}
        self.years = sorted(set(self.day_to_year.values()))
        self.days_within_year = sorted(set(((int(d) - 1) % 365) + 1 for d in self.days))
        self.scaling_factor_op_cost = 365 / len(self.days_within_year)
        self.day_weights = self._build_day_weights()
        self.time_intervals = self.build_mappers()
        self.shifts = self.get_shifts()
        self._mc_scaled_cache = {}
        self._mc_alpha = self._load_marginal_cost_alpha()

    def build_mappers(self):
        #self.mapper['Emissions']       = self.sample_emissions(self.series['Emissions'])
        self.mapper['MarginalCost']    = self.sample_marginal_cost(self.series['MarginalCost'])
        self.mapper['ExtractionGoal']  = self.sample_extraction_goal(self.series['ExtractionGoal'])
        self.mapper['Shifts']          = self.sample_shifts(self.series['Shifts'])
        self.base_hour                 = self.get_base_hour()
        self.mapper['Time_Intervals']  = self.get_time_intervals()
        self.mapper['NodeAssignment']  = self.sample_node_assignment(self.series['NodeAssignment'])
        self.mapper['StationAssignment']  = self.sample_station_assignment(self.series['StationAssignment'])
        if 'GenProfiles' in self.series.container:
            self.mapper['GenProfiles'] = self.sample_gen_profiles(self.series['GenProfiles'])
        #self.mapper['BatteryAssignment'] = self.sample_battery_assignment(self.series['BatteryAssignment'])
        #self.mapper['Misc'] = self.sample_misc(self.series['Misc'])
        return self.mapper['Time_Intervals']
    
    def _hours_of_day(self, day: int):
        """Horas del año 1 (1..8760) que corresponden al día 'day'. Cicla para años 2+."""
        day_in_year1 = ((int(day) - 1) % 365) + 1
        start = (day_in_year1 - 1) * 24 + 1
        return list(range(start, start + 24))

    def _load_marginal_cost_alpha(self) -> float:
        marginal_cost = self.mapper.get('MarginalCost')
        if marginal_cost is None or marginal_cost.empty:
            return 1.0

        profile_row = None
        if 'name' in marginal_cost.columns:
            profile_matches = marginal_cost[marginal_cost['name'] == 'Profile_fixed']
            if not profile_matches.empty:
                profile_row = profile_matches.iloc[0]
        elif 'Profile_fixed' in marginal_cost.index:
            profile_row = marginal_cost.loc['Profile_fixed']

        if profile_row is None:
            return 1.0

        series = pd.Series(profile_row).drop(labels=['id', 'name', 'time_resolution'], errors='ignore')
        numeric_values = pd.to_numeric(series, errors='coerce').dropna()
        if numeric_values.empty:
            return 1.0

        first_value = float(numeric_values.iloc[0])
        return first_value if first_value > 0 else 1.0

    def get_year_of_day(self, day: int) -> int:
        """
        Mapea un dÃ­a absoluto del horizonte a su aÃ±o correspondiente.
        ConvenciÃ³n:
        - 1..365 -> aÃ±o 1
        - 366..730 -> aÃ±o 2
        - etc.
        """
        day_int = int(day)
        if day_int <= 0:
            raise ValueError(f"day must be positive, got {day}")
        return ((day_int - 1) // 365) + 1


    def _build_day_weights(self) -> dict:
        """
        Asigna a cada dÃ­a modelado un peso igual a la cantidad de dÃ­as reales
        que representa dentro de su aÃ±o.

        Si hay un solo dÃ­a muestreado en un aÃ±o, pesa 365.
        Si hay n dÃ­as muestreados en un mismo aÃ±o, cada uno pesa 365/n.
        """
        days_by_year = {}
        for day in self.days:
            year = self.day_to_year[int(day)]
            days_by_year.setdefault(year, []).append(int(day))

        weights = {}
        for year, days_in_year in days_by_year.items():
            weight = 365.0 / len(days_in_year)
            for day in days_in_year:
                weights[day] = weight

        return weights

    def _scaled_day_marginal_cost(self, location_name: str, day: int, alpha: float | None = None):
        """
        Devuelve una Serie (index: horas del aÃ±o) del dÃ­a 'day' para 'location_name'
        con 0 -> 0.001 y escalada por alpha.
        Usa cache para no recalcular.
        """
        alpha_value = self._mc_alpha if alpha is None else float(alpha)
        key = (location_name, int(day), alpha_value)
        if key in self._mc_scaled_cache:
            return self._mc_scaled_cache[key]

        hours = self._hours_of_day(day)
        # Tomamos la fila por ubicaciÃ³n y las 24 columnas del dÃ­a
        row = self.mapper['MarginalCost'].loc[location_name, hours].astype(float)

        # Paso 2: 0 -> 0.001
        row = row.replace(0.0, 0.001)

        # Paso 3: escalar por alpha
        current_mean = float(row.mean())
        # Por seguridad: si fuese ~0 (no deberÃ­a ocurrir tras el reemplazo), no escalar
        scale_factor = alpha_value / current_mean if current_mean > 0 else 1.0
        scaled = row * scale_factor

        self._mc_scaled_cache[key] = scaled
        return scaled

    def get_marginal_cost_scaled(self, location_name: str, day: int, time_interval: int, alpha: float | None = None) -> float:
        """
        Costo marginal ajustado para (location, day, interval):
        (1) extrae el dÃ­a, (2) 0->0.001, (3) escala por alpha.
        Respeta delta_t (mapea intervalos sub-horarios al Ã­ndice horario por 'ceil').
        """
        import math
        day_in_year1 = ((int(day) - 1) % 365) + 1
        hour_of_year = math.ceil((day_in_year1 - 1) * 24 + time_interval * self.delta_t)
        scaled_day = self._scaled_day_marginal_cost(location_name, day, alpha=alpha)

        if hour_of_year in scaled_day.index:
            return float(scaled_day.loc[hour_of_year])
        else:
            return float(self.mapper['MarginalCost'].loc[location_name, hour_of_year])

    def sample_misc(self, misc_df: pd.DataFrame) -> dict:
            """
            Espera un DataFrame con columnas ['id','name','value'].
            Convierte cada 'value' a su tipo apropiado:
            - delta_t: float (coma decimal OK)
            - days:   int
            - chargers: int
            - Pmine: float
            """
            d = misc_df.set_index('name')['value'].to_dict()
            out = {}
            for k, v in d.items():
                s = str(v).strip()
                if k == 'delta_t':
                    out[k] = float(s.replace(',', '.'))
                elif k in ('days', 'chargers'):
                    out[k] = int(s)
                elif k == 'Pmine':
                    out[k] = float(s.replace(',', '.'))
                else:
                    out[k] = v
            return out
    
    def sample_station_assignment(self, station_assignment: pd.DataFrame) -> pd.DataFrame:
        """
        Toma la hoja 'StationAssignment' con las columnas
          ['id', 'elh_name', 'station_name'].

        """
        sa = station_assignment[['elh_name', 'station_name']].copy()
        sa.columns = ['elhd', 'station']
        return sa

    def sample_node_assignment(self, node_assignment: pd.DataFrame) -> pd.DataFrame:
        """
        Hoja 'NodeAssignment': columnas ['id', 'elhd_name', 'ex_node_name', 'year 1', 'year 2', ...]
        donde cada columna de año vale 1 si el LHD puede visitar ese nodo ese año, 0 si no.
        Funciona con nombres 'year 1'/'year 2' (Excel directo) o 1/2 (carga desde .npy).
        Devuelve DataFrame con ['elhd_name', 'node_name', 'year'] solo para combinaciones activas.
        """
        meta = {'id', 'elhd_name', 'ex_node_name'}
        year_cols = [c for c in node_assignment.columns if str(c) not in meta]

        def _parse_year(col) -> int:
            # 'year 1' -> 1,  '1' -> 1,  1 -> 1
            return int(str(col).strip().split()[-1])

        melted = node_assignment[['elhd_name', 'ex_node_name'] + year_cols].melt(
            id_vars=['elhd_name', 'ex_node_name'],
            value_vars=year_cols,
            var_name='year_col',
            value_name='active'
        )
        na = melted[melted['active'] == 1][['elhd_name', 'ex_node_name', 'year_col']].copy()
        na = na.rename(columns={'ex_node_name': 'node_name'})
        na['year'] = na['year_col'].apply(_parse_year)
        return na[['elhd_name', 'node_name', 'year']].reset_index(drop=True)

    def sample_battery_assignment(self, battery_assignment: pd.DataFrame) -> pd.DataFrame:
        """
        Toma la hoja 'BatteryAssignment' con columnas
          ['id','shift_name_start','shift_name_end','elhd_name','battery_name'].
        Agrega:
          - start_day, end_day
          - start_interval, end_interval
        Manteniendo el mismo estilo que sample_node_assignment.
        """
        ba = battery_assignment[['shift_name_start', 'shift_name_end', 'elhd_name', 'battery_name']].copy()
        ba['start_day'] = [self.get_shift_day_start(s) for s in ba['shift_name_start']]
        ba['end_day'] = [self.get_shift_day_end(s) for s in ba['shift_name_end']]
        ba['start_interval'] = [self.get_shift_start_interval(s) for s in ba['shift_name_start']]
        ba['end_interval'] = [self.get_shift_end_interval(s) for s in ba['shift_name_end']]
        return ba

    def sample_marginal_cost(self, marginal_cost: pd.DataFrame) -> pd.DataFrame:
        marginal_cost = marginal_cost.set_index('name')
        hours_selected = np.arange(1, 8761)  # siempre año 1; _hours_of_day cicla para años 2+
        return marginal_cost[hours_selected]

    def sample_emissions(self, emissions: pd.DataFrame) -> pd.DataFrame:
        emissions = emissions.set_index('name')
        hours_selected = np.arange((self.days[0] - 1) * 24 + 1,
                                   (self.days[len(self.days) - 1]) * 24 + 1)
        return emissions[hours_selected]

    def time_intervals_in_day(self, day: int) -> np.ndarray:
        return self.mapper['Time_Intervals'].loc[day, :]['interval'].values

    def sample_extraction_goal(self, extraction_goal: pd.DataFrame) -> pd.DataFrame:
        """
        Hoja 'ExtractionGoal': una columna por AÑO (encabezado = número de año,
        ej. 1, 2, 3...). El valor es la meta de extracción DIARIA de ese año,
        aplicada a cualquier día simulado que pertenezca a ese año sin importar
        cuántos días (estaciones) se muestreen dentro de él.
        """
        extraction_goal = extraction_goal.set_index('name')
        year_columns = {}
        for col in extraction_goal.columns:
            try:
                year_columns[col] = int(col)
            except (TypeError, ValueError):
                continue

        extraction_goal = extraction_goal[list(year_columns.keys())].copy()
        extraction_goal.columns = [year_columns[col] for col in extraction_goal.columns]
        missing_years = [y for y in self.years if y not in extraction_goal.columns]
        if missing_years:
            raise KeyError(
                "Missing year columns in ExtractionGoal for years in horizon: "
                f"{missing_years}"
            )
        return extraction_goal[self.years]

    def get_marginal_cost(self, location_name: str, day: int, time_interval: int) -> float:
        hour_of_year = math.ceil((day - 1) * 24 + time_interval * self.delta_t)
        return self.mapper['MarginalCost'].loc[location_name, hour_of_year]

    def get_emissions(self, tipo: str, day: int, time_interval: int) -> float:
        hour_of_year = math.ceil((day - 1) * 24 + time_interval * self.delta_t)
        return self.mapper['Emissions'].loc[tipo, hour_of_year]

    def get_extraction_goal(self, node_name: str, year: int) -> float:
        return self.mapper['ExtractionGoal'].loc[node_name, int(year)]

    def get_shift_start_interval(self, shift_name: str) -> float:
        h_start = self.mapper['Shifts'].loc[shift_name, :]['hour_start'].iloc[0]
        return h_start / self.delta_t + 1

    def get_shift_end_interval(self, shift_name: str) -> float:
        h_start = self.mapper['Shifts'].loc[shift_name, :]['hour_start'].iloc[0]
        duration = self.get_shift_duration(shift_name)
        return (h_start + duration) / self.delta_t + 1

    def get_shift_duration(self, shift_name: str) -> float:
        return self.mapper['Shifts'].loc[shift_name, :]['duration'].iloc[0]

    def sample_shifts(self, shifts: pd.DataFrame) -> pd.DataFrame:
        columns = ['id', 'name', 'day_start', 'day_end', 'hour_start', 'duration']
        if 'base_hour' in shifts.columns:
            columns.append('base_hour')
        shifts = shifts[columns].copy()
        shifts = shifts.set_index(['name', 'id'])
        return shifts

    def get_base_hour(self) -> float:
        """Hora real (decimal) en que arranca el horizonte (t=1), leida de la
        columna 'base_hour' de la hoja Shifts (basta con que un solo valor
        este lleno en toda la columna; el resto puede quedar vacio).
        Si la hoja no trae esa columna, o esta vacia (escenarios viejos),
        usa 8.5 por compatibilidad con el default DET actual."""
        shifts = self.mapper['Shifts']
        if 'base_hour' in shifts.columns:
            values = pd.to_numeric(shifts['base_hour'], errors='coerce').dropna()
            if not values.empty:
                return float(values.iloc[0])
        return 8.5

    def get_next_shift(self, shift_name: str):
        shift_id = self.mapper['Shifts'].loc[shift_name].index.item() - 1
        if shift_id < self.get_number_of_shifts():
            return self.shifts[shift_id + 1]
        else:
            return -1

    def get_intervals_between_shifts(self) -> np.ndarray:
        t = []
        for i in self.shifts:
            t.extend(list(np.arange(self.get_shift_start_interval(i),
                                    self.get_shift_end_interval(i))))
        intervals = np.array(self.time_intervals)
        shift_intervals = np.array(t)
        return self.time_intervals[~np.isin(intervals, shift_intervals)]

    def get_shift_at_interval(self, interval: int):
        hour = (interval - 1) * self.delta_t
        mask1 = self.mapper['Shifts']['hour_start'] <= hour
        mask2 = (self.mapper['Shifts']['hour_start'] + self.mapper['Shifts']['duration'] >= hour)
        filtered = self.mapper['Shifts'][mask1 & mask2]
        if len(filtered) >= 1:
            return filtered.index.get_level_values('name')[0]
        return -1

    def get_shifts(self) -> list:
        """Turnos activos para los días simulados: un turno con [day_start,day_end]
        se incluye si su rango de validez cubre alguno de los días simulados
        (no si day_start coincide exactamente con un día simulado — un turno con
        day_start=1, day_end=365 es válido todo el año, sin importar qué día
        represente cada muestra)."""
        shifts = self.series['Shifts']
        days_year1 = set(((d - 1) % 365) + 1 for d in self.days)
        mask = shifts.apply(
            lambda row: any(row['day_start'] <= d <= row['day_end'] for d in days_year1),
            axis=1,
        )
        shifts = shifts[mask]
        shifts = shifts.set_index(['name', 'id'])
        return list(shifts.index.get_level_values('name'))

    def get_number_of_shifts(self) -> int:
        return len(self.shifts)

    def get_shift_day_start(self, shift_name: str) -> int:
        return self.mapper['Shifts'].loc[shift_name, :]['day_start'].iloc[0]

    def get_shift_day_end(self, shift_name: str) -> int:
        return self.mapper['Shifts'].loc[shift_name, :]['day_end'].iloc[0]

    def get_time_intervals(self) -> np.ndarray:
        intervals = np.arange(1, 24 / self.delta_t + 1)
        return intervals

    def get_last_interval(self) -> int:
        return self.time_intervals[len(self.time_intervals) - 1]
    
    def get_station_assignment_to_elhd(self, elhd_name: str) -> list:
        sa = self.mapper['StationAssignment']
        sa_elhd = sa[sa["elhd"] == elhd_name]
        return list(sa_elhd['station'])

    def get_station_assignment(self, elhds: list) -> int:
        """Puebla `self.mapper['Stations_per_elhd']` con asignaciÃ³n estÃ¡tica.
        Para cada elhd almacena la lista de estaciones asociadas.
        """
        station_per_elhd = {}
        for elhd in elhds:
            station_per_elhd[elhd] = self.get_station_assignment_to_elhd(elhd)
        self.mapper['Stations_per_elhd'] = station_per_elhd
        return 0
       
    def get_elhd_at_station(self, stations: list) -> int:
        """Invierte `Stations_per_elhd` â†' `elhd_per_station[(station)] = [elhds]`"""
        elhd_per_station = {}
        for st in stations:
            elhd_per_station[st] = []
        for elhd, station_list in self.mapper.get('Stations_per_elhd', {}).items():
            for st in station_list:
                if st not in elhd_per_station:
                    elhd_per_station[st] = []
                elhd_per_station[st].append(elhd)
        self.mapper['elhd_per_station'] = elhd_per_station
        return 0

    def _nodes_for_elhd_year(self, elhd_name: str, year: int) -> list:
        na = self.mapper['NodeAssignment']
        return list(na.loc[(na['elhd_name'] == elhd_name) & (na['year'] == year), 'node_name'])

    def get_nodes_assigned_to_elhd(self, year: int, day: int, interval: int, elhd_name: str) -> list:
        return self.mapper['Nodes_assigned_at_interval'].get((year, day, interval, elhd_name), [])

    def get_node_assignment(self, elhds: list) -> int:
        node_assign_dict = {}
        for year in self.years:
            for day in self.days_within_year:
                for interval in self.time_intervals:
                    for elhd in elhds:
                        node_assign_dict[(year, day, interval, elhd)] = self._nodes_for_elhd_year(elhd, year)
        self.mapper['Nodes_assigned_at_interval'] = node_assign_dict
        return 0

    def get_elhd_at_node(self, nodes: list) -> int:
        elhd_dict = {}
        for y in self.years:
            for d in self.days_within_year:
                for t in self.time_intervals:
                    for node in nodes:
                        elhd_dict[(node, y, d, t)] = []
        for (y, d, t, elhd) in self.mapper['Nodes_assigned_at_interval']:
            for node in self.mapper['Nodes_assigned_at_interval'][(y, d, t, elhd)]:
                elhd_dict[(node, y, d, t)].append(elhd)
        self.mapper['elhd_at_node'] = elhd_dict
        return 0

    def get_batteries_assigned_to_elhd(self, day: int, interval: int, elhd_name: str) -> list:
        ba = self.mapper['BatteryAssignment']
        ba_elhd = ba[ba["elhd_name"] == elhd_name]
        assigned = []
        last_int = int(self.get_last_interval())
        for _, row in ba_elhd.iterrows():
            sd, ed = row.start_day, row.end_day
            # Â¿Este BA cubre el dÃ­a?
            if not (sd <= day <= ed):
                continue
            # si es dÃ­a de inicio, arrancamos en start_interval; si no, en la 1
            si = row.start_interval if day == sd else 1
            # si es dÃ­a de fin, paramos en end_interval; si no, en el Ãºltimo intervalo
            ei = row.end_interval if day == ed else last_int
            if si <= interval <= ei:
                assigned.append(row.battery_name)
        return assigned

    def get_battery_assignment(self, elhds: list) -> int:
        intervals = self.time_intervals
        batt_assign_dict = {}
        for day in self.days:
            for interval in intervals:
                for elhd in elhds:
                    batt_list = self.get_batteries_assigned_to_elhd(day, interval, elhd)
                    batt_assign_dict[(day, interval, elhd)] = batt_list
        self.mapper['Batteries_assigned_at_interval'] = batt_assign_dict
        return 0

    def get_elhd_with_battery(self, batteries: list) -> int:
        elhd_with_batt = {}
        for d in self.days:
            for t in self.time_intervals:
                for batt in batteries:
                    elhd_with_batt[(batt, d, t)] = []
        for (d, t, elhd) in self.mapper['Batteries_assigned_at_interval']:
            for batt in self.mapper['Batteries_assigned_at_interval'][(d, t, elhd)]:
                elhd_with_batt[(batt, d, t)].append(elhd)
        self.mapper['elhd_with_battery'] = elhd_with_batt
        return 0

    def _load_wp2_consumption_by_node(self, json_path, required_nodes):
        if not json_path:
            raise ValueError("Debe especificar la ruta del JSON de consumos WP2 (wp2_consumption_json).")
        if not os.path.isfile(json_path):
            raise FileNotFoundError(f"No se encontro el archivo JSON de consumos WP2: {json_path}")

        with open(json_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)

        if not isinstance(raw, dict):
            raise ValueError(
                f"El JSON de consumos WP2 ({json_path}) debe ser un objeto (dict) a nivel raiz."
            )

        missing_nodes = []
        invalid_entries = []
        consumption_by_node = {}

        for node in required_nodes:
            entry = raw.get(node)
            if not isinstance(entry, dict):
                missing_nodes.append(node)
                continue

            time_per_cycle_s = entry.get('time_per_cycle_s')
            energy_per_cycle_kwh = entry.get('energy_per_cycle_kwh')

            def _is_valid_number(value):
                return (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value > 0
                )

            if not _is_valid_number(time_per_cycle_s) or not _is_valid_number(energy_per_cycle_kwh):
                invalid_entries.append(node)
                continue

            consumption_by_node[node] = {
                'travel_hours': time_per_cycle_s / 3600.0,
                'energy_kwh': energy_per_cycle_kwh,
            }

        if missing_nodes or invalid_entries:
            details = []
            if missing_nodes:
                details.append(f"nodos faltantes en el JSON: {missing_nodes}")
            if invalid_entries:
                details.append(
                    f"nodos con 'time_per_cycle_s'/'energy_per_cycle_kwh' invalidos: {invalid_entries}"
                )
            raise ValueError(
                f"El JSON de consumos WP2 ({json_path}) es incompleto o invalido: "
                + "; ".join(details)
            )

        return consumption_by_node

    def get_trips(self, mine_system, consumption_model='wp1', wp2_consumption_json=None) -> pd.DataFrame:
        elhds = mine_system.get_system_lhds()
        nodes = mine_system.get_system_nodes()
        index = pd.MultiIndex.from_tuples(list(itertools.product(elhds, nodes)),
                                          names=['elhd', 'node'])
        trips = pd.DataFrame(index=index,
                             columns=['travel_duration', 'n_trips', 'energy_consumption'])

        consumption_by_node = None
        if consumption_model == 'wp2':
            consumption_by_node = self._load_wp2_consumption_by_node(wp2_consumption_json, nodes)

        for elhd, node in index:
            if consumption_model == 'wp2':
                travel_dur_hours = consumption_by_node[node]['travel_hours']
                energy_per_trip = consumption_by_node[node]['energy_kwh']
            else:
                distance_outbound = mine_system.layout.get_distance_to_d_node_outbound(node)
                distance_return = mine_system.layout.get_distance_to_d_node_return(node)
                tilt = mine_system.layout.get_tilt(node)

                travel_dur_hours, energy_per_trip = mine_system.elhd.get_total_trips_info(
                    distance_outbound, distance_return, tilt, elhd, self.delta_t
                )
            if travel_dur_hours <= self.delta_t:
                n_trips = int(np.rint(self.delta_t / travel_dur_hours))
            else:
                n_trips = 1

            trips.loc[(elhd, node), 'n_trips'] = n_trips
            trips.loc[(elhd, node), 'travel_duration'] = 1
            trips.loc[(elhd, node), 'energy_consumption'] = energy_per_trip

        self.mapper['Trips'] = trips
        return trips

    def get_n_intervals_trip(self, node_name: str, elhd_name: str) -> float:
        return self.mapper['Trips'].loc[elhd_name, node_name]['travel_duration']

    def get_n_trips(self, node_name: str, elhd_name: str) -> float:
        return self.mapper['Trips'].loc[elhd_name, node_name]['n_trips']

    def get_energy_consumption(self, node_name: str, elhd_name: str) -> float:
        return self.mapper['Trips'].loc[elhd_name, node_name]['energy_consumption']

    def get(self, start_col=None, end_col=None, keys=None):
        """
        Generic getter de tu cÃ³digo original (no tocamos esta parte).
        """
        if start_col and end_col:
            if keys:
                return self.data.loc[keys, start_col:end_col]
            return self.data.loc[:, start_col:end_col]
        else:
            if keys:
                return self.data.loc[keys]
            return self.data

    def get_time(self) -> list:
        df = self.get(start_col='value', end_col='value', keys='id_2').to_frame()
        df.index = df.reset_index().index + 1
        max_value = df.iloc[:, 0].max()
        return list(range(1, int(max_value) + 1))

    def get_days(self) -> list:
        df = self.get(start_col='value', end_col='value', keys='id_1').to_frame()
        df.index = df.reset_index().index + 1
        max_value = df.iloc[:, 0].max()
        return list(range(1, int(max_value) + 1))

    def get_start(self) -> int:
        return self.get_time()[0]

    def get_end(self) -> int:
        return self.get_time()[-1]

    def get_shift1_start(self) -> dict:
        keys = ['id_1', 'id_4', 'id_7', 'id_10', 'id_13', 'id_16', 'id_19']
        df = self.get(start_col='start', end_col='start', keys=keys)
        df.index = df.reset_index().index + 1
        return df.iloc[:, 0].to_dict()

    def get_shift1_end(self) -> dict:
        keys = ['id_1', 'id_4', 'id_7', 'id_10', 'id_13', 'id_16', 'id_19']
        df = self.get(start_col='end', end_col='end', keys=keys)
        df.index = df.reset_index().index + 1
        return df.iloc[:, 0].to_dict()

    def get_shift2_start(self) -> dict:
        keys = ['id_2', 'id_5', 'id_8', 'id_11', 'id_14', 'id_17', 'id_20']
        df = self.get(start_col='start', end_col='start', keys=keys)
        df.index = df.reset_index().index + 1
        return df.iloc[:, 0].to_dict()

    def get_shift2_end(self) -> dict:
        keys = ['id_2', 'id_5', 'id_8', 'id_11', 'id_14', 'id_17', 'id_20']
        df = self.get(start_col='end', end_col='end', keys=keys)
        df.index = df.reset_index().index + 1
        return df.iloc[:, 0].to_dict()

    def get_shift3_start(self) -> dict:
        keys = ['id_3', 'id_6', 'id_9', 'id_12', 'id_15', 'id_18', 'id_21']
        df = self.get(start_col='start', end_col='start', keys=keys)
        df.index = df.reset_index().index + 1
        return df.iloc[:, 0].to_dict()

    def get_shift3_end(self) -> dict:
        keys = ['id_3', 'id_6', 'id_9', 'id_12', 'id_15', 'id_18', 'id_21']
        df = self.get(start_col='end', end_col='end', keys=keys)
        df.index = df.reset_index().index + 1
        return df.iloc[:, 0].to_dict()

    #def get_n_chargers(self) -> int:
    #    """Devuelve el nÃºmero de cargadores segÃºn Misc â†' 'chargers'."""
    #    return self.mapper['Misc']['chargers']

    #def get_grid_power(self) -> float:
    #    """Devuelve el parÃ¡metro Pmine segÃºn Misc â†' 'Pmine'."""
    #    return self.mapper['Misc']['Pmine']

    # ------------------------------------------------------------------ #
    # Generación renovable
    # ------------------------------------------------------------------ #

    def sample_gen_profiles(self, gen_profiles: pd.DataFrame) -> pd.DataFrame:
        """
        Hoja GenProfiles: columnas ['name', 'day', 1, 2, ..., T]
        Retorna DataFrame indexado por (name, day), columnas int = intervalos.
        """
        meta_cols = {'id', 'name', 'day'}
        interval_cols = [c for c in gen_profiles.columns if str(c) not in meta_cols]
        gp = gen_profiles[['name', 'day'] + interval_cols].copy()
        gp['day'] = gp['day'].astype(int)
        gp = gp.set_index(['name', 'day'])
        gp.columns = [int(c) for c in gp.columns]
        return gp.sort_index()

    def get_alpha_g(self, gen_name: str, day: int, time_interval: int) -> float:
        """Perfil de disponibilidad alpha[g, d, t] en [0, 1]. Retorna 0 si no existe."""
        df = self.mapper.get('GenProfiles')
        if df is None:
            return 0.0
        day_in_year1 = ((int(day) - 1) % 365) + 1
        key = (gen_name, day_in_year1)
        t_hour = math.ceil(int(time_interval) * self.delta_t)
        if key in df.index and t_hour in df.columns:
            val = df.loc[key, t_hour]
            return float(val.iloc[0]) if hasattr(val, 'iloc') else float(val)
        return 0.0



