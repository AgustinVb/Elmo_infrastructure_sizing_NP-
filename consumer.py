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
        raise ValueError("JSON inesperado: la raÃ­z no es un objeto/dict.")

    vehicles = data["d"] if ("d" in data and isinstance(data["d"], dict)) else data
    if not isinstance(vehicles, dict) or len(vehicles) == 0:
        raise ValueError("No pude detectar series por vehÃ­culo (dict vacÃ­o o formato no soportado).")

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

    if not isinstance(data, dict) or "d" not in data or not isinstance(data["d"], dict):
        return trips_by_day

    for lhd_data in data["d"].values():
        if not isinstance(lhd_data, dict):
            continue
        t_block = lhd_data.get("t", {})
        if not isinstance(t_block, dict):
            continue

        for node_data in t_block.values():
            if not isinstance(node_data, dict):
                continue
            i_block = node_data.get("i", {})
            if not isinstance(i_block, dict):
                continue

            for day_key, day_data in i_block.items():
                if not isinstance(day_data, dict):
                    continue
                j_block = day_data.get("j", {})
                if not isinstance(j_block, dict):
                    continue

                day = str(day_key)
                trips_by_day.setdefault(day, 0.0)
                for val in j_block.values():
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

    if not isinstance(data, dict) or "d" not in data or not isinstance(data["d"], dict):
        return y_counts

    for i_name, lhd_data in data["d"].items():
        if not isinstance(lhd_data, dict):
            continue
        t_block = lhd_data.get("t", {})
        if not isinstance(t_block, dict):
            continue

        for j_name, node_data in t_block.items():
            if not isinstance(node_data, dict):
                continue
            i_block = node_data.get("i", {})
            if not isinstance(i_block, dict):
                continue

            for day_key, day_data in i_block.items():
                if not isinstance(day_data, dict):
                    continue
                t_inner = day_data.get("j", {})
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

    root = data.get("_1", {}) if isinstance(data, dict) else {}
    if not isinstance(root, dict):
        return m_values

    for i_name, i_data in root.items():
        if not isinstance(i_data, dict):
            continue
        j_block = i_data.get("_2", {})
        if not isinstance(j_block, dict):
            continue

        for j_name, j_data in j_block.items():
            if not isinstance(j_data, dict):
                continue
            d_block = j_data.get("_3", {})
            if not isinstance(d_block, dict):
                continue

            for day_key, m_val in d_block.items():
                m_values[(str(i_name), str(j_name), str(day_key))] = _as_float(m_val, 0.0)

    return m_values


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
                ["VehÃ­culos usados", str(int(s["n_vehicles_used"]))],
                ["Series (vehÃ­culo-dÃ­a)", str(int(s["n_series"]))],
                ["Delta t usado", f"{s['delta_minutes']:.3f} min"],
            ]
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
            print(make_table("M (EXTRACCIÃ“N)", ["MÃ©trica", "Valor"], rows))
            print()
            printed_any = True
        except Exception as ex:
            print(make_table("M (EXTRACCIÃ“N)", ["Estado", "Detalle"], [["OMITIDO", f"No se pudo calcular: {ex}"]]))
            print()
    else:
        print(make_table("M (EXTRACCIÃ“N)", ["Estado", "Detalle"], [["OMITIDO", "No encontrado o vacÃ­o/no usable."]]))
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
    
    # ---- Cost
    try:
        costs = calculate_total_costs(root)
        if costs:
            rows = [
                ["Costo energÃ­a carga (USD)", f"{costs['energy_cost']:.2f}"],
                ["Costo inversiÃ³n (USD)", f"{costs['investment_cost']:.2f}"],
                ["Costo potencia punta (USD)", f"{costs['power_cost']:.2f}"],
                ["Costo penalidad (USD)", f"{costs['penalty_cost']:.2f}"],
                ["COSTO TOTAL (USD)", f"{costs['total_cost']:.2f}"],
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
    Calcula el costo de inversiÃ³n en estaciones y cargadores.
    Usa X.json (estaciones instaladas), N_chargers.json (nÃºmero de cargadores)
    y parameters.json (costos).
    """
    x_path = find_json_in_folder(root, "X.json")
    n_chargers_path = find_json_in_folder(root, "N_chargers.json")
    params_path = find_json_in_folder(root, "parameters.json")
    
    if not params_path:
        raise ValueError("No se encontrÃ³ parameters.json")
    
    if is_effectively_empty_json(params_path):
        raise ValueError("parameters.json estÃ¡ vacÃ­o")
    
    params_data = load_json(params_path)
    
    total_cost = 0.0
    
    # Costo de estaciones
    if x_path and not is_effectively_empty_json(x_path):
        x_data = load_json(x_path)
        station_costs = params_data.get("station_cost_k", {})
        
        # X.json tiene estructura {"k": {"station_1": 1, ...}}
        if "k" in x_data and isinstance(x_data["k"], dict):
            for station_id, value in x_data["k"].items():
                try:
                    if float(value) > 0.5:  # estaciÃ³n instalada
                        # Buscar costo de la estaciÃ³n
                        if "_1" in station_costs:
                            cost = station_costs["_1"].get(station_id, 0.0)
                            total_cost += float(cost)
                except (ValueError, TypeError):
                    continue
    
    # Costo de cargadores
    if n_chargers_path and not is_effectively_empty_json(n_chargers_path):
        n_chargers_data = load_json(n_chargers_path)
        charger_cost = params_data.get("charger_cost", 0.0)
        
        # N_chargers.json tiene estructura {"k": {"station_1": 2, ...}}
        if "k" in n_chargers_data and isinstance(n_chargers_data["k"], dict):
            for station_id, value in n_chargers_data["k"].items():
                try:
                    n_chargers = float(value)
                    total_cost += n_chargers * float(charger_cost)
                except (ValueError, TypeError):
                    continue
    
    return total_cost


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
    
    # F_seg.json tiene estructura: {"_1": {"nodo": {"_2": {"dÃ­a": {"_3": {"segmento": valor}}}}}}
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
    
    total_cost = investment_cost + energy_cost + power_cost + penalty_cost
    
    return {
        "energy_cost": energy_cost,
        "investment_cost": investment_cost,
        "power_cost": power_cost,
        "penalty_cost": penalty_cost,
        "total_cost": total_cost,
    }


if __name__ == "__main__":
    main()
