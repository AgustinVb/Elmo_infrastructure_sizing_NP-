import math
import pandas as pd
from pandas import DataFrame, concat
import numpy as np
import itertools


class Timeseries(object):

    def __init__(self, series, days, delta_t):
        self.series = series
        self.days = days
        self.delta_t = delta_t
        self.scaling_factor_op_cost = 365/(len(self.days))
        # Conservamos el diccionario 'time' de tu código original por compatibilidad
        self.time, self.mapper = dict(hourly=1, daily=24, weekly=168), {}
        self.time_intervals = self.build_mappers()
        self.shifts = self.get_shifts()
        self._mc_scaled_cache = {}
        self._mc_alpha = self._load_marginal_cost_alpha()

    def build_mappers(self):
        #self.mapper['Emissions']       = self.sample_emissions(self.series['Emissions'])
        self.mapper['MarginalCost']    = self.sample_marginal_cost(self.series['MarginalCost'])
        self.mapper['ExtractionGoal']  = self.sample_extraction_goal(self.series['ExtractionGoal'])
        self.mapper['Shifts']          = self.sample_shifts(self.series['Shifts'])
        self.mapper['Time_Intervals']  = self.get_time_intervals()
        self.mapper['NodeAssignment']  = self.sample_node_assignment(self.series['NodeAssignment'])
        self.mapper['StationAssignment']  = self.sample_station_assignment(self.series['StationAssignment'])
        #self.mapper['BatteryAssignment'] = self.sample_battery_assignment(self.series['BatteryAssignment'])
        #self.mapper['Misc'] = self.sample_misc(self.series['Misc'])
        return self.mapper['Time_Intervals']
    
    def _hours_of_day(self, day: int):
        """Horas absolutas del año (1..8760) que pertenecen al día 'day'."""
        start = (int(day) - 1) * 24 + 1
        return list(range(start, start + 24))

    def _load_marginal_cost_alpha(self) -> float:
        """Lee alpha desde la fila Profile_fixed de MarginalCost si existe."""
        marginal_cost = self.mapper.get('MarginalCost')
        if marginal_cost is None or marginal_cost.empty:
            return 1.0

        profile_row = None
        if 'Profile_fixed' in marginal_cost.index:
            profile_row = marginal_cost.loc['Profile_fixed']
        elif 'Profile_fixed' in marginal_cost.get('name', []):
            try:
                profile_row = marginal_cost.set_index('name').loc['Profile_fixed']
            except Exception:
                profile_row = None

        if profile_row is None:
            return 1.0

        series = pd.to_numeric(profile_row, errors='coerce').dropna()
        if series.empty:
            return 1.0

        first_value = float(series.iloc[0])
        return first_value if first_value > 0 else 1.0

    def _scaled_day_marginal_cost(self, location_name: str, day: int, alpha: float | None = None):
        """
        Devuelve una Serie (index: horas del año) del día 'day' para 'location_name'
        con 0 -> 0.001 y escalada por alpha.
        Usa cache para no recalcular.
        """
        alpha_value = self._mc_alpha if alpha is None else float(alpha)
        key = (location_name, int(day), alpha_value)
        if key in self._mc_scaled_cache:
            return self._mc_scaled_cache[key]

        hours = self._hours_of_day(day)
        # Tomamos la fila por ubicación y las 24 columnas del día
        row = self.mapper['MarginalCost'].loc[location_name, hours].astype(float)

        # Paso 2: 0 -> 0.001
        row = row.replace(0.0, 0.001)

        # Paso 3: escalar usando alpha
        current_mean = float(row.mean())
        # Por seguridad: si fuese ~0 (no debería ocurrir tras el reemplazo), no escalar
        scale_factor = alpha_value / current_mean if current_mean > 0 else 1.0
        scaled = row * scale_factor

        self._mc_scaled_cache[key] = scaled
        return scaled

    def get_marginal_cost_scaled(self, location_name: str, day: int, time_interval: int, alpha: float | None = None) -> float:
        """
        Costo marginal ajustado para (location, day, interval):
        (1) extrae el día, (2) 0->0.001, (3) escala por alpha.
        Respeta delta_t (mapea intervalos sub-horarios al índice horario por 'ceil').
        """
        import math
        hour_of_year = math.ceil((day - 1) * 24 + time_interval * self.delta_t)
        scaled_day = self._scaled_day_marginal_cost(location_name, day, alpha=alpha)

        # Si por alguna razón el hour_of_year no está (no debería), cae al valor original
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
        Toma la hoja 'NodeAssignment' con las columnas
          ['shift_name_start', 'shift_name_end', 'elhd_name', 'ex_node_name'].
        Agrega las columnas:
          - start_day, end_day
          - start_interval, end_interval
        Igual que en el original, pero sin modificar la lógica.
        """
        na = node_assignment[['shift_name_start', 'shift_name_end', 'elhd_name', 'ex_node_name']].copy()
        na['start_day'] = [self.get_shift_day_start(s) for s in na['shift_name_start']]
        na['end_day'] = [self.get_shift_day_end(s) for s in na['shift_name_end']]
        na['start_interval'] = [self.get_shift_start_interval(s) for s in na['shift_name_start']]
        na['end_interval'] = [self.get_shift_end_interval(s) for s in na['shift_name_end']]
        return na

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
        hours_selected = np.arange((self.days[0] - 1) * 24 + 1,
                                   (self.days[len(self.days) - 1]) * 24 + 1)
        return marginal_cost[hours_selected]

    def sample_emissions(self, emissions: pd.DataFrame) -> pd.DataFrame:
        emissions = emissions.set_index('name')
        hours_selected = np.arange((self.days[0] - 1) * 24 + 1,
                                   (self.days[len(self.days) - 1]) * 24 + 1)
        return emissions[hours_selected]

    def time_intervals_in_day(self, day: int) -> np.ndarray:
        return self.mapper['Time_Intervals'].loc[day, :]['interval'].values

    def sample_extraction_goal(self, extraction_goal: pd.DataFrame) -> pd.DataFrame:
        extraction_goal = extraction_goal.set_index('name')
        days_selected = np.arange(self.days[0], (self.days[len(self.days) - 1]) + 1)
        return extraction_goal[days_selected]

    def get_marginal_cost(self, location_name: str, day: int, time_interval: int) -> float:
        hour_of_year = math.ceil((day - 1) * 24 + time_interval * self.delta_t)
        return self.mapper['MarginalCost'].loc[location_name, hour_of_year]

    def get_emissions(self, tipo: str, day: int, time_interval: int) -> float:
        hour_of_year = math.ceil((day - 1) * 24 + time_interval * self.delta_t)
        return self.mapper['Emissions'].loc[tipo, hour_of_year]

    def get_extraction_goal(self, node_name: str, day: int) -> float:
        return self.mapper['ExtractionGoal'].loc[node_name, day]

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
        shifts = shifts[['id', 'name', 'day_start', 'day_end', 'hour_start', 'duration']].copy()
        shifts = shifts.set_index(['name', 'id'])
        return shifts

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
        shifts = self.series['Shifts']
        shifts = shifts[shifts['day_start'].isin(self.days)]
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
        """Puebla `self.mapper['Stations_per_elhd']` con asignación estática.
        Para cada elhd almacena la lista de estaciones asociadas.
        """
        station_per_elhd = {}
        for elhd in elhds:
            station_per_elhd[elhd] = self.get_station_assignment_to_elhd(elhd)
        self.mapper['Stations_per_elhd'] = station_per_elhd
        return 0
       
    def get_elhd_at_station(self, stations: list) -> int:
        """Invierte `Stations_per_elhd` → `elhd_per_station[(station)] = [elhds]`"""
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

    def get_nodes_assigned_to_elhd(self, day: int, interval: int, elhd_name: str) -> list:
        if interval != -1:
            na = self.mapper['NodeAssignment']
            na_elhd = na[na["elhd_name"] == elhd_name]
            na_filt = na_elhd[(na_elhd["start_day"] <= day) & (na_elhd["end_day"] >= day) &
                              (na_elhd["start_interval"] <= interval) & (na_elhd["end_interval"] >= interval)]
            return list(na_filt['ex_node_name'])
        else:
            return []

    def get_node_assignment(self, elhds: list) -> int:
        intervals = self.time_intervals
        node_assign_dict = {}
        for day in self.days:
            for interval in intervals:
                for elhd in elhds:
                    node_list = self.get_nodes_assigned_to_elhd(day, interval, elhd)
                    node_assign_dict[(day, interval, elhd)] = node_list
        self.mapper['Nodes_assigned_at_interval'] = node_assign_dict
        return 0

    def get_elhd_at_node(self, nodes: list) -> int:
        elhd_dict = {}
        for d in self.days:
            for t in self.time_intervals:
                for node in nodes:
                    elhd_dict[(node, d, t)] = []
        for (d, t, elhd) in self.mapper['Nodes_assigned_at_interval']:
            for node in self.mapper['Nodes_assigned_at_interval'][(d, t, elhd)]:
                elhd_dict[(node, d, t)].append(elhd)
        self.mapper['elhd_at_node'] = elhd_dict
        return 0

    def get_batteries_assigned_to_elhd(self, day: int, interval: int, elhd_name: str) -> list:
        ba = self.mapper['BatteryAssignment']
        ba_elhd = ba[ba["elhd_name"] == elhd_name]
        assigned = []
        last_int = int(self.get_last_interval())
        for _, row in ba_elhd.iterrows():
            sd, ed = row.start_day, row.end_day
            # ¿Este BA cubre el día?
            if not (sd <= day <= ed):
                continue
            # si es día de inicio, arrancamos en start_interval; si no, en la 1
            si = row.start_interval if day == sd else 1
            # si es día de fin, paramos en end_interval; si no, en el último intervalo
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

    def get_trips(self, mine_system) -> pd.DataFrame:
        elhds = mine_system.get_system_lhds()
        nodes = mine_system.get_system_nodes()
        index = pd.MultiIndex.from_tuples(list(itertools.product(elhds, nodes)),
                                          names=['elhd', 'node'])
        trips = pd.DataFrame(index=index,
                             columns=['travel_duration', 'n_trips', 'energy_consumption'])

        for elhd, node in index:
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

            # 🔥 NUEVO: transformar kWh a litros de diésel si corresponde
            tech_type = mine_system.elhd.get_technology_type(elhd).lower()
            if tech_type == 'diesel':
                bsfc_g_per_kwh = 230          # Brake Specific Fuel Consumption [g/kWh]
                diesel_density_g_per_l = 832  # Densidad del diésel [g/L]

                # Calcular gramos de diésel
                grams_diesel = energy_per_trip * bsfc_g_per_kwh  # gramos

                # Convertir gramos a litros
                fuel_liters = grams_diesel / diesel_density_g_per_l  # litros

                trips.loc[(elhd, node), 'diesel_consumption'] = fuel_liters
            else:
                trips.loc[(elhd, node), 'diesel_consumption'] = 0

        self.mapper['Trips'] = trips
        return trips

    def get_n_intervals_trip(self, node_name: str, elhd_name: str) -> float:
        return self.mapper['Trips'].loc[elhd_name, node_name]['travel_duration']

    def get_n_trips(self, node_name: str, elhd_name: str) -> float:
        return self.mapper['Trips'].loc[elhd_name, node_name]['n_trips']

    def get_energy_consumption(self, node_name: str, elhd_name: str) -> float:
        return self.mapper['Trips'].loc[elhd_name, node_name]['energy_consumption']

    def get_diesel_consumption(self, node_name: str, elhd_name: str) -> float:
        return self.mapper['Trips'].loc[elhd_name, node_name]['diesel_consumption']

    def get(self, start_col=None, end_col=None, keys=None):
        """
        Generic getter de tu código original (no tocamos esta parte).
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
    #    """Devuelve el número de cargadores según Misc → 'chargers'."""
    #    return self.mapper['Misc']['chargers']

    #def get_grid_power(self) -> float:
    #    """Devuelve el parámetro Pmine según Misc → 'Pmine'."""
    #    return self.mapper['Misc']['Pmine']

