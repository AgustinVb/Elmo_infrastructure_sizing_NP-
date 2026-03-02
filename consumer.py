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

            # Energía cargada desde Sv (fórmula del modelo)
            charged_energy_kwh = None
            charged_meta: Dict[str, float] = {}
            try:
                charged_energy_kwh, charged_meta = calculate_charged_energy_from_sv(root)
            except Exception:
                charged_energy_kwh = None

            # Ascensos de B
            rise_b_kwh, rise_stats = total_rise_energy_from_b(b_path, eps=args.eps)

            rows = [
                ["Promedio consumo (solo descensos)", f"{kwhph:.6f} kWh/h"],
                ["Energía consumida (solo descensos)", f"{s['total_drop_kwh']:.6f} kWh"],
                ["Energía cargada (Sv*p_charger*delta_t)", f"{charged_energy_kwh:.6f} kWh" if charged_energy_kwh is not None else "N/D"],
                ["Energía por ascensos de B", f"{rise_b_kwh:.6f} kWh"],
                ["Pasos con ascenso (B)", str(int(rise_stats["n_rise_steps"]))],
                ["Pasos con descenso", str(int(s["n_drop_steps"]))],
                ["Pares totales evaluados", str(int(s["n_pairs_total"]))],
                ["Vehículos usados", str(int(s["n_vehicles_used"]))],
                ["Series (vehículo-día)", str(int(s["n_series"]))],
                ["Delta t usado", f"{s['delta_minutes']:.3f} min"],
            ]
            print(make_table("B (ELÉCTRICO)", ["Métrica", "Valor"], rows))
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
    
    delta_t = params_data.get("delta_t", 0.5)
    p_charger = params_data.get("p_charger", 353.0)  # potencia del cargador
    costo_electricidad = params_data.get("costo_electricidad", {})
    scaling_factor = params_data.get("scaling_factor_op_cost", 1.0)
    
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
    
    voll = float(params_data.get("Voll", 0.0))
    f_penalty_div = params_data.get("F_penalty_div", {})
    scaling_factor = float(params_data.get("scaling_factor_op_cost", 1.0))
    
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
        "investment_cost": investment_cost,
        "penalty_cost": penalty_cost,
        "total_cost": total_cost,
    }


if __name__ == "__main__":
    main()
