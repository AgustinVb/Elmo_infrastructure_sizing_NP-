#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
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


def _get_eta_charge_by_lhd(params_data: Dict[str, Any]) -> Dict[str, float]:
    """Lee eta_charge_i (Param de src/optimization/functions.py, indexado solo por
    elhd_set) desde parameters.json: estructura {"_1": {lhd_id: eta_charge}}.
    Devuelve {} si la corrida no trae ese parametro (escenario sin esa hoja/feature)."""
    raw = params_data.get("eta_charge_i", {})
    inner = raw.get("_1", {}) if isinstance(raw, dict) else {}
    if not isinstance(inner, dict):
        return {}
    result: Dict[str, float] = {}
    for lhd_id, val in inner.items():
        try:
            result[str(lhd_id)] = float(val)
        except (TypeError, ValueError):
            continue
    return result


def calculate_daily_charged_energy(
    p_json_path: Path,
    step_hours: float,
    eta_charge_by_lhd: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Suma energia diaria [kWh] desde P [kW] usando step_hours.

    Estructura esperada: k -> station -> i -> lhd -> y -> year -> d -> day -> t -> interval
    (compatibilidad con formato legado sin eje de anio: k -> station -> i -> lhd -> d -> day -> t).

    Por defecto (eta_charge_by_lhd=None) devuelve la energia BRUTA del lado del
    cargador (P tal cual, sin perdidas) -- comportamiento original. Si se entrega
    eta_charge_by_lhd ({lhd_id: eta_charge}, ver _get_eta_charge_by_lhd), cada
    potencia se escala por la eficiencia de carga de ESE LHD antes de sumar,
    devolviendo la energia NETA que efectivamente queda almacenada en la bateria.
    """
    data = load_json(p_json_path)
    energy_by_day: Dict[str, float] = {}

    if not isinstance(data, dict) or "k" not in data or not isinstance(data["k"], dict):
        return energy_by_day

    def _accumulate(lhd_id: str, d_block: Any) -> None:
        if not isinstance(d_block, dict):
            return
        eta = float(eta_charge_by_lhd.get(lhd_id, 1.0)) if eta_charge_by_lhd else 1.0
        for day_key, day_data in d_block.items():
            if not isinstance(day_data, dict):
                continue
            t_block = day_data.get("t", {})
            if not isinstance(t_block, dict):
                continue

            day = str(day_key)
            energy_by_day.setdefault(day, 0.0)
            for power in t_block.values():
                energy_by_day[day] += _as_float(power, 0.0) * step_hours * eta

    for station_data in data["k"].values():
        if not isinstance(station_data, dict):
            continue
        i_block = station_data.get("i", {})
        if not isinstance(i_block, dict):
            continue

        for lhd_id, lhd_data in i_block.items():
            if not isinstance(lhd_data, dict):
                continue
            year_block = lhd_data.get("y", {})
            if isinstance(year_block, dict) and year_block:
                for year_data in year_block.values():
                    _accumulate(lhd_id, year_data.get("d", {}) if isinstance(year_data, dict) else {})
            else:
                # Compatibilidad con formato legado (sin eje de anio)
                _accumulate(lhd_id, lhd_data.get("d", {}))

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
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Carpeta no vÃ¡lida: {root}")

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

        # EnergÃ­a neta almacenada en baterÃ­a (post eta_charge) y pÃ©rdidas de carga.
        # Solo se calcula si el escenario trae eta_charge_i en parameters.json
        # (hoja LHD con charge_efficiency/discharge_efficiency); si no, queda {}
        # y las columnas nuevas simplemente no aportan nada (compatibilidad con
        # escenarios/carpetas de resultados antiguas sin esa hoja).
        eta_charge_by_lhd: Dict[str, float] = {}
        if params_path and not is_effectively_empty_json(params_path):
            eta_charge_by_lhd = _get_eta_charge_by_lhd(load_json(params_path))
        net_energy_daily = (
            calculate_daily_charged_energy(p_path, step_hours, eta_charge_by_lhd=eta_charge_by_lhd)
            if (eta_charge_by_lhd and p_path and not is_effectively_empty_json(p_path))
            else {}
        )

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
            total_net_energy = 0.0
            total_cycles = 0.0
            show_net = bool(eta_charge_by_lhd)

            for d in all_days:
                trips = trips_daily.get(d, 0.0)
                energy = energy_daily.get(d, 0.0)
                net_energy = net_energy_daily.get(d, 0.0)
                cycles = cycles_daily.get(d, 0.0)

                total_trips += trips
                total_energy += energy
                total_net_energy += net_energy
                total_cycles += cycles

                row = [d, f"{trips:.0f}", f"{cycles:.3f}", f"{energy:.3f}"]
                if show_net:
                    row.extend([f"{net_energy:.3f}", f"{energy - net_energy:.3f}"])
                rows.append(row)

            total_row = ["TOTAL", f"{total_trips:.0f}", f"{total_cycles:.3f}", f"{total_energy:.3f}"]
            headers = ["DÃ­a", "Viajes diarios totales", "Ciclos (Y*n_trips)", "EnergÃ­a diaria bruta [kWh]"]
            if show_net:
                total_row.extend([f"{total_net_energy:.3f}", f"{total_energy - total_net_energy:.3f}"])
                headers.extend(["EnergÃ­a neta en baterÃ­a [kWh]", "PÃ©rdidas de carga (eta_charge) [kWh]"])
            rows.append(total_row)
            print(make_table(
                "OPERACIÃ“N DIARIA",
                headers,
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
                rows.append([
                    h,
                    f"{info['installed']:.0f}",
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
                ["Unidad", "Unidades instaladas", "P_max [kW]", "A_min [kWh]", "A_max [kWh]",
                 "Costo inv.", "Costo op. anual"],
                rows,
            ))
            print()
    except Exception as ex:
        print(make_table("ALMACENAMIENTO BESS", ["Estado", "Detalle"],
                         [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    # ---- Degradación batería on-board
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
                "DEGRADACION BATERIA ON-BOARD",
                ["Año", "B [kWh] (inicio)", "% b_max", "EFC año", "D [kWh] (fin)", "Reemplazo", "Costo reemplazo"],
                rows,
            ))
            print()
            print(make_table(
                "DEGRADACION BATERIA - RESUMEN",
                ["Métrica", "Valor"],
                [
                    ["Capacidad nominal b_max [kWh]",  f"{deg_info['b_max_kwh']:.1f}"],
                    ["Piso de degradación",             f"{deg_info['min_capacity_pct']:.0f}% de b_max"],
                    ["N° de reemplazos",                f"{deg_info['n_replacements']}"],
                    ["Costo total reemplazos",          f"{deg_info['total_replace_cost']:.2f}"],
                ],
            ))
            print()
    except Exception as ex:
        print(make_table("DEGRADACION BATERIA ON-BOARD", ["Estado", "Detalle"],
                         [["OMITIDO", f"No se pudo calcular: {ex}"]]))
        print()

    # ---- Cost
    try:
        costs = calculate_total_costs(root)
        if costs:
            rows = [
                ["Costo energía carga",         f"{costs['energy_cost']:.2f}"],
                ["Costo energía red P_red",      f"{costs.get('grid_energy_cost', 0.0):.2f}"],
                ["Energía total red (horizonte) [kWh]", f"{costs.get('grid_energy_kwh', 0.0):.2f}"],
                ["Energía total red (horizonte) [MWh]", f"{costs.get('grid_energy_kwh', 0.0) / 1000.0:.2f}"],
                ["Costo inversión estaciones",   f"{costs['investment_cost']:.2f}"],
                ["Costo inversión subestación",  f"{costs.get('substation_cost', 0.0):.2f}"],
                ["Costo inversión generación",   f"{costs['gen_inv_cost']:.2f}"],
                ["Costo operación generación",   f"{costs['gen_op_cost']:.2f}"],
                ["Costo inversión BESS",         f"{costs['bess_inv_cost']:.2f}"],
                ["Costo operación BESS",         f"{costs['bess_op_cost']:.2f}"],
                ["Costo reemplazo batería",      f"{costs.get('battery_replace_cost', 0.0):.2f}"],
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
def _get_discount_rate(params: Dict) -> float:
    return _as_float(params.get("discount_rate"), 0.0)


def _get_years_sorted(params: Dict) -> List[int]:
    """Años del horizonte, leídos desde costo_red (_1=year), siempre presente."""
    raw = params.get("costo_red", {})
    inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
    years = sorted(int(y) for y in inner.keys()) if isinstance(inner, dict) else []
    return years or [1]


def _has_degradation_data(params: Dict) -> bool:
    """Proxy de si la hoja BatteryDegradation existía al formular el modelo —
    misma condición que gatilla el descuento en ObjectiveRules (functions.py)."""
    return "gamma_coef" in params


def _annuity_factor(r: float, years: int) -> float:
    """AF(r,Y) = sum_{k=1..Y} 1/(1+r)^k. Usada solo por la rama de
    compatibilidad "swap" (horizonte unico) de calculate_investment_cost --
    esta rama de carga on-board ya no anualiza ningun costo de inversion
    (ver _year_discount_factor / _one_time_discounted_yearly_sum en
    functions.py)."""
    if years <= 0:
        return 0.0
    if r == 0:
        return float(years)
    return sum(1.0 / (1.0 + r) ** k for k in range(1, years + 1))


def _year_discount_factor(r: float, year: int, years_sorted: List[int]) -> float:
    """1/(1+r)^pos(year) — igual a _discount_factor en ObjectiveRules (functions.py)."""
    if r == 0:
        return 1.0
    pos = years_sorted.index(int(year)) + 1 if int(year) in years_sorted else 1
    return 1.0 / (1.0 + r) ** pos


def _indexed_vars_by_year(path: Optional[Path], outer_candidates: Tuple[str, ...] = ("k", "_1")) -> Dict[str, Dict[str, float]]:
    """Lee un JSON con estructura {outer_axis: {id: {'y': {year: val}}}}
    (variables de inversion multi-año Delta_X/Delta_N_chargers,
    indexadas por (id, y)) y devuelve {id: {year_str: val}}. Devuelve {} si
    el archivo no existe/esta vacio o no calza con la estructura esperada."""
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


def _indexed_vars_flat(path: Optional[Path], outer_candidates: Tuple[str, ...] = ("g", "_1")) -> Dict[str, float]:
    """Lee un JSON con estructura {outer_axis: {id: val}} (variable SIN
    indice de año, como G_g desde que se elimino Delta_G_g/el indice de año
    -- ver BoundRules en functions.py: G_g[g] se decide una sola vez para
    todo el horizonte) y devuelve {id: val}."""
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
    return {str(k): _as_float(v, 0.0) for k, v in outer.items()}


def _scalar_var(path: Optional[Path]) -> float:
    """Lee un JSON de una variable Pyomo escalar (sin ningun indice, como H
    desde que se elimino Delta_H/el indice de año -- ver BoundRules en
    functions.py: H se decide una sola vez para todo el horizonte). El
    Printer exporta un escalar como el numero JSON crudo (Printer.
    _export_single_variable_json, rama "not var_comp.is_indexed()"), no
    como un dict anidado."""
    if not path or is_effectively_empty_json(path):
        return 0.0
    return _as_float(load_json(path), 0.0)



def calculate_lhd_charge_cost(root: Path) -> float:
    """
    Calcula el costo de carga de los LHD elÃ©ctricos.
    Usa P.json (potencia de carga) y parameters.json (costos marginales y delta_t).

    Nota (eta_charge): usa P bruto (sin dividir por eta_charge) a propÃ³sito --
    ver el docstring de calculate_grid_energy_cost para la explicaciÃ³n completa
    de por quÃ© P ya equivale a "demanda neta / eficiencia".
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

    discount_r = _get_discount_rate(params_data) if _has_degradation_data(params_data) else 0.0
    years_sorted = _get_years_sorted(params_data)

    total_cost = 0.0

    def _cost_lookup(lhd_id, year_key, day_key, t_str) -> float:
        """costo_marginal: _1=lhd -> _2=year -> _3=day -> _4=interval (formato multi-año).
        Compatibilidad con formato legado sin año: _1=lhd -> _2=day -> _3=interval."""
        if "_1" not in costo_marginal:
            return 1.0
        cm_lhd = costo_marginal["_1"].get(lhd_id, {})
        cm_2 = cm_lhd.get("_2", {}) if isinstance(cm_lhd, dict) else {}
        cm_year = cm_2.get(str(year_key), {})
        if isinstance(cm_year, dict) and "_3" in cm_year:
            cm_day = cm_year["_3"].get(str(day_key), {})
            if isinstance(cm_day, dict) and "_4" in cm_day:
                return cm_day["_4"].get(str(t_str), 1.0)
        # legado: cm_2 ya es el nivel de día
        cm_day_legacy = cm_2.get(str(day_key), {})
        if isinstance(cm_day_legacy, dict) and "_3" in cm_day_legacy:
            return cm_day_legacy["_3"].get(str(t_str), 1.0)
        return 1.0

    # Estructura esperada: k -> station -> i -> lhd -> y -> year -> d -> day -> t -> interval
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
                year_block = lhd_data.get("y", {})
                if not isinstance(year_block, dict):
                    continue
                for year_key, year_data in year_block.items():
                    if not isinstance(year_data, dict):
                        continue
                    day_block = year_data.get("d", {})
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
                                cost = _cost_lookup(lhd_id, year_key, day_key, t_str)
                                disc = _year_discount_factor(discount_r, year_key, years_sorted)
                                total_cost += float(power) * float(cost) * float(delta_t) * disc
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
    Costo de inversión en estaciones. Detecta el tipo de escenario por la
    presencia de Sv.json o Z_swap.json:

    Swap (rama battery_swapping*, sin inversion multi-año aun -- X/N_chargers
    siguen siendo decisiones de un unico año, formato plano):
      Σ_k  station_cost_k[k] * X[k]
           + c_bays_k[k]                              * N_bays[k]
           + (charger_cost + c_charger_space_k[k])    * N_chargers[k]
           + (battery_cost + c_battery_space_k[k])    * N_batteries[k]
      (con la anualidad global _annuity_factor si hay datos de degradación)

    Carga on-board (esta rama, inversion multi-año -- idéntico a
    inversion_cost() de functions.py: usa las variables de INCREMENTO
    Delta_X/Delta_N_chargers de cada año y, pagadas una sola vez al año de
    compra y descontadas a valor presente, NO anualizadas -- ver
    _one_time_discounted_yearly_sum):
      Σ_y discount_factor(y) * Σ_k  station_cost_k[k] * Delta_X[k,y]
           + (c_bays_k[k] + charger_cost + c_charger_space_k[k]) * Delta_N_chargers[k,y]
    """
    params_path = find_json_in_folder(root, "parameters.json")

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

    p_station = _station_param("station_cost_k")
    p_bays    = _station_param("c_bays_k")
    p_char_sp = _station_param("c_charger_space_k")

    # Detecta si es escenario swap (tiene Sv.json o Z_swap.json) -- nunca
    # ocurre en esta rama (carga on-board), se mantiene por compatibilidad si
    # esta funcion se reutiliza en la rama battery_swapping*.
    is_swap = (
        find_json_in_folder(root, "Sv.json") is not None
        or find_json_in_folder(root, "Z_swap.json") is not None
    )

    if is_swap:
        # Variables de decisión por estación (formato plano, un unico año):
        # soporta {"k": {k: v}} o {"_1": {k: v}}
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

        x_map    = _station_vars(find_json_in_folder(root, "X.json"))
        char_map = _station_vars(find_json_in_folder(root, "N_chargers.json"))
        battery_cost = _as_float(params.get("battery_cost", 0.0))
        p_bat_sp  = _station_param("c_battery_space_k")
        bays_map = _station_vars(find_json_in_folder(root, "N_bays.json"))
        bat_map  = _station_vars(find_json_in_folder(root, "N_batteries.json"))
        stations = sorted(set(x_map) | set(char_map) | set(bays_map) | set(bat_map))

        total = 0.0
        for k in stations:
            total += (
                p_station.get(k, 0.0) * x_map.get(k, 0.0)
                + p_bays.get(k, 0.0)                          * bays_map.get(k, 0.0)
                + (charger_cost + p_char_sp.get(k, 0.0))      * char_map.get(k, 0.0)
                + (battery_cost + p_bat_sp.get(k, 0.0))       * bat_map.get(k, 0.0)
            )

        if _has_degradation_data(params):
            total = total * _annuity_factor(_get_discount_rate(params), len(_get_years_sorted(params)))
        return total

    # Carga on-board (esta rama): c_bays_k va agrupado con el costo por
    # cargador; usa las variables de incremento por año.
    delta_x_by_station = _indexed_vars_by_year(find_json_in_folder(root, "Delta_X.json"))
    delta_n_by_station = _indexed_vars_by_year(find_json_in_folder(root, "Delta_N_chargers.json"))
    stations = sorted(set(delta_x_by_station) | set(delta_n_by_station))

    years_sorted = _get_years_sorted(params)
    has_degradation = _has_degradation_data(params)
    discount_r = _get_discount_rate(params) if has_degradation else 0.0

    total = 0.0
    for k in stations:
        dx = delta_x_by_station.get(k, {})
        dn = delta_n_by_station.get(k, {})
        for y in years_sorted:
            y_key = str(y)
            yearly = (
                p_station.get(k, 0.0) * dx.get(y_key, 0.0)
                + (p_bays.get(k, 0.0) + charger_cost + p_char_sp.get(k, 0.0)) * dn.get(y_key, 0.0)
            )
            df = _year_discount_factor(discount_r, y, years_sorted)
            total += yearly * df

    return total


def calculate_substation_cost(root: Path) -> float:
    """Costo de inversión en potencia de subestación (idéntico a
    substation_investment_cost() de functions.py): P_max_k[k] ya no tiene
    índice de año ni Delta_P_max_k asociado (eliminados) -- se decide UNA
    sola vez para todo el horizonte, igual que G_g/H (ver BoundRules en
    functions.py), asi que P_max_k.json es directamente {"k": {station: kW}},
    sin niveles "y". Pagada una sola vez, descontada al primer año:
      discount_factor(primer_año) * Σ_k c_inv_ssee_k[k] * P_max_k[k]
    """
    params_path = find_json_in_folder(root, "parameters.json")
    pmax_path = find_json_in_folder(root, "P_max_k.json")

    if not params_path or is_effectively_empty_json(params_path):
        return 0.0
    if not pmax_path or is_effectively_empty_json(pmax_path):
        return 0.0

    params = load_json(params_path)

    def _station_param(key: str) -> Dict[str, float]:
        raw = params.get(key, {})
        if isinstance(raw, dict) and "_1" in raw and isinstance(raw["_1"], dict):
            raw = raw["_1"]
        return {str(k): _as_float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    c_inv_ssee = _station_param("c_inv_ssee_k")
    pmax_by_station = _indexed_vars_flat(pmax_path, outer_candidates=("k", "_1"))

    years_sorted = _get_years_sorted(params)
    first_year = years_sorted[0] if years_sorted else None
    has_degradation = _has_degradation_data(params)
    discount_r = _get_discount_rate(params) if has_degradation else 0.0

    if first_year is None:
        return 0.0

    df = _year_discount_factor(discount_r, first_year, years_sorted)
    return sum(c_inv_ssee.get(k, 0.0) * v * df for k, v in pmax_by_station.items())


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
        sum(P_pot[y] * 12 * 10 * discount_factor(y) for y in years)
    """
    p_pot_path  = find_json_in_folder(root, "P_pot.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not p_pot_path or is_effectively_empty_json(p_pot_path):
        return 0.0

    p_pot_data = load_json(p_pot_path)

    discount_r, years_sorted = 0.0, []
    if params_path and not is_effectively_empty_json(params_path):
        params_data = load_json(params_path)
        discount_r = _get_discount_rate(params_data) if _has_degradation_data(params_data) else 0.0
        years_sorted = _get_years_sorted(params_data)

    y_block = p_pot_data.get("y", p_pot_data) if isinstance(p_pot_data, dict) else p_pot_data
    total = 0.0
    if isinstance(y_block, dict):
        for y_key, val in y_block.items():
            if isinstance(val, dict):
                continue
            disc = _year_discount_factor(discount_r, y_key, years_sorted) if years_sorted else 1.0
            try:
                total += float(val) * 12.0 * 10.0 * disc
            except (TypeError, ValueError):
                continue
    return total


def calculate_grid_energy_cost(root: Path) -> float:
    """Costo de energía comprada a la red: sum(P_red[y,d,t] * costo_red[y,d,t] * delta_t).

    Usa P_red.json y parameters.json.
    Estructura P_red.json (multi-año): {"y": {"1": {"d": {"1": {"t": {"1.0": val}}}}}}

    Nota (eta_charge/eta_discharge): P_red ya es la energía bruta comprada a la
    red -- power_balance define P_red = sum(P) sin pérdidas, y battery_soc es la
    ÚNICA restricción que aplica eta_charge/eta_discharge (para dimensionar
    cuánto de P efectivamente queda en la batería). Como battery_boundary cierra
    el SOC de cada día, en el agregado se cumple P_red = (demanda neta de
    tracción de los LHD / eta_discharge) / eta_charge exactamente por
    construcción del modelo -- no hay que volver a dividir por la eficiencia
    acá, eso duplicaría la pérdida. Ver calculate_daily_charged_energy(...,
    eta_charge_by_lhd=...) para la energía NETA que sí queda en la batería.
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

    discount_r = _get_discount_rate(params_data) if _has_degradation_data(params_data) else 0.0
    years_sorted = _get_years_sorted(params_data)

    # Lookup costo_red[(y,d,t)]: estructura {"_1": {"y": {"_2": {"d": {"_3": {"t": val}}}}}}
    # (formato legado sin año: {"_1": {"d": {"_2": {"t": val}}}})
    cost_lookup: Dict[Tuple[str, str, str], float] = {}
    raw_ce = params_data.get("costo_red", params_data.get("costo_electricidad", {}))
    inner_ce = raw_ce.get("_1", raw_ce) if isinstance(raw_ce, dict) else {}
    if isinstance(inner_ce, dict):
        for y_key, y_val in inner_ce.items():
            if not isinstance(y_val, dict):
                continue
            d_block = y_val.get("_2", {})
            has_year_level = isinstance(d_block, dict) and any(
                isinstance(v, dict) and "_3" in v for v in d_block.values()
            )
            if has_year_level:
                for d_key, d_val in d_block.items():
                    if not isinstance(d_val, dict):
                        continue
                    t_block = d_val.get("_3", {})
                    if isinstance(t_block, dict):
                        for t_key, cost_val in t_block.items():
                            try:
                                cost_lookup[(str(y_key), str(d_key), str(t_key))] = float(cost_val)
                            except Exception:
                                pass
            else:
                # legado (sin año): y_key era en realidad el día
                t_block = d_block if isinstance(d_block, dict) else {}
                for t_key, cost_val in t_block.items():
                    try:
                        cost_lookup[("1", str(y_key), str(t_key))] = float(cost_val)
                    except Exception:
                        pass

    # Iterar P_red: {"y": {"1": {"d": {"1": {"t": {"1.0": val}}}}}}
    total_cost = 0.0
    y_block = pred_data.get("y", pred_data)
    if isinstance(y_block, dict):
        for y_key, y_val in y_block.items():
            if not isinstance(y_val, dict):
                continue
            d_block = y_val.get("d", y_val)
            if not isinstance(d_block, dict):
                continue
            for d_key, d_val in d_block.items():
                if not isinstance(d_val, dict):
                    continue
                t_block = d_val.get("t", d_val)
                if not isinstance(t_block, dict):
                    continue
                for t_key, pred_val in t_block.items():
                    cost_elec = cost_lookup.get((str(y_key), str(d_key), str(t_key)), 0.0)
                    try:
                        disc = _year_discount_factor(discount_r, y_key, years_sorted)
                        total_cost += cost_elec * float(pred_val) * delta_t * disc
                    except Exception:
                        pass

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
    y_block = pred_data.get("y", pred_data)
    if isinstance(y_block, dict):
        for y_val in y_block.values():
            if not isinstance(y_val, dict):
                continue
            d_block = y_val.get("d", y_val)
            if not isinstance(d_block, dict):
                continue
            for d_val in d_block.values():
                if not isinstance(d_val, dict):
                    continue
                t_block = d_val.get("t", d_val)
                if not isinstance(t_block, dict):
                    continue
                for pred_val in t_block.values():
                    try:
                        total_kwh += float(pred_val) * delta_t
                    except (TypeError, ValueError):
                        continue

    return total_kwh * scaling_factor


def calculate_gen_costs(root: Path) -> Dict[str, Any]:
    """
    Lee G_g.json y parameters.json. G_g[g] ya no tiene indice de año ni
    Delta_G_g asociado (eliminados) -- se decide UNA sola vez para todo el
    horizonte (ver BoundRules en functions.py), asi que G_g.json es
    directamente {"g": {gen_name: unidades_instaladas}}, sin niveles "y".

    Costo (idéntico a gen_investment_cost/gen_op_cost de functions.py):
      inv_cost = discount_factor(primer_año) * G_g[g] * c_inv_g[g] * p_max_g[g]  (pago unico)
      op_cost  = sum_y discount_factor(y) * G_g[g] * c_op_g[g] * p_max_g[g]      (recurrente)
    """
    gg_path     = find_json_in_folder(root, "G_g.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not gg_path or not params_path:
        return {}
    if is_effectively_empty_json(gg_path) or is_effectively_empty_json(params_path):
        return {}

    params_data = load_json(params_path)

    def _extract_param(key: str) -> Dict[str, float]:
        raw = params_data.get(key, {})
        inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
        return {str(g): _as_float(v, 0.0) for g, v in inner.items()} if isinstance(inner, dict) else {}

    gg_by_gen = _indexed_vars_flat(gg_path, outer_candidates=("g", "_1"))

    c_inv = _extract_param("c_inv_g")
    c_op  = _extract_param("c_op_g")
    p_max = _extract_param("p_max_g")

    gen_set = [str(g) for g in params_data.get("gen_set", list(gg_by_gen.keys()))]

    years_sorted = _get_years_sorted(params_data)
    first_year = years_sorted[0] if years_sorted else None
    has_degradation = _has_degradation_data(params_data)
    discount_r = _get_discount_rate(params_data) if has_degradation else 0.0

    gens: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = total_power = 0.0

    for g in gen_set:
        units = gg_by_gen.get(g, 0.0)
        power = units * p_max.get(g, 0.0)

        inv = 0.0
        if first_year is not None:
            inv = units * c_inv.get(g, 0.0) * p_max.get(g, 0.0) \
                * _year_discount_factor(discount_r, first_year, years_sorted)

        op = sum(
            units * c_op.get(g, 0.0) * p_max.get(g, 0.0) * _year_discount_factor(discount_r, y, years_sorted)
            for y in years_sorted
        )

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
    Lee H.json y parameters.json. H ya no tiene indice de año ni Delta_H
    asociado (eliminados) -- se decide UNA sola vez para todo el horizonte,
    como una VARIABLE ESCALAR (sin ningun indice, ni siquiera "y") -- ver
    BoundRules en functions.py. El Printer exporta un escalar como el
    numero JSON crudo (no un dict anidado), asi que H.json es directamente
    un numero.

    Costo (idéntico a bess_investment_cost/bess_op_cost de functions.py):
      inv_cost = discount_factor(primer_año) * H * c_inv_h[h]  (pago unico)
      op_cost  = sum_y discount_factor(y) * H * c_op_h[h]      (recurrente)
    """
    h_path      = find_json_in_folder(root, "H.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not h_path or not params_path:
        return {}
    if is_effectively_empty_json(h_path) or is_effectively_empty_json(params_path):
        return {}

    params_data = load_json(params_path)

    def _extract_param(key: str) -> Dict[str, float]:
        raw   = params_data.get(key, {})
        inner = raw.get("_1", raw) if isinstance(raw, dict) else {}
        return {str(h): _as_float(v, 0.0) for h, v in inner.items()} if isinstance(inner, dict) else {}

    units_final = _scalar_var(h_path)

    c_inv  = _extract_param("c_inv_h")
    c_op   = _extract_param("c_op_h")
    p_max  = _extract_param("p_max_h")
    a_max  = _extract_param("a_max_h")
    a_min  = _extract_param("a_min_h")

    stor_set = [str(h) for h in params_data.get("storage_set", list(c_inv.keys()))]

    years_sorted = _get_years_sorted(params_data)
    first_year = years_sorted[0] if years_sorted else None
    has_degradation = _has_degradation_data(params_data)
    discount_r = _get_discount_rate(params_data) if has_degradation else 0.0

    units_info: Dict[str, Dict[str, float]] = {}
    total_inv = total_op = 0.0

    for h in stor_set:
        inv = 0.0
        if first_year is not None:
            inv = units_final * c_inv.get(h, 0.0) * _year_discount_factor(discount_r, first_year, years_sorted)

        op = sum(
            units_final * c_op.get(h, 0.0) * _year_discount_factor(discount_r, y, years_sorted)
            for y in years_sorted
        )
        units_info[h] = {
            "installed":  units_final,
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


def calculate_battery_degradation_metrics(root: Path) -> Dict[str, Any]:
    """
    Lee R.json, b_bar.json, D.json, N_ciclos.json y parameters.json para
    reportar, por año, la degradación de la batería on-board (fleet-wide):
    - Capacidad al inicio (b_bar) y al final (D) del año [kWh], y % de la
      nominal (b_max) para b_bar
    - Ciclos equivalentes (EFC) del propio año (N_ciclos) -- ya no hay
      acumulado histórico explícito, el arrastre entre años lo da D[y-1]
      (ver b_y_link en functions.py)
    - Si hubo reemplazo (R=1) ese año y su costo (c_bat_replace)
    """
    b_bar_path = find_json_in_folder(root, "b_bar.json")
    params_path = find_json_in_folder(root, "parameters.json")

    if not b_bar_path or not params_path:
        return {}
    if is_effectively_empty_json(b_bar_path) or is_effectively_empty_json(params_path):
        return {}

    params_data = load_json(params_path)

    def _year_map(filename: str) -> Dict[str, float]:
        p = find_json_in_folder(root, filename)
        if not p or is_effectively_empty_json(p):
            return {}
        data = load_json(p)
        raw = data
        for candidate in ("y", "_1"):
            if isinstance(raw, dict) and candidate in raw:
                raw = raw[candidate]
                break
        return {str(y): _as_float(v, 0.0) for y, v in raw.items()} if isinstance(raw, dict) else {}

    b_bar_map     = _year_map("b_bar.json")
    d_map         = _year_map("D.json")
    r_map         = _year_map("R.json")
    n_ciclos_map  = _year_map("N_ciclos.json")

    b_max          = _as_float(params_data.get("b_max_fleet"), 0.0)
    min_frac       = _as_float(params_data.get("min_capacity_fraction"), 0.0)
    # c_bat_replace es el costo TOTAL de UNA sola batería; se escala por
    # la cantidad de LHD eléctricos (una batería por LHD) para obtener el
    # costo de reemplazar la flota completa, igual que en ObjectiveRules
    # (functions.py: battery_replace_cost).
    c_bat_replace  = _as_float(params_data.get("c_bat_replace"), 0.0)
    n_elhd         = len(params_data.get("elhd_set", []) or [])

    years = sorted(b_bar_map.keys(), key=lambda y: float(y))
    years_sorted_int = _get_years_sorted(params_data)
    discount_r = _get_discount_rate(params_data)

    years_info: Dict[str, Dict[str, Any]] = {}
    total_replace_cost = 0.0
    n_replacements = 0

    for y in years:
        b_bar = b_bar_map.get(y, 0.0)
        # R=0 no se exporta (binarias solo guardan valores en 1) -> default 0.0
        replaced = r_map.get(y, 0.0) >= 0.5
        disc = _year_discount_factor(discount_r, y, years_sorted_int)
        cost_y = n_elhd * c_bat_replace * disc if replaced else 0.0
        total_replace_cost += cost_y
        if replaced:
            n_replacements += 1
        years_info[y] = {
            "b_bar_kwh":    b_bar,
            "b_bar_pct":    (b_bar / b_max * 100.0) if b_max else 0.0,
            "n_ciclos":     n_ciclos_map.get(y, 0.0),
            "d_kwh":        d_map.get(y, 0.0),
            "replaced":     replaced,
            "replace_cost": cost_y,
        }

    return {
        "years":              years_info,
        "b_max_kwh":          b_max,
        "min_capacity_pct":   min_frac * 100.0 if min_frac else 0.0,
        "n_replacements":     n_replacements,
        "total_replace_cost": total_replace_cost,
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
        substation_cost = calculate_substation_cost(root)
    except Exception as ex:
        print(f"Advertencia al calcular costo de inversión en subestación: {ex}")
        substation_cost = 0.0

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
        grid_energy_kwh = calculate_grid_energy_total_kwh(root)
    except Exception as ex:
        print(f"Advertencia al calcular energía total de red: {ex}")
        grid_energy_kwh = 0.0

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

    try:
        deg_info = calculate_battery_degradation_metrics(root)
        battery_replace_cost = deg_info.get("total_replace_cost", 0.0)
    except Exception as ex:
        print(f"Advertencia al calcular costo de reemplazo de batería: {ex}")
        battery_replace_cost = 0.0

    total_cost = (investment_cost + substation_cost + grid_energy_cost + power_cost + penalty_cost
                  + gen_inv_cost + gen_op_cost + bess_inv_cost + bess_op_cost
                  + battery_replace_cost)

    return {
        "energy_cost":          energy_cost,
        "grid_energy_cost":     grid_energy_cost,
        "grid_energy_kwh":      grid_energy_kwh,
        "investment_cost":      investment_cost,
        "substation_cost":      substation_cost,
        "power_cost":           power_cost,
        "penalty_cost":         penalty_cost,
        "gen_inv_cost":         gen_inv_cost,
        "gen_op_cost":          gen_op_cost,
        "bess_inv_cost":        bess_inv_cost,
        "bess_op_cost":         bess_op_cost,
        "battery_replace_cost": battery_replace_cost,
        "total_cost":           total_cost,
    }


if __name__ == "__main__":
    main()
