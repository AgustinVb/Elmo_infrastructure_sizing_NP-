#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple, Optional

# ============================================================
# CONFIG: define aquí el delta t por defecto (minutos)
# (Puedes sobreescribirlo con --delta-minutes en la consola)
# ============================================================
DEFAULT_DELTA_MINUTES = 8.0


# -----------------------------
# Helpers lectura / vacío
# -----------------------------
def is_effectively_empty_json(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        if path.stat().st_size == 0:
            return True
    except OSError:
        return True

    try:
        txt = path.read_text(encoding="utf-8").strip()
        if txt == "":
            return True
        obj = json.loads(txt)
        if obj is None:
            return True
        if isinstance(obj, dict) and len(obj) == 0:
            return True
        if isinstance(obj, list) and len(obj) == 0:
            return True
    except Exception:
        # si no parsea, lo tratamos como no usable para evitar romper el batch
        return True

    return False


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_json_in_folder(root: Path, filename: str) -> Optional[Path]:
    direct = root / filename
    if direct.exists():
        return direct

    matches = list(root.rglob(filename))
    if not matches:
        return None

    # prioriza el más grande
    matches.sort(key=lambda p: (p.stat().st_size if p.exists() else 0), reverse=True)
    return matches[0]


# -----------------------------
# Tablas ASCII (sin dependencias)
# -----------------------------
def fmt_num(x: Any, nd: int = 3) -> str:
    if isinstance(x, (int, float)):
        return f"{x:.{nd}f}"
    return str(x)


def make_table(title: str, headers: List[str], rows: List[List[Any]]) -> str:
    # convierte todo a string
    srows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in srows:
        for j, c in enumerate(r):
            widths[j] = max(widths[j], len(c))

    def line(char: str = "-") -> str:
        return "+" + "+".join(char * (w + 2) for w in widths) + "+"

    def row(cells: List[str]) -> str:
        return "|" + "|".join(f" {cells[j].ljust(widths[j])} " for j in range(len(widths))) + "|"

    out = []
    if title:
        out.append(title)
    out.append(line("-"))
    out.append(row(headers))
    out.append(line("="))
    for r in srows:
        out.append(row(r))
        out.append(line("-"))
    return "\n".join(out)


# -----------------------------
# Serie genérica para E/B
# -----------------------------
def parse_timeseries(v_map: Dict[str, Any]) -> List[Tuple[float, float]]:
    items: List[Tuple[float, float]] = []
    for k, v in v_map.items():
        try:
            t = float(k)
            level = float(v)
        except Exception:
            continue
        items.append((t, level))
    items.sort(key=lambda x: x[0])
    return items


def iter_day_series_key(vehicle_node: Any, series_key: str) -> Iterator[Dict[str, Any]]:
    if not isinstance(vehicle_node, dict):
        return

    # Formato nuevo (printer actual): d -> <day> -> t -> {time:value}
    dnode = vehicle_node.get("d")
    if isinstance(dnode, dict):
        for _, day_node in dnode.items():
            if not isinstance(day_node, dict):
                continue
            tnode = day_node.get("t")
            if isinstance(tnode, dict):
                yield tnode
                continue
            # Compatibilidad por si viene con clave explícita de serie
            maybe_series = day_node.get(series_key)
            if isinstance(maybe_series, dict):
                yield maybe_series
        return

    tnode = vehicle_node.get("t")
    if isinstance(tnode, dict):
        for _, day_node in tnode.items():
            if isinstance(day_node, dict) and isinstance(day_node.get(series_key), dict):
                yield day_node[series_key]
        return

    if isinstance(vehicle_node.get(series_key), dict):
        yield vehicle_node[series_key]
        return

    for _, day_node in vehicle_node.items():
        if isinstance(day_node, dict) and isinstance(day_node.get(series_key), dict):
            yield day_node[series_key]


def mean_drop_rate_per_hour_fixed_dt(
    json_path: Path,
    delta_minutes: float,
    eps: float,
    series_key: str,
    label_total_drop: str,
) -> Tuple[float, Dict[str, float]]:
    """
    Promedio (unidades/h) considerando SOLO pasos donde el nivel baja,
    usando delta_minutes MANUAL (no inferido desde timestamps).
    """
    data = load_json(json_path)
    if not isinstance(data, dict):
        raise ValueError("JSON inesperado: la raíz no es un objeto/dict.")

    if "d" in data and isinstance(data["d"], dict):
        vehicles = data["d"]
    elif "i" in data and isinstance(data["i"], dict):
        vehicles = data["i"]
    else:
        vehicles = data
    if not isinstance(vehicles, dict) or len(vehicles) == 0:
        raise ValueError("No pude detectar series por vehículo (dict vacío o formato no soportado).")

    step_hours = delta_minutes / 60.0
    total_drop = 0.0
    n_drop_steps = 0
    n_vehicles_used = 0
    n_series = 0
    n_pairs_total = 0

    for _, vnode in vehicles.items():
        had_series = False
        for series_map in iter_day_series_key(vnode, series_key):
            had_series = True
            n_series += 1

            series = parse_timeseries(series_map)
            if len(series) < 2:
                continue

            for (_, v0), (_, v1) in zip(series, series[1:]):
                n_pairs_total += 1
                drop = v0 - v1
                if drop > eps:
                    total_drop += drop
                    n_drop_steps += 1

        if had_series:
            n_vehicles_used += 1

    if n_drop_steps == 0:
        raise ValueError("No se encontraron descensos (pasos con consumo) en el JSON.")

    mean_per_hour = total_drop / (n_drop_steps * step_hours)

    stats = {
        "mean_per_hour": mean_per_hour,
        label_total_drop: total_drop,
        "n_drop_steps": float(n_drop_steps),
        "n_pairs_total": float(n_pairs_total),
        "n_vehicles_used": float(n_vehicles_used),
        "n_series": float(n_series),
        "delta_minutes": float(delta_minutes),
    }
    return mean_per_hour, stats


def mean_consumption_lph(e_json_path: Path, delta_minutes: float, eps: float = 1e-9):
    return mean_drop_rate_per_hour_fixed_dt(
        json_path=e_json_path,
        delta_minutes=delta_minutes,
        eps=eps,
        series_key="i",  # E: instantes -> litros
        label_total_drop="total_drop_liters",
    )


def mean_consumption_kwhph(b_json_path: Path, delta_minutes: float, eps: float = 1e-9):
    return mean_drop_rate_per_hour_fixed_dt(
        json_path=b_json_path,
        delta_minutes=delta_minutes,
        eps=eps,
        series_key="i",  # B: instantes -> kWh
        label_total_drop="total_drop_kwh",
    )


def consumed_energy_swap_travel(root: Path, eps: float = 1e-9) -> Tuple[float, Dict[str, float]]:
    """
    Consumo eléctrico usando la ecuación del modelo en swap:

        B[i,d,t] = B_s[i,d,t-1] - consumo_intervalo(i,d,t)

    => consumo_intervalo(i,d,t) = B_s[i,d,t-1] - B[i,d,t]

    Esta métrica sí cuenta correctamente intervalos con swap/viaje.
    """
    b_path = find_json_in_folder(root, "B.json")
    bs_path = find_json_in_folder(root, "B_s.json")
    if not b_path or not bs_path:
        raise ValueError("No se encontraron B.json o B_s.json")
    if is_effectively_empty_json(b_path) or is_effectively_empty_json(bs_path):
        raise ValueError("B.json o B_s.json están vacíos/no usables")

    b_data = load_json(b_path)
    bs_data = load_json(bs_path)

    def _extract_series_map(data: Any) -> Dict[str, Dict[int, Dict[int, float]]]:
        out: Dict[str, Dict[int, Dict[int, float]]] = {}
        if isinstance(data, dict):
            root_i = data.get("i", {})
            if not isinstance(root_i, dict) or not root_i:
                root_i = data.get("_1", {})
            if not isinstance(root_i, dict) or not root_i:
                root_i = data.get("d", {})
        else:
            root_i = {}
        if not isinstance(root_i, dict):
            return out

        for lhd, lhd_node in root_i.items():
            if not isinstance(lhd_node, dict):
                continue
            d_node = lhd_node.get("d", {})
            if not isinstance(d_node, dict) or not d_node:
                d_node = lhd_node.get("_2", {})
            if not isinstance(d_node, dict) or not d_node:
                d_node = lhd_node.get("t", {})
            if not isinstance(d_node, dict):
                continue

            day_map: Dict[int, Dict[int, float]] = {}
            for day_k, day_node in d_node.items():
                try:
                    day = int(float(day_k))
                except Exception:
                    continue

                t_node = day_node.get("t", {}) if isinstance(day_node, dict) else {}
                if not isinstance(t_node, dict) or not t_node:
                    t_node = day_node.get("_3", {}) if isinstance(day_node, dict) else {}
                if not isinstance(t_node, dict) or not t_node:
                    t_node = day_node.get("i", {}) if isinstance(day_node, dict) else {}
                if not isinstance(t_node, dict):
                    continue

                ser: Dict[int, float] = {}
                for tk, tv in t_node.items():
                    try:
                        ser[int(float(tk))] = float(tv)
                    except Exception:
                        continue
                day_map[day] = ser

            out[lhd] = day_map

        return out

    b_map = _extract_series_map(b_data)
    bs_map = _extract_series_map(bs_data)

    total_consumed = 0.0
    n_intervals = 0
    n_negative_clipped = 0
    n_missing_pairs = 0

    for lhd, day_map in b_map.items():
        bs_day_map = bs_map.get(lhd, {})
        for day, b_ser in day_map.items():
            bs_ser = bs_day_map.get(day, {})
            if not b_ser or not bs_ser:
                continue

            for t in sorted(b_ser.keys()):
                if t <= 0:
                    continue
                b_t = b_ser.get(t)
                bs_prev = bs_ser.get(t - 1)
                if b_t is None or bs_prev is None:
                    n_missing_pairs += 1
                    continue

                cons = float(bs_prev) - float(b_t)
                if cons < -eps:
                    n_negative_clipped += 1
                total_consumed += max(0.0, cons)
                n_intervals += 1

    if n_intervals == 0:
        raise ValueError("No se pudieron construir intervalos válidos con B_s(t-1) y B(t)")

    meta = {
        "total_consumed_kwh": total_consumed,
        "n_intervals": float(n_intervals),
        "n_missing_pairs": float(n_missing_pairs),
        "n_negative_clipped": float(n_negative_clipped),
    }
    return total_consumed, meta


def total_rise_energy_from_b(b_json_path: Path, eps: float = 1e-9) -> Tuple[float, Dict[str, float]]:
    """
    Suma los ascensos de energía en B (kWh) considerando pasos consecutivos.
    Si B sube entre t y t+1, aporta (B[t+1]-B[t]).
    """
    data = load_json(b_json_path)
    if not isinstance(data, dict):
        raise ValueError("B.json inesperado: la raíz no es un objeto/dict.")

    if "d" in data and isinstance(data["d"], dict):
        vehicles = data["d"]
    elif "i" in data and isinstance(data["i"], dict):
        vehicles = data["i"]
    else:
        vehicles = data

    if not isinstance(vehicles, dict) or len(vehicles) == 0:
        raise ValueError("No pude detectar series por vehículo en B.json")

    total_rise = 0.0
    n_rise_steps = 0
    n_pairs_total = 0
    n_series = 0
    n_vehicles_used = 0

    for _, vnode in vehicles.items():
        had_series = False

        # En el formato actual de B.json, iter_day_series_key devuelve day_node['t']
        # (independiente de series_key), así que recorrer dos claves duplicaría series.
        for series_map in iter_day_series_key(vnode, "i"):
            had_series = True
            n_series += 1
            series = parse_timeseries(series_map)
            if len(series) < 2:
                continue

            for (_, v0), (_, v1) in zip(series, series[1:]):
                n_pairs_total += 1
                rise = v1 - v0
                if rise > eps:
                    total_rise += rise
                    n_rise_steps += 1

        if had_series:
            n_vehicles_used += 1

    stats = {
        "total_rise_kwh": total_rise,
        "n_rise_steps": float(n_rise_steps),
        "n_pairs_total": float(n_pairs_total),
        "n_vehicles_used": float(n_vehicles_used),
        "n_series": float(n_series),
    }
    return total_rise, stats


def calculate_b_frontier_delta(b_json_path: Path) -> Tuple[float, Dict[str, float]]:
    """
    ΔE_frontera_B = sum_i,d (B_i,d,tf - B_i,d,t0)
    usando primer y último valor temporal disponible por serie vehículo-día.
    """
    data = load_json(b_json_path)
    vehicles = data.get("i", {}) if isinstance(data, dict) else {}
    if not isinstance(vehicles, dict):
        raise ValueError("Formato de B.json no soportado para frontera")

    total_init = 0.0
    total_final = 0.0
    n_series = 0

    for _, vnode in vehicles.items():
        for series_map in iter_day_series_key(vnode, "i"):
            series = parse_timeseries(series_map)
            if len(series) < 1:
                continue
            total_init += float(series[0][1])
            total_final += float(series[-1][1])
            n_series += 1

    delta = total_final - total_init
    meta = {
        "b_initial_kwh": total_init,
        "b_final_kwh": total_final,
        "n_series": float(n_series),
    }
    return delta, meta


def calculate_station_inventory_delta(root: Path) -> Tuple[float, Dict[str, float]]:
    """
    Δinventario_estación en conteo de baterías:
      sum_k,d [(S_k,d,tf + X_dch_k,d,tf) - (S_k,d,t0 + X_dch_k,d,t0)]
    """
    s_path = find_json_in_folder(root, "S.json")
    x_dch_path = find_json_in_folder(root, "X_dch.json")

    if not s_path or not x_dch_path:
        raise ValueError("No se encontraron S.json o X_dch.json")
    if is_effectively_empty_json(s_path) or is_effectively_empty_json(x_dch_path):
        raise ValueError("S.json o X_dch.json vacíos/no usables")

    s_data = load_json(s_path)
    x_data = load_json(x_dch_path)

    def _to_series_map(obj: Any) -> Dict[Tuple[str, int], Dict[int, float]]:
        out: Dict[Tuple[str, int], Dict[int, float]] = {}
        s1 = obj.get("_1", {}) if isinstance(obj, dict) else {}
        if not isinstance(s1, dict):
            return out
        for station, s_node in s1.items():
            d_node = s_node.get("_2", {}) if isinstance(s_node, dict) else {}
            if not isinstance(d_node, dict):
                continue
            for day_k, day_node in d_node.items():
                try:
                    day = int(float(day_k))
                except Exception:
                    continue
                t_node = day_node.get("_3", {}) if isinstance(day_node, dict) else {}
                if not isinstance(t_node, dict):
                    continue
                ser: Dict[int, float] = {}
                for tk, tv in t_node.items():
                    try:
                        ser[int(float(tk))] = float(tv)
                    except Exception:
                        continue
                out[(station, day)] = ser
        return out

    s_map = _to_series_map(s_data)
    x_map = _to_series_map(x_data)

    keys = sorted(set(s_map.keys()) | set(x_map.keys()))
    init_total = 0.0
    final_total = 0.0
    n_station_days = 0

    for key in keys:
        s_ser = s_map.get(key, {})
        x_ser = x_map.get(key, {})
        if not s_ser and not x_ser:
            continue

        t_candidates = sorted(set(s_ser.keys()) | set(x_ser.keys()))
        if not t_candidates:
            continue

        t0 = t_candidates[0]
        tf = t_candidates[-1]

        s0 = float(s_ser.get(t0, 0.0))
        sf = float(s_ser.get(tf, 0.0))
        x0 = float(x_ser.get(t0, 0.0))
        xf = float(x_ser.get(tf, 0.0))

        init_total += (s0 + x0)
        final_total += (sf + xf)
        n_station_days += 1

    delta = final_total - init_total
    meta = {
        "inventory_initial": init_total,
        "inventory_final": final_total,
        "n_station_days": float(n_station_days),
    }
    return delta, meta


def calculate_charged_energy_from_sv(root: Path) -> Tuple[float, Dict[str, float]]:
    """
    Calcula energía cargada desde outputs con:
      sum(Sv[k,d,t,a] * p_charger * delta_t)
    usando Sv.json + parameters.json.
    """
    sv_path = find_json_in_folder(root, "Sv.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not sv_path or not params_path:
        raise ValueError("No se encontraron Sv.json o parameters.json")
    if is_effectively_empty_json(sv_path) or is_effectively_empty_json(params_path):
        raise ValueError("Sv.json o parameters.json están vacíos/no usables")

    sv_data = load_json(sv_path)
    params_data = load_json(params_path)

    p_charger = float(params_data.get("p_charger", 0.0))
    delta_t = float(params_data.get("delta_t", 0.0))

    def _sum_numbers(obj: Any) -> Tuple[float, int]:
        s = 0.0
        n = 0
        if isinstance(obj, dict):
            for v in obj.values():
                ss, nn = _sum_numbers(v)
                s += ss
                n += nn
        elif isinstance(obj, list):
            for v in obj:
                ss, nn = _sum_numbers(v)
                s += ss
                n += nn
        else:
            try:
                s += float(obj)
                n += 1
            except Exception:
                pass
        return s, n

    sv_sum, n_terms = _sum_numbers(sv_data)
    charged_energy_kwh = sv_sum * p_charger * delta_t
    meta = {
        "sv_sum": sv_sum,
        "p_charger": p_charger,
        "delta_t": delta_t,
        "n_terms": float(n_terms),
    }
    return charged_energy_kwh, meta


def calculate_real_charged_energy_from_swaps(
    root: Path,
    charge_intervals: Optional[int] = None,
    soc_base: Optional[float] = None,
    eps: Optional[float] = None,
) -> Tuple[float, Dict[str, float], List[Dict[str, Any]]]:
    """
        Energía cargada real por evento de swap hasta 100%:

            E_real_evento = bmax_i - B_llegada

        equivalente a:
            E_real_evento = bmax_i * (1 - SOC_llegada)

        Además retorna el detalle por evento (estación, LHD, día, t, SOC llegada, energía).
    """
    z_path = find_json_in_folder(root, "Z_swap.json")
    b_path = find_json_in_folder(root, "B.json")
    sv_path = find_json_in_folder(root, "Sv.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not z_path or not b_path or not sv_path or not params_path:
        raise ValueError("No se encontraron Z_swap.json, B.json, Sv.json o parameters.json")
    if (
        is_effectively_empty_json(z_path)
        or is_effectively_empty_json(b_path)
        or is_effectively_empty_json(sv_path)
        or is_effectively_empty_json(params_path)
    ):
        raise ValueError("Z_swap.json, B.json, Sv.json o parameters.json están vacíos/no usables")

    z_data = load_json(z_path)
    b_data = load_json(b_path)
    params_data = load_json(params_path)

    if eps is None:
        try:
            eps = float(params_data.get("eps", 1e-9))
        except Exception:
            eps = 1e-9

    delta_t = float(params_data.get("delta_t", 0.0))

    if charge_intervals is None:
        t_charge_raw = params_data.get("t_charge")
        resolved_intervals: Optional[int] = None
        try:
            if t_charge_raw is not None:
                resolved_intervals = int(round(float(t_charge_raw)))
        except Exception:
            resolved_intervals = None

        if resolved_intervals is None and delta_t > 0:
            t_swap_raw = params_data.get("t_swap", {}).get("_1", {})
            if isinstance(t_swap_raw, dict) and t_swap_raw:
                vals = []
                for v in t_swap_raw.values():
                    try:
                        vals.append(float(v))
                    except Exception:
                        continue
                if vals:
                    resolved_intervals = int(round(sum(vals) / len(vals) / delta_t))

        charge_intervals = resolved_intervals if (resolved_intervals is not None and resolved_intervals > 0) else 6

    if soc_base is None:
        bmin_raw = params_data.get("bmin_b", {}).get("_1", {})
        resolved_soc: Optional[float] = None
        if isinstance(bmin_raw, dict) and bmin_raw:
            vals = []
            for v in bmin_raw.values():
                try:
                    vals.append(float(v))
                except Exception:
                    continue
            if vals:
                resolved_soc = min(vals)
        soc_base = resolved_soc if resolved_soc is not None else 0.20

    # Referencia análoga a la función objetivo: sum(Sv * p_charger * delta_t)
    sv_energy_total, _ = calculate_charged_energy_from_sv(root)

    p_charger = float(params_data.get("p_charger", 0.0))
    bmax_raw = params_data.get("bmax_b", {}).get("_1", {})

    # Mapa B[i][d][t] -> nivel de energía (kWh)
    b_map: Dict[str, Dict[int, Dict[int, float]]] = {}
    vehicles = b_data.get("i", {}) if isinstance(b_data, dict) else {}
    if isinstance(vehicles, dict):
        for lhd, lhd_node in vehicles.items():
            dnode = lhd_node.get("d", {}) if isinstance(lhd_node, dict) else {}
            if not isinstance(dnode, dict):
                continue
            day_map: Dict[int, Dict[int, float]] = {}
            for day_k, day_node in dnode.items():
                try:
                    day = int(float(day_k))
                except Exception:
                    continue
                tnode = day_node.get("t", {}) if isinstance(day_node, dict) else {}
                if not isinstance(tnode, dict):
                    continue
                series: Dict[int, float] = {}
                for tk, tv in tnode.items():
                    try:
                        series[int(float(tk))] = float(tv)
                    except Exception:
                        continue
                day_map[day] = series
            b_map[lhd] = day_map

    total_real = 0.0
    n_events = 0
    n_events_with_discount = 0
    n_events_missing_b = 0
    n_events_missing_bmax = 0
    event_details: List[Dict[str, Any]] = []

    # Estructura esperada: _1[k]._2[i]._3[d]._4[t] = 0/1
    k_node = z_data.get("_1", {}) if isinstance(z_data, dict) else {}
    if not isinstance(k_node, dict):
        k_node = {}

    for station, st_data in k_node.items():
        i_node = st_data.get("_2", {}) if isinstance(st_data, dict) else {}
        if not isinstance(i_node, dict):
            continue
        for lhd, lhd_data in i_node.items():
            d_node = lhd_data.get("_3", {}) if isinstance(lhd_data, dict) else {}
            if not isinstance(d_node, dict):
                continue

            bmax_i_raw = bmax_raw.get(lhd)
            try:
                bmax_i = float(bmax_i_raw)
            except Exception:
                bmax_i = None

            for day_k, day_data in d_node.items():
                try:
                    day = int(float(day_k))
                except Exception:
                    continue

                t_node = day_data.get("_4", {}) if isinstance(day_data, dict) else {}
                if not isinstance(t_node, dict):
                    continue

                series = b_map.get(lhd, {}).get(day, {})

                for tk, val in t_node.items():
                    try:
                        zval = float(val)
                    except Exception:
                        continue
                    if zval <= 0.5 + eps:
                        continue

                    n_events += 1

                    if bmax_i is None or bmax_i <= 0:
                        n_events_missing_bmax += 1
                        continue

                    try:
                        t = int(float(tk))
                    except Exception:
                        continue

                    b_prev = series.get(t - 1)
                    if b_prev is None:
                        b_prev = series.get(t)
                    if b_prev is None:
                        n_events_missing_b += 1
                        continue

                    soc_arrival = b_prev / bmax_i
                    event_real = max(0.0, bmax_i - b_prev)
                    if event_real > eps:
                        n_events_with_discount += 1
                    total_real += event_real

                    event_details.append(
                        {
                            "station": station,
                            "lhd": lhd,
                            "day": day,
                            "t": t,
                            "soc_arrival": soc_arrival,
                            "bmax_kwh": bmax_i,
                            "b_arrival_kwh": b_prev,
                            "real_event_kwh": event_real,
                        }
                    )

    event_details.sort(key=lambda r: (r["day"], r["t"], r["lhd"], r["station"]))

    meta = {
        "events": float(n_events),
        "events_with_discount": float(n_events_with_discount),
        "events_missing_b": float(n_events_missing_b),
        "events_missing_bmax": float(n_events_missing_bmax),
        "base_energy_per_event_kwh": float(charge_intervals) * p_charger * delta_t,
        "base_energy_total_kwh": float(charge_intervals) * p_charger * delta_t * n_events,
        "sv_energy_total_kwh": sv_energy_total,
        "gap_vs_sv_kwh": sv_energy_total - total_real,
        "soc_base": soc_base,
        "charge_intervals": float(charge_intervals),
        "p_charger": p_charger,
        "delta_t": delta_t,
    }
    return total_real, meta, event_details


# -----------------------------
# M: extracción total (sumar números)
# -----------------------------
def total_extraction(m_json_path: Path) -> Tuple[float, Dict[str, int]]:
    data = load_json(m_json_path)

    def sum_numbers(obj: Any) -> Tuple[float, int]:
        s = 0.0
        n = 0
        if isinstance(obj, dict):
            for v in obj.values():
                ss, nn = sum_numbers(v)
                s += ss
                n += nn
        elif isinstance(obj, list):
            for v in obj:
                ss, nn = sum_numbers(v)
                s += ss
                n += nn
        else:
            try:
                fv = float(obj)
                s += fv
                n += 1
            except Exception:
                pass
        return s, n

    total, nvals = sum_numbers(data)
    meta = {"values_summed": nvals}
    return total, meta


# -----------------------------
# CLI principal
# -----------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Analiza outputs desde una carpeta: E (L/h), B (kWh/h) y M (extracción total). "
                    "Omite automáticamente JSON faltantes o vacíos."
    )
    ap.add_argument("folder", help="Ruta de la carpeta que contiene los JSON (búsqueda recursiva).")
    ap.add_argument(
        "--delta-minutes",
        type=float,
        default=DEFAULT_DELTA_MINUTES,
        help=f"Delta t manual (minutos) para E/B (default: {DEFAULT_DELTA_MINUTES})",
    )
    ap.add_argument("--eps", type=float, default=1e-9, help="Umbral para considerar descenso (default: 1e-9)")
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Carpeta no válida: {root}")

    e_path = find_json_in_folder(root, "E.json")
    b_path = find_json_in_folder(root, "B.json")
    m_path = find_json_in_folder(root, "M.json")

    # Tabla “inputs”
    inputs_rows = [
        ["Folder", str(root)],
        ["Delta t (min)", fmt_num(args.delta_minutes, 3)],
        ["E.json", str(e_path) if e_path else "NO ENCONTRADO"],
        ["B.json", str(b_path) if b_path else "NO ENCONTRADO"],
        ["M.json", str(m_path) if m_path else "NO ENCONTRADO"],
    ]
    print(make_table("INPUTS", ["Campo", "Valor"], inputs_rows))
    print()

    printed_any = False

    # ---- B
    if b_path and not is_effectively_empty_json(b_path):
        try:
            kwhph, s = mean_consumption_kwhph(b_path, delta_minutes=args.delta_minutes, eps=args.eps)
            consumed_swap_kwh, consumed_swap_meta = consumed_energy_swap_travel(root, eps=args.eps)

            # Energía cargada desde Sv (fórmula del modelo)
            charged_energy_kwh = None
            charged_meta: Dict[str, float] = {}
            try:
                charged_energy_kwh, charged_meta = calculate_charged_energy_from_sv(root)
            except Exception:
                charged_energy_kwh = None

            # Ascensos de B
            rise_b_kwh, rise_stats = total_rise_energy_from_b(b_path, eps=args.eps)

            # Energía cargada real por swap (base 20->100% en 6 intervalos - ajuste SOC llegada)
            real_swap_energy_kwh = None
            real_swap_meta: Dict[str, float] = {}
            real_swap_details: List[Dict[str, Any]] = []
            try:
                real_swap_energy_kwh, real_swap_meta, real_swap_details = calculate_real_charged_energy_from_swaps(
                    root,
                    eps=args.eps,
                )
            except Exception:
                real_swap_energy_kwh = None

            rows = [
                ["Promedio consumo (solo descensos)", f"{kwhph:.6f} kWh/h"],
                ["Energía consumida (B_s(t-1)-B(t), swap+viaje)", f"{consumed_swap_kwh:.6f} kWh"],
                ["Energía cargada (Sv*p_charger*delta_t)", f"{charged_energy_kwh:.6f} kWh" if charged_energy_kwh is not None else "N/D"],
                [
                    "Energía real cargada (sumatoria por batería swap)",
                    f"{real_swap_energy_kwh:.6f} kWh" if real_swap_energy_kwh is not None else "N/D",
                ],
                [
                    "Brecha vs energía Sv (Sv - real)",
                    f"{real_swap_meta.get('gap_vs_sv_kwh', 0.0):.6f} kWh" if real_swap_energy_kwh is not None else "N/D",
                ],
                [
                    "Eventos swap considerados",
                    str(int(real_swap_meta.get("events", 0.0))) if real_swap_energy_kwh is not None else "N/D",
                ],
                [
                    "Referencia energía Sv (función objetivo)",
                    f"{real_swap_meta.get('sv_energy_total_kwh', 0.0):.6f} kWh" if real_swap_energy_kwh is not None else "N/D",
                ],
                ["Energía por ascensos de B", f"{rise_b_kwh:.6f} kWh"],
                ["Pasos con ascenso (B)", str(int(rise_stats["n_rise_steps"]))],
                ["Intervalos consumo válidos (B_s,B)", str(int(consumed_swap_meta.get("n_intervals", 0.0)))],
                ["Pasos con descenso", str(int(s["n_drop_steps"]))],
                ["Pares totales evaluados", str(int(s["n_pairs_total"]))],
                ["Vehículos usados", str(int(s["n_vehicles_used"]))],
                ["Series (vehículo-día)", str(int(s["n_series"]))],
                ["Delta t usado", f"{s['delta_minutes']:.3f} min"],
            ]
            print(make_table("B (ELÉCTRICO)", ["Métrica", "Valor"], rows))
            print()

            if real_swap_details:
                det_headers = ["Estación", "LHD", "Día", "t", "SOC llegada (%)", "B llegada (kWh)", "Energía real (kWh)"]
                det_rows: List[List[Any]] = []
                for ev in real_swap_details:
                    det_rows.append([
                        ev["station"],
                        ev["lhd"],
                        int(ev["day"]),
                        int(ev["t"]),
                        f"{100.0 * float(ev['soc_arrival']):.2f}",
                        f"{float(ev['b_arrival_kwh']):.6f}",
                        f"{float(ev['real_event_kwh']):.6f}",
                    ])
                print(make_table("DETALLE SWAP: ENERGÍA REAL POR BATERÍA", det_headers, det_rows))
                print()

            # Balance energético simple
            try:
                b_frontier_delta, b_frontier_meta = calculate_b_frontier_delta(b_path)
                inv_delta, inv_meta = calculate_station_inventory_delta(root)
                residual_sv = (charged_energy_kwh if charged_energy_kwh is not None else 0.0) - consumed_swap_kwh - b_frontier_delta
                residual_real = (real_swap_energy_kwh if real_swap_energy_kwh is not None else 0.0) - consumed_swap_kwh - b_frontier_delta

                bal_rows = [
                    ["energia cargada modelo", f"{(charged_energy_kwh if charged_energy_kwh is not None else 0.0):.6f} kWh"],
                    ["E_cargada_real", f"{(real_swap_energy_kwh if real_swap_energy_kwh is not None else 0.0):.6f} kWh"],
                    ["E_consumida", f"{consumed_swap_kwh:.6f} kWh"],
                    ["Series B usadas", str(int(b_frontier_meta.get("n_series", 0.0)))],
                    ["Station-day usados", str(int(inv_meta.get("n_station_days", 0.0)))],
                ]
                print(make_table("BALANCE ENERGÉTICO", ["Término", "Valor"], bal_rows))
                print()
            except Exception as ex:
                print(make_table("BALANCE ENERGÉTICO", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
                print()

            printed_any = True
        except Exception as ex:
            print(make_table("B (ELÉCTRICO)", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
            print()
    else:
        print(make_table("B (ELÉCTRICO)", ["Estado", "Detalle"], [["OMITIDO", "No encontrado o vacío/no usable."]]))
        print()

    # ---- M
    if m_path and not is_effectively_empty_json(m_path):
        try:
            tot, meta = total_extraction(m_path)
            rows = [
                ["Extracción total (toda la operación)", f"{tot:.6f} (unidad de M)"],
                ["Valores numéricos sumados", str(meta["values_summed"])],
            ]
            print(make_table("M (EXTRACCIÓN)", ["Métrica", "Valor"], rows))
            print()
            printed_any = True
        except Exception as ex:
            print(make_table("M (EXTRACCIÓN)", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
            print()
    else:
        print(make_table("M (EXTRACCIÓN)", ["Estado", "Detalle"], [["OMITIDO", "No encontrado o vacío/no usable."]]))
        print()

    if not printed_any:
        print("No se pudo calcular nada: no encontré JSON utilizables (o todos estaban vacíos).")
    
    # ---- Cost
    try:
        costs = calculate_total_costs(root)
        if costs:
            rows = [
                ["Costo energía carga (USD)", f"{costs['energy_cost']:.2f}"],
                ["Costo energía carga real (USD)", f"{costs.get('real_energy_cost', 0.0):.2f}"],
                ["Costo inversión (USD)", f"{costs['investment_cost']:.2f}"],
                ["Costo penalidad (USD)", f"{costs['penalty_cost']:.2f}"],
                ["COSTO TOTAL (USD)", f"{costs['total_cost']:.2f}"],
            ]
            print(make_table("COSTOS", ["Concepto", "Valor"], rows))
            print()
    except Exception as ex:
        print(make_table("COSTOS", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()


# -----------------------------
# Módulo de cálculo de costos
# -----------------------------
def calculate_lhd_charge_cost(root: Path) -> float:
    """
    Calcula el costo de carga de los LHD eléctricos con battery swap.
    Usa Sv.json (baterías conectadas) y parameters.json (costo electricidad, p_charger, delta_t).
    Fórmula: sum(costo_electricidad[d,t] * Sv[k,d,t,a] * p_charger * delta_t)
    """
    sv_path = find_json_in_folder(root, "Sv.json")
    params_path = find_json_in_folder(root, "parameters.json")
    
    if not sv_path or not params_path:
        raise ValueError("No se encontraron Sv.json o parameters.json")
    
    if is_effectively_empty_json(sv_path) or is_effectively_empty_json(params_path):
        raise ValueError("Sv.json o parameters.json están vacíos")
    
    sv_data = load_json(sv_path)
    params_data = load_json(params_path)
    
    delta_t = params_data.get("delta_t")
    p_charger = params_data.get("p_charger")  # potencia del cargador
    costo_electricidad = params_data.get("costo_electricidad", {})
    scaling_factor = params_data.get("scaling_factor_op_cost")
    
    total_cost = 0.0
    
    # Estructura: Sv[k,d,t,a] = _1[station]._2[day]._3[time_t]._4[time_a]
    # costo_electricidad[d,t] = _1[day]._2[time_t]
    if "_1" in sv_data and isinstance(sv_data["_1"], dict):
        for station_id, station_data in sv_data["_1"].items():
            if "_2" in station_data and isinstance(station_data["_2"], dict):
                for day_id, day_data in station_data["_2"].items():
                    if "_3" in day_data and isinstance(day_data["_3"], dict):
                        for time_t, time_t_data in day_data["_3"].items():
                            # Obtener costo electricidad para este día y tiempo
                            cost_elec = 0.1  # valor por defecto
                            try:
                                if "_1" in costo_electricidad and day_id in costo_electricidad["_1"]:
                                    day_costs = costo_electricidad["_1"][day_id]
                                    if "_2" in day_costs and time_t in day_costs["_2"]:
                                        cost_elec = float(day_costs["_2"][time_t])
                            except (KeyError, ValueError, TypeError):
                                pass
                            
                            # Sumar sobre todos los tiempos de inicio 'a'
                            if "_4" in time_t_data and isinstance(time_t_data["_4"], dict):
                                for time_a, num_batteries in time_t_data["_4"].items():
                                    try:
                                        # Costo = costo_elec[d,t] * Sv[k,d,t,a] * p_charger * delta_t
                                        total_cost += cost_elec * float(num_batteries) * p_charger * delta_t
                                    except (ValueError, TypeError):
                                        continue
    
    return total_cost * scaling_factor


def calculate_investment_cost(root: Path) -> float:
    """
    Calcula el costo de inversión en estaciones, cargadores y baterias.
    Usa X.json (estaciones instaladas), N_chargers.json (número de cargadores),
    N_batteries.json (número de baterías) y parameters.json (costos).
    """
    x_path = find_json_in_folder(root, "X.json")
    n_chargers_path = find_json_in_folder(root, "N_chargers.json")
    n_batteries_path = find_json_in_folder(root, "N_batteries.json")
    params_path = find_json_in_folder(root, "parameters.json")
    
    if not params_path:
        raise ValueError("No se encontró parameters.json")
    
    if is_effectively_empty_json(params_path):
        raise ValueError("parameters.json está vacío")
    
    params_data = load_json(params_path)
    
    total_cost = 0.0
    
    # Costo de estaciones
    if x_path and not is_effectively_empty_json(x_path):
        x_data = load_json(x_path)
        station_costs = params_data.get("station_cost_k", {})
        
        # X.json puede tener estructura {"k": {...}} o {"_1": {...}}
        station_dict = None
        if "k" in x_data and isinstance(x_data["k"], dict):
            station_dict = x_data["k"]
        elif "_1" in x_data and isinstance(x_data["_1"], dict):
            station_dict = x_data["_1"]
        
        if station_dict:
            for station_id, value in station_dict.items():
                try:
                    if float(value) > 0.5:  # estación instalada
                        # Buscar costo de la estación
                        if "_1" in station_costs:
                            cost = station_costs["_1"].get(station_id, 0.0)
                            total_cost += float(cost)
                except (ValueError, TypeError):
                    continue
    
    # Costo de cargadores
    if n_chargers_path and not is_effectively_empty_json(n_chargers_path):
        n_chargers_data = load_json(n_chargers_path)
        charger_cost = params_data.get("charger_cost", 0.0)
        
        # N_chargers.json puede tener estructura {"k": {...}} o {"_1": {...}}
        station_dict = None
        if "k" in n_chargers_data and isinstance(n_chargers_data["k"], dict):
            station_dict = n_chargers_data["k"]
        elif "_1" in n_chargers_data and isinstance(n_chargers_data["_1"], dict):
            station_dict = n_chargers_data["_1"]
        
        if station_dict:
            for station_id, value in station_dict.items():
                try:
                    n_chargers = float(value)
                    total_cost += n_chargers * float(charger_cost)
                except (ValueError, TypeError):
                    continue
    
    # Costo de baterías
    if n_batteries_path and not is_effectively_empty_json(n_batteries_path):
        n_batteries_data = load_json(n_batteries_path)
        battery_cost = params_data.get("battery_cost", 0.0)
        
        # N_batteries.json puede tener estructura {"k": {...}} o {"_1": {...}}
        station_dict = None
        if "k" in n_batteries_data and isinstance(n_batteries_data["k"], dict):
            station_dict = n_batteries_data["k"]
        elif "_1" in n_batteries_data and isinstance(n_batteries_data["_1"], dict):
            station_dict = n_batteries_data["_1"]
        
        if station_dict:
            for station_id, value in station_dict.items():
                try:
                    n_batteries = float(value)
                    total_cost += n_batteries * float(battery_cost)
                except (ValueError, TypeError):
                    continue
    
    return total_cost   


def calculate_penalty_cost(root: Path) -> float:
    """
    Calcula el costo de penalidad por déficit de producción.
    Basado en la función F_penalty_cost de functions.py.
    F_penalty_cost = sum(F_seg[j, d, s] * (Voll / F_penalty_div[s]))
    """
    params_path = find_json_in_folder(root, "parameters.json")
    f_seg_path = find_json_in_folder(root, "F_seg.json")
    
    if not params_path or is_effectively_empty_json(params_path):
        return 0.0
    
    if not f_seg_path or is_effectively_empty_json(f_seg_path):
        return 0.0
    
    params_data = load_json(params_path)
    f_seg_data = load_json(f_seg_path)
    
    voll = float(params_data.get("Voll"))
    f_penalty_div = params_data.get("F_penalty_div", {})
    scaling_factor = float(params_data.get("scaling_factor_op_cost"))
    
    total_penalty = 0.0
    
    # F_seg.json tiene estructura: {"_1": {"nodo": {"_2": {"día": {"_3": {"segmento": valor}}}}}}
    if "_1" in f_seg_data and isinstance(f_seg_data["_1"], dict):
        for node_id, node_data in f_seg_data["_1"].items():
            if "_2" in node_data and isinstance(node_data["_2"], dict):
                for day_id, day_data in node_data["_2"].items():
                    if "_3" in day_data and isinstance(day_data["_3"], dict):
                        for seg_id, seg_value in day_data["_3"].items():
                            try:
                                # Obtener divisor de penalidad para este segmento
                                if "_1" in f_penalty_div:
                                    divisor = float(f_penalty_div["_1"].get(seg_id, 1.0))
                                else:
                                    divisor = 1.0
                                
                                penalty = float(seg_value) * (voll / divisor)
                                total_penalty += penalty
                            except (ValueError, TypeError, ZeroDivisionError):
                                continue
    
    return total_penalty * scaling_factor


def calculate_total_costs(root: Path) -> Dict[str, float]:
    """
    Calcula todos los costos del sistema:
    - Costo de energía (lhd_charge_cost)
    - Costo de inversión (inversion_cost)
    - Costo de penalidad (F_penalty_cost)
    - Costo total
    """
    try:
        energy_cost = calculate_lhd_charge_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de energía: {ex}")
        energy_cost = 0.0

    # Costo de energía "real":
    # - Si costo es fijo: E_real * costo_fijo * scaling_factor_op_cost
    # - Si costo es variable: energy_cost - (delta_E * costo_min * scaling_factor_op_cost)
    #   asumiendo que el delta de energía se habría cargado al menor costo.
    real_energy_cost = 0.0
    try:
        real_swap_energy_kwh, real_swap_meta, _ = calculate_real_charged_energy_from_swaps(root)
        sv_energy_total_kwh = float(real_swap_meta.get("sv_energy_total_kwh", 0.0))

        params_path = find_json_in_folder(root, "parameters.json")
        if not params_path or is_effectively_empty_json(params_path):
            raise ValueError("No se encontró parameters.json para costo fijo")

        params_data = load_json(params_path)
        scaling_factor = float(params_data.get("scaling_factor_op_cost", 1.0))

        costo_electricidad = params_data.get("costo_electricidad", {})
        fixed_candidates: List[float] = []
        if isinstance(costo_electricidad, dict) and isinstance(costo_electricidad.get("_1"), dict):
            for day_data in costo_electricidad["_1"].values():
                if not isinstance(day_data, dict):
                    continue
                t_map = day_data.get("_2", {})
                if not isinstance(t_map, dict):
                    continue
                for v in t_map.values():
                    try:
                        fixed_candidates.append(float(v))
                    except Exception:
                        continue

        if not fixed_candidates:
            raise ValueError("No hay valores en costo_electricidad para costo fijo")

        c_min = min(fixed_candidates)
        c_max = max(fixed_candidates)
        is_fixed_cost = abs(c_max - c_min) <= 1e-12

        if is_fixed_cost:
            real_energy_cost = float(real_swap_energy_kwh) * c_min * scaling_factor
        else:
            delta_energy_kwh = sv_energy_total_kwh - float(real_swap_energy_kwh)
            real_energy_cost = max(0.0, float(energy_cost) - delta_energy_kwh * c_min * scaling_factor)
    except Exception as ex:
        print(f"Advertencia al calcular costo de energía real: {ex}")
        real_energy_cost = 0.0
    
    try:
        investment_cost = calculate_investment_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de inversión: {ex}")
        investment_cost = 0.0
    
    try:
        penalty_cost = calculate_penalty_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de penalidad: {ex}")
        penalty_cost = 0.0
    
    total_cost = investment_cost + energy_cost + penalty_cost
    
    return {
        "energy_cost": energy_cost,
        "real_energy_cost": real_energy_cost,
        "investment_cost": investment_cost,
        "penalty_cost": penalty_cost,
        "total_cost": total_cost,
    }


if __name__ == "__main__":
    main()
