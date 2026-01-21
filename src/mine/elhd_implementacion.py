from turtle import distance
from pandas import DataFrame
import numpy as np


class ELHD(object):
    """Objeto ELHD (Electric / Diesel Load-Haul-Dump).

    Este módulo solo maneja los datos que vienen de la hoja “LHD” en el Excel.
    Todos los parámetros relacionados con baterías han sido movidos a `battery.py`.

    Lógica de inversión en esquina (delays) implementada:
      OUTBOUND:
        1) Acelera desde 0 hasta recorrer 15 m (o menos si el tramo es más corto).
        2) Frena en seco con desaceleración infinita (STOP instantáneo).
           - No hay regeneración.
           - La frenada NO agrega consumo extra (solo se disipa la energía cinética acumulada).
        3) Espera t_delay_outbound (solo afecta consumo auxiliar por tiempo).
        4) Recorre la distancia restante con perfil "acelerar + (crucero opcional) + STOP instantáneo".
      INBOUND:
        Igual que outbound.

    Nota física importante:
      - El STOP instantáneo se modela como tiempo ~0 sin término de energía. La energía cinética ganada
        durante la aceleración se contabiliza como consumo (y se pierde al frenar, sin recuperar).

    Unidades esperadas (consistentes con tu código histórico):
      - speed en Excel: km/h
      - acceleration en Excel: m/s²
      - loading_time / discharging_time en Excel: segundos
      - delays: segundos
      - potencias: kW
      - masas: toneladas (t)
    """

    __slots__ = ["data", "elhds", "mapper"]

    # Distancia antes de la inversión (m)
    TURN_SPLIT_DISTANCE_M = 15.0

    # Delays por defecto (s)
    DEFAULT_DELAY_OUTBOUND_S = 15.0
    DEFAULT_DELAY_RETURN_S = 15.0

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
        """Retorna (total_time_sec, energy_kWh) para 1 ciclo.

        El tiempo total incluye: outbound + delay_out + loading + inbound + delay_ret + discharging.
        La energía incluye: tracción + auxiliares + hidráulica.
        Los delays solo agregan energía auxiliar (aux_power * tiempo).
        """
        v_max = float(self.get_speed(elhd_name)) * (1000.0 / 3600.0)  # km/h -> m/s
        a = float(self.get_acceleration(elhd_name))
        a = max(a, 1e-9)

        t_load = float(self.get_loading_time(elhd_name))
        t_discharge = float(self.get_discharging_time(elhd_name))

        t_delay_outbound = float(self.DEFAULT_DELAY_OUTBOUND_S)
        t_delay_return = float(self.DEFAULT_DELAY_RETURN_S)

        # Split distancias
        d_out_1, d_out_2 = self._split_distance(distance_outbound, self.TURN_SPLIT_DISTANCE_M)
        d_ret_1, d_ret_2 = self._split_distance(distance_return, self.TURN_SPLIT_DISTANCE_M)

        # Perfiles por tramo (STOP instantáneo al final de cada tramo)
        out1 = self._segment_accel_only(distance_m=d_out_1, a=a)
        out2 = self._segment_accel_then_cruise_stop(distance_m=d_out_2, v_max=v_max, a=a)
        ret1 = self._segment_accel_only(distance_m=d_ret_1, a=a)
        ret2 = self._segment_accel_then_cruise_stop(distance_m=d_ret_2, v_max=v_max, a=a)

        # Tiempos de viaje (sin load/unload)
        t_outbound = out1["t_total"] + t_delay_outbound + out2["t_total"]
        t_return = ret1["t_total"] + t_delay_return + ret2["t_total"]

        total_time_sec = t_outbound + t_load + t_return + t_discharge

        energy_kwh = self.engine_energy_with_instant_stops_and_delays(
            elhd_name=elhd_name,
            tilt=tilt,
            outbound_segments=[out1, out2],
            return_segments=[ret1, ret2],
            t_delay_outbound=t_delay_outbound,
            t_delay_return=t_delay_return,
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

    @staticmethod
    def _segment_accel_only(distance_m, a):
        """Tramo: acelerar desde 0 hasta recorrer distance_m, luego STOP instantáneo.

        No hay crucero.
        Retorna dict con: t_acc, t_const, v_peak, t_total.
        """
        d = max(float(distance_m), 0.0)
        a = max(float(a), 1e-9)
        if d <= 0.0:
            return {"t_acc": 0.0, "t_const": 0.0, "v_peak": 0.0, "t_total": 0.0}

        t_acc = np.sqrt(2.0 * d / a)
        v_peak = a * t_acc
        t_const = 0.0
        return {"t_acc": float(t_acc), "t_const": float(t_const), "v_peak": float(v_peak), "t_total": float(t_acc)}

    @staticmethod
    def _segment_accel_then_cruise_stop(distance_m, v_max, a):
        """Tramo: acelerar desde 0, (crucero opcional), luego STOP instantáneo.

        - Si no alcanza v_max: solo acelera (triangular incompleto) y para.
        - Si alcanza v_max: acelera hasta v_max y luego mantiene v_max.

        Retorna dict con: t_acc, t_const, v_peak, t_total.
        """
        d = max(float(distance_m), 0.0)
        v_max = max(float(v_max), 0.0)
        a = max(float(a), 1e-9)

        if d <= 0.0 or v_max <= 0.0:
            return {"t_acc": 0.0, "t_const": 0.0, "v_peak": 0.0, "t_total": 0.0}

        d_acc = (v_max ** 2) / (2.0 * a)  # distancia para llegar a v_max

        if d >= d_acc:
            t_acc = v_max / a
            v_peak = v_max
            t_const = (d - d_acc) / v_max
        else:
            t_acc = np.sqrt(2.0 * d / a)
            v_peak = a * t_acc
            t_const = 0.0

        return {
            "t_acc": float(t_acc),
            "t_const": float(t_const),
            "v_peak": float(v_peak),
            "t_total": float(t_acc + t_const),
        }

    # ------------------------------------------------------------------ #
    # Energía: componentes por tramo (acel + constante)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _delta_kinetic_event(mass_kg, v_peak):
        """Energía cinética ganada al acelerar desde 0 hasta v_peak (sin regeneración) [J]."""
        m = max(float(mass_kg), 0.0)
        v = max(float(v_peak), 0.0)
        return 0.5 * m * (v ** 2)

    def _aerodynamic_loss_segment(self, elhd_name, t_acc, t_const, v_peak):
        """Integral de potencia aerodinámica ~ v^3 (acel + constante), Joules [J]."""
        a = float(self.get_acceleration(elhd_name))
        a = max(a, 1e-9)
        frontal_area = float(self.get_frontal_area(elhd_name))
        rho = 1.225
        c_drag = 1.95
        constant = 0.5 * rho * frontal_area * c_drag

        t_acc = max(float(t_acc), 0.0)
        t_const = max(float(t_const), 0.0)
        v_peak = max(float(v_peak), 0.0)

        # aceleración: ∫(a t)^3 dt = a^3 t^4/4
        loss_acc = (a ** 3) * (t_acc ** 4) / 4.0
        # crucero: v^3 * t
        loss_cte = (v_peak ** 3) * t_const

        return constant * (loss_acc + loss_cte)

    def _rolling_resistance_loss_segment(self, elhd_name, tilt, t_acc, t_const, v_peak, mass_kg):
        """Pérdidas por rodadura (modelo original) integradas (acel + constante), Joules [J]."""
        g = 9.81
        cr = 1.0
        c1 = 0.000
        c2 = 0.03

        a = float(self.get_acceleration(elhd_name))
        a = max(a, 1e-9)

        t_acc = max(float(t_acc), 0.0)
        t_const = max(float(t_const), 0.0)
        v_peak = max(float(v_peak), 0.0)
        mass_kg = max(float(mass_kg), 0.0)

        const = mass_kg * g * np.cos(tilt) * cr

        # aceleración: ∫ (c1 v + c2) v dt con v=a t  => (c1 a^2 t^3 / 3 + c2 a t^2 / 2)
        e_acc = (c1 * (a ** 2) * (t_acc ** 3) / 3.0 + c2 * a * (t_acc ** 2) / 2.0)
        # crucero
        e_cte = (c1 * v_peak + c2) * v_peak * t_const

        return const * (e_acc + e_cte)

    def _gravitational_work_segment(self, tilt_effective, t_acc, t_const, v_peak, mass_kg, elhd_name):
        """Trabajo gravitacional (acel + constante), Joules [J]."""
        g = 9.81
        a = float(self.get_acceleration(elhd_name))
        a = max(a, 1e-9)

        t_acc = max(float(t_acc), 0.0)
        t_const = max(float(t_const), 0.0)
        v_peak = max(float(v_peak), 0.0)
        mass_kg = max(float(mass_kg), 0.0)

        cte = mass_kg * g * np.sin(tilt_effective)

        # distancia aceleración: a t^2 / 2 ; distancia crucero: v_peak t
        s_acc = a * (t_acc ** 2) / 2.0
        s_cte = v_peak * t_const

        return cte * (s_acc + s_cte)

    # ------------------------------------------------------------------ #
    # Energía total: tracción + auxiliares + hidráulicos
    # ------------------------------------------------------------------ #

    def engine_energy_with_instant_stops_and_delays(
        self,
        elhd_name,
        tilt,
        outbound_segments,
        return_segments,
        t_delay_outbound,
        t_delay_return,
        t_load,
        t_discharge,
    ):
        """Energía total por ciclo [kWh] con stops instantáneos y delays como espera.

        - outbound_segments / return_segments: lista de dicts con t_acc, t_const, v_peak, t_total.
        - STOP instantáneo: no agrega consumo extra, pero no hay regeneración.
          Se contabiliza la energía cinética ganada en cada tramo como consumo (evento cinético).
        - delays: solo afectan energía auxiliar (aux_power * tiempo).
        """
        # Masas
        m_out = float(self.get_weight(elhd_name)) * 1000.0
        m_ret = (float(self.get_weight(elhd_name)) + float(self.get_load_capacity(elhd_name))) * 1000.0

        # Eficiencias
        eng_eff = max(float(self.get_engine_efficiency(elhd_name)), 1e-9)
        tr_eff = max(float(self.get_transmission_efficiency(elhd_name)), 1e-9)

        # Energía en ruedas (J)
        wheel_energy_J = 0.0

        # Outbound (descargado): tilt
        for seg in outbound_segments:
            t_acc = seg["t_acc"]
            t_const = seg["t_const"]
            v_peak = seg["v_peak"]

            wheel_energy_J += self._gravitational_work_segment(tilt, t_acc, t_const, v_peak, m_out, elhd_name)
            wheel_energy_J += self._aerodynamic_loss_segment(elhd_name, t_acc, t_const, v_peak)
            wheel_energy_J += self._rolling_resistance_loss_segment(elhd_name, tilt, t_acc, t_const, v_peak, m_out)
            wheel_energy_J += self._delta_kinetic_event(m_out, v_peak)

        # Return (cargado): -tilt
        for seg in return_segments:
            t_acc = seg["t_acc"]
            t_const = seg["t_const"]
            v_peak = seg["v_peak"]

            wheel_energy_J += self._gravitational_work_segment(-tilt, t_acc, t_const, v_peak, m_ret, elhd_name)
            wheel_energy_J += self._aerodynamic_loss_segment(elhd_name, t_acc, t_const, v_peak)
            wheel_energy_J += self._rolling_resistance_loss_segment(elhd_name, tilt, t_acc, t_const, v_peak, m_ret)
            wheel_energy_J += self._delta_kinetic_event(m_ret, v_peak)

        # Energía auxiliar (J): movimiento + load/unload + delays
        aux_power_kW = float(self.get_aux_power(elhd_name))
        travel_time_s = sum(s["t_total"] for s in outbound_segments) + sum(s["t_total"] for s in return_segments)
        total_aux_time_s = (
            travel_time_s
            + float(t_load)
            + float(t_discharge)
            + float(t_delay_outbound)
            + float(t_delay_return)
        )
        aux_energy_J = 1000.0 * aux_power_kW * total_aux_time_s

        # Energía hidráulica (J): load/unload
        hydraulic_power_kW = float(self.get_hydraulic_power(elhd_name))
        hydraulic_energy_J = 1000.0 * hydraulic_power_kW * (float(t_load) + float(t_discharge))

        # Total (J): replico tu estructura histórica de eficiencias
        total_J = (wheel_energy_J / (eng_eff * tr_eff)) + ((aux_energy_J + hydraulic_energy_J) / tr_eff)

        # J -> kWh
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
            t_acc = seg["t_acc"]
            t_const = seg["t_const"]
            v_peak = seg["v_peak"]

            wheel_energy_J += self._gravitational_work_segment(
                tilt_effective=tilt,
                t_acc=t_acc,
                t_const=t_const,
                v_peak=v_peak,
                mass_kg=m,
                elhd_name=elhd_name,
            )

            wheel_energy_J += self._aerodynamic_loss_segment(
                elhd_name,
                t_acc,
                t_const,
                v_peak,
            )

            wheel_energy_J += self._rolling_resistance_loss_segment(
                elhd_name,
                tilt,
                t_acc,
                t_const,
                v_peak,
                m,
            )
            # Evento cinético (sin regeneración)
            wheel_energy_J += self._delta_kinetic_event(m, v_peak)

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
