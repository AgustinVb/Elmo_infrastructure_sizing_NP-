#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
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


def _iter_leaf_records(node: Any, path: Tuple[str, ...] = ()):
    """Yield (path, scalar_value) pairs for every leaf in a nested JSON tree."""
    if isinstance(node, dict):
        if not node:
            return
        if all(not isinstance(v, dict) for v in node.values()):
            for key, value in node.items():
                yield path + (str(key),), value
            return
        for key, child in node.items():
            yield from _iter_leaf_records(child, path + (str(key),))
    elif isinstance(node, list):
        for idx, child in enumerate(node):
            yield from _iter_leaf_records(child, path + (str(idx),))
    else:
        yield path, node


def _axis_map_from_path(path: Tuple[str, ...]) -> Dict[str, str]:
    """Convert alternating axis-name / axis-value tokens into a dict."""
    tokens = [str(token) for token in path]
    axis_map: Dict[str, str] = {}
    for idx in range(0, len(tokens) - 1, 2):
        axis_map[tokens[idx]] = tokens[idx + 1]
    return axis_map


def _unwrap_named_tree(node: Any) -> Any:
    """Return the most useful nested dict level for legacy parameter/variable JSON wrappers."""
    if not isinstance(node, dict):
        return node
    for key in ("i", "k", "b", "d", "_1"):
        value = node.get(key)
        if isinstance(value, dict):
            return value
    if len(node) == 1:
        first_value = next(iter(node.values()))
        if isinstance(first_value, dict):
            return first_value
    return node


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

    def _extract_series_map(data: Any, lhd_axis: str) -> Dict[str, Dict[int, Dict[int, float]]]:
        out: Dict[str, Dict[int, Dict[int, float]]] = {}
        if not isinstance(data, dict):
            return out

        for path, value in _iter_leaf_records(data):
            axis_map = _axis_map_from_path(path)
            lhd = axis_map.get(lhd_axis) or axis_map.get("i") or axis_map.get("b") or axis_map.get("_1")
            day_key = axis_map.get("d") or axis_map.get("day") or axis_map.get("_2")
            time_key = axis_map.get("t") or axis_map.get("time") or axis_map.get("_3")
            if lhd is None or day_key is None or time_key is None:
                continue
            try:
                day = int(float(day_key))
                t = int(float(time_key))
                out.setdefault(str(lhd), {}).setdefault(day, {})[t] = float(value)
            except Exception:
                continue

        return out

    b_map = _extract_series_map(b_data, "i")
    bs_map = _extract_series_map(bs_data, "b")

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
        for path, value in _iter_leaf_records(obj):
            axis_map = _axis_map_from_path(path)
            station = axis_map.get("k") or axis_map.get("station") or axis_map.get("i") or axis_map.get("b")
            day_key = axis_map.get("d") or axis_map.get("day")
            time_key = axis_map.get("t") or axis_map.get("time")
            if station is None or day_key is None or time_key is None:
                continue
            try:
                day = int(float(day_key))
                time_index = int(float(time_key))
                out.setdefault((str(station), day), {})[time_index] = float(value)
            except Exception:
                continue
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

    sv_sum = 0.0
    n_terms = 0
    for _, value in _iter_leaf_records(sv_data):
        try:
            sv_sum += float(value)
            n_terms += 1
        except Exception:
            continue

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
            t_swap_raw = _unwrap_named_tree(params_data.get("t_swap", {}))
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
        bmin_raw = _unwrap_named_tree(params_data.get("bmin_b", {}))
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
    bmax_raw = params_data.get("bmax_b", {})

    # Mapa B[i][d][t] -> nivel de energía (kWh)
    b_map: Dict[str, Dict[int, Dict[int, float]]] = {}
    for path, b_val in _iter_leaf_records(b_data):
        axis_map = _axis_map_from_path(path)
        lhd = axis_map.get("i") or axis_map.get("lhd") or axis_map.get("_1")
        day_key = axis_map.get("d") or axis_map.get("day") or axis_map.get("_2")
        time_key = axis_map.get("t") or axis_map.get("time") or axis_map.get("_3")
        if lhd is None or day_key is None or time_key is None:
            continue
        try:
            day = int(float(day_key))
            t = int(float(time_key))
            b_map.setdefault(str(lhd), {}).setdefault(day, {})[t] = float(b_val)
        except Exception:
            continue

    total_real = 0.0
    n_events = 0
    n_events_with_discount = 0
    n_events_missing_b = 0
    n_events_missing_bmax = 0
    soc_arrival_sum = 0.0
    soc_arrival_min: Optional[float] = None
    soc_arrival_max: Optional[float] = None
    event_details: List[Dict[str, Any]] = []

    bmax_map: Dict[str, float] = {}
    for path, bmax_val in _iter_leaf_records(bmax_raw):
        axis_map = _axis_map_from_path(path)
        station = axis_map.get("b") or axis_map.get("i") or axis_map.get("_1")
        if station is None:
            continue
        try:
            bmax_map[str(station)] = float(bmax_val)
        except Exception:
            continue

    # Z_swap[k,i,d,t] = 0/1, soportando ambos formatos de exportación.
    for path, z_val in _iter_leaf_records(z_data):
        axis_map = _axis_map_from_path(path)
        station = axis_map.get("k") or axis_map.get("station") or axis_map.get("_1")
        lhd = axis_map.get("i") or axis_map.get("lhd") or axis_map.get("_2")
        day_key = axis_map.get("d") or axis_map.get("day") or axis_map.get("_3")
        time_key = axis_map.get("t") or axis_map.get("time") or axis_map.get("_4")
        if station is None or lhd is None or day_key is None or time_key is None:
            continue

        try:
            zval = float(z_val)
        except Exception:
            continue
        if zval <= 0.5 + eps:
            continue

        n_events += 1

        bmax_i = bmax_map.get(str(lhd))
        if bmax_i is None or bmax_i <= 0:
            n_events_missing_bmax += 1
            continue

        try:
            day = int(float(day_key))
            t = int(float(time_key))
        except Exception:
            continue

        series = b_map.get(str(lhd), {}).get(day, {})
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
        soc_arrival_sum += soc_arrival
        soc_arrival_min = soc_arrival if soc_arrival_min is None else min(soc_arrival_min, soc_arrival)
        soc_arrival_max = soc_arrival if soc_arrival_max is None else max(soc_arrival_max, soc_arrival)

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
        "soc_arrival_avg": (soc_arrival_sum / n_events) if n_events else 0.0,
        "soc_arrival_min": soc_arrival_min if soc_arrival_min is not None else 0.0,
        "soc_arrival_max": soc_arrival_max if soc_arrival_max is not None else 0.0,
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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def calculate_daily_trips(y_json_path: Path) -> Dict[str, float]:
    """Cuenta viajes diarios como activaciones positivas de Y por día."""
    data = load_json(y_json_path)
    trips_by_day: Dict[str, float] = {}

    for path, value in _iter_leaf_records(data):
        axis_map = _axis_map_from_path(path)
        day_key = axis_map.get("d") or axis_map.get("day") or axis_map.get("_3")
        if day_key is None:
            continue
        if _as_float(value, 0.0) > 0.5:
            day = str(day_key)
            trips_by_day[day] = trips_by_day.get(day, 0.0) + 1.0

    return trips_by_day


def _extract_y_counts(y_json_path: Path) -> Dict[Tuple[str, str, str], float]:
    """Retorna sum_t Y[i,j,d,t] por clave (i,j,d)."""
    data = load_json(y_json_path)
    y_counts: Dict[Tuple[str, str, str], float] = {}

    for path, value in _iter_leaf_records(data):
        axis_map = _axis_map_from_path(path)
        i_name = axis_map.get("i") or axis_map.get("_1")
        j_name = axis_map.get("j") or axis_map.get("_2")
        day_key = axis_map.get("d") or axis_map.get("day") or axis_map.get("_3")
        if i_name is None or j_name is None or day_key is None:
            continue
        key = (str(i_name), str(j_name), str(day_key))
        y_counts[key] = y_counts.get(key, 0.0) + _as_float(value, 0.0)

    return y_counts


def _extract_m_values(m_json_path: Path) -> Dict[Tuple[str, str, str], float]:
    """Retorna M[i,j,d] por clave (i,j,d)."""
    data = load_json(m_json_path)
    m_values: Dict[Tuple[str, str, str], float] = {}

    for path, value in _iter_leaf_records(data):
        axis_map = _axis_map_from_path(path)
        i_name = axis_map.get("b") or axis_map.get("i") or axis_map.get("_1")
        j_name = axis_map.get("j") or axis_map.get("_2")
        day_key = axis_map.get("d") or axis_map.get("day") or axis_map.get("_3")
        if i_name is None or j_name is None or day_key is None:
            continue
        m_values[(str(i_name), str(j_name), str(day_key))] = _as_float(value, 0.0)

    return m_values


def calculate_cycles_from_y_ntrips(y_json_path: Path, m_json_path: Path, params_path: Path) -> Tuple[float, Dict[str, float]]:
    """Calcula ciclos con cycles_total = sum(Y[i,j] * n_trips[j,i]).

    n_trips[j,i] se reconstruye desde outputs usando:
    M[i,j,d] = (sum_t Y[i,j,d,t]) * g_i[i] * filling_factor[i] * n_trips[j,i]
    """
    y_counts = _extract_y_counts(y_json_path)
    m_values = _extract_m_values(m_json_path)
    params = load_json(params_path)

    g_i = _unwrap_named_tree(params.get("g_i", {})) if isinstance(params, dict) else {}
    filling = _unwrap_named_tree(params.get("filling_factor", {})) if isinstance(params, dict) else {}

    ntr_num: Dict[Tuple[str, str], float] = {}
    ntr_den: Dict[Tuple[str, str], float] = {}

    for (i_name, j_name, day_key), y_sum in y_counts.items():
        if y_sum <= 0.0:
            continue
        g = _as_float(g_i.get(i_name, 0.0), 0.0)
        f = _as_float(filling.get(i_name, 0.0), 0.0)
        if g <= 0.0 or f <= 0.0:
            continue

        m_val = _as_float(m_values.get((i_name, j_name, day_key), 0.0), 0.0)
        ntr_day = m_val / (y_sum * g * f)
        pair = (i_name, j_name)
        ntr_num[pair] = ntr_num.get(pair, 0.0) + ntr_day * y_sum
        ntr_den[pair] = ntr_den.get(pair, 0.0) + y_sum

    n_trips_map: Dict[Tuple[str, str], float] = {}
    for pair, den in ntr_den.items():
        if den > 0.0:
            n_trips_map[pair] = ntr_num[pair] / den

    cycles_total = 0.0
    cycles_by_day: Dict[str, float] = {}
    for (i_name, j_name, day_key), y_sum in y_counts.items():
        ntr = n_trips_map.get((i_name, j_name), 0.0)
        cycles = y_sum * ntr
        cycles_total += cycles
        cycles_by_day[day_key] = cycles_by_day.get(day_key, 0.0) + cycles

    return cycles_total, cycles_by_day


def calculate_daily_charged_energy(root: Path) -> Dict[str, float]:
    """Suma energía diaria [kWh] desde P (on-board) o Sv (swap)."""
    params_path = find_json_in_folder(root, "parameters.json")
    if not params_path or is_effectively_empty_json(params_path):
        return {}

    params = load_json(params_path)
    dt = _as_float(params.get("delta_t", params.get("dt", 0.0)), 0.0)
    if dt <= 0.0:
        return {}

    # Swap: Sv[k,d,t,a] -> energia = sum_a Sv * p_charger * dt
    sv_path = find_json_in_folder(root, "Sv.json")
    if sv_path and not is_effectively_empty_json(sv_path):
        p_charger = _as_float(params.get("p_charger", 0.0), 0.0)
        out: Dict[str, float] = {}
        sv_data = load_json(sv_path)
        for path, sv_val in _iter_leaf_records(sv_data):
            axis_map = _axis_map_from_path(path)
            day_key = axis_map.get("d") or axis_map.get("day") or axis_map.get("_2")
            if day_key is None:
                continue
            day = str(day_key)
            out.setdefault(day, 0.0)
            out[day] += _as_float(sv_val, 0.0) * p_charger * dt
        return out

    # On-board: P[k,i,d,t] -> energia = sum P * dt
    p_path = find_json_in_folder(root, "P.json")
    if p_path and not is_effectively_empty_json(p_path):
        out: Dict[str, float] = {}
        p_data = load_json(p_path)
        if "k" in p_data and isinstance(p_data["k"], dict):
            for station_data in p_data["k"].values():
                i_block = station_data.get("i", {}) if isinstance(station_data, dict) else {}
                if not isinstance(i_block, dict):
                    continue
                for lhd_data in i_block.values():
                    d_block = lhd_data.get("d", {}) if isinstance(lhd_data, dict) else {}
                    if not isinstance(d_block, dict):
                        continue
                    for day_key, day_data in d_block.items():
                        t_block = day_data.get("t", {}) if isinstance(day_data, dict) else {}
                        if not isinstance(t_block, dict):
                            continue
                        day = str(day_key)
                        out.setdefault(day, 0.0)
                        for power in t_block.values():
                            out[day] += _as_float(power, 0.0) * dt
        return out

    return {}


# -----------------------------
# Soporte para problema desacoplado por macrobloque
# -----------------------------
def find_macrobloque_subfolders(root: Path) -> List[Path]:
    """Detecta subcarpetas inmediatas de `root` que contienen su propio
    parameters.json (cada una es la salida completa de un macrobloque/estacion
    resuelto por separado, ej. output/MB_test/station_1, station_2, station_3).

    Devuelve [] si `root` ya tiene parameters.json directamente (carpeta de
    una sola corrida, no desacoplada).
    """
    if (root / "parameters.json").exists():
        return []

    subfolders = []
    for child in sorted(root.iterdir()):
        if child.name == "combined" or child.name.endswith("_stage1"):
            continue
        if child.is_dir() and (child / "parameters.json").exists():
            subfolders.append(child)
    return subfolders


def _peak_clock_interval_set(delta_t: float, max_t: int, start_hour: int = 9,
                              windows=(("18:00", "22:00"),)) -> set:
    """Replica _build_intervals_from_clock_windows de functions.py: convierte
    ventanas horarias (HH:MM) a indices de intervalo, dado que el horizonte
    operativo arranca en `start_hour` (09:00 por defecto)."""
    dt_minutes = int(round(delta_t * 60))
    if dt_minutes <= 0:
        return set()
    base_minutes = start_hour * 60

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
    return out


def _year_of_day(day: int) -> int:
    return ((int(day) - 1) // 365) + 1


_STATION_DAY_RE = re.compile(r"^(.+)_d(\d+)$")


def group_by_station(subfolders: List[Path]) -> Optional[Dict[str, List[Path]]]:
    """Si las subcarpetas siguen el patron <estacion>_d<dia> (salida de
    run_macrobloques_decomposicion.py --parallel_days), agrupa por estacion.
    Devuelve None si alguna subcarpeta no sigue ese patron."""
    groups: Dict[str, List[Path]] = {}
    for sub in subfolders:
        m = _STATION_DAY_RE.match(sub.name)
        if not m:
            return None
        station = m.group(1)
        groups.setdefault(station, []).append(sub)
    return groups


def read_total_station_var(
    subfolders: List[Path],
    var_name: str,
    station_groups: Optional[Dict[str, List[Path]]],
    is_day_decomposed: bool,
) -> float:
    """Suma una variable de infraestructura por estacion (N_chargers, N_bays,
    N_batteries) entre todas las estaciones.

    Es infraestructura fija por estacion (no depende del dia), asi que si hay
    descomposicion por dia se cuenta una sola vez por estacion (se usa la
    primera subcarpeta-dia disponible de cada una)."""
    reps = [subs[0] for subs in station_groups.values()] if (is_day_decomposed and station_groups) else subfolders

    total = 0.0
    for sub in reps:
        var_path = find_json_in_folder(sub, f"{var_name}.json")
        if not var_path or is_effectively_empty_json(var_path):
            continue
        inner = load_json(var_path)
        # Distintas variables serializan su (unico) indice bajo distinta
        # clave ("k" para N_chargers/N_batteries, "_1" para N_bays) -- se
        # despega solo ese primer nivel (no en loop: el dict resultante
        # station_name->valor puede legitimamente tener una sola entrada,
        # por ejemplo en una subcarpeta de una sola estacion).
        if isinstance(inner, dict) and len(inner) == 1:
            inner = next(iter(inner.values()))
        if not isinstance(inner, dict):
            continue
        total += sum(_as_float(v, 0.0) for v in inner.values())
    return total


def read_scaling_factor_op_cost(subfolders: List[Path]) -> Optional[float]:
    """Lee scaling_factor_op_cost (365 / dias significativos) desde el primer
    parameters.json disponible; es el mismo valor para todas las subcarpetas
    de una misma corrida (ver `run_macrobloques_decomposicion.solve_macrobloque_day`,
    que lo fija explicitamente a 365/total_n_days en cada subproblema)."""
    for sub in subfolders:
        params_path = find_json_in_folder(sub, "parameters.json")
        if params_path and not is_effectively_empty_json(params_path):
            params_data = load_json(params_path)
            val = params_data.get("scaling_factor_op_cost")
            if val is not None:
                return _as_float(val, None)
    return None


def calculate_peak_charging_power(subfolders: List[Path]) -> Tuple[float, Dict[str, float]]:
    """Potencia pico de carga = max_intervalo(sum_k_a Sv[k,d,t,a]) * p_charger.

    Combina los Sv.json de todas las subcarpetas (estaciones) para el mismo
    (d,t) antes de buscar el maximo, ya que las estaciones cargan en paralelo.
    """
    combined: Dict[Tuple[int, int], float] = {}
    p_charger = None

    for sub in subfolders:
        sv_path = find_json_in_folder(sub, "Sv.json")
        params_path = find_json_in_folder(sub, "parameters.json")
        if not sv_path or not params_path:
            continue
        if is_effectively_empty_json(sv_path) or is_effectively_empty_json(params_path):
            continue

        if p_charger is None:
            params_data = load_json(params_path)
            p_charger = _as_float(params_data.get("p_charger", 0.0))

        sv_data = load_json(sv_path)
        for path, sv_val in _iter_leaf_records(sv_data):
            axis_map = _axis_map_from_path(path)
            day_key = axis_map.get("d") or axis_map.get("day")
            time_key = axis_map.get("t") or axis_map.get("time")
            if day_key is None or time_key is None:
                continue
            try:
                d = int(float(day_key))
                t = int(float(time_key))
                v = float(sv_val)
            except Exception:
                continue
            key = (d, t)
            combined[key] = combined.get(key, 0.0) + v

    if not combined or not p_charger:
        return 0.0, {"note": "No se pudo calcular (faltan Sv.json o p_charger)"}

    max_sv = max(combined.values())
    peak_power_kw = max_sv * p_charger
    best_key = max(combined, key=lambda k: combined[k])
    meta = {
        "max_baterias_cargando": max_sv,
        "p_charger_kw": p_charger,
        "peak_power_kw": peak_power_kw,
        "dia_pico": best_key[0],
        "intervalo_pico": best_key[1],
    }
    return peak_power_kw, meta


def calculate_combined_peak_power_cost(subfolders: List[Path]) -> Tuple[float, Dict[str, float]]:
    """Costo por potencia pico 'ex post', calculado con la demanda REAL
    combinada (suma de P_red de todos los macrobloques en cada dia/intervalo),
    en vez de sumar el P_pot de cada macrobloque por separado (que sale 0
    porque cada uno ve solo su propia demanda aislada).

    Replica la logica de power_peak_limit (functions.py): solo cuenta
    demanda en dias de temporada de punta (91<=d<=244) y en el rango horario
    18:00-22:00 (con el horizonte operativo arrancando a las 09:00).
    """
    combined: Dict[int, Dict[int, float]] = {}
    delta_t = None
    demand_charge_coef = 12 * 10

    for sub in subfolders:
        pred_path = find_json_in_folder(sub, "P_red.json")
        params_path = find_json_in_folder(sub, "parameters.json")
        if not pred_path or not params_path:
            continue
        if is_effectively_empty_json(pred_path) or is_effectively_empty_json(params_path):
            continue

        params_data = load_json(params_path)
        if delta_t is None:
            delta_t = _as_float(params_data.get("delta_t", 0.0))
            try:
                demand_charge_coef = float(params_data.get("demand_charge_coef", 12 * 10))
            except Exception:
                demand_charge_coef = 12 * 10

        pred_data = load_json(pred_path)
        for path, value in _iter_leaf_records(pred_data):
            axis_map = _axis_map_from_path(path)
            day_key = axis_map.get("d") or axis_map.get("day")
            time_key = axis_map.get("t") or axis_map.get("time")
            if day_key is None or time_key is None:
                continue
            try:
                day = int(float(day_key))
                t = int(float(time_key))
                v = float(value)
            except Exception:
                continue
            combined.setdefault(day, {})[t] = combined.get(day, {}).get(t, 0.0) + v

    if not combined or not delta_t:
        return 0.0, {"note": "No se pudo calcular (faltan P_red.json/parameters.json)"}

    max_t = max(t for day_map in combined.values() for t in day_map)
    peak_t_set = _peak_clock_interval_set(delta_t, max_t)

    p_pot_ex_post: Dict[int, float] = {}
    for day, t_map in combined.items():
        if not (91 <= day <= 244):
            continue
        year = _year_of_day(day)
        for t, v in t_map.items():
            if t not in peak_t_set:
                continue
            p_pot_ex_post[year] = max(p_pot_ex_post.get(year, 0.0), v)

    total_cost = sum(v * demand_charge_coef for v in p_pot_ex_post.values())
    meta = {f"P_pot_ex_post_year_{y}": v for y, v in p_pot_ex_post.items()}
    meta["demand_charge_coef"] = demand_charge_coef
    return total_cost, meta


def analyze_macrobloques(root: Path, subfolders: List[Path], args) -> None:
    """Corre el analisis de costos/metricas de cada macrobloque por separado
    y agrega (suma) los resultados, ya que cada subcarpeta es un subproblema
    independiente (mismo p_peak global, pero resuelto sin acoplar estaciones).
    """
    summary_only = getattr(args, "summary_only", False)
    station_groups = group_by_station(subfolders)
    is_day_decomposed = bool(station_groups) and any(len(v) > 1 for v in station_groups.values())

    header_rows = [["Carpeta raiz", str(root)]]
    if is_day_decomposed:
        header_rows.append(["Macrobloques (estaciones)", str(len(station_groups))])
        header_rows.append(["Subproblemas (estacion x dia)", str(len(subfolders))])
    else:
        header_rows.append(["Macrobloques detectados", str(len(subfolders))])
    if not summary_only:
        header_rows.append(["Subcarpetas", ", ".join(p.name for p in subfolders)])
    print(make_table("MODO DESACOPLADO", ["Campo", "Valor"], header_rows))
    print()

    per_station_costs: Dict[str, Dict[str, float]] = {}
    per_station_extraction: Dict[str, float] = {}
    per_station_energy: Dict[str, float] = {}
    per_station_consumed: Dict[str, float] = {}
    per_station_real_charged: Dict[str, float] = {}

    for sub in subfolders:
        if not summary_only:
            print("=" * 70)
            print(f"SUBPROBLEMA: {sub.name}")
            print("=" * 70)
        try:
            costs = calculate_total_costs(sub)
        except Exception as ex:
            costs = {}
            if not summary_only:
                print(f"  (No se pudieron calcular costos para {sub.name}: {ex})")
        per_station_costs[sub.name] = costs

        m_path = find_json_in_folder(sub, "M.json")
        if m_path and not is_effectively_empty_json(m_path):
            try:
                tot, _ = total_extraction(m_path)
                per_station_extraction[sub.name] = tot
            except Exception:
                per_station_extraction[sub.name] = 0.0
        else:
            per_station_extraction[sub.name] = 0.0

        try:
            energy_daily = calculate_daily_charged_energy(sub)
            per_station_energy[sub.name] = sum(energy_daily.values())
        except Exception:
            per_station_energy[sub.name] = 0.0

        try:
            consumed_kwh, _ = consumed_energy_swap_travel(sub)
            per_station_consumed[sub.name] = consumed_kwh
        except Exception:
            per_station_consumed[sub.name] = 0.0

        try:
            real_charged_kwh, _, _ = calculate_real_charged_energy_from_swaps(sub)
            per_station_real_charged[sub.name] = real_charged_kwh
        except Exception:
            per_station_real_charged[sub.name] = 0.0

        if not summary_only:
            if costs:
                rows = [[k, f"{v:.2f}"] for k, v in costs.items()]
                print(make_table(f"COSTOS ({sub.name})", ["Concepto", "Valor"], rows))
            print(f"Extraccion total ({sub.name}): {per_station_extraction[sub.name]:.2f}")
            print(f"Energia cargada total ({sub.name}): {per_station_energy[sub.name]:.2f} kWh")
            print()

    cost_keys: List[str] = []
    for costs in per_station_costs.values():
        for k in costs:
            if k not in cost_keys:
                cost_keys.append(k)

    if not summary_only:
        rows = []
        for k in cost_keys:
            row = [k] + [f"{per_station_costs[s.name].get(k, 0.0):.2f}" for s in subfolders]
            row.append(f"{sum(per_station_costs[s.name].get(k, 0.0) for s in subfolders):.2f}")
            rows.append(row)

        headers = ["Concepto"] + [s.name for s in subfolders] + ["TOTAL"]
        print(make_table("COSTOS COMBINADOS (suma ingenua de macrobloques)", headers, rows))
        print()

    # El peak_power_cost de cada macrobloque sale 0 porque cada uno ve solo
    # su propia demanda aislada. El valor real (ex post) se calcula con la
    # demanda combinada de los 3 macrobloques en el mismo dia/intervalo.
    combined_peak_cost, peak_meta = calculate_combined_peak_power_cost(subfolders)
    peak_charging_kw, peak_charging_meta = calculate_peak_charging_power(subfolders)
    naive_total = sum(per_station_costs[s.name].get("total_cost", 0.0) for s in subfolders)
    naive_peak = sum(per_station_costs[s.name].get("peak_power_cost", 0.0) for s in subfolders)
    corrected_total = naive_total - naive_peak + combined_peak_cost

    if not summary_only:
        peak_rows = [[k, f"{v:.2f}" if isinstance(v, (int, float)) else str(v)] for k, v in peak_meta.items()]
        peak_rows.append(["Costo potencia pico ex post (combinado)", f"{combined_peak_cost:.2f}"])
        peak_rows.append(["Costo potencia pico (suma ingenua de macrobloques)", f"{naive_peak:.2f}"])
        peak_rows.append(["COSTO TOTAL (suma ingenua)", f"{naive_total:.2f}"])
        peak_rows.append(["COSTO TOTAL (corregido con peak ex post)", f"{corrected_total:.2f}"])
        print(make_table("COSTO POTENCIA PICO EX POST Y TOTAL CORREGIDO", ["Concepto", "Valor"], peak_rows))
        print()

    # Si las subcarpetas vienen de --parallel_days (<estacion>_d<dia>), cada
    # (estacion,dia) re-incluye el costo de inversion COMPLETO (es CAPEX, no
    # depende del dia) -> sumarlo por cada dia lo infla N veces (N = dias por
    # estacion). Hay que contarlo una sola vez por estacion.
    if is_day_decomposed:
        # Solo los componentes que realmente entran en total_cost (ver
        # calculate_total_costs): energy_cost/real_energy_cost son medidas
        # diagnosticas alternativas, no se suman al total.
        opex_keys = ("grid_energy_cost", "penalty_cost", "gen_op_cost", "bess_op_cost")

        capex_total = 0.0
        opex_total = 0.0
        real_energy_cost_total = 0.0
        dedup_rows = []
        for station, subs_for_station in station_groups.items():
            capex_value = per_station_costs[subs_for_station[0].name].get("investment_cost", 0.0) \
                + per_station_costs[subs_for_station[0].name].get("gen_inv_cost", 0.0) \
                + per_station_costs[subs_for_station[0].name].get("bess_inv_cost", 0.0)
            opex_value = sum(
                per_station_costs[s.name].get(k, 0.0)
                for s in subs_for_station for k in opex_keys
            )
            real_ec_value = sum(
                per_station_costs[s.name].get("real_energy_cost", 0.0)
                for s in subs_for_station
            )
            capex_total += capex_value
            opex_total += opex_value
            real_energy_cost_total += real_ec_value
            dedup_rows.append([station, str(len(subs_for_station)), f"{capex_value:.2f}", f"{opex_value:.2f}", f"{real_ec_value:.2f}"])

        if not summary_only:
            dedup_rows_display = dedup_rows + [["TOTAL", str(len(subfolders)), f"{capex_total:.2f}", f"{opex_total:.2f}", f"{real_energy_cost_total:.2f}"]]
            print(make_table(
                "COSTOS DEDUPLICADOS POR ESTACION (inversion contada 1 vez, no por dia)",
                ["Estacion", "N dias", "Inversion (1 vez)", "Operacion (suma dias)", "Costo carga real (suma dias)"],
                dedup_rows_display,
            ))
            print()

        dedup_total = capex_total + opex_total + combined_peak_cost
        real_total_with_real_ec = capex_total + real_energy_cost_total + sum(
            per_station_costs[s.name].get(k, 0.0)
            for s in subfolders for k in ("penalty_cost", "gen_op_cost", "bess_op_cost")
        ) + combined_peak_cost
        print(make_table(
            "COSTO TOTAL FINAL (deduplicado, 3 estaciones, todos los dias)",
            ["Concepto", "Valor"],
            [
                ["Inversion (1 vez por estacion)", f"{capex_total:.2f}"],
                ["Operacion (suma de todos los dias)", f"{opex_total:.2f}"],
                ["Potencia pico ex post (combinado)", f"{combined_peak_cost:.2f}"],
                ["COSTO TOTAL", f"{dedup_total:.2f}"],
                ["--- alternativo (costo carga real) ---", ""],
                ["Costo carga real (bmax-B_llegada, suma dias)", f"{real_energy_cost_total:.2f}"],
                ["COSTO TOTAL (con costo carga real)", f"{real_total_with_real_ec:.2f}"],
            ],
        ))
        print()
    elif summary_only:
        # Carpeta plana (station_1/2/3, sin dia): el total corregido ya no
        # tiene el problema de duplicar inversion por dia.
        real_energy_cost_total = sum(
            per_station_costs[s.name].get("real_energy_cost", 0.0) for s in subfolders
        )
        print(make_table(
            "COSTO TOTAL FINAL (3 estaciones)",
            ["Concepto", "Valor"],
            [
                ["Potencia pico ex post (combinado)", f"{combined_peak_cost:.2f}"],
                ["COSTO TOTAL", f"{corrected_total:.2f}"],
                ["--- alternativo (costo carga real) ---", ""],
                ["Costo carga real (bmax-B_llegada)", f"{real_energy_cost_total:.2f}"],
            ],
        ))
        print()

    extraction_total = sum(per_station_extraction.values())
    energy_total = sum(per_station_energy.values())
    consumed_total = sum(per_station_consumed.values())
    real_charged_total = sum(per_station_real_charged.values())
    peak_charging_str = (
        f"{peak_charging_kw:.2f} kW  "
        f"(max {peak_charging_meta.get('max_baterias_cargando', 0):.0f} bat x "
        f"{peak_charging_meta.get('p_charger_kw', 0):.0f} kW, "
        f"dia {peak_charging_meta.get('dia_pico', '?')} t={peak_charging_meta.get('intervalo_pico', '?')})"
    )

    total_n_chargers = read_total_station_var(subfolders, "N_chargers", station_groups, is_day_decomposed)
    total_n_bays = read_total_station_var(subfolders, "N_bays", station_groups, is_day_decomposed)
    total_n_batteries = read_total_station_var(subfolders, "N_batteries", station_groups, is_day_decomposed)
    scaling_factor = read_scaling_factor_op_cost(subfolders)
    extraction_annual_str = f"{extraction_total * scaling_factor:.2f}" if scaling_factor else "N/D"
    energy_annual_str = f"{energy_total * scaling_factor:.2f}" if scaling_factor else "N/D"
    consumed_annual_str = f"{consumed_total * scaling_factor:.2f}" if scaling_factor else "N/D"

    if summary_only:
        print(make_table(
            "METRICAS COMBINADAS (3 estaciones, todos los dias)",
            ["Concepto", "Valor"],
            [
                ["Extraccion total combinada (dias significativos)", f"{extraction_total:.2f}"],
                ["Extraccion total anualizada", extraction_annual_str],
                ["Energia cargada total combinada (Sv) [kWh] (dias significativos)", f"{energy_total:.2f}"],
                ["Energia cargada total anualizada [kWh]", energy_annual_str],
                ["Energia cargada real (bmax-B_llegada) [kWh]", f"{real_charged_total:.2f}"],
                ["Energia consumida por vehiculos (B_s-B) [kWh] (dias significativos)", f"{consumed_total:.2f}"],
                ["Energia consumida por vehiculos anualizada [kWh]", consumed_annual_str],
                ["Potencia pico de carga (combinada)", peak_charging_str],
                ["Cargadores totales (todas las estaciones)", f"{total_n_chargers:.0f}"],
                ["Bahias totales (todas las estaciones)", f"{total_n_bays:.0f}"],
                ["Baterias totales (todas las estaciones)", f"{total_n_batteries:.0f}"],
            ],
        ))
    else:
        headers = ["Concepto"] + [s.name for s in subfolders] + ["TOTAL"]
        summary_rows = [
            ["Extraccion total combinada (dias significativos)"] + [f"{per_station_extraction[s.name]:.2f}" for s in subfolders] + [f"{extraction_total:.2f}"],
            ["Extraccion total anualizada"] + ["" for _ in subfolders] + [extraction_annual_str],
            ["Energia cargada total combinada (Sv) [kWh] (dias significativos)"] + [f"{per_station_energy[s.name]:.2f}" for s in subfolders] + [f"{energy_total:.2f}"],
            ["Energia cargada total anualizada [kWh]"] + ["" for _ in subfolders] + [energy_annual_str],
            ["Energia cargada real (bmax-B_llegada) [kWh]"] + [f"{per_station_real_charged[s.name]:.2f}" for s in subfolders] + [f"{real_charged_total:.2f}"],
            ["Energia consumida por vehiculos (B_s-B) [kWh] (dias significativos)"] + [f"{per_station_consumed[s.name]:.2f}" for s in subfolders] + [f"{consumed_total:.2f}"],
            ["Energia consumida por vehiculos anualizada [kWh]"] + ["" for _ in subfolders] + [consumed_annual_str],
            ["Potencia pico de carga (combinada) [kW]"] + ["" for s in subfolders] + [peak_charging_str],
            ["Cargadores totales (todas las estaciones)"] + ["" for _ in subfolders] + [f"{total_n_chargers:.0f}"],
            ["Bahias totales (todas las estaciones)"] + ["" for _ in subfolders] + [f"{total_n_bays:.0f}"],
            ["Baterias totales (todas las estaciones)"] + ["" for _ in subfolders] + [f"{total_n_batteries:.0f}"],
        ]
        print(make_table("METRICAS COMBINADAS", headers, summary_rows))
    print()


# -----------------------------
# CLI principal
# -----------------------------
def analyze_single_folder(root: Path, args) -> None:
    e_path = find_json_in_folder(root, "E.json")
    b_path = find_json_in_folder(root, "B.json")
    m_path = find_json_in_folder(root, "M.json")
    y_path = find_json_in_folder(root, "Y.json")
    p_path = find_json_in_folder(root, "P.json")
    sv_path = find_json_in_folder(root, "Sv.json")
    params_path = find_json_in_folder(root, "parameters.json")

    # Tabla “inputs”
    inputs_rows = [
        ["Folder", str(root)],
        ["Delta t (min)", fmt_num(args.delta_minutes, 3)],
        ["E.json", str(e_path) if e_path else "NO ENCONTRADO"],
        ["B.json", str(b_path) if b_path else "NO ENCONTRADO"],
        ["M.json", str(m_path) if m_path else "NO ENCONTRADO"],
        ["Y.json", str(y_path) if y_path else "NO ENCONTRADO"],
        ["P.json", str(p_path) if p_path else "NO ENCONTRADO"],
        ["Sv.json", str(sv_path) if sv_path else "NO ENCONTRADO"],
        ["parameters.json", str(params_path) if params_path else "NO ENCONTRADO"],
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
                    "SOC llegada promedio",
                    f"{100.0 * float(real_swap_meta.get('soc_arrival_avg', 0.0)):.2f}%" if real_swap_energy_kwh is not None else "N/D",
                ],
                [
                    "SOC llegada mínimo",
                    f"{100.0 * float(real_swap_meta.get('soc_arrival_min', 0.0)):.2f}%" if real_swap_energy_kwh is not None else "N/D",
                ],
                [
                    "SOC llegada máximo",
                    f"{100.0 * float(real_swap_meta.get('soc_arrival_max', 0.0)):.2f}%" if real_swap_energy_kwh is not None else "N/D",
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

    # ---- Operación diaria (viajes, ciclos y energía)
    try:
        trips_daily = calculate_daily_trips(y_path) if (y_path and not is_effectively_empty_json(y_path)) else {}
        cycles_total = 0.0
        cycles_daily: Dict[str, float] = {}
        if (
            y_path and m_path and params_path
            and not is_effectively_empty_json(y_path)
            and not is_effectively_empty_json(m_path)
            and not is_effectively_empty_json(params_path)
        ):
            cycles_total, cycles_daily = calculate_cycles_from_y_ntrips(y_path, m_path, params_path)

        energy_daily = calculate_daily_charged_energy(root)

        all_days = sorted(set(trips_daily.keys()) | set(cycles_daily.keys()) | set(energy_daily.keys()), key=lambda d: float(d))
        if all_days:
            rows = []
            total_trips = 0.0
            total_cycles = 0.0
            total_energy = 0.0
            for d in all_days:
                trips = trips_daily.get(d, 0.0)
                cycles = cycles_daily.get(d, 0.0)
                energy = energy_daily.get(d, 0.0)

                total_trips += trips
                total_cycles += cycles
                total_energy += energy

                rows.append([d, f"{trips:.0f}", f"{cycles:.3f}", f"{energy:.3f}"])

            rows.append(["TOTAL", f"{total_trips:.0f}", f"{total_cycles:.3f}", f"{total_energy:.3f}"])
            print(make_table(
                "OPERACIÓN DIARIA",
                ["Día", "Viajes diarios totales", "Ciclos (Y*n_trips)", "Energía diaria [kWh]"],
                rows,
            ))
            print()
            print(make_table(
                "CICLOS TOTALES",
                ["Métrica", "Valor"],
                [["cycles_total = sum(Y[i,j] * n_trips[j,i])", f"{cycles_total:.3f}"]],
            ))
            print()
            printed_any = True
    except Exception as ex:
        print(make_table("OPERACIÓN DIARIA", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    if not printed_any:
        print("No se pudo calcular nada: no encontré JSON utilizables (o todos estaban vacíos).")

    # ---- Generación renovable
    try:
        gen_info = calculate_gen_costs(root)
        if gen_info and gen_info.get("generators"):
            rows = []
            for g, info in gen_info["generators"].items():
                rows.append([g, f"{info['units']:.0f}", f"{info['p_max_unit']:.0f}",
                              f"{info['power_kw']:.0f}", f"{info['inv_cost']:.2f}", f"{info['op_cost']:.2f}"])
            rows.append(["TOTAL", "", "", f"{gen_info['total_power_kw']:.0f}",
                         f"{gen_info['total_inv_cost']:.2f}", f"{gen_info['total_op_cost']:.2f}"])
            print(make_table(
                "GENERACION RENOVABLE",
                ["Generador", "Unidades", "P_max/unidad [kW]", "Potencia inst. [kW]",
                 "Costo inv.", "Costo op. anual"],
                rows,
            ))
            print()
    except Exception as ex:
        print(make_table("GENERACION RENOVABLE", ["Estado", "Detalle"],
                         [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    # ---- Almacenamiento BESS
    try:
        bess_info = calculate_bess_costs(root)
        if bess_info and bess_info.get("units"):
            rows = []
            for h, info in bess_info["units"].items():
                estado = "Instalado" if info["installed"] >= 0.5 else "No instalado"
                rows.append([h, estado, f"{info['p_max_kw']:.0f}", f"{info['a_min_kwh']:.0f}",
                              f"{info['a_max_kwh']:.0f}", f"{info['inv_cost']:.2f}", f"{info['op_cost']:.2f}"])
            rows.append(["TOTAL", "", "", "", "",
                         f"{bess_info['total_inv_cost']:.2f}", f"{bess_info['total_op_cost']:.2f}"])
            print(make_table(
                "ALMACENAMIENTO BESS",
                ["Unidad", "Estado", "P_max [kW]", "A_min [kWh]", "A_max [kWh]",
                 "Costo inv.", "Costo op. anual"],
                rows,
            ))
            print()
    except Exception as ex:
        print(make_table("ALMACENAMIENTO BESS", ["Estado", "Detalle"],
                         [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    # ---- Cost
    try:
        costs = calculate_total_costs(root)
        if costs:
            rows = [
                ["Costo energía carga (USD)",       f"{costs['energy_cost']:.2f}"],
                ["Costo energía red P_red (USD)",    f"{costs.get('grid_energy_cost', 0.0):.2f}"],
                ["Costo energía carga real (USD)",   f"{costs.get('real_energy_cost', 0.0):.2f}"],
                ["Costo potencia pico (USD)",        f"{costs.get('peak_power_cost', 0.0):.2f}"],
                ["Costo inversión estaciones (USD)", f"{costs['investment_cost']:.2f}"],
                ["Costo inversión generación (USD)", f"{costs.get('gen_inv_cost', 0.0):.2f}"],
                ["Costo operación generación (USD)", f"{costs.get('gen_op_cost', 0.0):.2f}"],
                ["Costo inversión BESS (USD)",       f"{costs.get('bess_inv_cost', 0.0):.2f}"],
                ["Costo operación BESS (USD)",       f"{costs.get('bess_op_cost', 0.0):.2f}"],
                ["Costo penalidad (USD)",            f"{costs['penalty_cost']:.2f}"],
                ["COSTO TOTAL (USD)",                f"{costs['total_cost']:.2f}"],
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

    delta_t = float(params_data.get("delta_t", 0.0))
    p_charger = float(params_data.get("p_charger", 0.0))
    scaling_factor = float(params_data.get("scaling_factor_op_cost", 1.0))
    costo_electricidad = _unwrap_named_tree(params_data.get("costo_electricidad", {}))

    cost_lookup: Dict[Tuple[str, str], float] = {}
    for path, cost_val in _iter_leaf_records(costo_electricidad):
        axis_map = _axis_map_from_path(path)
        day_key = axis_map.get("d") or axis_map.get("day") or (path[0] if len(path) >= 1 else None)
        time_key = axis_map.get("t") or axis_map.get("time") or (path[-1] if len(path) >= 1 else None)
        if day_key is None or time_key is None:
            continue
        try:
            cost_lookup[(str(day_key), str(time_key))] = float(cost_val)
        except Exception:
            continue

    total_cost = 0.0
    for path, sv_val in _iter_leaf_records(sv_data):
        axis_map = _axis_map_from_path(path)
        day_key = axis_map.get("d") or axis_map.get("day")
        time_key = axis_map.get("t") or axis_map.get("time")
        if day_key is None or time_key is None:
            continue
        cost_elec = cost_lookup.get((str(day_key), str(time_key)), 0.0)
        try:
            total_cost += cost_elec * float(sv_val) * p_charger * delta_t
        except Exception:
            continue

    return total_cost * scaling_factor


def calculate_grid_energy_cost(root: Path) -> float:
    """Costo de energía comprada a la red: sum(P_red[d,t] * costo_electricidad[d,t] * delta_t).

    Usa P_red.json y parameters.json.
    """
    pred_path   = find_json_in_folder(root, "P_red.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not pred_path or not params_path:
        raise ValueError("No se encontraron P_red.json o parameters.json")
    if is_effectively_empty_json(pred_path) or is_effectively_empty_json(params_path):
        raise ValueError("P_red.json o parameters.json están vacíos")

    pred_data   = load_json(pred_path)
    params_data = load_json(params_path)

    delta_t        = float(params_data.get("delta_t", 0.0))
    scaling_factor = float(params_data.get("scaling_factor_op_cost", 1.0))
    costo_electricidad = _unwrap_named_tree(
        params_data.get("costo_red", params_data.get("costo_electricidad", {}))
    )

    cost_lookup: Dict[Tuple[str, str], float] = {}
    for path, cost_val in _iter_leaf_records(costo_electricidad):
        axis_map = _axis_map_from_path(path)
        day_key  = axis_map.get("d") or axis_map.get("day") or (path[0] if path else None)
        time_key = axis_map.get("t") or axis_map.get("time") or (path[-1] if path else None)
        if day_key is None or time_key is None:
            continue
        try:
            cost_lookup[(str(day_key), str(time_key))] = float(cost_val)
        except Exception:
            continue

    total_cost = 0.0
    for path, pred_val in _iter_leaf_records(pred_data):
        axis_map = _axis_map_from_path(path)
        day_key  = axis_map.get("d") or axis_map.get("day")
        time_key = axis_map.get("t") or axis_map.get("time")
        if day_key is None or time_key is None:
            continue
        cost_elec = cost_lookup.get((str(day_key), str(time_key)), 0.0)
        try:
            total_cost += cost_elec * float(pred_val) * delta_t
        except Exception:
            continue

    return total_cost * scaling_factor


def calculate_peak_power_cost(root: Path) -> float:
    """Calcula el costo por potencia pico usando P_pot.json y el coeficiente de demanda."""
    ppot_path = find_json_in_folder(root, "P_pot.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not ppot_path or not params_path:
        return 0.0
    if is_effectively_empty_json(ppot_path) or is_effectively_empty_json(params_path):
        return 0.0

    ppot_data = load_json(ppot_path)
    params_data = load_json(params_path)

    try:
        if isinstance(ppot_data, dict):
            ppot_value = None
            if "P_pot" in ppot_data:
                ppot_value = ppot_data.get("P_pot")
            elif "value" in ppot_data:
                ppot_value = ppot_data.get("value")
            elif ppot_data:
                ppot_value = next(iter(ppot_data.values()))
            ppot = float(ppot_value)
        else:
            ppot = float(ppot_data)
    except Exception:
        return 0.0

    try:
        demand_charge_coef = float(params_data.get("demand_charge_coef", 12 * 10))
    except Exception:
        demand_charge_coef = 12 * 10

    return ppot * demand_charge_coef


def calculate_investment_cost(root: Path) -> float:
    """
    Calcula el costo de inversión en estaciones exactamente como la función objetivo:
      Σ_k  station_cost_k[k] * X[k]
           + c_bays_k[k] * N_bays[k]
           + c_crane_k[k] * N_bays[k]
           + (charger_cost + c_charger_space_k[k]) * N_chargers[k]
           + (battery_cost  + c_battery_space_k[k]) * N_batteries[k]
    """
    x_path           = find_json_in_folder(root, "X.json")
    n_bays_path      = find_json_in_folder(root, "N_bays.json")
    n_chargers_path  = find_json_in_folder(root, "N_chargers.json")
    n_batteries_path = find_json_in_folder(root, "N_batteries.json")
    params_path      = find_json_in_folder(root, "parameters.json")

    if not params_path or is_effectively_empty_json(params_path):
        raise ValueError("No se encontró parameters.json")

    params_data = load_json(params_path)

    charger_cost    = _as_float(params_data.get("charger_cost", 0.0))
    battery_cost    = _as_float(params_data.get("battery_cost", 0.0))
    station_cost_k  = _unwrap_named_tree(params_data.get("station_cost_k",   {}))
    c_bays_k        = _unwrap_named_tree(params_data.get("c_bays_k",         {}))
    c_crane_k       = _unwrap_named_tree(params_data.get("c_crane_k",        {}))
    c_charger_space = _unwrap_named_tree(params_data.get("c_charger_space_k", {}))
    c_battery_space = _unwrap_named_tree(params_data.get("c_battery_space_k", {}))

    def _station_scalars(path: Optional[Path]) -> Dict[str, float]:
        if not path or is_effectively_empty_json(path):
            return {}
        raw = _unwrap_named_tree(load_json(path))
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
        return out

    x_map           = _station_scalars(x_path)
    n_bays_map      = _station_scalars(n_bays_path)
    n_chargers_map  = _station_scalars(n_chargers_path)
    n_batteries_map = _station_scalars(n_batteries_path)

    stations = sorted(set(x_map) | set(n_bays_map) | set(n_chargers_map) | set(n_batteries_map))

    total_cost = 0.0
    for k in stations:
        x_k          = x_map.get(k, 0.0)
        n_bays       = n_bays_map.get(k, 0.0)
        n_chargers   = n_chargers_map.get(k, 0.0)
        n_batteries  = n_batteries_map.get(k, 0.0)
        c_station    = _as_float(station_cost_k.get(k, 0.0))  if isinstance(station_cost_k,  dict) else 0.0
        c_bay        = _as_float(c_bays_k.get(k, 0.0))        if isinstance(c_bays_k,        dict) else 0.0
        c_crane      = _as_float(c_crane_k.get(k, 0.0))       if isinstance(c_crane_k,       dict) else 0.0
        c_char_sp    = _as_float(c_charger_space.get(k, 0.0)) if isinstance(c_charger_space, dict) else 0.0
        c_bat_sp     = _as_float(c_battery_space.get(k, 0.0)) if isinstance(c_battery_space, dict) else 0.0

        total_cost += (
            c_station * x_k
            + c_bay   * n_bays
            + c_crane * n_bays
            + (charger_cost + c_char_sp) * n_chargers
            + (battery_cost + c_bat_sp)  * n_batteries
        )

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
    
    voll = float(params_data.get("Voll", 0.0))
    f_penalty_div = _unwrap_named_tree(params_data.get("F_penalty_div", {}))
    scaling_factor = float(params_data.get("scaling_factor_op_cost", 1.0))
    
    total_penalty = 0.0
    
    for path, value in _iter_leaf_records(f_seg_data):
        axis_map = _axis_map_from_path(path)
        seg_id = axis_map.get("seg") or axis_map.get("_3")
        if seg_id is None:
            continue
        try:
            divisor = float(f_penalty_div.get(str(seg_id), 1.0)) if isinstance(f_penalty_div, dict) else 1.0
            total_penalty += float(value) * (voll / divisor)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    
    return total_penalty * scaling_factor


def calculate_gen_costs(root: Path) -> Dict[str, Any]:
    """
    Lee G_g.json y parameters.json para calcular por generador:
    unidades instaladas, potencia instalada [kW], costo inversión y costo operación.
    """
    gg_path     = find_json_in_folder(root, "G_g.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not gg_path or not params_path:
        return {}
    if is_effectively_empty_json(gg_path) or is_effectively_empty_json(params_path):
        return {}

    gg_data     = load_json(gg_path)
    params_data = load_json(params_path)

    def _extract_param(params: Dict, key: str) -> Dict[str, float]:
        raw   = params.get(key, {})
        inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
        return {str(g): _as_float(v, 0.0) for g, v in inner.items()} if isinstance(inner, dict) else {}

    gg_raw = gg_data
    for candidate in ("g", "_1"):
        if isinstance(gg_raw, dict) and candidate in gg_raw:
            gg_raw = gg_raw[candidate]
            break
    gg_map  = {str(g): _as_float(v, 0.0) for g, v in gg_raw.items()} if isinstance(gg_raw, dict) else {}
    c_inv   = _extract_param(params_data, "c_inv_g")
    c_op    = _extract_param(params_data, "c_op_g")
    p_max   = _extract_param(params_data, "p_max_g")
    gen_set = [str(g) for g in params_data.get("gen_set", list(gg_map.keys()))]

    gens: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = total_power = 0.0

    for g in gen_set:
        units = gg_map.get(g, 0.0)
        inv   = units * c_inv.get(g, 0.0) * p_max.get(g, 0.0)
        op    = units * c_op.get(g, 0.0)  * p_max.get(g, 0.0)
        power = units * p_max.get(g, 0.0)
        gens[g] = {"units": units, "p_max_unit": p_max.get(g, 0.0), "power_kw": power,
                   "inv_cost": inv, "op_cost": op}
        total_inv   += inv
        total_op    += op
        total_power += power

    return {"generators": gens, "total_inv_cost": total_inv,
            "total_op_cost": total_op, "total_power_kw": total_power}


def calculate_bess_costs(root: Path) -> Dict[str, Any]:
    """
    Lee H_h.json y parameters.json para calcular por unidad BESS:
    estado de instalación, P_max, capacidad, costo inversión y operación.
    """
    hh_path     = find_json_in_folder(root, "H_h.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not hh_path or not params_path:
        return {}
    if is_effectively_empty_json(hh_path) or is_effectively_empty_json(params_path):
        return {}

    hh_data     = load_json(hh_path)
    params_data = load_json(params_path)

    def _extract_param(params: Dict, key: str) -> Dict[str, float]:
        raw   = params.get(key, {})
        inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
        return {str(h): _as_float(v, 0.0) for h, v in inner.items()} if isinstance(inner, dict) else {}

    raw_hh = hh_data
    for candidate in ("h", "_1"):
        if isinstance(raw_hh, dict) and candidate in raw_hh:
            raw_hh = raw_hh[candidate]
            break
    hh_map = {str(h): _as_float(v, 0.0) for h, v in raw_hh.items()} if isinstance(raw_hh, dict) else {}

    c_inv    = _extract_param(params_data, "c_inv_h")
    c_op     = _extract_param(params_data, "c_op_h")
    p_max    = _extract_param(params_data, "p_max_h")
    a_max    = _extract_param(params_data, "a_max_h")
    a_min    = _extract_param(params_data, "a_min_h")
    stor_set = [str(h) for h in params_data.get("storage_set", list(hh_map.keys()))]

    units_info: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = 0.0

    for h in stor_set:
        installed = hh_map.get(h, 0.0)
        inv = installed * c_inv.get(h, 0.0)
        op  = installed * c_op.get(h, 0.0)
        units_info[h] = {"installed": installed, "p_max_kw": p_max.get(h, 0.0),
                         "a_max_kwh": a_max.get(h, 0.0), "a_min_kwh": a_min.get(h, 0.0),
                         "inv_cost": inv, "op_cost": op}
        total_inv += inv
        total_op  += op

    return {"units": units_info, "total_inv_cost": total_inv, "total_op_cost": total_op}


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
    # - Si parameters.energy_cost == "Profile_fixed": E_real * costo_fijo * scaling_factor_op_cost
    # - En otro caso (perfil variable):
    #   energy_cost - (delta_E * costo_min * scaling_factor_op_cost),
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
        for _, v in _iter_leaf_records(costo_electricidad):
            try:
                fixed_candidates.append(float(v))
            except Exception:
                continue

        if not fixed_candidates:
            raise ValueError("No hay valores en costo_electricidad para costo fijo")

        c_min = min(fixed_candidates)
        energy_cost_profile = str(params_data.get("energy_cost", "")).strip()

        if energy_cost_profile == "Profile_fixed":
            # Costo fijo: real_cost = energia_real * precio_unico * escala
            real_energy_cost = float(real_swap_energy_kwh) * c_min * scaling_factor
        else:
            # Costo variable: escala el costo del modelo proporcionalmente a la
            # energia real vs la energia que Sv realmente cargo.
            # real_cost = energy_cost * (real_energy / sv_energy)
            # => usa el precio promedio ponderado real del modelo, sin asumir c_min.
            if sv_energy_total_kwh > 0:
                real_energy_cost = float(energy_cost) * (float(real_swap_energy_kwh) / sv_energy_total_kwh)
            else:
                real_energy_cost = 0.0
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

    try:
        peak_power_cost = calculate_peak_power_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo por potencia pico: {ex}")
        peak_power_cost = 0.0

    try:
        grid_energy_cost = calculate_grid_energy_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de energía de red: {ex}")
        grid_energy_cost = 0.0

    try:
        gen_info     = calculate_gen_costs(root)
        gen_inv_cost = gen_info.get("total_inv_cost", 0.0)
        gen_op_cost  = gen_info.get("total_op_cost",  0.0)
    except Exception as ex:
        print(f"Advertencia al calcular costos de generación: {ex}")
        gen_inv_cost = gen_op_cost = 0.0

    try:
        bess_info     = calculate_bess_costs(root)
        bess_inv_cost = bess_info.get("total_inv_cost", 0.0)
        bess_op_cost  = bess_info.get("total_op_cost",  0.0)
    except Exception as ex:
        print(f"Advertencia al calcular costos de almacenamiento: {ex}")
        bess_inv_cost = bess_op_cost = 0.0

    total_cost = (investment_cost + grid_energy_cost + penalty_cost + peak_power_cost
                  + gen_inv_cost + gen_op_cost + bess_inv_cost + bess_op_cost)

    return {
        "energy_cost":      energy_cost,
        "grid_energy_cost": grid_energy_cost,
        "real_energy_cost": real_energy_cost,
        "investment_cost":  investment_cost,
        "penalty_cost":     penalty_cost,
        "peak_power_cost":  peak_power_cost,
        "gen_inv_cost":     gen_inv_cost,
        "gen_op_cost":      gen_op_cost,
        "bess_inv_cost":   bess_inv_cost,
        "bess_op_cost":    bess_op_cost,
        "total_cost":      total_cost,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Analiza outputs desde una carpeta: E (L/h), B (kWh/h), M (extracción total) y costos. "
                    "Si la carpeta no tiene parameters.json pero sus subcarpetas si (ej. salida de la "
                    "descomposicion por macrobloque), agrega automaticamente los resultados de cada una. "
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
    ap.add_argument(
        "--summary_only", action="store_true",
        help="En modo desacoplado (macrobloques), muestra solo el total agregado final "
             "(3 estaciones, todos los dias), sin el detalle por subproblema.",
    )
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Carpeta no válida: {root}")

    subfolders = find_macrobloque_subfolders(root)
    if subfolders:
        analyze_macrobloques(root, subfolders, args)
    else:
        analyze_single_folder(root, args)


if __name__ == "__main__":
    main()
