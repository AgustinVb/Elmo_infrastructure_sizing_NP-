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
    total_real_grid = 0.0
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

    # Eficiencia de carga por LHD (hoja LHD, eta_charge_i): la energia que
    # entra a la bateria (bmax_i - B_llegada) es POST-eficiencia; para
    # estimar lo que habria que comprarle a la red hay que des-escalarla
    # dividiendo por eta_charge_i, igual que hace battery_soc/battery_soc_swap
    # en functions.py.
    eta_charge_raw = params_data.get("eta_charge_i", {})
    eta_charge_map: Dict[str, float] = {}
    for path, eta_val in _iter_leaf_records(eta_charge_raw):
        axis_map = _axis_map_from_path(path)
        lhd_key = axis_map.get("i") or axis_map.get("b") or axis_map.get("_1")
        if lhd_key is None:
            continue
        try:
            eta_charge_map[str(lhd_key)] = float(eta_val)
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
        eta_c = eta_charge_map.get(str(lhd), 1.0)
        event_real_grid = event_real / eta_c if eta_c > 0 else event_real
        if event_real > eps:
            n_events_with_discount += 1
        total_real += event_real
        total_real_grid += event_real_grid
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
                "real_event_grid_kwh": event_real_grid,
                "eta_charge": eta_c,
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
        "real_grid_energy_kwh": total_real_grid,
        "gap_vs_sv_grid_kwh": sv_energy_total - total_real_grid,
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
                    "Energía real cargada a la red estimada (real / eta_charge)",
                    f"{real_swap_meta.get('real_grid_energy_kwh', 0.0):.6f} kWh" if real_swap_energy_kwh is not None else "N/D",
                ],
                [
                    "Brecha vs energía Sv (grid)",
                    f"{real_swap_meta.get('gap_vs_sv_grid_kwh', 0.0):.6f} kWh" if real_swap_energy_kwh is not None else "N/D",
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
                rows.append([g, f"{info['units']:.3f}", f"{info['p_max_unit']:.0f}",
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
                rows.append([h, f"{info['installed']:.3f}", f"{info['p_max_kw']:.0f}", f"{info['a_min_kwh']:.0f}",
                              f"{info['a_max_kwh']:.0f}", f"{info['inv_cost']:.2f}", f"{info['op_cost']:.2f}"])
            rows.append(["TOTAL", "", "", "", "",
                         f"{bess_info['total_inv_cost']:.2f}", f"{bess_info['total_op_cost']:.2f}"])
            print(make_table(
                "ALMACENAMIENTO BESS",
                ["Unidad", "Unidades instaladas", "P_max [kW]", "A_min [kWh]", "A_max [kWh]",
                 "Costo inv.", "Costo op. anual"],
                rows,
            ))
            print()
    except Exception as ex:
        print(make_table("ALMACENAMIENTO BESS", ["Estado", "Detalle"],
                         [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    # ---- Degradación batería pool swap
    try:
        deg_info = calculate_battery_degradation_metrics(root)
        if deg_info and deg_info.get("years"):
            rows = []
            for y, info in deg_info["years"].items():
                rows.append([
                    y,
                    f"{info['b_bar_kwh']:.1f}",
                    f"{info['b_bar_pct']:.1f}%",
                    f"{info['n_ciclos']:.1f}",
                    f"{info['d_kwh']:.1f}",
                    "SI" if info["replaced"] else "NO",
                    f"{info['replace_cost']:.2f}",
                ])
            print(make_table(
                "DEGRADACION BATERIA POOL SWAP",
                ["Año", "B [kWh] (inicio)", "% b_max", "EFC año", "D [kWh] (fin)", "Reemplazo", "Costo reemplazo"],
                rows,
            ))
            print()
            print(make_table(
                "DEGRADACION BATERIA - RESUMEN",
                ["Métrica", "Valor"],
                [
                    ["Capacidad nominal b_max_pool [kWh]", f"{deg_info['b_max_pool_kwh']:.1f}"],
                    ["Piso de degradación",                 f"{deg_info['min_capacity_pct']:.0f}% de b_max_pool"],
                    ["N_batteries_total (pool)",             f"{deg_info['n_batteries_total']:.0f}"],
                    ["N° de reemplazos",                     f"{deg_info['n_replacements']}"],
                    ["Costo total reemplazos (USD)",         f"{deg_info['total_replace_cost']:.2f}"],
                ],
            ))
            print()
    except Exception as ex:
        print(make_table("DEGRADACION BATERIA POOL SWAP", ["Estado", "Detalle"],
                         [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    # ---- Cost
    try:
        costs = calculate_total_costs(root)
        if costs:
            rows = [
                ["Costo energía carga (USD)",       f"{costs['energy_cost']:.2f}"],
                ["Costo energía red P_red (USD)",    f"{costs.get('grid_energy_cost', 0.0):.2f}"],
                ["Energía total red (horizonte) [kWh]", f"{costs.get('grid_energy_kwh', 0.0):.2f}"],
                ["Energía total red (horizonte) [MWh]", f"{costs.get('grid_energy_kwh', 0.0) / 1000.0:.2f}"],
                ["Costo energía carga real (USD)",   f"{costs.get('real_energy_cost', 0.0):.2f}"],
                ["Costo potencia pico (USD)",        f"{costs.get('peak_power_cost', 0.0):.2f}"],
                ["Costo inversión estaciones (USD)", f"{costs['investment_cost']:.2f}"],
                ["Costo inversión generación (USD)", f"{costs.get('gen_inv_cost', 0.0):.2f}"],
                ["Costo operación generación (USD)", f"{costs.get('gen_op_cost', 0.0):.2f}"],
                ["Costo inversión BESS (USD)",       f"{costs.get('bess_inv_cost', 0.0):.2f}"],
                ["Costo operación BESS (USD)",       f"{costs.get('bess_op_cost', 0.0):.2f}"],
                ["Costo reemplazo batería (USD)",    f"{costs.get('battery_replace_cost', 0.0):.2f}"],
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

def _get_discount_rate(params: Dict) -> float:
    return _as_float(params.get("discount_rate"), 0.0)


def _get_years_sorted(params: Dict) -> List[int]:
    """Años del horizonte, leídos desde costo_electricidad (_1=year), siempre presente."""
    raw = params.get("costo_electricidad", {})
    inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
    years = sorted(int(y) for y in inner.keys()) if isinstance(inner, dict) else []
    return years or [1]


def _has_degradation_data(params: Dict) -> bool:
    """Proxy de si la hoja BatteryDegradation existía al formular el modelo —
    misma condición que gatilla el descuento en ObjectiveRules (functions.py)."""
    return "gamma_coef" in params


def _annuity_factor(r: float, years: int) -> float:
    """AF(r,Y) = sum_{k=1..Y} 1/(1+r)^k — igual a annuity_factor_expr en functions.py."""
    if years <= 0:
        return 0.0
    if r == 0:
        return float(years)
    return sum(1.0 / (1.0 + r) ** k for k in range(1, years + 1))


def _year_discount_factor(r: float, year: Any, years_sorted: List[int]) -> float:
    """1/(1+r)^pos(year) — igual a _discount_factor en ObjectiveRules (functions.py)."""
    if r == 0:
        return 1.0
    pos = years_sorted.index(int(year)) + 1 if int(year) in years_sorted else 1
    return 1.0 / (1.0 + r) ** pos


def _annuity_factor_year(r: float, pos_y: int, n_years: int) -> float:
    """AF_y(r,Y) = sum_{n=pos_y..n_years} 1/(1+r)^n — igual a annuity_factor_expr(model, y)
    en functions.py (inversion multi-año: NPV de un costo anualizado que
    empieza a devengarse en el año de posicion pos_y y se mantiene hasta el
    fin del horizonte, sin retiro)."""
    if n_years <= 0 or pos_y > n_years:
        return 0.0
    if r == 0:
        return float(n_years - pos_y + 1)
    return sum(1.0 / (1.0 + r) ** n for n in range(pos_y, n_years + 1))


def _indexed_vars_by_year(path: Optional[Path], outer_candidates: Tuple[str, ...] = ("k", "_1")) -> Dict[str, Dict[str, float]]:
    """Lee un JSON con estructura {outer_axis: {id: {'y': {year: val}}}}
    (variables de inversion multi-año Delta_X/Delta_N_bays/Delta_N_chargers/
    Delta_N_batteries/Delta_G_g, indexadas por (id, y)) y devuelve
    {id: {year_str: val}}. Devuelve {} si el archivo no existe/esta vacio o
    no calza con la estructura esperada."""
    if not path or is_effectively_empty_json(path):
        return {}
    data = load_json(path)
    if not isinstance(data, dict):
        return {}
    outer = None
    for candidate in outer_candidates:
        if candidate in data:
            outer = data[candidate]
            break
    if not isinstance(outer, dict):
        return {}
    result: Dict[str, Dict[str, float]] = {}
    for outer_id, inner in outer.items():
        if not isinstance(inner, dict):
            continue
        y_block = inner.get("y", inner)
        if not isinstance(y_block, dict):
            continue
        result[str(outer_id)] = {str(y): _as_float(v, 0.0) for y, v in y_block.items()}
    return result


def _year_only_vars(path: Optional[Path]) -> Dict[str, float]:
    """Lee un JSON con estructura {'y': {year: val}} (variables de inversion
    multi-año sin eje adicional, como Delta_H/H) y devuelve {year_str: val}."""
    if not path or is_effectively_empty_json(path):
        return {}
    data = load_json(path)
    if not isinstance(data, dict):
        return {}
    inner = data.get("y", data)
    return {str(y): _as_float(v, 0.0) for y, v in inner.items()} if isinstance(inner, dict) else {}


def calculate_lhd_charge_cost(root: Path) -> float:
    """
    Calcula el costo de carga de los LHD eléctricos con battery swap.
    Usa Sv.json (baterías conectadas, ejes k,y,d,t,a) y parameters.json
    (costo_electricidad indexado por año/día/intervalo, p_charger, delta_t).
    Fórmula: sum(Sv[k,y,d,t,a] * p_charger * costo_electricidad[y,d,t] * delta_t * discount_factor(y))
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
    costo_electricidad = params_data.get("costo_electricidad", {})

    discount_r = _get_discount_rate(params_data) if _has_degradation_data(params_data) else 0.0
    years_sorted = _get_years_sorted(params_data)

    def _cost_lookup(year_key, day_key, t_key) -> float:
        """costo_electricidad: _1=year -> _2=day -> _3=interval."""
        if "_1" not in costo_electricidad:
            return 0.0
        cm_year = costo_electricidad["_1"].get(str(year_key), {})
        cm_2 = cm_year.get("_2", {}) if isinstance(cm_year, dict) else {}
        cm_day = cm_2.get(str(day_key), {})
        if isinstance(cm_day, dict) and "_3" in cm_day:
            try:
                return float(cm_day["_3"].get(str(t_key), 0.0))
            except Exception:
                return 0.0
        return 0.0

    total_cost = 0.0
    for path, sv_val in _iter_leaf_records(sv_data):
        axis_map = _axis_map_from_path(path)
        y_key = axis_map.get("y")
        day_key = axis_map.get("d")
        time_key = axis_map.get("t")
        if y_key is None or day_key is None or time_key is None:
            continue
        try:
            cost_elec = _cost_lookup(y_key, day_key, time_key)
            disc = _year_discount_factor(discount_r, y_key, years_sorted)
            total_cost += cost_elec * float(sv_val) * p_charger * delta_t * disc
        except Exception:
            continue

    return total_cost * scaling_factor


def calculate_grid_energy_cost(root: Path) -> float:
    """Costo de energía comprada a la red: sum(P_red[y,d,t] * costo_electricidad[y,d,t] * delta_t * discount_factor(y)).

    Usa P_red.json (ejes y,d,t) y parameters.json.
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
    costo_electricidad = params_data.get("costo_red", params_data.get("costo_electricidad", {}))

    discount_r = _get_discount_rate(params_data) if _has_degradation_data(params_data) else 0.0
    years_sorted = _get_years_sorted(params_data)

    # Lookup costo_electricidad[(y,d,t)]: _1=year -> _2=day -> _3=interval.
    cost_lookup: Dict[Tuple[str, str, str], float] = {}
    if "_1" in costo_electricidad:
        for y_key, y_val in costo_electricidad["_1"].items():
            d_block = y_val.get("_2", {}) if isinstance(y_val, dict) else {}
            for d_key, d_val in d_block.items():
                t_block = d_val.get("_3", {}) if isinstance(d_val, dict) else {}
                for t_key, cost_val in t_block.items():
                    try:
                        cost_lookup[(str(y_key), str(d_key), str(t_key))] = float(cost_val)
                    except Exception:
                        continue

    total_cost = 0.0
    for path, pred_val in _iter_leaf_records(pred_data):
        axis_map = _axis_map_from_path(path)
        y_key    = axis_map.get("y")
        day_key  = axis_map.get("d")
        time_key = axis_map.get("t")
        if y_key is None or day_key is None or time_key is None:
            continue
        cost_elec = cost_lookup.get((str(y_key), str(day_key), str(time_key)), 0.0)
        try:
            disc = _year_discount_factor(discount_r, y_key, years_sorted)
            total_cost += cost_elec * float(pred_val) * delta_t * disc
        except Exception:
            continue

    return total_cost * scaling_factor


def calculate_grid_energy_total_kwh(root: Path) -> float:
    """Energía total comprada a la red durante todo el horizonte, en kWh.

    Misma estructura que calculate_grid_energy_cost pero sin costo ni
    descuento: sum(P_red[y,d,t] * delta_t), anualizando cada año con
    scaling_factor_op_cost (extrapola el/los día(s) representativo(s) a los
    365 días de ese año) y sumando los años del horizonte. Es una cantidad
    física (energía), no monetaria, por eso no se aplica tasa de descuento.
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

    total_kwh = 0.0
    for _, pred_val in _iter_leaf_records(pred_data):
        try:
            total_kwh += float(pred_val) * delta_t
        except Exception:
            continue

    return total_kwh * scaling_factor


def calculate_peak_power_cost(root: Path) -> float:
    """Calcula el costo por potencia pico: sum(P_pot[y] * demand_charge_coef * discount_factor(y)).

    P_pot.json está indexado por año ({"y": {"1": val1, "2": val2, ...}}) — no
    es un escalar, aunque el escenario tenga un solo año en el horizonte.
    """
    ppot_path = find_json_in_folder(root, "P_pot.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not ppot_path or not params_path:
        return 0.0
    if is_effectively_empty_json(ppot_path) or is_effectively_empty_json(params_path):
        return 0.0

    ppot_data = load_json(ppot_path)
    params_data = load_json(params_path)

    try:
        demand_charge_coef = float(params_data.get("demand_charge_coef", 12 * 10))
    except Exception:
        demand_charge_coef = 12 * 10

    discount_r = _get_discount_rate(params_data) if _has_degradation_data(params_data) else 0.0
    years_sorted = _get_years_sorted(params_data)

    y_block = ppot_data.get("y", ppot_data) if isinstance(ppot_data, dict) else ppot_data

    total = 0.0
    if isinstance(y_block, dict):
        for y_key, val in y_block.items():
            if isinstance(val, dict):
                continue
            disc = _year_discount_factor(discount_r, y_key, years_sorted)
            try:
                total += float(val) * demand_charge_coef * disc
            except (TypeError, ValueError):
                continue
    else:
        try:
            total = float(y_block) * demand_charge_coef
        except Exception:
            total = 0.0

    return total


def calculate_investment_cost(root: Path) -> float:
    """
    Calcula el costo de inversión en estaciones (multi-año), idéntico a
    inversion_cost() de functions.py: usa las variables de INCREMENTO
    Delta_X/Delta_N_bays/Delta_N_chargers/Delta_N_batteries de cada año y,
    anualizadas con AF_y(r,Y):
      Σ_y AF_y(r,Y) Σ_k  station_cost_k[k] * Delta_X[k,y]
           + (c_bays_k[k] + c_crane_k[k]) * Delta_N_bays[k,y]
           + (charger_cost + c_charger_space_k[k]) * Delta_N_chargers[k,y]
           + (battery_cost + c_battery_space_k[k]) * Delta_N_batteries[k,y]
    """
    params_path = find_json_in_folder(root, "parameters.json")

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

    delta_x_by_station    = _indexed_vars_by_year(find_json_in_folder(root, "Delta_X.json"))
    delta_bays_by_station = _indexed_vars_by_year(find_json_in_folder(root, "Delta_N_bays.json"))
    delta_ch_by_station   = _indexed_vars_by_year(find_json_in_folder(root, "Delta_N_chargers.json"))
    delta_bat_by_station  = _indexed_vars_by_year(find_json_in_folder(root, "Delta_N_batteries.json"))

    stations = sorted(
        set(delta_x_by_station) | set(delta_bays_by_station)
        | set(delta_ch_by_station) | set(delta_bat_by_station)
    )

    years_sorted = _get_years_sorted(params_data)
    n_years = len(years_sorted)
    has_degradation = _has_degradation_data(params_data)
    discount_r = _get_discount_rate(params_data) if has_degradation else 0.0

    total_cost = 0.0
    for k in stations:
        c_station = _as_float(station_cost_k.get(k, 0.0))  if isinstance(station_cost_k,  dict) else 0.0
        c_bay     = _as_float(c_bays_k.get(k, 0.0))        if isinstance(c_bays_k,        dict) else 0.0
        c_crane   = _as_float(c_crane_k.get(k, 0.0))       if isinstance(c_crane_k,       dict) else 0.0
        c_char_sp = _as_float(c_charger_space.get(k, 0.0)) if isinstance(c_charger_space, dict) else 0.0
        c_bat_sp  = _as_float(c_battery_space.get(k, 0.0)) if isinstance(c_battery_space, dict) else 0.0

        dx  = delta_x_by_station.get(k, {})
        dba = delta_bays_by_station.get(k, {})
        dch = delta_ch_by_station.get(k, {})
        dbt = delta_bat_by_station.get(k, {})

        for pos_y, y in enumerate(years_sorted, start=1):
            y_key = str(y)
            yearly = (
                c_station * dx.get(y_key, 0.0)
                + (c_bay + c_crane) * dba.get(y_key, 0.0)
                + (charger_cost + c_char_sp) * dch.get(y_key, 0.0)
                + (battery_cost + c_bat_sp) * dbt.get(y_key, 0.0)
            )
            af = _annuity_factor_year(discount_r, pos_y, n_years) if has_degradation else 1.0
            total_cost += yearly * af

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
    Lee Delta_G_g.json (inversión por año), G_g.json (stock acumulado, para
    reportar unidades instaladas al final del horizonte) y parameters.json.

    Costo multi-año (idéntico a gen_investment_cost/gen_op_cost de
    functions.py): sum_y AF_y(r,Y) * Delta_G_g[g,y] * c_inv_g[g]/c_op_g[g] * p_max_g[g].
    """
    delta_gg_path = find_json_in_folder(root, "Delta_G_g.json")
    gg_path       = find_json_in_folder(root, "G_g.json")
    params_path   = find_json_in_folder(root, "parameters.json")

    if not delta_gg_path or not params_path:
        return {}
    if is_effectively_empty_json(delta_gg_path) or is_effectively_empty_json(params_path):
        return {}

    params_data = load_json(params_path)

    def _extract_param(key: str) -> Dict[str, float]:
        raw   = params_data.get(key, {})
        inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
        return {str(g): _as_float(v, 0.0) for g, v in inner.items()} if isinstance(inner, dict) else {}

    delta_gg_by_gen = _indexed_vars_by_year(delta_gg_path, outer_candidates=("g", "_1"))
    stock_gg_by_gen = _indexed_vars_by_year(gg_path, outer_candidates=("g", "_1"))

    c_inv = _extract_param("c_inv_g")
    c_op  = _extract_param("c_op_g")
    p_max = _extract_param("p_max_g")
    gen_set = [str(g) for g in params_data.get("gen_set", list(delta_gg_by_gen.keys()))]

    years_sorted = _get_years_sorted(params_data)
    n_years = len(years_sorted)
    last_year_key = str(years_sorted[-1]) if years_sorted else None
    has_degradation = _has_degradation_data(params_data)
    discount_r = _get_discount_rate(params_data) if has_degradation else 0.0

    gens: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = total_power = 0.0

    for g in gen_set:
        deltas = delta_gg_by_gen.get(g, {})
        inv = op = 0.0
        for pos_y, y in enumerate(years_sorted, start=1):
            dval = deltas.get(str(y), 0.0)
            af = _annuity_factor_year(discount_r, pos_y, n_years) if has_degradation else 1.0
            inv += dval * c_inv.get(g, 0.0) * p_max.get(g, 0.0) * af
            op  += dval * c_op.get(g, 0.0)  * p_max.get(g, 0.0) * af

        # Unidades acumuladas al final del horizonte (= total construido, sin
        # retiro de activos); fallback a la suma de incrementos si G_g.json
        # no esta disponible.
        units = stock_gg_by_gen.get(g, {}).get(last_year_key, 0.0) if last_year_key else sum(deltas.values())
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
    Lee Delta_H.json (inversión del cluster BESS por año, sin eje h -- el
    modelo asume un único cluster candidato), H.json (stock acumulado, para
    reportar unidades instaladas al final del horizonte) y parameters.json.

    Costo multi-año (idéntico a bess_investment_cost/bess_op_cost de
    functions.py): sum_y AF_y(r,Y) * Delta_H[y] * c_inv_h[h]/c_op_h[h].
    """
    delta_h_path = find_json_in_folder(root, "Delta_H.json")
    h_path       = find_json_in_folder(root, "H.json")
    params_path  = find_json_in_folder(root, "parameters.json")

    if not delta_h_path or not params_path:
        return {}
    if is_effectively_empty_json(delta_h_path) or is_effectively_empty_json(params_path):
        return {}

    params_data = load_json(params_path)

    def _extract_param(key: str) -> Dict[str, float]:
        raw   = params_data.get(key, {})
        inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
        return {str(h): _as_float(v, 0.0) for h, v in inner.items()} if isinstance(inner, dict) else {}

    delta_h_by_year = _year_only_vars(delta_h_path)
    h_by_year       = _year_only_vars(h_path)

    c_inv    = _extract_param("c_inv_h")
    c_op     = _extract_param("c_op_h")
    p_max    = _extract_param("p_max_h")
    a_max    = _extract_param("a_max_h")
    a_min    = _extract_param("a_min_h")
    stor_set = [str(h) for h in params_data.get("storage_set", list(c_inv.keys()))]

    years_sorted = _get_years_sorted(params_data)
    n_years = len(years_sorted)
    last_year_key = str(years_sorted[-1]) if years_sorted else None
    has_degradation = _has_degradation_data(params_data)
    discount_r = _get_discount_rate(params_data) if has_degradation else 0.0

    # Unidades acumuladas del cluster al final del horizonte (sin retiro de
    # activos); fallback a la suma de incrementos si H.json no esta disponible.
    units_final = h_by_year.get(last_year_key, 0.0) if last_year_key else sum(delta_h_by_year.values())

    units_info: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = 0.0

    for h in stor_set:
        inv = op = 0.0
        for pos_y, y in enumerate(years_sorted, start=1):
            dval = delta_h_by_year.get(str(y), 0.0)
            af = _annuity_factor_year(discount_r, pos_y, n_years) if has_degradation else 1.0
            inv += dval * c_inv.get(h, 0.0) * af
            op  += dval * c_op.get(h, 0.0) * af
        units_info[h] = {"installed": units_final, "p_max_kw": p_max.get(h, 0.0),
                         "a_max_kwh": a_max.get(h, 0.0), "a_min_kwh": a_min.get(h, 0.0),
                         "inv_cost": inv, "op_cost": op}
        total_inv += inv
        total_op  += op

    return {"units": units_info, "total_inv_cost": total_inv, "total_op_cost": total_op}


def calculate_battery_degradation_metrics(root: Path) -> Dict[str, Any]:
    """
    Lee R.json, b_bar.json, D.json, N_ciclos.json, N_batteries_total.json
    y parameters.json para reportar, por año, la degradación del pool de
    baterías de swap (global, fleet-wide):
    - Capacidad al inicio (b_bar) y al final (D) del año [kWh], y % de la
      nominal (b_max_pool) para b_bar
    - Ciclos equivalentes (EFC) del propio año (N_ciclos) -- ya no hay
      acumulado histórico explícito, el arrastre entre años lo da D[y-1]
      (ver b_y_link en functions.py)
    - Si hubo reemplazo (R=1) ese año y su costo (c_bat_replace * N_batteries_total)
    """
    b_bar_path = find_json_in_folder(root, "b_bar.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not b_bar_path or not params_path:
        return {}
    if is_effectively_empty_json(b_bar_path) or is_effectively_empty_json(params_path):
        return {}

    params_data = load_json(params_path)

    def _year_value_map(filename: str) -> Dict[str, float]:
        p = find_json_in_folder(root, filename)
        if not p or is_effectively_empty_json(p):
            return {}
        data = load_json(p)
        out: Dict[str, float] = {}
        for path, val in _iter_leaf_records(data):
            if not path:
                continue
            try:
                out[str(path[-1])] = float(val)
            except Exception:
                continue
        return out

    b_bar_map    = _year_value_map("b_bar.json")
    d_map        = _year_value_map("D.json")
    r_map        = _year_value_map("R.json")
    n_ciclos_map = _year_value_map("N_ciclos.json")

    # b_max_pool, min_capacity_fraction, c_bat_replace, discount_rate son
    # parametros globales (no indexados), escritos como escalar en
    # parameters.json.
    b_max_pool    = _as_float(params_data.get("b_max_pool"), 0.0)
    min_frac      = _as_float(params_data.get("min_capacity_fraction"), 0.0)
    # c_bat_replace es el costo TOTAL de UNA sola bateria del pool.
    c_bat_replace = _as_float(params_data.get("c_bat_replace"), 0.0)
    discount_r    = _as_float(params_data.get("discount_rate"), 0.0)

    # Inversion multi-año: N_batteries_total[y] y n_battery_fleet[y] (pool +
    # una bateria por LHD de swap) crecen año a año, ya no son un escalar
    # unico para todo el horizonte — se leen con eje de año.
    n_batteries_total_by_year = _year_only_vars(find_json_in_folder(root, "N_batteries_total.json"))
    n_batteries_fleet_by_year = _year_only_vars(find_json_in_folder(root, "n_battery_fleet.json"))
    n_slhd = len(params_data.get("slhd_set", []) or [])

    # Costo de reemplazo por año: se lee Z_repl[y] directo (ya linealiza
    # exacto R[y]*n_battery_fleet[y] en el modelo, ver
    # ObjectiveRules.battery_replace_cost/z_repl_upper1/upper2/lower) en vez
    # de reconstruirlo aca — evita duplicar esa linealizacion en el reporte.
    z_repl_by_year = _year_only_vars(find_json_in_folder(root, "Z_repl.json"))

    years = sorted(b_bar_map.keys(), key=lambda y: float(y))
    years_sorted_int = _get_years_sorted(params_data)
    last_year_key = str(years_sorted_int[-1]) if years_sorted_int else None

    years_info: Dict[str, Dict[str, Any]] = {}
    total_replace_cost = 0.0
    n_replacements = 0

    for y in years:
        b_bar = b_bar_map.get(y, 0.0)
        # R=0 no se exporta (binarias solo guardan valores en 1) -> default 0.0
        replaced = r_map.get(y, 0.0) >= 0.5
        disc = _year_discount_factor(discount_r, y, years_sorted_int)
        cost_y = z_repl_by_year.get(y, 0.0) * c_bat_replace * disc
        total_replace_cost += cost_y
        if replaced:
            n_replacements += 1
        years_info[y] = {
            "b_bar_kwh":    b_bar,
            "b_bar_pct":    (b_bar / b_max_pool * 100.0) if b_max_pool else 0.0,
            "n_ciclos":     n_ciclos_map.get(y, 0.0),
            "d_kwh":        d_map.get(y, 0.0),
            "replaced":     replaced,
            "replace_cost": cost_y,
        }

    # Tamaño del pool/flota al final del horizonte (sin retiro de activos,
    # es el maximo acumulado) para el resumen agregado.
    n_batteries_total_final = n_batteries_total_by_year.get(last_year_key, 0.0) if last_year_key else 0.0
    n_batteries_fleet_final = n_batteries_fleet_by_year.get(last_year_key, n_batteries_total_final + n_slhd) if last_year_key else (n_batteries_total_final + n_slhd)

    return {
        "years":              years_info,
        "b_max_pool_kwh":     b_max_pool,
        "min_capacity_pct":   min_frac * 100.0 if min_frac else 0.0,
        "n_batteries_total":  n_batteries_total_final,
        "n_slhd":             n_slhd,
        "n_batteries_fleet":  n_batteries_fleet_final,
        "n_replacements":     n_replacements,
        "total_replace_cost": total_replace_cost,
    }


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
        grid_energy_kwh = calculate_grid_energy_total_kwh(root)
    except Exception as ex:
        print(f"Advertencia al calcular energía total de red: {ex}")
        grid_energy_kwh = 0.0

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

    try:
        degradation_info    = calculate_battery_degradation_metrics(root)
        battery_replace_cost = degradation_info.get("total_replace_cost", 0.0)
    except Exception as ex:
        print(f"Advertencia al calcular costo de reemplazo de batería: {ex}")
        battery_replace_cost = 0.0

    # Todas las componentes (investment_cost, peak_power_cost, gen_*, bess_*,
    # grid_energy_cost, battery_replace_cost) aplican el mismo descuento NPV
    # (annuity_factor_expr/_discount_factor) que usa la función objetivo
    # cuando hay datos de BatteryDegradation cargados, así que este
    # total_cost debería coincidir con el objetivo reportado por Gurobi.
    total_cost = (investment_cost + grid_energy_cost + penalty_cost + peak_power_cost
                  + gen_inv_cost + gen_op_cost + bess_inv_cost + bess_op_cost
                  + battery_replace_cost)

    return {
        "energy_cost":          energy_cost,
        "grid_energy_cost":     grid_energy_cost,
        "grid_energy_kwh":      grid_energy_kwh,
        "real_energy_cost":     real_energy_cost,
        "investment_cost":      investment_cost,
        "penalty_cost":         penalty_cost,
        "peak_power_cost":      peak_power_cost,
        "gen_inv_cost":         gen_inv_cost,
        "gen_op_cost":          gen_op_cost,
        "bess_inv_cost":        bess_inv_cost,
        "bess_op_cost":         bess_op_cost,
        "battery_replace_cost": battery_replace_cost,
        "total_cost":           total_cost,
    }


if __name__ == "__main__":
    main()
