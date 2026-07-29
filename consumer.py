#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple, Optional

# ============================================================
# CONFIG: define aquÃ­ el delta t por defecto (minutos)
# (Puedes sobreescribirlo con --delta-minutes en la consola)
# ============================================================
DEFAULT_DELTA_MINUTES = 8.0


# -----------------------------
# Helpers lectura / vacÃ­o
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

    # prioriza el mÃ¡s grande
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
# Serie genÃ©rica para E/B
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

def iter_timeseries_from_i_d_t(data: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Extract timeseries from i -> d -> t layout (for B.json).
    Yields each t-series dict (time_interval -> value).
    """
    if not isinstance(data, dict) or "i" not in data or not isinstance(data["i"], dict):
        return
    
    for lhd_data in data["i"].values():
        if not isinstance(lhd_data, dict) or "d" not in lhd_data:
            continue
        d_block = lhd_data.get("d")
        if not isinstance(d_block, dict):
            continue
        
        for day_data in d_block.values():
            if not isinstance(day_data, dict) or "t" not in day_data:
                continue
            t_series = day_data.get("t")
            if isinstance(t_series, dict):
                yield t_series


def mean_drop_rate_per_hour_fixed_dt(
    json_path: Path,
    delta_minutes: float,
    eps: float,
    label_total_drop: str,
) -> Tuple[float, Dict[str, float]]:
    """
    Calcula consumo medio horario considerando SOLO pasos donde el nivel baja,
    leyendo la estructura i -> d -> t de B.json (SOC por baterÃ­a, dÃ­a, intervalo).
    """
    data = load_json(json_path)
    if not isinstance(data, dict):
        raise ValueError("JSON inesperado: la raÃ­z no es un objeto/dict.")

    step_hours = delta_minutes / 60.0
    total_drop = 0.0
    n_drop_steps = 0
    n_vehicles_used = 0
    n_series = 0
    n_pairs_total = 0
    vehicles_seen = set()

    for series_map in iter_timeseries_from_i_d_t(data):
        if not isinstance(series_map, dict) or len(series_map) == 0:
            continue
        
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
        
        if n_series == 1:
            vehicles_seen.add(True)

    if n_drop_steps == 0:
        raise ValueError("No se encontraron descensos (pasos con consumo) en el JSON.")

    mean_per_hour = total_drop / (n_drop_steps * step_hours)

    stats = {
        "mean_per_hour": mean_per_hour,
        label_total_drop: total_drop,
        "n_drop_steps": float(n_drop_steps),
        "n_pairs_total": float(n_pairs_total),
        "n_vehicles_used": float(len(vehicles_seen) if vehicles_seen else 1),
        "n_series": float(n_series),
        "delta_minutes": float(delta_minutes),
    }
    return mean_per_hour, stats


def mean_consumption_lph(e_json_path: Path, delta_minutes: float, eps: float = 1e-9):
    return mean_drop_rate_per_hour_fixed_dt(
        json_path=e_json_path,
        delta_minutes=delta_minutes,
        eps=eps,
        label_total_drop="total_drop_liters",
    )


def mean_consumption_kwhph(b_json_path: Path, delta_minutes: float, eps: float = 1e-9):
    return mean_drop_rate_per_hour_fixed_dt(
        json_path=b_json_path,
        delta_minutes=delta_minutes,
        eps=eps,
        label_total_drop="total_drop_kwh",
    )


def peak_power_from_p(p_json_path: Path) -> float:
    """Potencia peak de operación [kW]: max_{d,t} sum_{k,i} P[k,i,d,t]."""
    data = load_json(p_json_path)
    if not isinstance(data, dict) or "k" not in data or not isinstance(data["k"], dict):
        raise ValueError("Estructura P.json inesperada")

    power_by_dt: Dict[Tuple[str, str], float] = {}

    for station_data in data["k"].values():
        if not isinstance(station_data, dict):
            continue
        for lhd_data in station_data.get("i", {}).values():
            if not isinstance(lhd_data, dict):
                continue
            for day_key, day_data in lhd_data.get("d", {}).items():
                if not isinstance(day_data, dict):
                    continue
                for t_key, power in day_data.get("t", {}).items():
                    key = (str(day_key), str(t_key))
                    power_by_dt[key] = power_by_dt.get(key, 0.0) + _as_float(power, 0.0)

    if not power_by_dt:
        raise ValueError("No se encontraron valores en P.json")

    return max(power_by_dt.values())


# -----------------------------
# M: extracciÃ³n total (sumar nÃºmeros)
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
    """Cuenta viajes diarios como activaciones positivas de Y por dÃ­a."""
    data = load_json(y_json_path)
    trips_by_day: Dict[str, float] = {}

    if not isinstance(data, dict) or "i" not in data or not isinstance(data["i"], dict):
        return trips_by_day

    for lhd_data in data["i"].values():
        if not isinstance(lhd_data, dict):
            continue
        j_block = lhd_data.get("j", {})
        if not isinstance(j_block, dict):
            continue

        for node_data in j_block.values():
            if not isinstance(node_data, dict):
                continue
            d_block = node_data.get("d", {})
            if not isinstance(d_block, dict):
                continue

            for day_key, day_data in d_block.items():
                if not isinstance(day_data, dict):
                    continue
                t_block = day_data.get("t", {})
                if not isinstance(t_block, dict):
                    continue

                day = str(day_key)
                trips_by_day.setdefault(day, 0.0)
                for val in t_block.values():
                    if _as_float(val, 0.0) > 0.5:
                        trips_by_day[day] += 1.0

    return trips_by_day


def calculate_daily_charged_energy(p_json_path: Path, step_hours: float) -> Dict[str, float]:
    """Suma energÃ­a diaria [kWh] desde P [kW] usando step_hours."""
    data = load_json(p_json_path)
    energy_by_day: Dict[str, float] = {}

    if not isinstance(data, dict) or "k" not in data or not isinstance(data["k"], dict):
        return energy_by_day

    for station_data in data["k"].values():
        if not isinstance(station_data, dict):
            continue
        i_block = station_data.get("i", {})
        if not isinstance(i_block, dict):
            continue

        for lhd_data in i_block.values():
            if not isinstance(lhd_data, dict):
                continue
            d_block = lhd_data.get("d", {})
            if not isinstance(d_block, dict):
                continue

            for day_key, day_data in d_block.items():
                if not isinstance(day_data, dict):
                    continue
                t_block = day_data.get("t", {})
                if not isinstance(t_block, dict):
                    continue

                day = str(day_key)
                energy_by_day.setdefault(day, 0.0)
                for power in t_block.values():
                    energy_by_day[day] += _as_float(power, 0.0) * step_hours

    return energy_by_day


def _extract_y_counts(y_json_path: Path) -> Dict[Tuple[str, str, str], float]:
    """Retorna sum_t Y[i,j,d,t] por clave (i,j,d)."""
    data = load_json(y_json_path)
    y_counts: Dict[Tuple[str, str, str], float] = {}

    if not isinstance(data, dict) or "i" not in data or not isinstance(data["i"], dict):
        return y_counts

    for i_name, lhd_data in data["i"].items():
        if not isinstance(lhd_data, dict):
            continue
        j_block = lhd_data.get("j", {})
        if not isinstance(j_block, dict):
            continue

        for j_name, node_data in j_block.items():
            if not isinstance(node_data, dict):
                continue
            d_block = node_data.get("d", {})
            if not isinstance(d_block, dict):
                continue

            for day_key, day_data in d_block.items():
                if not isinstance(day_data, dict):
                    continue
                t_inner = day_data.get("t", {})
                if not isinstance(t_inner, dict):
                    continue

                key = (str(i_name), str(j_name), str(day_key))
                y_counts.setdefault(key, 0.0)
                for val in t_inner.values():
                    y_counts[key] += _as_float(val, 0.0)

    return y_counts


def _extract_m_values(m_json_path: Path) -> Dict[Tuple[str, str, str], float]:
    """Retorna M[i,j,d] por clave (i,j,d)."""
    data = load_json(m_json_path)
    m_values: Dict[Tuple[str, str, str], float] = {}

    if not isinstance(data, dict) or "i" not in data or not isinstance(data["i"], dict):
        return m_values

    for i_name, i_data in data["i"].items():
        if not isinstance(i_data, dict):
            continue
        j_block = i_data.get("j", {})
        if not isinstance(j_block, dict):
            continue

        for j_name, j_data in j_block.items():
            if not isinstance(j_data, dict):
                continue
            d_block = j_data.get("d", {})
            if not isinstance(d_block, dict):
                continue

            for day_key, m_val in d_block.items():
                # day_data can be a dict with 't' -> {interval: value} or a direct numeric
                if isinstance(m_val, dict):
                    # Prefer summing over 't' if present, else sum any numeric children
                    t_block = m_val.get("t") if isinstance(m_val.get("t", None), dict) else None
                    if isinstance(t_block, dict):
                        total = sum(_as_float(v, 0.0) for v in t_block.values())
                    else:
                        # Sum any numeric entries inside the dict (fallback)
                        total = 0.0
                        for v in m_val.values():
                            total += _as_float(v, 0.0)
                else:
                    total = _as_float(m_val, 0.0)

                m_values[(str(i_name), str(j_name), str(day_key))] = total

    return m_values


def calculate_max_extraction_by_node(m_json_path: Path, y_json_path: Path) -> Dict[str, Any]:
    """Calcula la máxima extracción por nodo j.
    
    Retorna:
    - max_extraction: valor máximo de M sumado por nodo
    - max_node: nodo j con máxima extracción
    - num_intervals: cantidad de intervalos asignados a ese nodo (sum(Y) > 0.5)
    """
    m_values = _extract_m_values(m_json_path)
    y_counts = _extract_y_counts(y_json_path)
    
    # Suma M por nodo j (agregando sobre i, d)
    m_by_node: Dict[str, float] = {}
    for (i_name, j_name, day_key), m_val in m_values.items():
        m_by_node.setdefault(j_name, 0.0)
        m_by_node[j_name] += m_val
    
    if not m_by_node:
        return {
            "max_extraction": 0.0,
            "max_node": "N/A",
            "num_intervals": 0.0,
        }
    
    # Encontrar nodo con máxima extracción
    max_node = max(m_by_node, key=m_by_node.get)
    max_extraction = m_by_node[max_node]
    
    # Contar intervalos asignados a ese nodo (sum(Y[i, max_node, d, t]) > 0.5)
    num_intervals = 0.0
    for (i_name, j_name, day_key), y_sum in y_counts.items():
        if j_name == max_node and y_sum > 0.5:
            num_intervals += y_sum
    
    return {
        "max_extraction": max_extraction,
        "max_node": max_node,
        "num_intervals": num_intervals,
    }


def calculate_cycles_from_y_ntrips(y_json_path: Path, m_json_path: Path, params_path: Path) -> Tuple[float, Dict[str, float]]:
    """Calcula ciclos con cycles_total = sum(Y[i,j] * n_trips[j,i]).

    n_trips[j,i] se reconstruye desde outputs usando:
    M[i,j,d] = (sum_t Y[i,j,d,t]) * g_i[i] * n_trips[j,i] * filling_factor[i]
    """
    y_counts = _extract_y_counts(y_json_path)
    m_values = _extract_m_values(m_json_path)
    params = load_json(params_path)

    g_i = params.get("g_i", {}).get("_1", {}) if isinstance(params, dict) else {}
    filling = params.get("filling_factor", {}).get("_1", {}) if isinstance(params, dict) else {}

    # EstimaciÃ³n de n_trips[j,i] por promedio ponderado en Y
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

    # cycles_total = sum(Y * n_trips)
    cycles_total = 0.0
    cycles_by_day: Dict[str, float] = {}
    for (i_name, j_name, day_key), y_sum in y_counts.items():
        ntr = n_trips_map.get((i_name, j_name), 0.0)
        cycles = y_sum * ntr
        cycles_total += cycles
        cycles_by_day[day_key] = cycles_by_day.get(day_key, 0.0) + cycles

    return cycles_total, cycles_by_day


def infer_step_hours(root: Path, fallback_minutes: float) -> float:
    params_path = find_json_in_folder(root, "parameters.json")
    if params_path and not is_effectively_empty_json(params_path):
        params_data = load_json(params_path)
        dt = params_data.get("dt", params_data.get("delta_t", None))
        if dt is not None:
            dt_val = _as_float(dt, -1.0)
            if dt_val > 0:
                return dt_val
    return fallback_minutes / 60.0


# -----------------------------
# Agregacion de macrobloques (output de run_descomposicion.py)
# -----------------------------
_STATION_DAY_RE = re.compile(r"^(.+)_d(\d+)$")


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


def group_by_station(subfolders: List[Path]) -> Optional[Dict[str, List[Path]]]:
    """Si las subcarpetas siguen el patron <estacion>_d<dia> (salida de
    run_descomposicion.py --parallel_days), agrupa por estacion.
    Devuelve None si alguna subcarpeta no sigue ese patron."""
    groups: Dict[str, List[Path]] = {}
    for sub in subfolders:
        m = _STATION_DAY_RE.match(sub.name)
        if not m:
            return None
        station = m.group(1)
        groups.setdefault(station, []).append(sub)
    return groups


def _year_of_day(day: int) -> int:
    return ((int(day) - 1) // 365) + 1


def _peak_clock_interval_set(delta_t: float, max_t: int, base_hour: float = 8.5) -> set:
    """Replica el calculo de model.time_intervals_peak_set en
    OptSets.build_sets (functions.py): horizonte operativo arranca a
    base_hour (leido de parameters.json, escrito desde Shifts.base_hour),
    ventana punta 18:00-22:00."""
    dt_minutes = int(round(delta_t * 60))
    if dt_minutes <= 0:
        return set()
    base_minutes = int(round(base_hour * 60))
    out = set()
    for t in range(1, max_t + 1):
        clock = (base_minutes + (t - 1) * dt_minutes) % 1440
        if 18 * 60 <= clock < 22 * 60:
            out.add(t)
    return out


def calculate_combined_peak_power_cost(subfolders: List[Path]) -> Tuple[float, Dict[str, float]]:
    """Costo por potencia pico 'ex post', calculado con la demanda REAL
    combinada (suma de P_red de todos los macrobloques en cada dia/intervalo),
    en vez de sumar el power_cost de cada macrobloque por separado (que puede
    subestimarse porque cada uno ve solo su propia demanda aislada y puede
    evitar cargar en horario punta, cosa que la demanda combinada de todas
    las estaciones no necesariamente puede evitar).

    Replica power_peak_limit / power_cost_peak_limit (functions.py): demand
    charge = 12*10 por P_pot[year], solo en dias de temporada de punta
    (91<=d<=244) y en la ventana horaria 18:00-22:00 (horizonte operativo
    arrancando a las 08:30).
    """
    combined: Dict[int, Dict[int, float]] = {}
    delta_t = None
    base_hour = 8.5
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
            base_hour = _as_float(params_data.get("base_hour", 8.5))

        pred_data = load_json(pred_path)
        d_block = pred_data.get("d", pred_data)
        if not isinstance(d_block, dict):
            continue
        for d_key, d_val in d_block.items():
            if not isinstance(d_val, dict):
                continue
            t_block = d_val.get("t", d_val)
            if not isinstance(t_block, dict):
                continue
            try:
                day = int(float(d_key))
            except Exception:
                continue
            for t_key, value in t_block.items():
                try:
                    t = int(float(t_key))
                    v = float(value)
                except Exception:
                    continue
                combined.setdefault(day, {})[t] = combined.get(day, {}).get(t, 0.0) + v

    if not combined or not delta_t:
        return 0.0, {"note": "No se pudo calcular (faltan P_red.json/parameters.json)"}

    max_t = max(t for day_map in combined.values() for t in day_map)
    peak_t_set = _peak_clock_interval_set(delta_t, max_t, base_hour)

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


def calculate_peak_charging_power(subfolders: List[Path]) -> Tuple[float, Dict[str, float]]:
    """Potencia pico de carga combinada [kW] = max_{d,t} sum_{k,i} P[k,i,d,t],
    combinando los P.json de todas las subcarpetas (estaciones) para el mismo
    (d,t) antes de buscar el maximo, ya que las estaciones cargan en
    paralelo."""
    combined: Dict[Tuple[int, int], float] = {}

    for sub in subfolders:
        p_path = find_json_in_folder(sub, "P.json")
        if not p_path or is_effectively_empty_json(p_path):
            continue
        data = load_json(p_path)
        if not isinstance(data, dict) or not isinstance(data.get("k"), dict):
            continue
        for station_data in data["k"].values():
            if not isinstance(station_data, dict):
                continue
            for lhd_data in station_data.get("i", {}).values():
                if not isinstance(lhd_data, dict):
                    continue
                for d_key, d_val in lhd_data.get("d", {}).items():
                    if not isinstance(d_val, dict):
                        continue
                    for t_key, power in d_val.get("t", {}).items():
                        try:
                            key = (int(float(d_key)), int(float(t_key)))
                            v = float(power)
                        except Exception:
                            continue
                        combined[key] = combined.get(key, 0.0) + v

    if not combined:
        return 0.0, {"note": "No se pudo calcular (faltan P.json)"}

    best_key = max(combined, key=lambda k: combined[k])
    peak_power_kw = combined[best_key]
    meta = {
        "peak_power_kw": peak_power_kw,
        "dia_pico": best_key[0],
        "intervalo_pico": best_key[1],
    }
    return peak_power_kw, meta


def read_total_n_chargers(
    subfolders: List[Path],
    station_groups: Optional[Dict[str, List[Path]]],
    is_day_decomposed: bool,
) -> float:
    """Suma N_chargers de todas las estaciones.

    N_chargers es infraestructura fija por estacion (no depende del dia), asi
    que si hay descomposicion por dia se cuenta una sola vez por estacion
    (se usa la primera subcarpeta-dia disponible de cada una)."""
    reps = [subs[0] for subs in station_groups.values()] if (is_day_decomposed and station_groups) else subfolders

    total = 0.0
    for sub in reps:
        n_path = find_json_in_folder(sub, "N_chargers.json")
        if not n_path or is_effectively_empty_json(n_path):
            continue
        data = load_json(n_path)
        inner = data.get("k", data) if isinstance(data, dict) else {}
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
            p_path = find_json_in_folder(sub, "P.json")
            step_hours = infer_step_hours(sub, DEFAULT_DELTA_MINUTES)
            energy_daily = (
                calculate_daily_charged_energy(p_path, step_hours)
                if (p_path and not is_effectively_empty_json(p_path)) else {}
            )
            per_station_energy[sub.name] = sum(energy_daily.values())
        except Exception:
            per_station_energy[sub.name] = 0.0

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

    # El power_cost de cada macrobloque puede subestimar la realidad porque
    # cada uno ve solo su propia demanda aislada. El valor real (ex post) se
    # calcula con la demanda combinada de todos los macrobloques en el mismo
    # dia/intervalo.
    combined_peak_cost, peak_meta = calculate_combined_peak_power_cost(subfolders)
    peak_charging_kw, peak_charging_meta = calculate_peak_charging_power(subfolders)
    naive_total = sum(per_station_costs[s.name].get("total_cost", 0.0) for s in subfolders)
    naive_peak = sum(per_station_costs[s.name].get("power_cost", 0.0) for s in subfolders)
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
        opex_keys = ("grid_energy_cost", "penalty_cost", "gen_op_cost", "bess_op_cost")

        capex_total = 0.0
        opex_total = 0.0
        dedup_rows = []
        for station, subs_for_station in station_groups.items():
            capex_value = per_station_costs[subs_for_station[0].name].get("investment_cost", 0.0) \
                + per_station_costs[subs_for_station[0].name].get("gen_inv_cost", 0.0) \
                + per_station_costs[subs_for_station[0].name].get("bess_inv_cost", 0.0)
            opex_value = sum(
                per_station_costs[s.name].get(k, 0.0)
                for s in subs_for_station for k in opex_keys
            )
            capex_total += capex_value
            opex_total += opex_value
            dedup_rows.append([station, str(len(subs_for_station)), f"{capex_value:.2f}", f"{opex_value:.2f}"])

        if not summary_only:
            dedup_rows_display = dedup_rows + [["TOTAL", str(len(subfolders)), f"{capex_total:.2f}", f"{opex_total:.2f}"]]
            print(make_table(
                "COSTOS DEDUPLICADOS POR ESTACION (inversion contada 1 vez, no por dia)",
                ["Estacion", "N dias", "Inversion (1 vez)", "Operacion (suma dias)"],
                dedup_rows_display,
            ))
            print()

        dedup_total = capex_total + opex_total + combined_peak_cost
        print(make_table(
            "COSTO TOTAL FINAL (deduplicado, todas las estaciones, todos los dias)",
            ["Concepto", "Valor"],
            [
                ["Inversion (1 vez por estacion)", f"{capex_total:.2f}"],
                ["Operacion (suma de todos los dias)", f"{opex_total:.2f}"],
                ["Potencia pico ex post (combinado)", f"{combined_peak_cost:.2f}"],
                ["COSTO TOTAL", f"{dedup_total:.2f}"],
            ],
        ))
        print()
    elif summary_only:
        # Carpeta plana (station_1/2/3, sin dia): el total corregido ya no
        # tiene el problema de duplicar inversion por dia.
        print(make_table(
            "COSTO TOTAL FINAL",
            ["Concepto", "Valor"],
            [
                ["Potencia pico ex post (combinado)", f"{combined_peak_cost:.2f}"],
                ["COSTO TOTAL", f"{corrected_total:.2f}"],
            ],
        ))
        print()

    extraction_total = sum(per_station_extraction.values())
    energy_total = sum(per_station_energy.values())
    peak_charging_str = (
        f"{peak_charging_kw:.2f} kW "
        f"(dia {peak_charging_meta.get('dia_pico', '?')} t={peak_charging_meta.get('intervalo_pico', '?')})"
    )

    total_n_chargers = read_total_n_chargers(subfolders, station_groups, is_day_decomposed)
    scaling_factor = read_scaling_factor_op_cost(subfolders)
    extraction_annual_str = f"{extraction_total * scaling_factor:.2f}" if scaling_factor else "N/D"
    energy_annual_str = f"{energy_total * scaling_factor:.2f}" if scaling_factor else "N/D"

    if summary_only:
        print(make_table(
            "METRICAS COMBINADAS (todas las estaciones, todos los dias)",
            ["Concepto", "Valor"],
            [
                ["Extraccion total combinada (dias significativos)", f"{extraction_total:.2f}"],
                ["Extraccion total anualizada", extraction_annual_str],
                ["Energia cargada total combinada (P) [kWh] (dias significativos)", f"{energy_total:.2f}"],
                ["Energia cargada total anualizada [kWh]", energy_annual_str],
                ["Potencia pico de carga (combinada)", peak_charging_str],
                ["Cargadores totales (todas las estaciones)", f"{total_n_chargers:.0f}"],
            ],
        ))
    else:
        headers = ["Concepto"] + [s.name for s in subfolders] + ["TOTAL"]
        summary_rows = [
            ["Extraccion total combinada (dias significativos)"] + [f"{per_station_extraction[s.name]:.2f}" for s in subfolders] + [f"{extraction_total:.2f}"],
            ["Extraccion total anualizada"] + ["" for _ in subfolders] + [extraction_annual_str],
            ["Energia cargada total combinada (P) [kWh] (dias significativos)"] + [f"{per_station_energy[s.name]:.2f}" for s in subfolders] + [f"{energy_total:.2f}"],
            ["Energia cargada total anualizada [kWh]"] + ["" for _ in subfolders] + [energy_annual_str],
            ["Potencia pico de carga (combinada) [kW]"] + ["" for _ in subfolders] + [peak_charging_str],
            ["Cargadores totales (todas las estaciones)"] + ["" for _ in subfolders] + [f"{total_n_chargers:.0f}"],
        ]
        print(make_table("METRICAS COMBINADAS", headers, summary_rows))
    print()


# -----------------------------
# CLI principal
# -----------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Analiza outputs desde una carpeta: E (L/h), B (kWh/h) y M (extracciÃ³n total). "
                    "Omite automÃ¡ticamente JSON faltantes o vacÃ­os."
    )
    ap.add_argument("folder", help="Ruta de la carpeta que contiene los JSON (bÃºsqueda recursiva).")
    ap.add_argument(
        "--delta-minutes",
        type=float,
        default=DEFAULT_DELTA_MINUTES,
        help=f"Delta t manual (minutos) para E/B (default: {DEFAULT_DELTA_MINUTES})",
    )
    ap.add_argument("--eps", type=float, default=1e-9, help="Umbral para considerar descenso (default: 1e-9)")
    ap.add_argument(
        "--summary_only", action="store_true",
        help="En carpetas desacopladas (macrobloques), imprime solo el resumen agregado final.",
    )
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Carpeta no vÃ¡lida: {root}")

    macrobloque_subfolders = find_macrobloque_subfolders(root)
    if macrobloque_subfolders:
        analyze_macrobloques(root, macrobloque_subfolders, args)
        return

    e_path = find_json_in_folder(root, "E.json")
    b_path = find_json_in_folder(root, "B.json")
    m_path = find_json_in_folder(root, "M.json")
    y_path = find_json_in_folder(root, "Y.json")
    p_path = find_json_in_folder(root, "P.json")
    params_path = find_json_in_folder(root, "parameters.json")

    # Tabla â€œinputsâ€
    inputs_rows = [
        ["Folder", str(root)],
        ["Delta t (min)", fmt_num(args.delta_minutes, 3)],
        ["E.json", str(e_path) if e_path else "NO ENCONTRADO"],
        ["B.json", str(b_path) if b_path else "NO ENCONTRADO"],
        ["M.json", str(m_path) if m_path else "NO ENCONTRADO"],
        ["Y.json", str(y_path) if y_path else "NO ENCONTRADO"],
        ["P.json", str(p_path) if p_path else "NO ENCONTRADO"],
        ["parameters.json", str(params_path) if params_path else "NO ENCONTRADO"],
    ]
    print(make_table("INPUTS", ["Campo", "Valor"], inputs_rows))
    print()

    printed_any = False

    # ---- B
    if b_path and not is_effectively_empty_json(b_path):
        try:
            kwhph, s = mean_consumption_kwhph(b_path, delta_minutes=args.delta_minutes, eps=args.eps)
            rows = [
                ["Promedio consumo (solo descensos)", f"{kwhph:.6f} kWh/h"],
                ["EnergÃ­a consumida (solo descensos)", f"{s['total_drop_kwh']:.6f} kWh"],
                ["Pasos con descenso", str(int(s["n_drop_steps"]))],
                ["Pares totales evaluados", str(int(s["n_pairs_total"]))],
                ["Series (bateria-dia)", str(int(s["n_series"]))],
                ["Delta t usado", f"{s['delta_minutes']:.3f} min"],
            ]
            if p_path and not is_effectively_empty_json(p_path):
                try:
                    p_peak = peak_power_from_p(p_path)
                    rows.append(["P_peak operacion", f"{p_peak:.3f} kW"])
                except Exception:
                    pass
            print(make_table("B (ELÃ‰CTRICO)", ["MÃ©trica", "Valor"], rows))
            print()
            printed_any = True
        except Exception as ex:
            print(make_table("B (ELÃ‰CTRICO)", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
            print()
    else:
        print(make_table("B (ELÃ‰CTRICO)", ["Estado", "Detalle"], [["OMITIDO", "No encontrado o vacÃ­o/no usable."]]))
        print()

    # ---- M
    if m_path and not is_effectively_empty_json(m_path):
        try:
            tot, meta = total_extraction(m_path)
            rows = [
                ["ExtracciÃ³n total (toda la operaciÃ³n)", f"{tot:.6f} (unidad de M)"],
                ["Valores numÃ©ricos sumados", str(meta["values_summed"])],
            ]            
            # Agregar métricas de máxima extracción por nodo
            if y_path and not is_effectively_empty_json(y_path):
                try:
                    max_info = calculate_max_extraction_by_node(m_path, y_path)
                    rows.append(["Máxima extracción (nodo)", f"{max_info['max_extraction']:.6f}"])
                    rows.append(["Nodo con máxima extracción", str(max_info['max_node'])])
                    rows.append(["Intervalos asignados a ese nodo", f"{max_info['num_intervals']:.0f}"])
                except Exception as ex:
                    pass
            
            print(make_table("M (EXTRACCION)", ["Metrica", "Valor"], rows))
            print()
            printed_any = True
        except Exception as ex:
            print(make_table("M (EXTRACCION)", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
            print()
    else:
        print(make_table("M (EXTRACCION)", ["Estado", "Detalle"], [["OMITIDO", "No encontrado o vacio/no usable."]]))
        print()

    if not printed_any:
        print("No se pudo calcular nada: no encontrÃ© JSON utilizables (o todos estaban vacÃ­os).")

    # ---- OperaciÃ³n diaria (viajes y energÃ­a)
    try:
        trips_daily = calculate_daily_trips(y_path) if (y_path and not is_effectively_empty_json(y_path)) else {}
        step_hours = infer_step_hours(root, args.delta_minutes)
        energy_daily = calculate_daily_charged_energy(p_path, step_hours) if (p_path and not is_effectively_empty_json(p_path)) else {}
        cycles_total = 0.0
        cycles_daily: Dict[str, float] = {}
        if (
            y_path and m_path and params_path
            and not is_effectively_empty_json(y_path)
            and not is_effectively_empty_json(m_path)
            and not is_effectively_empty_json(params_path)
        ):
            cycles_total, cycles_daily = calculate_cycles_from_y_ntrips(y_path, m_path, params_path)

        all_days = sorted(set(trips_daily.keys()) | set(energy_daily.keys()) | set(cycles_daily.keys()), key=lambda d: float(d))
        if all_days:
            rows = []
            total_trips = 0.0
            total_energy = 0.0
            total_cycles = 0.0

            for d in all_days:
                trips = trips_daily.get(d, 0.0)
                energy = energy_daily.get(d, 0.0)
                cycles = cycles_daily.get(d, 0.0)

                total_trips += trips
                total_energy += energy
                total_cycles += cycles

                rows.append([
                    d,
                    f"{trips:.0f}",
                    f"{cycles:.3f}",
                    f"{energy:.3f}",
                ])

            rows.append([
                "TOTAL",
                f"{total_trips:.0f}",
                f"{total_cycles:.3f}",
                f"{total_energy:.3f}",
            ])
            print(make_table(
                "OPERACIÃ“N DIARIA",
                ["DÃ­a", "Viajes diarios totales", "Ciclos (Y*n_trips)", "EnergÃ­a diaria [kWh]"],
                rows,
            ))
            print()
            print(make_table(
                "CICLOS TOTALES",
                ["MÃ©trica", "Valor"],
                [["cycles_total = sum(Y[i,j] * n_trips[j,i])", f"{cycles_total:.3f}"]],
            ))
            print()
    except Exception as ex:
        print(make_table("OPERACIÃ“N DIARIA", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()
    
    # ---- Generación renovable
    try:
        gen_info = calculate_gen_costs(root)
        if gen_info and gen_info.get("generators"):
            rows = []
            for g, info in gen_info["generators"].items():
                rows.append([
                    g,
                    f"{info['units']:.0f}",
                    f"{info['p_max_unit']:.0f}",
                    f"{info['power_kw']:.0f}",
                    f"{info['inv_cost']:.2f}",
                    f"{info['op_cost']:.2f}",
                ])
            rows.append([
                "TOTAL", "", "",
                f"{gen_info['total_power_kw']:.0f}",
                f"{gen_info['total_inv_cost']:.2f}",
                f"{gen_info['total_op_cost']:.2f}",
            ])
            print(make_table(
                "GENERACION RENOVABLE",
                ["Generador", "Unidades", "P_max/unidad [kW]", "Potencia inst. [kW]", "Costo inv.", "Costo op. anual"],
                rows,
            ))
            print()
    except Exception as ex:
        print(make_table("GENERACION RENOVABLE", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    # ---- Almacenamiento BESS
    try:
        bess_info = calculate_bess_costs(root)
        if bess_info and bess_info.get("units"):
            rows = []
            for h, info in bess_info["units"].items():
                estado = "Instalado" if info["installed"] >= 0.5 else "No instalado"
                rows.append([
                    h,
                    estado,
                    f"{info['p_max_kw']:.0f}",
                    f"{info['a_min_kwh']:.0f}",
                    f"{info['a_max_kwh']:.0f}",
                    f"{info['inv_cost']:.2f}",
                    f"{info['op_cost']:.2f}",
                ])
            rows.append([
                "TOTAL", "", "", "", "",
                f"{bess_info['total_inv_cost']:.2f}",
                f"{bess_info['total_op_cost']:.2f}",
            ])
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
                ["Costo energía carga",         f"{costs['energy_cost']:.2f}"],
                ["Costo energía red P_red",      f"{costs.get('grid_energy_cost', 0.0):.2f}"],
                ["Costo inversión estaciones",   f"{costs['investment_cost']:.2f}"],
                ["Costo inversión generación",   f"{costs['gen_inv_cost']:.2f}"],
                ["Costo operación generación",   f"{costs['gen_op_cost']:.2f}"],
                ["Costo inversión BESS",         f"{costs['bess_inv_cost']:.2f}"],
                ["Costo operación BESS",         f"{costs['bess_op_cost']:.2f}"],
                ["Costo potencia punta",         f"{costs['power_cost']:.2f}"],
                ["Costo penalidad",              f"{costs['penalty_cost']:.2f}"],
                ["COSTO TOTAL",                  f"{costs['total_cost']:.2f}"],
            ]
            print(make_table("COSTOS", ["Concepto", "Valor"], rows))
            print()
    except Exception as ex:
        print(make_table("COSTOS", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()


# -----------------------------
# MÃ³dulo de cÃ¡lculo de costos
# -----------------------------
def calculate_lhd_charge_cost(root: Path) -> float:
    """
    Calcula el costo de carga de los LHD elÃ©ctricos.
    Usa P.json (potencia de carga) y parameters.json (costos marginales y delta_t).
    """
    p_path = find_json_in_folder(root, "P.json")
    params_path = find_json_in_folder(root, "parameters.json")
    
    if not p_path or not params_path:
        raise ValueError("No se encontraron P.json o parameters.json")
    
    if is_effectively_empty_json(p_path) or is_effectively_empty_json(params_path):
        raise ValueError("P.json o parameters.json estÃ¡n vacÃ­os")
    
    p_data = load_json(p_path)
    params_data = load_json(params_path)
    
    delta_t = params_data.get("dt", params_data.get("delta_t", 0.5))
    costo_marginal = params_data.get("costo_marginal", {})
    scaling_factor = params_data.get("scaling_factor_op_cost", 1.0)
    
    total_cost = 0.0
    
    # Estructura esperada: k -> station -> i -> lhd -> d -> day -> t -> interval
    if "k" in p_data and isinstance(p_data["k"], dict):
        for station_data in p_data["k"].values():
            if not isinstance(station_data, dict):
                continue
            lhd_block = station_data.get("i", {})
            if not isinstance(lhd_block, dict):
                continue
            for lhd_id, lhd_data in lhd_block.items():
                if not isinstance(lhd_data, dict):
                    continue
                day_block = lhd_data.get("d", {})
                if not isinstance(day_block, dict):
                    continue
                for day_key, day_data in day_block.items():
                    if not isinstance(day_data, dict):
                        continue
                    time_block = day_data.get("t", {})
                    if not isinstance(time_block, dict):
                        continue
                    for t_str, power in time_block.items():
                        try:
                            cost = 1.0
                            if "_1" in costo_marginal:
                                cm_lhd = costo_marginal["_1"].get(lhd_id, {})
                                if "_2" in cm_lhd:
                                    cm_day = cm_lhd["_2"].get(str(day_key), {})
                                    if "_3" in cm_day:
                                        cost = cm_day["_3"].get(str(t_str), 1.0)

                            total_cost += float(power) * float(cost) * float(delta_t)
                        except (ValueError, TypeError):
                            continue

    # Compatibilidad con estructura antigua: d -> station -> t -> lhd -> i -> day -> ? -> interval
    elif "d" in p_data and isinstance(p_data["d"], dict):
        for station_data in p_data["d"].values():
            if not isinstance(station_data, dict):
                continue
            t_block = station_data.get("t", {})
            if not isinstance(t_block, dict):
                continue
            for lhd_id, lhd_data in t_block.items():
                if not isinstance(lhd_data, dict):
                    continue
                i_block = lhd_data.get("i", {})
                if not isinstance(i_block, dict):
                    continue
                for day_key, day_data in i_block.items():
                    if not isinstance(day_data, dict):
                        continue
                    for time_data in day_data.values():
                        if not isinstance(time_data, dict):
                            continue
                        for t_str, power in time_data.items():
                            try:
                                cost = 1.0
                                if "_1" in costo_marginal:
                                    cm_lhd = costo_marginal["_1"].get(lhd_id, {})
                                    if "_2" in cm_lhd:
                                        cm_day = cm_lhd["_2"].get(str(day_key), {})
                                        if "_3" in cm_day:
                                            cost = cm_day["_3"].get(str(t_str), 1.0)

                                total_cost += float(power) * float(cost) * float(delta_t)
                            except (ValueError, TypeError):
                                continue
    
    return total_cost * scaling_factor


def calculate_investment_cost(root: Path) -> float:
    """
    Costo de inversión en estaciones, idéntico a inversion_cost() de functions.py.

    Detecta el tipo de escenario por la presencia de Sv.json o Z_swap.json:

    Swap:
      Σ_k  station_cost_k[k] * X[k]
           + c_bays_k[k]                              * N_bays[k]
           + (charger_cost + c_charger_space_k[k])    * N_chargers[k]
           + (battery_cost + c_battery_space_k[k])    * N_batteries[k]

    Carga on-board:
      Σ_k  station_cost_k[k] * X[k]
           + (c_bays_k[k] + charger_cost + c_charger_space_k[k]) * N_chargers[k]
    """
    x_path           = find_json_in_folder(root, "X.json")
    n_chargers_path  = find_json_in_folder(root, "N_chargers.json")
    params_path      = find_json_in_folder(root, "parameters.json")

    if not params_path or is_effectively_empty_json(params_path):
        raise ValueError("No se encontró parameters.json")

    params = load_json(params_path)

    charger_cost = _as_float(params.get("charger_cost", 0.0))

    # Parámetros por estación: soporta {"_1": {k: v}} o directamente {k: v}
    def _station_param(key: str) -> Dict[str, float]:
        raw = params.get(key, {})
        if isinstance(raw, dict) and "_1" in raw and isinstance(raw["_1"], dict):
            raw = raw["_1"]
        return {str(k): _as_float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    # Variables de decisión por estación: soporta {"k": {k: v}} o {"_1": {k: v}}
    def _station_vars(path: Optional[Path]) -> Dict[str, float]:
        if not path or is_effectively_empty_json(path):
            return {}
        data = load_json(path)
        if not isinstance(data, dict):
            return {}
        inner = data.get("k") or data.get("_1") or {}
        if not isinstance(inner, dict):
            return {}
        return {str(k): _as_float(v) for k, v in inner.items()}

    p_station = _station_param("station_cost_k")
    p_bays    = _station_param("c_bays_k")
    p_char_sp = _station_param("c_charger_space_k")

    x_map    = _station_vars(x_path)
    char_map = _station_vars(n_chargers_path)
    stations = sorted(set(x_map) | set(char_map))

    # Detecta si es escenario swap (tiene Sv.json o Z_swap.json)
    is_swap = (
        find_json_in_folder(root, "Sv.json") is not None
        or find_json_in_folder(root, "Z_swap.json") is not None
    )

    if is_swap:
        battery_cost = _as_float(params.get("battery_cost", 0.0))
        p_bat_sp  = _station_param("c_battery_space_k")
        n_bays_path      = find_json_in_folder(root, "N_bays.json")
        n_batteries_path = find_json_in_folder(root, "N_batteries.json")
        bays_map = _station_vars(n_bays_path)
        bat_map  = _station_vars(n_batteries_path)
        stations = sorted(set(stations) | set(bays_map) | set(bat_map))

        total = 0.0
        for k in stations:
            total += (
                p_station.get(k, 0.0) * x_map.get(k, 0.0)
                + p_bays.get(k, 0.0)                          * bays_map.get(k, 0.0)
                + (charger_cost + p_char_sp.get(k, 0.0))      * char_map.get(k, 0.0)
                + (battery_cost + p_bat_sp.get(k, 0.0))       * bat_map.get(k, 0.0)
            )
    else:
        # Carga on-board: c_bays_k va agrupado con el costo por cargador
        total = 0.0
        for k in stations:
            total += (
                p_station.get(k, 0.0) * x_map.get(k, 0.0)
                + (p_bays.get(k, 0.0) + charger_cost + p_char_sp.get(k, 0.0)) * char_map.get(k, 0.0)
            )

    return total


def calculate_penalty_cost(root: Path) -> float:
    """
    Calcula el costo de penalidad por dÃ©ficit de producciÃ³n.
    Basado en la funciÃ³n F_penalty_cost de functions.py.
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
    f_penalty_div = params_data.get("F_penalty_div", {})
    scaling_factor = float(params_data.get("scaling_factor_op_cost", 1.0))
    
    total_penalty = 0.0

    def _get_divisor_for_segment(seg_key: str) -> float:
        # Soporta formatos: {"_1": {"1": val}} o {"1": val} o nested
        try:
            if isinstance(f_penalty_div, dict):
                if "_1" in f_penalty_div and isinstance(f_penalty_div["_1"], dict):
                    return float(f_penalty_div["_1"].get(seg_key, 1.0))
                # directo al nivel superior: {"1": 1000, ...}
                if seg_key in f_penalty_div:
                    return float(f_penalty_div.get(seg_key, 1.0))
                # intentar buscar en cualquier dict hijo
                for v in f_penalty_div.values():
                    if isinstance(v, dict) and seg_key in v:
                        return float(v.get(seg_key, 1.0))
        except (ValueError, TypeError, ZeroDivisionError):
            pass
        return 1.0

    # Soportar dos formatos habituales de F_seg.json:
    # 1) formato con ejes indexados por guiones bajos: {"_1": {node: {"_2": {day: {"_3": {seg: val}}}}}}
    # 2) formato con nombres de ejes: {"j": {node: {"d": {day: {"seg": {seg: val}}}}}}
    if "_1" in f_seg_data and isinstance(f_seg_data["_1"], dict):
        for node_id, node_data in f_seg_data["_1"].items():
            if "_2" in node_data and isinstance(node_data["_2"], dict):
                for day_id, day_data in node_data["_2"].items():
                    if "_3" in day_data and isinstance(day_data["_3"], dict):
                        for seg_id, seg_value in day_data["_3"].items():
                            try:
                                divisor = _get_divisor_for_segment(seg_id)
                                total_penalty += float(seg_value) * (voll / divisor)
                            except (ValueError, TypeError, ZeroDivisionError):
                                continue
    elif "j" in f_seg_data and isinstance(f_seg_data["j"], dict):
        for node_id, node_data in f_seg_data["j"].items():
            # node_data expected like {"d": {"1": {"seg": {"1": val}}}}
            days = node_data.get("d", {}) if isinstance(node_data, dict) else {}
            for day_id, day_data in days.items():
                segs = day_data.get("seg", {}) if isinstance(day_data, dict) else {}
                for seg_id, seg_value in segs.items():
                    try:
                        divisor = _get_divisor_for_segment(seg_id)
                        total_penalty += float(seg_value) * (voll / divisor)
                    except (ValueError, TypeError, ZeroDivisionError):
                        continue

    return total_penalty * scaling_factor


def _sum_numeric_leaves(data: Any) -> float:
    """Suma todos los valores numericos en una estructura JSON anidada."""
    if isinstance(data, dict):
        return sum(_sum_numeric_leaves(v) for v in data.values())
    if isinstance(data, list):
        return sum(_sum_numeric_leaves(v) for v in data)
    try:
        return float(data)
    except (TypeError, ValueError):
        return 0.0


def calculate_power_cost(root: Path) -> float:
    """
    Calcula el costo por potencia punta.

    Misma formula que ObjectiveRules.power_cost:
        sum(P_pot[y] * 12 * 10 for y in years)
    """
    p_pot_path = find_json_in_folder(root, "P_pot.json")

    if not p_pot_path or is_effectively_empty_json(p_pot_path):
        return 0.0

    p_pot_data = load_json(p_pot_path)
    total_p_peak = _sum_numeric_leaves(p_pot_data)
    return total_p_peak * 12.0 * 10.0


def calculate_grid_energy_cost(root: Path) -> float:
    """Costo de energía comprada a la red: sum(P_red[d,t] * costo_red[d,t] * delta_t).

    Usa P_red.json y parameters.json.
    Estructura P_red.json: {"d": {"1": {"t": {"1.0": val, ...}}}}
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

    # Lookup costo_red[(d,t)]: estructura {"_1": {"d": {"_2": {"t": val}}}}
    cost_lookup: Dict[Tuple[str, str], float] = {}
    raw_ce = params_data.get("costo_red", params_data.get("costo_electricidad", {}))
    inner_ce = raw_ce.get("_1", raw_ce) if isinstance(raw_ce, dict) else {}
    if isinstance(inner_ce, dict):
        for d_key, d_val in inner_ce.items():
            if isinstance(d_val, dict):
                t_block = d_val.get("_2", d_val)
                if isinstance(t_block, dict):
                    for t_key, cost_val in t_block.items():
                        try:
                            cost_lookup[(str(d_key), str(t_key))] = float(cost_val)
                        except Exception:
                            pass

    # Iterar P_red: {"d": {"1": {"t": {"1.0": val}}}}
    total_cost = 0.0
    d_block = pred_data.get("d", pred_data)
    if isinstance(d_block, dict):
        for d_key, d_val in d_block.items():
            if not isinstance(d_val, dict):
                continue
            t_block = d_val.get("t", d_val)
            if not isinstance(t_block, dict):
                continue
            for t_key, pred_val in t_block.items():
                cost_elec = cost_lookup.get((str(d_key), str(t_key)), 0.0)
                try:
                    total_cost += cost_elec * float(pred_val) * delta_t
                except Exception:
                    pass

    return total_cost * scaling_factor


def calculate_gen_costs(root: Path) -> Dict[str, Any]:
    """
    Lee G_g.json y parameters.json para calcular por generador:
    - Unidades instaladas (G_g)
    - Potencia instalada total [kW] = G_g * p_max_g
    - Costo de inversión = G_g * c_inv_g
    - Costo de operación anual = G_g * c_op_g
    """
    gg_path = find_json_in_folder(root, "G_g.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not gg_path or not params_path:
        return {}
    if is_effectively_empty_json(gg_path) or is_effectively_empty_json(params_path):
        return {}

    gg_data = load_json(gg_path)
    params_data = load_json(params_path)

    def _extract_param(params: Dict, key: str) -> Dict[str, float]:
        raw = params.get(key, {})
        inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
        return {str(g): _as_float(v, 0.0) for g, v in inner.items()} if isinstance(inner, dict) else {}

    gg_raw = gg_data
    for candidate in ("g", "_1"):
        if isinstance(gg_raw, dict) and candidate in gg_raw:
            gg_raw = gg_raw[candidate]
            break
    gg_map = {str(g): _as_float(v, 0.0) for g, v in gg_raw.items()} if isinstance(gg_raw, dict) else {}
    c_inv = _extract_param(params_data, "c_inv_g")
    c_op  = _extract_param(params_data, "c_op_g")
    p_max = _extract_param(params_data, "p_max_g")

    gen_set = [str(g) for g in params_data.get("gen_set", list(gg_map.keys()))]

    gens: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = total_power = 0.0

    for g in gen_set:
        units = gg_map.get(g, 0.0)
        inv   = units * c_inv.get(g, 0.0) * p_max.get(g, 0.0)
        op    = units * c_op.get(g, 0.0)  * p_max.get(g, 0.0)
        power = units * p_max.get(g, 0.0)
        gens[g] = {
            "units":      units,
            "p_max_unit": p_max.get(g, 0.0),
            "power_kw":   power,
            "inv_cost":   inv,
            "op_cost":    op,
        }
        total_inv   += inv
        total_op    += op
        total_power += power

    return {
        "generators":      gens,
        "total_inv_cost":  total_inv,
        "total_op_cost":   total_op,
        "total_power_kw":  total_power,
    }


def calculate_bess_costs(root: Path) -> Dict[str, Any]:
    """
    Lee H_h.json y parameters.json para calcular por unidad BESS:
    - Instalada (H_h = 1) o no (H_h = 0)
    - Potencia máxima [kW], capacidad [kWh]
    - Costo de inversión = H_h * c_inv_h
    - Costo de operación anual = H_h * c_op_h
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

    # H_h puede venir como {"h": {"Bess": 1}} o {"_1": {"Bess": 1}} o {"Bess": 1}
    raw_hh = hh_data
    for candidate in ("h", "_1"):
        if isinstance(raw_hh, dict) and candidate in raw_hh:
            raw_hh = raw_hh[candidate]
            break
    hh_map  = {str(h): _as_float(v, 0.0) for h, v in raw_hh.items()} if isinstance(raw_hh, dict) else {}

    c_inv  = _extract_param(params_data, "c_inv_h")
    c_op   = _extract_param(params_data, "c_op_h")
    p_max  = _extract_param(params_data, "p_max_h")
    a_max  = _extract_param(params_data, "a_max_h")
    a_min  = _extract_param(params_data, "a_min_h")

    stor_set = [str(h) for h in params_data.get("storage_set", list(hh_map.keys()))]

    units_info: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = 0.0

    for h in stor_set:
        installed = hh_map.get(h, 0.0)
        inv = installed * c_inv.get(h, 0.0)
        op  = installed * c_op.get(h, 0.0)
        units_info[h] = {
            "installed":  installed,
            "p_max_kw":   p_max.get(h, 0.0),
            "a_max_kwh":  a_max.get(h, 0.0),
            "a_min_kwh":  a_min.get(h, 0.0),
            "inv_cost":   inv,
            "op_cost":    op,
        }
        total_inv += inv
        total_op  += op

    return {
        "units":          units_info,
        "total_inv_cost": total_inv,
        "total_op_cost":  total_op,
    }


def calculate_total_costs(root: Path) -> Dict[str, float]:
    """
    Calcula todos los costos del sistema:
    - Costo de energÃ­a (lhd_charge_cost)
    - Costo de inversiÃ³n (inversion_cost)
    - Costo de penalidad (F_penalty_cost)
    - Costo total
    """
    try:
        energy_cost = calculate_lhd_charge_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de energÃ­a: {ex}")
        energy_cost = 0.0
    
    try:
        investment_cost = calculate_investment_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de inversiÃ³n: {ex}")
        investment_cost = 0.0
    
    try:
        penalty_cost = calculate_penalty_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de penalidad: {ex}")
        penalty_cost = 0.0

    try:
        power_cost = calculate_power_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo por potencia: {ex}")
        power_cost = 0.0

    try:
        grid_energy_cost = calculate_grid_energy_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de energía de red: {ex}")
        grid_energy_cost = 0.0

    try:
        gen_info = calculate_gen_costs(root)
        gen_inv_cost = gen_info.get("total_inv_cost", 0.0)
        gen_op_cost  = gen_info.get("total_op_cost",  0.0)
    except Exception as ex:
        print(f"Advertencia al calcular costos de generación: {ex}")
        gen_inv_cost = gen_op_cost = 0.0

    try:
        bess_info    = calculate_bess_costs(root)
        bess_inv_cost = bess_info.get("total_inv_cost", 0.0)
        bess_op_cost  = bess_info.get("total_op_cost",  0.0)
    except Exception as ex:
        print(f"Advertencia al calcular costos de almacenamiento: {ex}")
        bess_inv_cost = bess_op_cost = 0.0

    total_cost = investment_cost + grid_energy_cost + power_cost + penalty_cost + gen_inv_cost + gen_op_cost + bess_inv_cost + bess_op_cost

    return {
        "energy_cost":      energy_cost,
        "grid_energy_cost": grid_energy_cost,
        "investment_cost":  investment_cost,
        "power_cost":       power_cost,
        "penalty_cost":     penalty_cost,
        "gen_inv_cost":     gen_inv_cost,
        "gen_op_cost":      gen_op_cost,
        "bess_inv_cost":    bess_inv_cost,
        "bess_op_cost":     bess_op_cost,
        "total_cost":       total_cost,
    }


if __name__ == "__main__":
    main()
