from pandas import DataFrame
import numpy as np


class ELHD(object):
    """Objeto ELHD (Electric / Diesel Load-Haul-Dump).

    Este módulo solo maneja los datos que vienen de la hoja “LHD” en el Excel.
    Todos los parámetros relacionados con baterías han sido movidos a `battery.py`.

    Logica actual de viajes:
      OUTBOUND:
        - La maniobra de inversion se modela solo en la ida.
        - Primer tramo: primeros 15 m del viaje + 10 m extra asociados al avance
          de la maniobra.
        - El equipo se detiene desacelerando con la misma magnitud de aceleracion
          configurada.
        - Segundo tramo: resto del viaje + 10 m extra asociados a la continuacion
          de la maniobra.
        - Luego vuelve a acelerar y desacelera al final del viaje.
      INBOUND:
        - Viaje continuo de inicio a fin, sin pausas ni metros extra por maniobra.
        - Perfil cinematico: acelerar + (crucero opcional) + desacelerar.

    Nota fisica importante:
      - No hay regeneracion.
      - La energia cinetica ganada en cada re-arranque se contabiliza como consumo
        y luego se pierde durante la frenada.

    Unidades esperadas:
      - speed en Excel: km/h
      - acceleration en Excel: m/s^2
      - deceleration en Excel: m/s^2
      - loading_time / discharging_time en Excel: segundos
      - potencias: kW
      - masas: toneladas (t)
    """

    __slots__ = ["data", "elhds", "mapper"]

    # Distancias asociadas a la maniobra de inversion (m)
    TURN_SPLIT_DISTANCE_M = 15.0
    TURN_MANEUVER_ENTRY_M = 10.0
    TURN_MANEUVER_EXIT_M = 10.0

    # ------------------------------------------------------------------ #
    # Constructor & utilidades genéricas
    # ------------------------------------------------------------------ #
    def __init__(self, data: DataFrame, cols_id: str):
        self.data = data.set_index(cols_id, drop=False).sort_index()
        self.elhds = list(self.data.index)
        self.mapper = {}

    def _get(self, col: str, keys=None):
        if keys is not None:
            return self.data.loc[keys, col]
        return self.data[col]

    # ------------------------------------------------------------------ #
    # Getters (mantengo nombres del archivo original)
    # ------------------------------------------------------------------ #

    def get_id(self, keys=None):
        return self._get("id", keys)

    def get_name(self, keys=None):
        return self._get("name", keys)

    def get_energy_cost(self, keys=None):
        return self._get("energy_cost", keys)

    def get_pmax_charge(self, keys=None):
        return self._get("charge_power", keys)

    def get_e_max(self, keys=None):
        return self._get("battery_capacity", keys)

    def get_soc_min(self, keys=None):
        return self._get("min_soc", keys)

    def get_charge_efficiency(self, keys=None):
        return self._get("charge_efficiency", keys)

    def get_discharge_efficiency(self, keys=None):
        return self._get("discharge_efficiency", keys)

    def get_total_chargers(self, keys=None):
        return self._get("total_chargers", keys)

    def get_operation_mode(self, keys=None):
        return self._get("operation_mode", keys)

    def get_swap_time(self, keys=None):
        return self._get("swap_time", keys)

    def get_technology_type(self, keys=None):
        return self._get("technology_type", keys)

    def get_model(self, keys=None):
        return self._get("model", keys)

    def get_load_capacity(self, keys=None):
        return self._get("load_capacity", keys)

    def get_filling_factor(self, keys=None):
        return self._get("filling_factor", keys)

    def get_loading_time(self, keys=None):
        return self._get("loading_time", keys)

    def get_discharging_time(self, keys=None):
        return self._get("discharging_time", keys)

    def get_weight(self, keys=None):
        return self._get("weight", keys)

    def get_engine_power(self, keys=None):
        return self._get("engine_power", keys)

    def get_frontal_area(self, keys=None):
        return self._get("frontal_area", keys)

    def get_tank_capacity(self, keys=None):
        return self._get("tank_capacity", keys)

    def get_min_capacity(self, keys=None):
        return self._get("min_capacity", keys)

    def get_fuel_consumption(self, keys=None):
        return self._get("fuel_consumption", keys)

    def get_refueling_time(self, keys=None):
        return self._get("refueling_time", keys)

    def get_engine_efficiency(self, keys=None):
        return self._get("engine_efficiency", keys)

    def get_transmission_efficiency(self, keys=None):
        return self._get("transmission_efficiency", keys)

    def get_speed(self, keys=None):
        return self._get("speed", keys)

    def get_acceleration(self, keys=None):
        return self._get("acceleration", keys)

    def get_deceleration(self, keys=None):
        if "deceleration" in self.data.columns:
            return self._get("deceleration", keys)
        return self.get_acceleration(keys)

    def get_hydraulic_power(self, keys=None):
        return self._get("hydraulic_power", keys)

    def get_aux_power(self, keys=None):
        return self._get("aux_power", keys)

    # ------------------------------------------------------------------ #
    # API principal
    # ------------------------------------------------------------------ #

    def get_total_trips_info(self, distance_outbound, distance_return, tilt, elhd_name, delta_t):
        travel_dur_sec, energy = self.get_per_trip_info(distance_outbound, distance_return, tilt, elhd_name)
        travel_dur = travel_dur_sec / 3600.0
        return travel_dur, energy

    def get_per_trip_info(self, distance_outbound, distance_return, tilt, elhd_name):
        """Retorna (total_time_sec, energy_kWh) para 1 ciclo."""
        v_max = float(self.get_speed(elhd_name)) * (1000.0 / 3600.0)  # km/h -> m/s
        a_acc = max(float(self.get_acceleration(elhd_name)), 1e-9)
        a_dec = max(float(self.get_deceleration(elhd_name)), 1e-9)

        t_load = float(self.get_loading_time(elhd_name))
        t_discharge = float(self.get_discharging_time(elhd_name))

        # Outbound con maniobra de inversion: primer tramo corto + resto.
        d_out_1, d_out_2 = self._split_distance_with_turn_maneuver(distance_outbound)
        out1 = self._segment_accel_then_cruise_then_decel(distance_m=d_out_1, v_max=v_max, a_acc=a_acc, a_dec=a_dec)
        out2 = self._segment_accel_then_cruise_then_decel(distance_m=d_out_2, v_max=v_max, a_acc=a_acc, a_dec=a_dec)

        # Inbound continuo, sin maniobra de inversion.
        ret = self._segment_accel_then_cruise_then_decel(
            distance_m=distance_return,
            v_max=v_max,
            a_acc=a_acc,
            a_dec=a_dec,
        )

        t_outbound = out1["t_total"] + out2["t_total"]
        t_return = ret["t_total"]
        total_time_sec = t_outbound + t_load + t_return + t_discharge

        energy_kwh = self.engine_energy_with_segment_profiles(
            elhd_name=elhd_name,
            tilt=tilt,
            outbound_segments=[out1, out2],
            return_segments=[ret],
            t_load=t_load,
            t_discharge=t_discharge,
        )

        return total_time_sec, energy_kwh

    # ------------------------------------------------------------------ #
    # Helpers de cinemática
    # ------------------------------------------------------------------ #

    @staticmethod
    def _split_distance(distance_total, first_leg_m):
        d_total = max(float(distance_total), 0.0)
        d1 = min(float(first_leg_m), d_total)
        d2 = max(d_total - d1, 0.0)
        return d1, d2

    def _split_distance_with_turn_maneuver(self, distance_total):
        d1, d2 = self._split_distance(distance_total, self.TURN_SPLIT_DISTANCE_M)
        if d1 <= 0.0 and d2 <= 0.0:
            return 0.0, 0.0
        return (
            d1 + float(self.TURN_MANEUVER_ENTRY_M),
            d2 + float(self.TURN_MANEUVER_EXIT_M),
        )

    @staticmethod
    def _segment_accel_then_cruise_then_decel(distance_m, v_max, a_acc, a_dec):
        """Tramo: acelerar desde 0, crucero opcional y desacelerar hasta 0."""
        d = max(float(distance_m), 0.0)
        v_max = max(float(v_max), 0.0)
        a_acc = max(float(a_acc), 1e-9)
        a_dec = max(float(a_dec), 1e-9)

        if d <= 0.0 or v_max <= 0.0:
            return {"t_acc": 0.0, "t_const": 0.0, "t_dec": 0.0, "v_peak": 0.0, "t_total": 0.0}

        d_acc = (v_max ** 2) / (2.0 * a_acc)
        d_dec = (v_max ** 2) / (2.0 * a_dec)
        d_min_stop = d_acc + d_dec

        if d >= d_min_stop:
            v_peak = v_max
            t_acc = v_peak / a_acc
            t_const = (d - d_min_stop) / v_peak
        else:
            v_peak = np.sqrt((2.0 * d) / ((1.0 / a_acc) + (1.0 / a_dec)))
            t_acc = v_peak / a_acc
            t_const = 0.0

        t_dec = v_peak / a_dec if v_peak > 0.0 else 0.0

        return {
            "t_acc": float(t_acc),
            "t_const": float(t_const),
            "t_dec": float(t_dec),
            "v_peak": float(v_peak),
            "t_total": float(t_acc + t_const + t_dec),
        }

    def _segment_accel_then_cruise_stop(self, distance_m, v_max, a):
        """Compatibilidad con el nombre usado por la lógica de carga."""
        return self._segment_accel_then_cruise_then_decel(
            distance_m=distance_m,
            v_max=v_max,
            a_acc=a,
            a_dec=a,
        )

    @staticmethod
    def _integrals_decel(v_peak, decel, t_dec):
        v_peak = max(float(v_peak), 0.0)
        decel = max(float(decel), 1e-9)
        t_dec = max(float(t_dec), 0.0)

        int_v_dt = v_peak * t_dec - 0.5 * decel * (t_dec ** 2)
        int_v2_dt = (v_peak ** 2) * t_dec - v_peak * decel * (t_dec ** 2) + (decel ** 2) * (t_dec ** 3) / 3.0
        int_v3_dt = (
            (v_peak ** 3) * t_dec
            - 1.5 * (v_peak ** 2) * decel * (t_dec ** 2)
            + v_peak * (decel ** 2) * (t_dec ** 3)
            - (decel ** 3) * (t_dec ** 4) / 4.0
        )
        return int_v_dt, int_v2_dt, int_v3_dt

    # ------------------------------------------------------------------ #
    # Energía: componentes por tramo (acel + constante)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _delta_kinetic_event(mass_kg, v_peak):
        """Energia cinetica ganada al acelerar desde 0 hasta v_peak [J]."""
        m = max(float(mass_kg), 0.0)
        v = max(float(v_peak), 0.0)
        return 0.5 * m * (v ** 2)

    def _aerodynamic_loss_segment(self, elhd_name, seg):
        """Integral de potencia aerodinamica ~ v^3, Joules [J]."""
        a_acc = max(float(self.get_acceleration(elhd_name)), 1e-9)
        a_dec = max(float(self.get_deceleration(elhd_name)), 1e-9)
        frontal_area = float(self.get_frontal_area(elhd_name))
        rho = 1.225
        c_drag = 1.95
        constant = 0.5 * rho * frontal_area * c_drag

        t_acc = max(float(seg["t_acc"]), 0.0)
        t_const = max(float(seg["t_const"]), 0.0)
        t_dec = max(float(seg["t_dec"]), 0.0)
        v_peak = max(float(seg["v_peak"]), 0.0)

        loss_acc = (a_acc ** 3) * (t_acc ** 4) / 4.0
        loss_cte = (v_peak ** 3) * t_const
        _, _, loss_dec = self._integrals_decel(v_peak, a_dec, t_dec)

        return constant * (loss_acc + loss_cte + loss_dec)

    def _rolling_resistance_loss_segment(self, elhd_name, tilt, seg, mass_kg):
        """Perdidas por rodadura integradas, Joules [J]."""
        g = 9.81
        cr = 1.0
        c1 = 0.000
        c2 = 0.03

        a_acc = max(float(self.get_acceleration(elhd_name)), 1e-9)
        a_dec = max(float(self.get_deceleration(elhd_name)), 1e-9)
        t_acc = max(float(seg["t_acc"]), 0.0)
        t_const = max(float(seg["t_const"]), 0.0)
        t_dec = max(float(seg["t_dec"]), 0.0)
        v_peak = max(float(seg["v_peak"]), 0.0)
        mass_kg = max(float(mass_kg), 0.0)

        const = mass_kg * g * np.cos(tilt) * cr

        e_acc = c1 * (a_acc ** 2) * (t_acc ** 3) / 3.0 + c2 * a_acc * (t_acc ** 2) / 2.0
        e_cte = (c1 * v_peak + c2) * v_peak * t_const
        int_v_dec, int_v2_dec, _ = self._integrals_decel(v_peak, a_dec, t_dec)
        e_dec = c1 * int_v2_dec + c2 * int_v_dec

        return const * (e_acc + e_cte + e_dec)

    def _gravitational_work_segment(self, tilt_effective, seg, mass_kg, elhd_name):
        """Trabajo gravitacional del tramo, Joules [J]."""
        g = 9.81
        a_acc = max(float(self.get_acceleration(elhd_name)), 1e-9)
        a_dec = max(float(self.get_deceleration(elhd_name)), 1e-9)

        t_acc = max(float(seg["t_acc"]), 0.0)
        t_const = max(float(seg["t_const"]), 0.0)
        t_dec = max(float(seg["t_dec"]), 0.0)
        v_peak = max(float(seg["v_peak"]), 0.0)
        mass_kg = max(float(mass_kg), 0.0)

        cte = mass_kg * g * np.sin(tilt_effective)

        s_acc = a_acc * (t_acc ** 2) / 2.0
        s_cte = v_peak * t_const
        s_dec = v_peak * t_dec - 0.5 * a_dec * (t_dec ** 2)

        return cte * (s_acc + s_cte + s_dec)

    # ------------------------------------------------------------------ #
    # Energía total: tracción + auxiliares + hidráulicos
    # ------------------------------------------------------------------ #

    def engine_energy_with_segment_profiles(
        self,
        elhd_name,
        tilt,
        outbound_segments,
        return_segments,
        t_load,
        t_discharge,
    ):
        """Energia total por ciclo [kWh] usando perfiles por segmento con frenado fisico."""
        m_out = float(self.get_weight(elhd_name)) * 1000.0
        m_ret = (float(self.get_weight(elhd_name)) + float(self.get_load_capacity(elhd_name))) * 1000.0

        eng_eff = max(float(self.get_engine_efficiency(elhd_name)), 1e-9)
        tr_eff = max(float(self.get_transmission_efficiency(elhd_name)), 1e-9)

        wheel_energy_J = 0.0

        # Outbound descargado: tilt
        for seg in outbound_segments:
            wheel_energy_J += self._gravitational_work_segment(tilt, seg, m_out, elhd_name)
            wheel_energy_J += self._aerodynamic_loss_segment(elhd_name, seg)
            wheel_energy_J += self._rolling_resistance_loss_segment(elhd_name, tilt, seg, m_out)
            wheel_energy_J += self._delta_kinetic_event(m_out, seg["v_peak"])

        # Return cargado: -tilt
        for seg in return_segments:
            wheel_energy_J += self._gravitational_work_segment(-tilt, seg, m_ret, elhd_name)
            wheel_energy_J += self._aerodynamic_loss_segment(elhd_name, seg)
            wheel_energy_J += self._rolling_resistance_loss_segment(elhd_name, tilt, seg, m_ret)
            wheel_energy_J += self._delta_kinetic_event(m_ret, seg["v_peak"])

        aux_power_kW = float(self.get_aux_power(elhd_name))
        travel_time_s = sum(s["t_total"] for s in outbound_segments) + sum(s["t_total"] for s in return_segments)
        total_aux_time_s = travel_time_s + float(t_load) + float(t_discharge)
        aux_energy_J = 1000.0 * aux_power_kW * total_aux_time_s

        hydraulic_power_kW = float(self.get_hydraulic_power(elhd_name))
        hydraulic_energy_J = 1000.0 * hydraulic_power_kW * (float(t_load) + float(t_discharge))

        total_J = (wheel_energy_J / (eng_eff * tr_eff)) + ((aux_energy_J + hydraulic_energy_J) / tr_eff)
        return total_J / (1000.0 * 3600.0)
    
    def engine_energy_charge_travel(self, distance, elhd_name, tilt):
        """
        Energía [kWh] para ir y volver a estación de carga (sin carga),
         usando modelo con STOP instantáneo.
        """
        distance = float(distance)
        if distance <= 0.0:
            return 0.0

        # Parámetros
        v_max = float(self.get_speed(elhd_name)) * (1000.0 / 3600.0)
        a = max(float(self.get_acceleration(elhd_name)), 1e-9)

        # Masa (sin carga)
        m = float(self.get_weight(elhd_name)) * 1000.0

        # Segmento ida y vuelta (mismo perfil)
        seg = self._segment_accel_then_cruise_stop(
            distance_m=distance,
            v_max=v_max,
            a=a,
        )

        # Energía en ruedas (J)
        wheel_energy_J = 0.0

        for _ in range(2):  # ida y vuelta
            wheel_energy_J += self._gravitational_work_segment(tilt, seg, m, elhd_name)
            wheel_energy_J += self._aerodynamic_loss_segment(elhd_name, seg)
            wheel_energy_J += self._rolling_resistance_loss_segment(elhd_name, tilt, seg, m)
            # Evento cinético (sin regeneración)
            wheel_energy_J += self._delta_kinetic_event(m, seg["v_peak"])

        # Energía auxiliar (J): solo tiempo de viaje
        aux_power_kW = float(self.get_aux_power(elhd_name))
        travel_time_s = 2.0 * seg["t_total"]
        aux_energy_J = 1000.0 * aux_power_kW * travel_time_s

        # Eficiencias
        eng_eff = max(float(self.get_engine_efficiency(elhd_name)), 1e-9)
        tr_eff = max(float(self.get_transmission_efficiency(elhd_name)), 1e-9)

        total_J = (wheel_energy_J / (eng_eff * tr_eff)) + (aux_energy_J / tr_eff)

        return total_J / (1000.0 * 3600.0)
    
    def time_charge_station(self, distance, elhd_name):
        """
        Tiempo [h] para ir a estación de carga (solo ida),
        usando modelo con STOP instantáneo.
        """
        distance = float(distance)
        if distance <= 0.0:
            return 0.0

        v_max = float(self.get_speed(elhd_name)) * (1000.0 / 3600.0)
        a = max(float(self.get_acceleration(elhd_name)), 1e-9)
        seg = self._segment_accel_then_cruise_stop(
            distance_m=distance,
            v_max=v_max,
            a=a,
        )

        # segundos -> horas
        return seg["t_total"] / 3600.0
