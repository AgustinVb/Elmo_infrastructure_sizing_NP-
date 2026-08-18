# json_plotter.py
# Autor: ELMO-UG (daniel)
# Ejecuta:  python json_plotter.py --json_dir /ruta/a/carpeta_con_json
# Salida:   /ruta/a/carpeta_con_json/plots/*.png

import os
import json
import argparse
import itertools
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.ticker import MultipleLocator, FixedLocator, FuncFormatter
from matplotlib.patches import Patch
import matplotlib.patches as mpatches
import matplotlib.patheffects as path_effects

plt.rcParams["figure.dpi"] = 120
plt.rcParams["savefig.bbox"] = "tight"
plt.rcParams["axes.grid"] = True

DEFAULT_ENERGY_PRICE_SCALE = 1.0


# -------------------- Utils --------------------


def _numeric_or_str(x: str):
    try:
        if "." in str(x):
            fx = float(x)
            return int(fx) if fx.is_integer() else fx
        return int(x)
    except (ValueError, TypeError):
        return x

def _flatten_dict(d: Dict[str, Any], path: Tuple=()) -> List[Tuple[Tuple, Any]]:
    rows = []
    for k, v in d.items():
        k2 = _numeric_or_str(k)
        if isinstance(v, dict):
            rows.extend(_flatten_dict(v, path + (k2,)))
        else:
            rows.append((path + (k2,), v))
    return rows

def _load_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _collect_named_leaf_records(tree: Any, tokens: Tuple[Tuple[str, Any], ...] = ()) -> List[Tuple[Tuple[Tuple[str, Any], ...], Any]]:
    records: List[Tuple[Tuple[Tuple[str, Any], ...], Any]] = []
    if not isinstance(tree, dict):
        return records

    for axis_name, axis_values in tree.items():
        if not isinstance(axis_values, dict):
            records.append((tokens + ((str(axis_name), axis_values),), axis_values))
            continue

        for axis_value, child in axis_values.items():
            next_tokens = tokens + ((str(axis_name), _numeric_or_str(axis_value)),)
            if isinstance(child, dict):
                records.extend(_collect_named_leaf_records(child, next_tokens))
            else:
                records.append((next_tokens, child))

    return records


# -------------------- Loaders --------------------
def load_B_df(path: str) -> Optional[pd.DataFrame]:
    data = _load_json(path)
    if not data:
        return None

    rows = []
    for tokens, value in _collect_named_leaf_records(data):
        axes = {name: axis_value for name, axis_value in tokens}
        if {"i", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="i")
            if isinstance(axes["i"], str):
                rows.append({"year": yr, "lhd": str(axes["i"]), "day": day, "interval": iv, "value": float(value)})
            else:
                rows.append({"year": yr, "lhd": axes["i"], "day": day, "interval": iv, "value": float(value)})

    if rows:
        return (pd.DataFrame(rows)
                .sort_values(["year", "lhd", "day", "interval"])
                .reset_index(drop=True))

    d = data.get("d", {})
    for lhd, d_block in d.items():
        t_block = d_block.get("t", {})
        for day, day_block in t_block.items():
            # B.json puede usar "b" o "i" con series por intervalo
            b_block = day_block.get("b", {}) or day_block.get("i", {})
            for interval, value in b_block.items():
                rows.append({
                    "lhd": lhd,
                    "day": _numeric_or_str(day),
                    "interval": _numeric_or_str(interval),
                    "value": float(value),
                })
    return (pd.DataFrame(rows)
            .sort_values(["lhd", "day", "interval"])
            .reset_index(drop=True)) if rows else pd.DataFrame(columns=["lhd","day","interval","value"])


def load_binary_Y_df(path: str) -> Optional[pd.DataFrame]:
    data = _load_json(path)
    if not data:
        return None

    rows = []
    for tokens, value in _collect_named_leaf_records(data):
        axes = {name: axis_value for name, axis_value in tokens}
        if {"i", "j", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="i")
            rows.append({
                "year": yr,
                "lhd": str(axes["i"]),
                "node": str(axes["j"]),
                "day": day,
                "interval": iv,
                "value": float(value),
            })

    if rows:
        return (pd.DataFrame(rows)
                .sort_values(["year", "lhd", "node", "day", "interval"])
                .reset_index(drop=True))

    d = data.get("d", {})
    for lhd, t_block in d.items():
        t_dict = t_block.get("t", {})
        for node, i_block in t_dict.items():
            i_dict = i_block.get("i", {})
            for day, j_block in i_dict.items():
                j_dict = j_block.get("j", {})
                for interval, val in j_dict.items():
                    rows.append({
                        "lhd": lhd,
                        "node": str(node),
                        "day": _numeric_or_str(day),
                        "interval": _numeric_or_str(interval),
                        "value": float(val),
                    })
    return (pd.DataFrame(rows)
            .sort_values(["lhd","node","day","interval"])
            .reset_index(drop=True)) if rows else pd.DataFrame(columns=["lhd","node","day","interval","value"])


def load_generic_variable_df(path: str, varname: str) -> Optional[pd.DataFrame]:
    """
    Soporta patrones:
      - d -> lhd -> t -> day -> b -> interval : value
      - d -> lhd -> t -> day -> i -> interval : value   (p.ej. C.json, E.json)
      - d -> lhd -> t -> node -> i -> day -> j -> interval : value (tipo Y)
      - k -> station -> d -> lhd -> t -> day -> i -> interval : value (p.ej. P.json con eje k)
      - Especial M.json: _1 -> LHD -> _2 -> CXXXX -> _3 -> day : value
    """
    data = _load_json(path)
    if not data:
        return None
    rows = []

    for tokens, value in _collect_named_leaf_records(data):
        axes = {name: axis_value for name, axis_value in tokens}
        row: Dict[str, Any] = {"value": float(value)}

        if {"k", "i", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="i")
            row.update({"year": yr, "station": str(axes["k"]), "lhd": str(axes["i"]),
                        "day": day, "interval": iv})
        elif {"i", "j", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="i")
            row.update({"year": yr, "lhd": str(axes["i"]), "node": str(axes["j"]),
                        "day": day, "interval": iv})
        elif {"i", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="i")
            row.update({"year": yr, "lhd": str(axes["i"]), "day": day, "interval": iv})
        else:
            row = {}

        if row:
            rows.append(row)

    if rows:
        df = pd.DataFrame(rows)
        cols = [c for c in ["year", "lhd", "station", "node", "j", "day", "interval", "value"] if c in df.columns]
        return df[cols].sort_values([c for c in cols if c != "value"]).reset_index(drop=True)

    def _find_interval_map(obj):
        """Recursively search for a dict whose values are scalar (int/float/str). Return it or None."""
        if not isinstance(obj, dict):
            return None
        # if all values are scalars (not dict/list), return this dict
        all_scalar = True
        for v in obj.values():
            if isinstance(v, dict) or isinstance(v, list):
                all_scalar = False
                break
        if all_scalar and obj:
            return obj
        # otherwise recurse into dict-valued children
        for v in obj.values():
            if isinstance(v, dict):
                found = _find_interval_map(v)
                if found is not None:
                    return found
        return None

    # -------------------------------------------------------------------------
    # Caso especial: M.json
    # Formatos soportados:
    #   - _1 -> LHD -> _2 -> CXXXX -> _3 -> day : value
    #   - _1 -> LHD -> _2 -> CXXXX -> _3 -> day -> _4 -> interval : value
    # -------------------------------------------------------------------------
    if any(str(k).startswith("_") for k in data.keys()):
        for k1, v1 in data.items():            # _1
            for lhd, v2 in v1.items():         # LH518B_1
                for k2, v3 in v2.items():     # _2
                    for j, v4 in v3.items():  # C0044, C0045... nodo j
                        for k3, val_dict in v4.items():  # _3
                            for day, val in val_dict.items():
                                if isinstance(val, dict):
                                    # Nuevo formato: el valor diario trae un mapa por intervalo.
                                    interval_map = val.get("_4", val)
                                    if isinstance(interval_map, dict):
                                        for interval, val_t in interval_map.items():
                                            if isinstance(val_t, (dict, list)):
                                                continue
                                            rows.append({
                                                "lhd": lhd,
                                                "j": j,
                                                "day": _numeric_or_str(day),
                                                "interval": _numeric_or_str(interval),
                                                "value": float(val_t)
                                            })
                                else:
                                    # Formato legacy: valor diario escalar.
                                    rows.append({
                                        "lhd": lhd,
                                        "j": j,
                                        "day": _numeric_or_str(day),
                                        "value": float(val)
                                    })

    # -------------------------------------------------------------------------
    # Caso 1: Estructura con eje 'k' (P.json, etc.)
    # Se estandariza a: k -> station -> i -> lhd -> d -> day -> t -> interval
    # -------------------------------------------------------------------------
    elif "k" in data:
        for station, k_block in data["k"].items():
            if not isinstance(k_block, dict):
                continue
            i_block = k_block.get("i", {})
            if not isinstance(i_block, dict):
                continue

            for lhd, lhd_block in i_block.items():
                if not isinstance(lhd_block, dict):
                    continue
                d_data = lhd_block.get("d", {})
                if not isinstance(d_data, dict):
                    continue
                for day, day_block in d_data.items():
                    if not isinstance(day_block, dict):
                        continue
                    t_map = day_block.get("t", {})
                    if not isinstance(t_map, dict):
                        continue
                    for interval, val in t_map.items():
                        try:
                            rows.append({
                                "lhd": _numeric_or_str(lhd),
                                "station": _numeric_or_str(station),
                                "day": _numeric_or_str(day),
                                "interval": _numeric_or_str(interval),
                                "value": float(val),
                            })
                        except Exception:
                            continue

    # -------------------------------------------------------------------------
    # Caso 2: Estructura directa con 'd' (estructura original)
    # -------------------------------------------------------------------------
    elif "d" in data:
        for outer_key, outer_block in data["d"].items():
            if not isinstance(outer_block, dict):
                continue
            if "t" in outer_block and isinstance(outer_block["t"], dict):
                # Detect whether this is a station wrapper (e.g. 'station_1' -> t -> LHDs)
                t_keys = list(outer_block["t"].keys())
                station_wrapper = False
                if isinstance(outer_key, str) and outer_key.lower().startswith("station"):
                    station_wrapper = True
                else:
                    for kk in t_keys[:5]:
                        try:
                            if isinstance(kk, str) and kk.upper().startswith("LH"):
                                station_wrapper = True
                                break
                        except Exception:
                            continue

                if station_wrapper:
                    station = outer_key
                    for lhd_key, v1 in outer_block["t"].items():
                        if isinstance(v1, dict) and "i" in v1 and isinstance(v1["i"], dict):
                            for day, j_block in v1["i"].items():
                                inner = _find_interval_map(j_block)
                                if inner:
                                    for interval, val in inner.items():
                                        if isinstance(val, dict) or isinstance(val, list):
                                            continue
                                        rows.append({
                                            "lhd": _numeric_or_str(lhd_key),
                                            "station": station,
                                            "day": _numeric_or_str(day),
                                            "interval": _numeric_or_str(interval),
                                            "value": float(val),
                                        })
                else:
                    lhd = outer_key
                    t_block = outer_block
                    for k1, v1 in t_block["t"].items():
                        if isinstance(v1, dict) and "i" in v1 and isinstance(v1["i"], dict):
                            for day, j_block in v1["i"].items():
                                if isinstance(j_block, dict):
                                    if "j" in j_block and isinstance(j_block["j"], dict):
                                        for interval, val in j_block["j"].items():
                                            rows.append({
                                                "lhd": lhd,
                                                "node": str(k1),
                                                "day": _numeric_or_str(day),
                                                "interval": _numeric_or_str(interval),
                                                "value": float(val),
                                            })
                                    else:
                                        inner = _find_interval_map(j_block)
                                        if inner:
                                            for interval, val in inner.items():
                                                if isinstance(val, dict) or isinstance(val, list):
                                                    continue
                                                rows.append({
                                                    "lhd": lhd,
                                                    "node": str(k1),
                                                    "day": _numeric_or_str(day),
                                                    "interval": _numeric_or_str(interval),
                                                    "value": float(val),
                                                })
                        elif isinstance(v1, dict) and "b" in v1:
                            for interval, val in v1["b"].items():
                                rows.append({
                                    "lhd": lhd,
                                    "day": _numeric_or_str(k1),
                                    "interval": _numeric_or_str(interval),
                                    "value": float(val),
                                })
                        elif isinstance(v1, dict) and "i" in v1:
                            for interval, val in v1["i"].items():
                                rows.append({
                                    "lhd": lhd,
                                    "day": _numeric_or_str(k1),
                                    "interval": _numeric_or_str(interval),
                                    "value": float(val),
                                })
                        else:
                            flat = _flatten_dict(v1)
                            for path_tup, val in flat:
                                interval = None
                                for key in reversed(path_tup):
                                    if isinstance(key, (int, float)):
                                        interval = key
                                        break
                                rows.append({
                                    "lhd": lhd,
                                    "day": _numeric_or_str(k1),
                                    "interval": interval,
                                    "value": float(val) if isinstance(val,(int,float)) else np.nan,
                                })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    cols = [c for c in ["year","lhd","station","node","j","day","interval","value"] if c in df.columns]
    return df[cols].sort_values([c for c in cols if c!="value"]).reset_index(drop=True)


def _has_broken_year(axes: dict) -> bool:
    """Detecta formato roto: índice y estaba ausente en schema viejo → quedó como _3/_4/_5."""
    return any(str(k).startswith("_") and str(k)[1:].isdigit() for k in axes)

def _extract_ydt_simple(axes: dict, key_h: str = None):
    """Extrae (year, day, interval) de axes en formato nuevo (y/d/t) o viejo roto (d/t/_N).
    key_h: nombre del índice fijo extra (g, h, etc.) a ignorar en la detección.
    """
    if "y" in axes:
        # Formato nuevo correcto: y, d, t presentes
        return int(axes["y"]), _numeric_or_str(axes["d"]), _numeric_or_str(axes["t"])
    # Formato viejo roto: d=year, t=day, _N=interval
    broken_key = next((k for k in axes if str(k).startswith("_") and str(k)[1:].isdigit()), None)
    if broken_key:
        return int(axes["d"]), _numeric_or_str(axes["t"]), _numeric_or_str(axes[broken_key])
    # Formato muy viejo: sin year, d=day, t=interval
    return 1, _numeric_or_str(axes["d"]), _numeric_or_str(axes["t"])


def load_p_red_df(path: str) -> Optional[pd.DataFrame]:
    """Carga P_red.json → DataFrame con columnas [year, day, interval, value].
    Soporta formato nuevo (y,d,t) y viejo roto (d,t,_3)."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes)
            rows.append({"year": yr, "day": day, "interval": iv, "value": float(val)})
    return pd.DataFrame(rows).sort_values(["year", "day", "interval"]).reset_index(drop=True) if rows else None


def load_p_gen_df(path: str) -> Optional[pd.DataFrame]:
    """Carga P_gen.json → DataFrame con columnas [gen, year, day, interval, value]."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"g", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="g")
            rows.append({"gen": str(axes["g"]), "year": yr, "day": day, "interval": iv, "value": float(val)})
    return pd.DataFrame(rows).sort_values(["gen", "year", "day", "interval"]).reset_index(drop=True) if rows else None


def load_p_bat_df(path: str) -> Optional[pd.DataFrame]:
    """Carga P_bat.json → DataFrame con columnas [storage, year, day, interval, value]."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"h", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="h")
            rows.append({"storage": str(axes["h"]), "year": yr, "day": day, "interval": iv, "value": float(val)})
    return pd.DataFrame(rows).sort_values(["storage", "year", "day", "interval"]).reset_index(drop=True) if rows else None


def load_a_h_df(path: str) -> Optional[pd.DataFrame]:
    """Carga A_h.json → DataFrame con columnas [storage, year, day, interval, value] en kWh."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"h", "d", "t"}.issubset(axes):
            yr, day, iv = _extract_ydt_simple(axes, key_h="h")
            rows.append({"storage": str(axes["h"]), "year": yr, "day": day, "interval": iv, "value": float(val)})
    return pd.DataFrame(rows).sort_values(["storage", "year", "day", "interval"]).reset_index(drop=True) if rows else None


def load_year_indexed_df(path: str, value_name: str = "value") -> Optional[pd.DataFrame]:
    """Carga un JSON de una variable indexada solo por año (ej. R, b_bar, D,
    N_ciclos) → DataFrame con columnas [year, <value_name>]."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if set(axes.keys()) == {"y"}:
            rows.append({"year": int(axes["y"]), value_name: float(val)})
    return pd.DataFrame(rows).sort_values("year").reset_index(drop=True) if rows else None


# -------------------- Parámetros --------------------
class Parameters:
    def __init__(self, params_path: str, energy_price_scale: float = 1.0):
        self.path = params_path
        self.ok = False
        self.delta_t = 1.0
        self.base_hour = 8.5
        self.energy_price_scale = float(energy_price_scale)

        self.m_j = None
        self.costo_marginal = None

        # Dos formas de emisiones:
        # a) por LHD (legacy): self.emissions  (cols: lhd, day, interval, emission_rate)
        # b) por tecnología (nuevo): self.emissions_electric (cols: day, interval, emission_rate)
        self.emissions = None
        self.emissions_electric = None

        self.pe_i = None
        self.b_max_fleet = None
        self.min_capacity_fraction = None
        self.between_shifts = []
        self.meals = []
        self.maintenance = []
        # DET defaults (esquema viejo, se mantiene por compatibilidad con
        # carpetas de resultados generadas antes de la redefinicion DET)
        self.shift_change = []
        self.forced_detention = []
        # DET defaults (esquema nuevo: between_shifts/meal/maintenance/road_clearing
        # separados, ver OptSets.build_sets en src/optimization/functions.py)
        self.meal_det = []
        self.maintenance_det = []
        self.road_clearing = []
        self.between_shifts_det = []

        self._load()

    def _load(self):
        data = _load_json(self.path)
        if not data:
            print(f"⚠️ No se encontró parameters.json en {self.path}")
            return
        self.ok = True
        self.delta_t = float(data.get("delta_t", 1.0))
        self.base_hour = float(data.get("base_hour", 8.5))
        if "b_max_fleet" in data:
            self.b_max_fleet = float(data["b_max_fleet"])
        if "min_capacity_fraction" in data:
            self.min_capacity_fraction = float(data["min_capacity_fraction"])

        # -------- m_j (demanda por nodo y año): _1->node _2->year:value --------
        m_j = {}
        for pex, blk in data.get("m_j", {}).get("_1", {}).items():
            if not isinstance(blk, dict):
                continue
            for year, val in blk.get("_2", {}).items():
                m_j.setdefault(pex, {})[_numeric_or_str(year)] = float(val)
        self.m_j = m_j if m_j else None

        # -------- costo_marginal: _1->lhd _2->year _3->day _4->interval --------
        cm = []
        for lhd, blk1 in data.get("costo_marginal", {}).get("_1", {}).items():
            for year_or_day, blk2 in blk1.get("_2", {}).items():
                if not isinstance(blk2, dict):
                    continue
                blk3 = blk2.get("_3", {})
                if blk3:
                    # new format: _2=year, _3=day, _4=interval
                    for day, blk4 in blk3.items():
                        for interval, price in blk4.get("_4", {}).items():
                            cm.append({
                                "lhd": lhd,
                                "year": _numeric_or_str(year_or_day),
                                "day": _numeric_or_str(day),
                                "interval": _numeric_or_str(interval),
                                "price": float(price) * self.energy_price_scale,
                            })
                else:
                    # legacy format: _2=day, values are intervals
                    for interval, price in blk2.items():
                        if not isinstance(price, dict):
                            cm.append({
                                "lhd": lhd,
                                "year": 1,
                                "day": _numeric_or_str(year_or_day),
                                "interval": _numeric_or_str(interval),
                                "price": float(price) * self.energy_price_scale,
                            })
        self.costo_marginal = pd.DataFrame(cm) if cm else None

        # -------- emisiones por LHD (si existiese legacy key 'emisiones') --------
        em = []
        if "emisiones" in data:
            for lhd, blk1 in data["emisiones"].get("_1", {}).items():
                for day, t_blk in blk1.get("_2", {}).items():
                    for interval, e in t_blk.get("_3", {}).items():
                        em.append({
                            "lhd": lhd,
                            "day": _numeric_or_str(day),
                            "interval": _numeric_or_str(interval),
                            "emission_rate": float(e),
                        })
        self.emissions = pd.DataFrame(em) if em else None

        # -------- emisiones eléctricas (nuevo) --------
        em_e = []
        if "emissions_electric" in data:
            for day, t_blk in data["emissions_electric"].get("_1", {}).items():
                for interval, e in t_blk.get("_2", {}).items():
                    em_e.append({
                        "day": _numeric_or_str(day),
                        "interval": _numeric_or_str(interval),
                        "emission_rate": float(e),
                    })
        self.emissions_electric = pd.DataFrame(em_e) if em_e else None

        # (opcional) guarda pe_i si lo usas en otros reportes
        self.pe_i = data.get("pe_i")

        # -------- intervalos especiales para visualización --------
        self.between_shifts = sorted(int(v) for v in data.get("time_intervals_between_shifts_set", []))

        meal_raw = data.get("time_intervals_mid_shift_meal_set", data.get("time_intervals_meal_set", []))
        self.meals = sorted(int(v) for v in meal_raw)

        maint_raw = data.get("time_intevals_maintenance_set", data.get("time_intervals_maintenance_set", []))
        self.maintenance = sorted(int(v) for v in maint_raw)

        # DET-specific sets (written by Printer when model exposes them)
        # Support multiple possible key names for backward compatibility
        shift_raw = data.get("time_intervals_shift_change_det_set", data.get("time_intervals_shift_change_set", []))
        self.shift_change = sorted(int(v) for v in shift_raw)

        forced_raw = data.get("time_intervals_forced_detention_set", data.get("time_intervals_fuel_delay_set", []))
        self.forced_detention = sorted(int(v) for v in forced_raw)

        # DET-specific sets (esquema nuevo: between_shifts/meal/maintenance/
        # road_clearing separados). Vacios si la corrida es de antes de la
        # redefinicion DET -- en ese caso el plotter cae al sombreado 2-color
        # viejo (shift_change/forced_detention) via el fallback en
        # plot_charge_power_vs_price / _load_special_intervals.
        self.meal_det = sorted(int(v) for v in data.get("time_intervals_meal_det_set", []))
        self.maintenance_det = sorted(int(v) for v in data.get("time_intervals_maintenance_det_set", []))
        self.road_clearing = sorted(int(v) for v in data.get("time_intervals_road_clearing_det_set", []))
        self.between_shifts_det = sorted(int(v) for v in data.get("time_intervals_between_shifts_det_set", []))

# -------------------- Plotter --------------------
class JSONPlotter:
    MONTH_LABELS = {
            1: "January",
            2: "February",
            3: "March",
            4: "April",
            5: "May",
            6: "June",
            7: "July",
            8: "August",
            9: "September",
            10: "October",
            11: "November",
            12: "December",
        }

    # Paleta de sombreado para intervalos DET (esquema nuevo: (color, alpha))
    DET_SHADE_COLORS = {
        "meal": ("red", 0.15),
        "road_clearing": ("gold", 0.20),
        "maintenance": ("gray", 0.20),
        "between_shifts": ("blue", 0.20),
    }

    def __init__(self, json_dir: str, energy_price_scale: float = DEFAULT_ENERGY_PRICE_SCALE, mode: str = "DCH"):
        self.json_dir = json_dir
        self.mode = mode.upper() if isinstance(mode, str) else "DCH"

        # Guardar SIEMPRE en <json_dir>/plots
        self.plot_dir = os.path.join(self.json_dir, "plots")
        os.makedirs(self.plot_dir, exist_ok=True)

        # Variables
        self.df_B   = load_B_df(os.path.join(json_dir, "B.json"))
        self.df_Y   = load_binary_Y_df(os.path.join(json_dir, "Y.json"))
        self.df_P   = load_generic_variable_df(os.path.join(json_dir, "P.json"), "P")
        self.df_C   = load_generic_variable_df(os.path.join(json_dir, "C.json"), "C")
        self.df_E   = load_generic_variable_df(os.path.join(json_dir, "E.json"), "E")
        self.df_M   = load_generic_variable_df(os.path.join(json_dir, "M.json"), "M")
        self.df_Pred = load_p_red_df(os.path.join(json_dir, "P_red.json"))
        self.df_Pgen = load_p_gen_df(os.path.join(json_dir, "P_gen.json"))
        self.df_Pbat = load_p_bat_df(os.path.join(json_dir, "P_bat.json"))
        self.df_Ah   = load_a_h_df(os.path.join(json_dir, "A_h.json"))

        # Degradación de batería on-board (fleet-wide, un valor por año)
        self.df_R        = load_year_indexed_df(os.path.join(json_dir, "R.json"), "R")
        self.df_b_bar    = load_year_indexed_df(os.path.join(json_dir, "b_bar.json"), "b_bar")
        self.df_D        = load_year_indexed_df(os.path.join(json_dir, "D.json"), "D")
        self.df_N_ciclos = load_year_indexed_df(os.path.join(json_dir, "N_ciclos.json"), "N_ciclos")

        # Parámetros
        self.params = Parameters(os.path.join(json_dir, "parameters.json"), energy_price_scale=energy_price_scale)
        self.delta_t = self.params.delta_t if self.params.ok else 1.0
        self.start_hour = self.params.base_hour if self.params.ok else 8.5

        # Dominio
        self.days = self._detect_days()
        self.years = self._detect_years()
        self.intervals = self._detect_intervals()
        self.special_intervals = self._load_special_intervals()

    def _filter_yd(self, df: pd.DataFrame, y: int, d: int) -> pd.DataFrame:
        """Filtra un DataFrame por (year, day) usando índices booleanos.
        Tolera DataFrames sin columna 'year' (datos generados antes del índice de año).
        """
        mask = df["day"] == d
        if "year" in df.columns:
            mask = mask & (df["year"] == y)
        return df[mask]

    def _detect_years(self) -> List[int]:
        sources = []
        for df in [self.df_B, self.df_Y, self.df_P, self.df_M, self.df_Pred]:
            if df is not None and "year" in df.columns and not df.empty:
                sources.append(sorted(df["year"].dropna().unique().tolist()))
        return sorted({int(y) for lst in sources for y in lst}) if sources else [1]

    def _detect_days(self) -> List[int]:
        sources = []
        for df in [self.df_B, self.df_Y, self.df_P, self.df_C, self.df_E, self.df_M]:
            if df is not None and "day" in df.columns and not df.empty:
                sources.append(sorted(df["day"].dropna().unique().tolist()))
        if self.params.costo_marginal is not None:
            sources.append(sorted(self.params.costo_marginal["day"].unique().tolist()))
        return sorted({int(d) for lst in sources for d in lst}) if sources else []

    def _detect_intervals(self) -> List[int]:
        sources = []
        for df in [self.df_B, self.df_Y, self.df_P, self.df_C, self.df_E, self.df_M]:
            if df is not None and "interval" in df.columns and not df.empty:
                sources.append(sorted(df["interval"].dropna().unique().tolist()))
        if self.params.costo_marginal is not None:
            sources.append(sorted(self.params.costo_marginal["interval"].unique().tolist()))
        if not sources:
            return []
        ints = sorted({int(t) for lst in sources for t in lst})
        if ints and ints[0] == 0 and 1 in ints:
            return list(range(min(ints), max(ints) + 1))
        return ints
    
    def _rep_day_label(self, d: int) -> str:
        """
        Devuelve 'día Mes' determinando a qué intervalo mensual pertenece d.
        """
        try:
            di = int(d)
        except Exception:
            return f"Day {d}"

        # Caso: el dataset ya usa 1..12 como meses
        if self.days and max(self.days) <= 12:
            return self.MONTH_LABELS.get(di, f"Month {di}")

        # No asumimos que min(self.days) == 1 de enero
        min_day = min(self.days) if self.days else 1
        day_of_year = ((di - min_day) % 365) + 1

        month_lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        cum = 0
        for m, ml in enumerate(month_lengths, start=1):
            start = cum + 1
            end = cum + ml
            if start <= day_of_year <= end:
                return self.MONTH_LABELS.get(m, f"Month{m}")
            cum += ml
        return f"Day {d}"

    def _season_label(self, d: int) -> str:
        """
        Devuelve estación del año para el día d (hemisferio sur):
        Summer, Autumn, Winter, Spring.
        """
        try:
            di = int(d)
        except Exception:
            return ""

        if self.days and max(self.days) <= 12:
            month = di
        else:
            min_day = min(self.days) if self.days else 1
            day_of_year = ((di - min_day) % 365) + 1
            month_lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            cum = 0
            month = 1
            for m, ml in enumerate(month_lengths, start=1):
                if day_of_year <= cum + ml:
                    month = m
                    break
                cum += ml

        if month in (12, 1, 2):
            return "Summer"
        if month in (3, 4, 5):
            return "Autumn"
        if month in (6, 7, 8):
            return "Winter"
        return "Spring"
    
    # -------------------- NUEVO MÉTODO DE TIEMPO FIJO --------------------
    def _get_fixed_time_ticks(self, mode="interval"):
        """
        Devuelve ticks y etiquetas FIJAS para: 09:00, 13:00, 17:00, 21:00, 01:00, 05:00, 09:00(+1).
        mode="hour": devuelve posiciones 0, 4, 8... (para gráficos con eje X en horas)
        mode="interval": devuelve posiciones 1, 1+step... (para gráficos con eje X en intervalos)
        """
        # Horas relativas desde el inicio (0h = 09:00 AM)
        rel_hours = [0, 4, 8, 12, 16, 20, 24]
        labels = ["09:00", "13:00", "17:00", "21:00", "01:00", "05:00", "09:00"]
        
        if mode == "hour":
            return rel_hours, labels
        else:
            # Convertir horas a índice de intervalo: index = 1 + (horas / delta_t)
            # Ejemplo: Si delta_t=0.5, hora 4 es intervalo 1 + 8 = 9.
            dt = float(self.delta_t)
            ticks = [1 + h / dt for h in rel_hours]
            return ticks, labels
    # ---------------------------------------------------------------------

    def _xtick_hours(self):
        if not self.intervals:
            return [], []
        step = max(1, int(2 / self.delta_t))  # cada ~2h
        ticks = self.intervals[::step]
        labels = [f"{int((t-1)*self.delta_t)}h" for t in ticks]
        return ticks, labels

    def _get_hourly_time_ticks(self, start_hour: int = 9):
        """
        Devuelve ticks horarios (cada 1h) para 24 horas,
        comenzando en start_hour y cerrando en start_hour del día siguiente.
        """
        ticks = list(range(0, 25))
        labels = [f"{(start_hour + h) % 24:02d}:00" for h in ticks]
        return ticks, labels

    def _build_intervals_from_clock_windows(self, windows: List[Tuple[str, str]], start_hour: int = 9) -> List[int]:
        if not self.intervals:
            return []

        dt_minutes = int(round(float(self.delta_t) * 60.0))
        if dt_minutes <= 0:
            return []

        max_t = int(max(self.intervals))
        base_minutes = start_hour * 60

        def _parse_hhmm(s: str) -> int:
            hh, mm = s.strip().split(":")
            return int(hh) * 60 + int(mm)

        out = set()
        for start_str, end_str in windows:
            start_min = _parse_hhmm(start_str)
            end_min = _parse_hhmm(end_str)

            if start_min < base_minutes:
                start_min += 24 * 60
            if end_min < base_minutes:
                end_min += 24 * 60
            if end_min <= start_min:
                end_min += 24 * 60

            a = start_min - base_minutes
            b = end_min - base_minutes

            for t in range(1, max_t + 1):
                s = int(round((t - 1) * dt_minutes))
                e = int(round(t * dt_minutes))
                if max(s, a) < min(e, b):
                    out.add(t)

        allowed = set(int(v) for v in self.intervals)
        return sorted(v for v in out if v in allowed)

    def _load_special_intervals(self) -> Dict[str, List[int]]:
        sets_path = os.path.join(self.json_dir, "sets.json")
        sets_data = _load_json(sets_path)
        if sets_data:
            return {
                "between_shifts": sorted(int(v) for v in sets_data.get("time_intervals_between_shifts_set", [])),
                "meal": sorted(int(v) for v in sets_data.get("time_intervals_meal_set", [])),
                "maintenance": sorted(int(v) for v in sets_data.get("time_intervals_maintenance_set", [])),
            }
        # Default DCH scheme (colaciones, cambios de turno, mantenimiento)
        if self.mode == "DCH":
            return {
                "between_shifts": self._build_intervals_from_clock_windows([
                    ("19:00", "21:00"),
                    ("07:00", "09:00"),
                ], start_hour=9),
                "meal": self._build_intervals_from_clock_windows([
                    ("14:04", "14:56"),
                    ("02:04", "02:56"),
                ], start_hour=9),
                "maintenance": self._build_intervals_from_clock_windows([
                    ("10:04", "12:26"),
                    ("16:04", "17:26"),
                    ("22:04", "00:26"),
                    ("04:04", "05:26"),
                ], start_hour=9),
            }

        # DET scheme: use provided shift_change + fuel_delay windows to build DET intervals
        if self.mode == "DET":
            det_windows = [
                ("09:00", "10:12"),          # fuel_delay during shift 2 (overlap from 09:00)
                ("16:00", "17:04"),          # shift_change at 16:00
                ("17:04", "18:16"),          # fuel_delay
                ("00:00", "01:04"),          # shift_change at 00:00
                ("01:04", "02:16"),          # fuel_delay
                ("08:00", "09:04"),          # shift_change next day
            ]
            det_intervals = self._build_intervals_from_clock_windows(det_windows, start_hour=9)
            return {
                "between_shifts": [],
                "meal": [],
                "maintenance": det_intervals,
            }

        # Fallback: empty sets
        return {"between_shifts": [], "meal": [], "maintenance": []}

    # ---------- Plots ----------
    def plot_charge_power_vs_price(self):
        if self.df_P is None or self.df_P.empty:
            print("⚠️ No hay P.json. Omitiendo 'ChargePower_vs_price'.")
            return

        title_fs = 22
        axis_label_fs = 16
        tick_fs = 14
        legend_fs = 14

        start_hour = self.start_hour
        dt = float(self.delta_t)

        for y, d in itertools.product(self.years, self.days):
            p_day = (self._filter_yd(self.df_P, y, d)[["interval", "value"]]
                        .groupby("interval")["value"].sum()
                        .reindex(self.intervals).fillna(0.0))

            times = np.array([(t - 1) * dt for t in self.intervals])
            pcharge = p_day.values

            times_step = np.append(times, times[-1] + dt) + start_hour
            pcharge_step = np.append(pcharge, pcharge[-1])

            fig, ax1 = plt.subplots(figsize=(9, 5.6))
            ax2 = ax1.twinx()
            ax2.grid(False)

            def _group_consecutive(vals):
                vals = sorted(set(int(v) for v in vals if int(v) in set(self.intervals)))
                if not vals:
                    return []
                groups = []
                start = vals[0]
                prev = vals[0]
                for curr in vals[1:]:
                    if curr == prev + 1:
                        prev = curr
                        continue
                    groups.append((start, prev))
                    start = curr
                    prev = curr
                groups.append((start, prev))
                return groups

            between_shifts_color = 'gray'
            between_shifts_alpha = 0.6
            meal_color = 'gray'
            meal_alpha = 0.15
            maintenance_color = '#FF9999'
            maintenance_alpha = 0.15

            if self.mode == "DET":
                meal_det_intervals = self.params.meal_det
                road_clearing_intervals = self.params.road_clearing
                maintenance_det_intervals = self.params.maintenance_det
                between_shifts_det_intervals = self.params.between_shifts_det

                if meal_det_intervals or road_clearing_intervals or maintenance_det_intervals or between_shifts_det_intervals:
                    # Esquema DET nuevo (between_shifts/meal/maintenance/road_clearing
                    # separados, ver OptSets.build_sets): sombreado de 4 colores.
                    shade_specs = [
                        (between_shifts_det_intervals, *self.DET_SHADE_COLORS["between_shifts"]),
                        (meal_det_intervals, *self.DET_SHADE_COLORS["meal"]),
                        (road_clearing_intervals, *self.DET_SHADE_COLORS["road_clearing"]),
                        (maintenance_det_intervals, *self.DET_SHADE_COLORS["maintenance"]),
                    ]
                else:
                    # Fallback: carpeta de resultados de antes de la redefinicion DET
                    # (parameters.json solo trae shift_change/forced_detention viejos).
                    shift_change_intervals = (
                        self.params.shift_change
                        if getattr(self.params, 'shift_change', None)
                        else self.special_intervals.get("shift_change", [])
                    )
                    forced_intervals = (
                        self.params.forced_detention
                        if getattr(self.params, 'forced_detention', None)
                        else self.special_intervals.get("forced_detention", [])
                    )
                    shade_specs = [
                        (shift_change_intervals, between_shifts_color, between_shifts_alpha),
                        (forced_intervals, between_shifts_color, between_shifts_alpha),
                    ]
                peak_windows = [("18:00", "22:00")]
                peak_intervals = self._build_intervals_from_clock_windows(peak_windows, start_hour=self.start_hour)
            else:
                between_shifts_intervals = (
                    self.params.between_shifts
                    if getattr(self.params, 'between_shifts', None)
                    else self.special_intervals.get("between_shifts", [])
                )
                meal_intervals = (
                    self.params.meals
                    if getattr(self.params, 'meals', None)
                    else self.special_intervals.get("meal", [])
                )
                maintenance_intervals = (
                    self.params.maintenance
                    if getattr(self.params, 'maintenance', None)
                    else self.special_intervals.get("maintenance", [])
                )

                shade_specs = [
                    (between_shifts_intervals, between_shifts_color, between_shifts_alpha),
                    (meal_intervals, meal_color, meal_alpha),
                    (maintenance_intervals, maintenance_color, maintenance_alpha),
                ]
            for intervals_list, color, alpha in shade_specs:
                for gs, ge in _group_consecutive(intervals_list):
                    x0 = start_hour + (gs - 1) * dt
                    x1 = start_hour + ge * dt
                    ax1.axvspan(x0, x1, color=color, alpha=alpha, linewidth=0)

            ax1.step(times_step, pcharge_step, where='post', label='Charge Power', color='blue', linewidth=1.5)

            # Energy Price line on right axis
            price_line = None
            if self.params.costo_marginal is not None and not self.params.costo_marginal.empty:
                cm = self._filter_yd(self.params.costo_marginal, y, d) if "day" in self.params.costo_marginal.columns else self.params.costo_marginal
                if cm.empty:
                    cm = self.params.costo_marginal.query("day == @d") if "day" in self.params.costo_marginal.columns else self.params.costo_marginal
                if not cm.empty:
                    price_by_interval = (cm.groupby("interval")["price"].mean()
                                         .reindex(self.intervals).ffill().bfill().fillna(0.0))
                    p_times = np.array([(t - 1) * dt + start_hour for t in self.intervals])
                    p_times_step = np.append(p_times, p_times[-1] + dt)
                    p_vals_step = np.append(price_by_interval.values, price_by_interval.values[-1])
                    price_line, = ax2.plot(p_times_step, p_vals_step, color='red', linewidth=1.8,
                                           label='Energy Price', zorder=3)

            ax2.set_ylabel('Energy Price [USD/kWh]', color='black', fontsize=axis_label_fs)
            ax2.tick_params(axis='y', labelcolor='black', labelsize=tick_fs)
            ax2.set_ylim(0, 0.30)
            ax2.yaxis.set_major_locator(MultipleLocator(0.05))

            ax1.set_ylabel('Charge Power [kW]', color='black', fontsize=axis_label_fs)
            ax1.set_xlabel('Hour', fontsize=axis_label_fs)
            ax1.tick_params(axis='y', labelcolor='black', labelsize=tick_fs)
            ax1.set_ylim(0, 2500)
            ax1.set_xlim(times_step[0], times_step[-1])
            ax1.grid(False)
            ax1.set_axisbelow(False)

            if self.mode == "DET":
                ymin, ymax = ax1.get_ylim()
                for gs, ge in _group_consecutive(peak_intervals):
                    x0 = start_hour + (gs - 1) * dt
                    x1 = start_hour + ge * dt
                    hatch_rect = mpatches.Rectangle(
                        (x0, ymin),
                        x1 - x0,
                        ymax - ymin,
                        facecolor='none',
                        edgecolor='#d62728',
                        hatch='///',
                        linewidth=0.0,
                        alpha=0.35,
                        zorder=2,
                    )
                    ax1.add_patch(hatch_rect)
                    ax1.vlines([x0, x1], ymin=ymin, ymax=ymax, colors='#d62728', linestyles='--', linewidth=1.2, alpha=0.9)

            end = np.ceil(times_step[-1])
            xticks = np.arange(start_hour, end + 1, 4)
            ax1.xaxis.set_major_locator(FixedLocator(xticks))

            def hour_formatter(x, pos):
                h = int(np.floor(x)) % 24
                m = int(round((x - np.floor(x)) * 60)) % 60
                return f"{h:02d}:{m:02d}"

            ax1.xaxis.set_major_formatter(FuncFormatter(hour_formatter))
            ax1.xaxis.set_minor_locator(MultipleLocator(1))
            ax1.tick_params(axis='x', labelsize=tick_fs)

            line1 = plt.Line2D([0], [0], color='blue', linewidth=1.5, label='Charge Power')
            patch_between = mpatches.Patch(color=between_shifts_color, alpha=between_shifts_alpha, label='Between Shifts')
            patch_meal = mpatches.Patch(color=meal_color, alpha=meal_alpha, label='Meal')
            patch_maint = mpatches.Patch(color=maintenance_color, alpha=maintenance_alpha, label='Maintenance')
            patch_peak_hatch = mpatches.Patch(facecolor='none', edgecolor='#d62728', hatch='///', label='Peak hours')

            if self.mode == "DET":
                if meal_det_intervals or road_clearing_intervals or maintenance_det_intervals or between_shifts_det_intervals:
                    between_color_det, between_alpha_det = self.DET_SHADE_COLORS["between_shifts"]
                    meal_color_det, meal_alpha_det = self.DET_SHADE_COLORS["meal"]
                    road_color, road_alpha = self.DET_SHADE_COLORS["road_clearing"]
                    maint_color_det, maint_alpha_det = self.DET_SHADE_COLORS["maintenance"]
                    patch_between_det = mpatches.Patch(color=between_color_det, alpha=between_alpha_det, label='Between Shifts')
                    patch_meal_det = mpatches.Patch(color=meal_color_det, alpha=meal_alpha_det, label='Meal')
                    patch_road = mpatches.Patch(color=road_color, alpha=road_alpha, label='Road Clearing')
                    patch_maint_det = mpatches.Patch(color=maint_color_det, alpha=maint_alpha_det, label='Maintenance')
                    handles = [line1, patch_peak_hatch, patch_between_det, patch_meal_det, patch_road, patch_maint_det]
                else:
                    handles = [line1, patch_peak_hatch, patch_between]
            else:
                handles = [line1, patch_between, patch_meal, patch_maint]
                if price_line is not None:
                    line_price = plt.Line2D([0], [0], color='red', linewidth=1.8, label='Energy Price')
                    handles.insert(1, line_price)

            month = self._rep_day_label(d)
            fig.suptitle(f"Y{y} {month} – Total Charge Power vs Energy Price", fontsize=title_fs, y=1.06)

            fig.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, 0.99),
                       ncol=len(handles), fontsize=legend_fs, frameon=True)

            plt.tight_layout()
            fig.subplots_adjust(top=0.82)

            fig.savefig(
                os.path.join(self.plot_dir, f"ChargePower_vs_price_Y{y}_{month}.png"),
                bbox_inches="tight", dpi=120
            )
            plt.close(fig)

    
    def plot_node_extraction_vs_demand(self):
            # Dentro de plot_node_extraction_vs_demand, justo después de cargar el DataFrame M
            df = load_generic_variable_df(self.json_dir, "M")
            if df.empty:
                print("⚠️ No hay M.json o está vacío. Omitiendo 'Extraction vs Demand'.")
                return

            # Creamos una columna unificada que siempre exista
            df['node_plot'] = df['node'] if 'node' in df.columns else df.get('j')
            
            mj = self.params.m_j or {}

            for d in self.days:
                df_day = self.df_M.query("day == @d")
                if df_day.empty:
                    continue

                # Extracción total por nodo j (sumando equipos i)
                extr = (
                    df_day
                    .groupby("node")["value"]
                    .sum()
                    .rename("extracted_tons")
                    .to_frame()
               )

                fig, ax = plt.subplots(figsize=(max(8, 0.35 * len(extr)), 5))
                x = np.arange(len(extr.index))

                ax.bar(
                    x,
                    extr["extracted_tons"].values,
                    edgecolor="black",
                    label="Extraction"
                )
                # Demanda desde m_j[j, d]
                if mj:
                    demand = [mj.get(node if node in mj else str(node), {}).get(d, np.nan) 
                               for node, d in zip(extr['node_plot'], extr['day'])]
                    ax.plot(
                        x,
                        demand,
                        linestyle="--",
                        linewidth=2.5,
                        color="red",
                        label="Demand"
                    )

                ax.set_xticks(x)
                ax.set_xticklabels(extr.index, rotation=90)
                ax.set_ylabel("Extracted tons")
                ax.set_title(f"Extraction vs Demand – {self._rep_day_label(d)}")

                if mj:
                    ax.legend(loc="upper left")

                fig.tight_layout()

                month = self._rep_day_label(d)
                fig.savefig(
                    os.path.join(self.plot_dir, f"Extraction_vs_Demand_{month}.png")
                )
                plt.close(fig)


    def plot_lhd_soc_vs_price_and_states(self):
        if (self.df_B is None or self.df_B.empty) and (self.df_E is None or self.df_E.empty):
            print("⚠️ No hay B.json ni E.json. Omitiendo 'SoC_vs_price_and_states'.")
            return
        if self.params.costo_marginal is None or self.params.costo_marginal.empty:
            print("⚠️ No hay costo marginal en parameters.json. Omitiendo 'SoC_vs_price_and_states'.")
            return

        delta_t = float(self.delta_t)
    

        # Detectar LHDs (desde B o E)
        lhds = set()
        if self.df_B is not None and not self.df_B.empty:
            lhds.update(self.df_B["lhd"].unique().tolist())
        if self.df_E is not None and not self.df_E.empty:
            lhds.update(self.df_E["lhd"].unique().tolist())
        lhds = sorted(lhds)

        for lhd in lhds:
            for y, day in itertools.product(self.years, self.days):
                _b = self._filter_yd(self.df_B, y, day) if (self.df_B is not None and not self.df_B.empty) else pd.DataFrame()
                dfB_sel = _b[_b["lhd"] == lhd] if not _b.empty else pd.DataFrame()
                _e = self.df_E[self.df_E["day"] == day] if (self.df_E is not None and not self.df_E.empty) else pd.DataFrame()
                dfE_sel = _e[_e["lhd"] == lhd] if not _e.empty else pd.DataFrame()

                if dfB_sel.empty and dfE_sel.empty:
                    continue
                is_electric = not dfB_sel.empty

                palette = {
                    "State-of-Charge": "#f78c11",
                    "Fuel": "#4169E1",
                    "Energy Price": "#3366CC",
                    "Charging": "#3366CC",
                    "Inactive": "#B0B0B0",
                    "In-Transit": "#F0BF57",
                }

                fig = plt.figure(figsize=(18, 6))
                fig.subplots_adjust(left=0.06, right=0.92, top=0.84, bottom=0.12)
                gs = gridspec.GridSpec(3, 1, height_ratios=[0.65, 4.2, 1.0], hspace=0.08)
                legend_ax = fig.add_subplot(gs[0])
                legend_ax.axis("off")
                ax_main = fig.add_subplot(gs[1])
                ax_task = fig.add_subplot(gs[2], sharex=ax_main)
                ax_price = ax_main.twinx()

                if is_electric:
                    series = (dfB_sel.groupby("interval")["value"].mean()
                              .reindex(self.intervals)
                              .ffill().bfill().fillna(0.0))
                    s0 = float(series.iloc[0]) if len(series) else 0.0
                    y_raw = np.array([s0] + series.tolist(), dtype=float)
                    max_ref = max(float(np.nanmax(y_raw)), 1e-6)
                    y_level = (y_raw / max_ref) * 100.0
                    y_label = "State-of-Charge [%]"
                    soc_color = palette["State-of-Charge"]
                    soc_name = "State-of-Charge"
                else:
                    series = (dfE_sel.groupby("interval")["value"].mean()
                              .reindex(self.intervals)
                              .ffill().bfill().fillna(0.0))
                    s0 = float(series.iloc[0]) if len(series) else 0.0
                    y_raw = np.array([s0] + series.tolist(), dtype=float)
                    max_ref = max(float(np.nanmax(y_raw)), 1e-6)
                    y_level = (y_raw / max_ref) * 100.0
                    y_label = "Fuel Level [%]"
                    soc_color = palette["Fuel"]
                    soc_name = "Fuel"

                x_steps = np.array([0.0] + [t * delta_t for t in self.intervals], dtype=float)
                line_soc = ax_main.plot(x_steps, y_level, color=soc_color, lw=3, alpha=1.0, label=soc_name)[0]
                line_soc.set_path_effects([
                    path_effects.SimpleLineShadow(offset=(1.2, -1.2), alpha=0.25),
                    path_effects.Normal(),
                ])

                price_day = (self.params.costo_marginal[
                                 (self.params.costo_marginal["day"] == day) if "day" in self.params.costo_marginal.columns else slice(None)
                             ].groupby("interval")["price"].mean()
                             .reindex(self.intervals)
                             .ffill().bfill().fillna(0.0)
                             .to_numpy(dtype=float))
                y_price = np.concatenate([[price_day[0] if len(price_day) else 0.0], price_day])
                line_price = ax_price.plot(
                    x_steps,
                    y_price,
                    color=palette["Energy Price"],
                    linestyle="--",
                    linewidth=2.6,
                    alpha=0.9,
                    label="Energy Price",
                )[0]

                _y = self._filter_yd(self.df_Y, y, day) if (self.df_Y is not None and not self.df_Y.empty) else pd.DataFrame()
                Y_filtered = _y[(_y["lhd"] == lhd) & (_y["value"] >= 0.5)] if not _y.empty else pd.DataFrame()
                _p = self._filter_yd(self.df_P, y, day) if (self.df_P is not None and not self.df_P.empty) else pd.DataFrame()
                P_filtered = _p[_p["lhd"] == lhd] if not _p.empty else pd.DataFrame()

                states = []
                for t in self.intervals:
                    is_traveling = False
                    if not Y_filtered.empty and "interval" in Y_filtered.columns:
                        is_traveling = not Y_filtered.query("interval == @t").empty

                    is_charging = False
                    if is_electric and not P_filtered.empty and {"interval", "value"}.issubset(P_filtered.columns):
                        is_charging = not P_filtered.query("interval == @t and value > 1").empty

                    if is_traveling:
                        states.append("In-Transit")
                    elif is_charging:
                        states.append("Charging")
                    else:
                        states.append("Inactive")

                if states:
                    segments = []
                    start_idx = 0
                    current_state = states[0]
                    for i in range(1, len(states)):
                        if states[i] != current_state:
                            segments.append((start_idx, i, current_state))
                            start_idx = i
                            current_state = states[i]
                    segments.append((start_idx, len(states), current_state))

                    for start_idx, end_idx, state in segments:
                        x_start = start_idx * delta_t
                        width = (end_idx - start_idx) * delta_t
                        ax_task.barh(
                            0.0,
                            width=width,
                            left=x_start,
                            height=0.56,
                            color=palette[state],
                            edgecolor=palette[state],
                            alpha=1.0,
                        )

                row1_names = [soc_name, "Energy Price"]
                row2_names = ["Charging", "Inactive", "In-Transit"]
                row1_handles = [
                    plt.Line2D([0], [0], color=palette[name], lw=4, label=name)
                    for name in row1_names
                ]
                row2_handles = [
                    plt.Line2D([0], [0], color=palette[name], lw=6, label=name)
                    for name in row2_names
                ]

                legend1 = ax_main.legend(
                    handles=row1_handles,
                    loc="upper left",
                    bbox_to_anchor=(0.15, 1.27),
                    ncols=len(row1_handles),
                    frameon=True,
                    fontsize=16,
                    title_fontsize=17,
                    framealpha=0.6,
                )
                legend2 = ax_main.legend(
                    handles=row2_handles,
                    loc="upper right",
                    bbox_to_anchor=(0.9, 1.35),
                    ncols=len(row2_handles),
                    frameon=True,
                    fontsize=16,
                    title="Task States",
                    title_fontsize=17,
                    framealpha=0.6,
                )
                ax_main.add_artist(legend1)
                legend_ax.set_xlim(0, 24)

                ax_main.set_ylabel(y_label, fontsize=17, color=soc_color)
                ax_price.set_ylabel("Energy Price [USD/kWh]", fontsize=17, color=palette["Energy Price"])
                ax_main.set_ylim(-5, 105)
                ax_main.yaxis.set_major_locator(MultipleLocator(20.0))
                ax_main.set_yticks([0, 20, 40, 60, 80, 100])

                ax_price.set_ylim(0.0, 0.30)
                ax_price.yaxis.set_major_locator(MultipleLocator(0.05))

                ticks, labels = self._get_hourly_time_ticks(start_hour=9)
                ax_task.set_xticks(ticks)
                ax_task.set_xticklabels(labels, fontsize=13, rotation=0, ha="center")

                ax_main.set_xlim(0, 24)
                ax_task.set_xlim(0, 24)
                ax_price.set_xlim(0, 24)

                ax_task.set_xlabel("Time", fontsize=18)
                ax_task.set_yticks([])
                ax_task.set_ylabel("Tasks", fontsize=18)

                ax_main.tick_params(labelbottom=False, bottom=False, axis="y", labelsize=16)
                ax_price.tick_params(labelbottom=False, bottom=False, axis="y", labelsize=16)
                ax_task.tick_params(axis="x", labelsize=15.5)
                ax_main.tick_params(axis="x", which="both", bottom=False, top=False, labelbottom=False, labeltop=False)
                ax_price.tick_params(axis="x", which="both", bottom=False, top=False, labelbottom=False, labeltop=False)
                ax_main.grid(False)
                ax_main.grid(axis="x", which="major", linestyle="--", alpha=0.5)
                ax_main.yaxis.grid(False)
                ax_price.grid(False)
                ax_task.grid(False)

                month = self._rep_day_label(day)
                fig.suptitle(f"LHD {lhd} Y{y} {month}", y=0.96, fontsize=18)

                fig.savefig(os.path.join(self.plot_dir, f"SoC_vs_price_LHD-{lhd}_Y{y}_day-{day}.png"), dpi=150, bbox_inches="tight")
                plt.close(fig)


    def plot_emissions_profiles_for_optimized_day(self):
        # Verificación de datos
        have_elec = (self.params.emissions_electric is not None and
                     not self.params.emissions_electric.empty)
        if not have_elec:
            print("ℹ️ No hay perfiles de emisiones eléctricas. Omitiendo 'Emission Profile'.")
            return


        for y, d in itertools.product(self.years, self.days):

            e_day = (self.params.emissions_electric[self.params.emissions_electric["day"] == d]
                    .groupby("interval")["emission_rate"].mean()
                    .reindex(self.intervals)
                    .ffill().bfill().fillna(0.0))

            if e_day.isna().all() or (e_day == 0).all():
                continue

            fig = plt.figure(figsize=(14, 8))
            gs = gridspec.GridSpec(2, 1, height_ratios=[0.20, 0.80], hspace=0.08)
            ax_top = fig.add_subplot(gs[0])   # título + leyenda
            ax = fig.add_subplot(gs[1])       # gráfico principal

            ax.plot(self.intervals, e_day.values, linewidth=3.0,
                    label="Electric Emissions", color="C2")

            ax.set_ylim(0.0, 0.5)
            
            # --- Ticks fijos ---
            ticks, labels = self._get_fixed_time_ticks(mode="interval")
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels)
            ax.set_xlim(ticks[0], ticks[-1])
            # -------------------

            ax.margins(x=0.0)
            ax.grid(False)

            ax.set_xlabel("Time", fontsize=22, labelpad=10)
            ax.set_ylabel("Emission Rate [kgCO2/kWh]", fontsize=22, labelpad=10)

            ax.tick_params(axis="x", labelsize=20, pad=8)
            ax.tick_params(axis="y", labelsize=20, pad=20)

            ax_top.set_axis_off()
            month = self._rep_day_label(d)
            ax_top.text(
                0.5, 0.78,
                f"{month} – Electric Emission Profile",
                ha="center", va="center", fontsize=32
            )
          
            handles, labels_ = ax.get_legend_handles_labels()
            leg = ax_top.legend(handles, labels_, loc="center", ncols=1, fontsize=18,
                                frameon=True, fancybox=False, framealpha=1.0,
                                bbox_to_anchor=(0.5, 0.18))
            leg.get_frame().set_edgecolor("#cccccc")
            leg.get_frame().set_linewidth(1.2)
            leg.get_frame().set_facecolor("white")

            month = self._rep_day_label(d)
            fig.savefig(
                        os.path.join(self.plot_dir, f"EmissionProfiles_Y{y}_{month}.png"),
                        bbox_inches="tight"
            )
            plt.close(fig)


    def plot_lhd_costs_bars(self):
        if self.params.costo_marginal is None or self.params.costo_marginal.empty:
            print("⚠️ No hay costo marginal. Omitiendo 'LHD_total_costs'.")
            return
        lhds = sorted(self.params.costo_marginal["lhd"].unique().tolist())
        costs = {l: 0.0 for l in lhds}

        cm = self.params.costo_marginal
        if self.df_P is not None and not self.df_P.empty:
            for lhd in lhds:
                for y, d in itertools.product(self.years, self.days):
                    _cm = cm[cm["day"] == d] if "day" in cm.columns else cm
                    price = _cm.groupby("interval")["price"].mean()
                    _pp = self._filter_yd(self.df_P, y, d)
                    p = _pp[_pp["lhd"] == lhd].groupby("interval")["value"].mean()
                    if not p.empty and not price.empty:
                        merged = pd.concat([p.rename("value"), price], axis=1).dropna()
                        costs[lhd] += float((merged["value"] * merged["price"] * self.delta_t).sum())
        elif self.df_E is not None and not self.df_E.empty:
            for lhd in lhds:
                for y, d in itertools.product(self.years, self.days):
                    _cm = cm[cm["day"] == d] if "day" in cm.columns else cm
                    price = _cm.groupby("interval")["price"].mean()
                    _e = self.df_E[self.df_E["day"] == d] if "day" in self.df_E.columns else self.df_E
                    e = _e[_e["lhd"] == lhd].groupby("interval")["value"].mean()
                    if not e.empty and not price.empty:
                        merged = pd.concat([e.rename("value"), price], axis=1).dropna()
                        costs[lhd] += float((merged["value"] * merged["price"]).sum())

        if any(v != 0 for v in costs.values()):
            fig, ax = plt.subplots(figsize=(10,5))
            x = np.arange(len(lhds))
            ax.bar(x, [costs[l] for l in lhds], edgecolor="black")
            ax.set_xticks(x); ax.set_xticklabels(lhds, rotation=45, ha="right")
            ax.set_ylabel("Total Cost [USD]")
            ax.set_title("Total Cost per LHD")
            fig.tight_layout()
            fig.savefig(os.path.join(self.plot_dir, "LHD_total_costs.png"))
            plt.close(fig)

    def plot_material_extraction_by_point(self):
        """
        Genera un grafico de barras AZULES mostrando el material total extraido (Variable M)
        por cada punto de extraccion (nodo j) y compara con la demanda (m_j).
        """
        if self.df_M is None or self.df_M.empty:
            print("AVISO: No hay datos en M.json. Omitiendo 'Material Extraction by Point'.")
            return

        # Detectar la columna que representa el punto de extraccion (nodo).
        standard_cols = {"lhd", "day", "interval", "value", "station"}
        cols = list(self.df_M.columns)
        if 'node' in cols:
            node_col = 'node'
        elif 'j' in cols:
            node_col = 'j'
        else:
            node_candidates = [c for c in cols if c not in standard_cols]
            if node_candidates:
                node_col = node_candidates[0]
            else:
                if len(cols) > 1:
                    node_col = cols[1]
                else:
                    return

        mj = self.params.m_j or {}

        for y, d in itertools.product(self.years, self.days):
            # Filtrar datos del dia
            df_day = self._filter_yd(self.df_M, y, d)
            if df_day.empty:
                continue

            # Agrupar por punto de extraccion (nodo) y sumar toneladas
            extr = (df_day.groupby(node_col)["value"]
                    .sum()
                    .rename("extracted_tons")
                    .to_frame()
                    .sort_values("extracted_tons", ascending=False))

            if extr.empty:
                continue

            # --- GRAFICO PRINCIPAL: Top 30 nodos + todos los nodos en segundo grafico ---
            
            # 1. Grafico con todos los nodos (reducido para ser legible)
            fig, ax = plt.subplots(figsize=(16, 8))
            x = np.arange(len(extr.index))
            
            # Barras de material extraido (COLOR AZUL)
            bars = ax.bar(x, extr["extracted_tons"].values, 
                   color='#1f77b4', edgecolor="black", alpha=0.85, width=0.8, 
                   label="Material Extracted (M)")
            
            # Formato de ejes (solo mostrar etiquetas cada N nodos para evitar sobreposicion)
            step = max(1, len(extr) // 20)  # Mostrar ~20 etiquetas
            ax.set_xticks(x[::step])
            ax.set_xticklabels(extr.index[::step], rotation=45, ha='right', fontsize=9)
            
            ax.set_ylabel("Total Material [t]", fontsize=12)
            ax.set_xlabel("Extraction Point (j)", fontsize=12)
            
            month_label = self._rep_day_label(d)
            ax.set_title(f"Material Extraction - All Nodes - Y{y} {month_label}",
                        fontsize=14, pad=15)

            ax.grid(axis='y', linestyle=':', alpha=0.6)
            ax.legend(loc="upper right", fontsize=11, frameon=True).get_frame().set_edgecolor("#cccccc")

            fig.tight_layout()
            filename = f"Material_Extraction_M_AllNodes_Y{y}_{month_label.replace(' ', '_')}.png"
            save_path = os.path.join(self.plot_dir, filename)
            fig.savefig(save_path, dpi=120, bbox_inches='tight')
            plt.close(fig)
            
            # 2. Grafico con TOP 30 nodos (mas legible)
            top_n = 30
            extr_top = extr.head(top_n)
            
            fig, ax = plt.subplots(figsize=(12, 7))
            x_top = np.arange(len(extr_top.index))
            
            # Barras
            bars = ax.bar(x_top, extr_top["extracted_tons"].values, 
                   color='#1f77b4', edgecolor="black", alpha=0.85, width=0.65, 
                   label="Material Extracted (M)")
            
            # Demanda para top nodes
            # Etiquetas legibles
            ax.set_xticks(x_top)
            ax.set_xticklabels(extr_top.index, rotation=45, ha='right', fontsize=11)
            
            ax.set_ylabel("Total Material [t]", fontsize=13)
            ax.set_xlabel("Extraction Point (j)", fontsize=13)
            
            ax.set_title(f"Material Extraction - Top {top_n} Nodes - Y{y} {month_label}",
                        fontsize=15, pad=15)

            ax.grid(axis='y', linestyle=':', alpha=0.6)
            ax.legend(loc="upper right", fontsize=12, frameon=True).get_frame().set_edgecolor("#cccccc")

            fig.tight_layout()
            filename = f"Material_Extraction_M_Top{top_n}_Y{y}_{month_label.replace(' ', '_')}.png"
            save_path = os.path.join(self.plot_dir, filename)
            fig.savefig(save_path, dpi=150, bbox_inches='tight')
            plt.close(fig)

    def plot_gen_capacity_profile(self):
        """
        Perfil de capacidad disponible por generador renovable:
          P_cap[g, d, t] = G_g[g] * p_max_g[g] * alpha_g[g, d, t]

        Fuente: G_g.json + parameters.json (campos g_max_g, p_max_g, alpha_g).
        Se superpone P_gen[g, d, t] como línea discontinua para comparar
        capacidad vs despacho real.
        """
        params_data = _load_json(os.path.join(self.json_dir, "parameters.json"))
        gg_data     = _load_json(os.path.join(self.json_dir, "G_g.json"))

        if not params_data or not gg_data:
            print("INFO: Faltan G_g.json o parameters.json — omitiendo perfil de capacidad solar.")
            return

        # G_g: unidades instaladas por generador
        gg_raw = gg_data.get("_1", gg_data)
        gg: Dict[str, float] = {str(g): float(v) for g, v in gg_raw.items()} if isinstance(gg_raw, dict) else {}

        # p_max_g: potencia nominal por unidad [kW]
        p_max_raw = params_data.get("p_max_g", {})
        p_max_inner = p_max_raw.get("_1", p_max_raw) if isinstance(p_max_raw, dict) else {}
        p_max: Dict[str, float] = {str(g): float(v) for g, v in p_max_inner.items()} if isinstance(p_max_inner, dict) else {}

        # alpha_g nuevo: _1→gen→_2→year→_3→day→_4→interval:value
        # alpha_g legacy: _1→gen→_2→day→_3→interval:value
        alpha_root = params_data.get("alpha_g", {}).get("_1", {})

        gen_set = [str(g) for g in params_data.get("gen_set", list(gg.keys()))]
        active_gens = [g for g in gen_set if gg.get(g, 0.0) > 0 and p_max.get(g, 0.0) > 0]

        if not active_gens:
            print("INFO: No hay generadores instalados — omitiendo perfil de capacidad.")
            return

        gen_colors = {
            "Solar_PV": "#F4A836",
            "Wind":     "#4FC3F7",
            "Diesel":   "#A5D6A7",
        }
        fallback_colors = ["#CE93D8", "#80CBC4", "#EF9A9A", "#FFF176"]
        for idx, g in enumerate(active_gens):
            if g not in gen_colors:
                gen_colors[g] = fallback_colors[idx % len(fallback_colors)]

        dt = float(self.delta_t)
        x_rel = np.array([(t - 1) * dt for t in self.intervals])
        x_step = np.append(x_rel, x_rel[-1] + dt)

        def _step(arr: np.ndarray) -> np.ndarray:
            return np.append(arr, arr[-1])

        for y, d in itertools.product(self.years, self.days):
            fig, ax = plt.subplots(figsize=(14, 5))
            handles = []

            for g in active_gens:
                units = gg.get(g, 0.0)
                pmax  = p_max.get(g, 0.0)
                cap_total = units * pmax   # kW máximos instalados

                # Obtener alpha para (g, y, day) — soporta formato nuevo y legacy
                g_block = alpha_root.get(g, {})
                _2 = g_block.get("_2", {})
                # Formato nuevo: _2→year→_3→day→_4→interval
                _year_block = _2.get(str(y), {})
                _3_new = _year_block.get("_3", {})
                _day_block_new = _3_new.get(str(d), {})
                alpha_day_raw = _day_block_new.get("_4", {})
                if not alpha_day_raw:
                    # Formato legacy: _2→day→_3→interval
                    _day_block_old = _2.get(str(d), {})
                    alpha_day_raw = _day_block_old.get("_3", {})

                def _get_alpha(t):
                    v = alpha_day_raw.get(f"{float(t)}", alpha_day_raw.get(str(t), 0.0))
                    return float(v) if not isinstance(v, dict) else 0.0

                capacity = np.array([cap_total * _get_alpha(t) for t in self.intervals])

                color = gen_colors[g]
                # Área rellena = capacidad disponible
                ax.fill_between(x_step, 0, _step(capacity),
                                step="post", color=color, alpha=0.35, linewidth=0)
                ax.step(x_step, _step(capacity), where="post",
                        color=color, linewidth=2.0, label=f"{g} — cap. disponible")
                handles.append(mpatches.Patch(color=color, alpha=0.5, label=f"{g} cap. (G·Pmax·α)"))

                # Despacho real (P_gen) como línea discontinua
                if self.df_Pgen is not None and not self.df_Pgen.empty:
                    gen_day = (
                        self._filter_yd(self.df_Pgen, y, d).pipe(lambda df: df[df["gen"] == g])
                        .groupby("interval")["value"].mean()
                        .reindex(self.intervals)
                        .fillna(0.0)
                        .values
                    )
                    ax.step(x_step, _step(gen_day), where="post",
                            color=color, linewidth=1.5, linestyle="--", alpha=0.9)
                    handles.append(
                        plt.Line2D([0], [0], color=color, linewidth=1.5, linestyle="--",
                                   label=f"{g} despacho real (P_gen)")
                    )

            month = self._rep_day_label(d)
            ax.set_title(f"Perfil de capacidad solar disponible — Y{y} {month}", fontsize=14)
            ax.set_xlabel("Hora", fontsize=12)
            ax.set_ylabel("Potencia [kW]", fontsize=12)
            ax.set_xlim(0, 24)
            ax.set_ylim(bottom=0)
            ax.legend(handles=handles, loc="upper right", fontsize=10, framealpha=0.85)
            ax.grid(axis="y", linestyle="--", alpha=0.4)

            ticks, labels = self._get_hourly_time_ticks(start_hour=9)
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels, rotation=45, fontsize=9, ha="right")

            # Anotación con potencia pico instalada
            peak = max((gg.get(g, 0) * p_max.get(g, 0) for g in active_gens), default=0)
            ax.axhline(peak, color="gray", linewidth=0.8, linestyle=":", alpha=0.6)
            ax.text(0.5, peak, f"  Pico inst. {peak:,.0f} kW",
                    va="bottom", ha="left", fontsize=8, color="gray", transform=ax.get_yaxis_transform())

            fig.tight_layout()
            fname = f"GenCapacityProfile_Y{y}_{month.replace(' ', '_')}.png"
            fig.savefig(os.path.join(self.plot_dir, fname), dpi=150, bbox_inches="tight")
            plt.close(fig)

    def plot_power_dispatch(self):
        """
        Grafico de despacho de potencia apilado por dia.
        Bandas (de abajo hacia arriba):
          - Una por cada tecnologia de generacion (P_gen)
          - Encima: potencia comprada a la red (P_red)
        La suma de las bandas = demanda total de carga en cada intervalo.
        Si no existen P_red.json ni P_gen.json el metodo se omite silenciosamente.
        """
        if self.df_Pred is None or self.df_Pred.empty:
            print("INFO: No hay P_red.json — omitiendo grafico de despacho de potencia.")
            return

        gen_colors = {
            "Solar_PV": "#F4A836",
            "Wind":     "#4FC3F7",
            "Diesel":   "#A5D6A7",
        }
        fallback_colors = ["#CE93D8", "#80CBC4", "#EF9A9A", "#FFF176"]
        grid_color  = "#90A4AE"
        bess_color  = "#4CAF50"   # verde — descarga BESS
        bess_charge_color = "#C8E6C9"  # verde claro — carga BESS (demanda extra)

        dt = float(self.delta_t)

        gen_names: List[str] = []
        if self.df_Pgen is not None and not self.df_Pgen.empty:
            gen_names = sorted(self.df_Pgen["gen"].unique().tolist())

        storage_names: List[str] = []
        if self.df_Pbat is not None and not self.df_Pbat.empty:
            storage_names = sorted(self.df_Pbat["storage"].unique().tolist())

        for color_idx, g in enumerate(gen_names):
            if g not in gen_colors:
                gen_colors[g] = fallback_colors[color_idx % len(fallback_colors)]

        for y, d in itertools.product(self.years, self.days):
            p_red_day = (
                self._filter_yd(self.df_Pred, y, d)
                .groupby("interval")["value"].mean()
                .reindex(self.intervals)
                .fillna(0.0)
            )

            # Eje X en horas relativas (0 = 09:00), coherente con _get_hourly_time_ticks
            x_step = np.array([(t - 1) * dt for t in self.intervals])
            x_step = np.append(x_step, x_step[-1] + dt)  # escalon final

            def _step(series: np.ndarray) -> np.ndarray:
                return np.append(series, series[-1])

            fig, ax = plt.subplots(figsize=(14, 5))
            bottoms = np.zeros(len(self.intervals))
            handles = []

            # --- generacion renovable (bandas inferiores) ---
            for g in gen_names:
                if self.df_Pgen is not None and not self.df_Pgen.empty:
                    gen_day = (
                        self._filter_yd(self.df_Pgen, y, d).pipe(lambda df: df[df["gen"] == g])
                        .groupby("interval")["value"].mean()
                        .reindex(self.intervals)
                        .fillna(0.0)
                        .values
                    )
                else:
                    gen_day = np.zeros(len(self.intervals))

                color = gen_colors.get(g, "#CE93D8")
                ax.fill_between(x_step, _step(bottoms), _step(bottoms + gen_day),
                                step="post", alpha=0.88, color=color, linewidth=0)
                handles.append(mpatches.Patch(color=color, label=f"Gen. {g}"))
                bottoms = bottoms + gen_day

            # --- BESS: descarga (P_bat > 0) como banda entre gen y red ---
            bess_added = False
            for h in storage_names:
                p_bat_raw = (
                    self._filter_yd(self.df_Pbat, y, d).pipe(lambda df: df[df["storage"] == h])
                    .groupby("interval")["value"].mean()
                    .reindex(self.intervals)
                    .fillna(0.0)
                    .values
                )
                discharge = np.clip(p_bat_raw, 0, None)   # parte positiva = descarga
                charge    = np.clip(p_bat_raw, None, 0)   # parte negativa = carga

                if discharge.any():
                    ax.fill_between(x_step, _step(bottoms), _step(bottoms + discharge),
                                    step="post", alpha=0.88, color=bess_color, linewidth=0)
                    if not bess_added:
                        handles.append(mpatches.Patch(color=bess_color, label="BESS descarga"))
                    bottoms = bottoms + discharge
                    bess_added = True

                # Carga BESS: muestra como área rayada debajo de la demanda total
                if charge.any():
                    charge_abs = np.abs(charge)
                    ax.fill_between(x_step, _step(bottoms + p_red_day.values),
                                    _step(bottoms + p_red_day.values + charge_abs),
                                    step="post", alpha=0.5, color=bess_charge_color,
                                    linewidth=0, hatch="///")
                    handles.append(mpatches.Patch(facecolor=bess_charge_color, hatch="///",
                                                  edgecolor="gray", alpha=0.6,
                                                  label="BESS carga"))

            # --- red electrica (banda superior) ---
            ax.fill_between(x_step,
                            _step(bottoms),
                            _step(bottoms + p_red_day.values),
                            step="post", alpha=0.88, color=grid_color, linewidth=0)
            handles.append(mpatches.Patch(color=grid_color, label="Red eléctrica"))

            # --- decoracion ---
            month = self._rep_day_label(d)
            ax.set_title(f"Despacho de potencia — Y{y} {month}", fontsize=14)
            ax.set_xlabel("Hora", fontsize=12)
            ax.set_ylabel("Potencia [kW]", fontsize=12)
            ax.set_xlim(0, 24)
            ax.set_ylim(bottom=0)
            ax.legend(handles=handles, loc="upper right", fontsize=10, framealpha=0.8)
            ax.grid(axis="y", linestyle="--", alpha=0.4)

            ticks, labels = self._get_hourly_time_ticks(start_hour=9)
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels, rotation=45, fontsize=9, ha="right")

            fig.tight_layout()
            fname = f"PowerDispatch_Y{y}_{month.replace(' ', '_')}.png"
            fig.savefig(os.path.join(self.plot_dir, fname), dpi=150, bbox_inches="tight")
            plt.close(fig)

    def plot_bess_energy_state(self):
        """
        Gráfico del estado de energía (A_h) del BESS por día.
        Eje izquierdo: A_h [kWh] — Eje derecho: P_bat [kW] (descarga +, carga -).
        """
        if self.df_Ah is None or self.df_Ah.empty:
            print("INFO: No hay A_h.json — omitiendo gráfico de estado BESS.")
            return

        bess_color   = "#4CAF50"
        charge_color = "#1565C0"
        dt = float(self.delta_t)

        storage_names = sorted(self.df_Ah["storage"].unique().tolist())

        for h in storage_names:
            for y, d in itertools.product(self.years, self.days):
                a_day = (
                    self._filter_yd(self.df_Ah, y, d).pipe(lambda df: df[df["storage"] == h])
                    .groupby("interval")["value"].mean()
                    .reindex(self.intervals)
                    .fillna(0.0)
                )

                x_rel  = np.array([(t - 1) * dt for t in self.intervals])
                x_step = np.append(x_rel, x_rel[-1] + dt)

                fig, ax1 = plt.subplots(figsize=(14, 5))
                ax2 = ax1.twinx()
                ax2.grid(False)

                # Estado de energía (área rellena)
                soc_vals = np.append(a_day.values, a_day.values[-1])
                ax1.fill_between(x_step, 0, soc_vals, step="post",
                                 color=bess_color, alpha=0.35, linewidth=0)
                ax1.step(x_step, soc_vals, where="post",
                         color=bess_color, linewidth=2.0, label="Estado energía A_h")

                # Potencia BESS (si existe)
                if self.df_Pbat is not None and not self.df_Pbat.empty:
                    p_bat = (
                        self._filter_yd(self.df_Pbat, y, d).pipe(lambda df: df[df["storage"] == h])
                        .groupby("interval")["value"].mean()
                        .reindex(self.intervals)
                        .fillna(0.0)
                    )
                    p_step = np.append(p_bat.values, p_bat.values[-1])
                    ax2.step(x_step, p_step, where="post",
                             color=charge_color, linewidth=1.8, linestyle="--",
                             alpha=0.85, label="P_bat (+desc / -carga)")
                    ax2.axhline(0, color="gray", linewidth=0.6, linestyle=":")
                    ax2.set_ylabel("Potencia BESS [kW]", fontsize=11, color=charge_color)
                    ax2.tick_params(axis="y", labelcolor=charge_color, labelsize=10)

                month = self._rep_day_label(d)
                ax1.set_title(f"Estado de energía BESS — {h} — Y{y} {month}", fontsize=14)
                ax1.set_xlabel("Hora", fontsize=12)
                ax1.set_ylabel("Energía almacenada [kWh]", fontsize=12, color=bess_color)
                ax1.tick_params(axis="y", labelcolor=bess_color, labelsize=10)
                ax1.set_xlim(0, 24)
                ax1.set_ylim(bottom=0)
                ax1.grid(axis="y", linestyle="--", alpha=0.4)

                ticks, labels = self._get_hourly_time_ticks(start_hour=9)
                ax1.set_xticks(ticks)
                ax1.set_xticklabels(labels, rotation=45, fontsize=9, ha="right")

                # Leyenda combinada
                lines1, lbl1 = ax1.get_legend_handles_labels()
                lines2, lbl2 = ax2.get_legend_handles_labels() if self.df_Pbat is not None else ([], [])
                ax1.legend(lines1 + lines2, lbl1 + lbl2, loc="upper right",
                           fontsize=10, framealpha=0.85)

                fig.tight_layout()
                fname = f"BESS_EnergyState_{h}_Y{y}_{month.replace(' ', '_')}.png"
                fig.savefig(os.path.join(self.plot_dir, fname), dpi=150, bbox_inches="tight")
                plt.close(fig)

    def plot_battery_degradation(self):
        """
        Degradación de batería on-board (fleet-wide, un valor por año).
        Panel superior: capacidad al INICIO (b_bar, barras) y al FINAL (D,
        marcadores) de cada año, con líneas de referencia de b_max y del
        piso (min_capacity_fraction*b_max), y los años de reemplazo (R=1)
        resaltados. Panel inferior: ciclos equivalentes DEL año (N_ciclos) --
        el driver del fade b_bar->D vía d_y_fade (ver functions.py); ya no
        es un acumulado histórico (CumEFC), la formulación actual no lleva
        arrastre explícito, el arrastre entre años lo da D[y-1] en b_y_link.
        """
        if self.df_b_bar is None or self.df_b_bar.empty:
            print("INFO: No hay b_bar.json — omitiendo gráfico de degradación de batería.")
            return

        cap_color     = "#1565C0"
        end_color     = "#5E35B1"
        replace_color = "#F57C00"
        cycles_color  = "#00897B"
        ref_color     = "#616161"

        years = sorted(self.df_b_bar["year"].unique().tolist())
        b_bar_vals = self.df_b_bar.set_index("year")["b_bar"].reindex(years)

        replaced = set()
        if self.df_R is not None and not self.df_R.empty:
            replaced = set(self.df_R.loc[self.df_R["R"] > 0.5, "year"].tolist())

        b_max = self.params.b_max_fleet
        min_frac = self.params.min_capacity_fraction

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(max(6, 1.2 * len(years) + 2), 7), sharex=True)

        bar_colors = [replace_color if y in replaced else cap_color for y in years]
        ax1.bar([str(y) for y in years], b_bar_vals.values, color=bar_colors, width=0.6,
                 zorder=3, label="B (inicio de año)")

        if self.df_D is not None and not self.df_D.empty:
            d_vals = self.df_D.set_index("year")["D"].reindex(years)
            ax1.scatter([str(y) for y in years], d_vals.values, color=end_color, marker="D",
                        s=55, zorder=4, label="D (fin de año)")

        if b_max is not None:
            ax1.axhline(b_max, color=ref_color, linewidth=1.2, linestyle="--", label="b_max (nominal)")
        if b_max is not None and min_frac is not None:
            ax1.axhline(min_frac * b_max, color=ref_color, linewidth=1.2, linestyle=":",
                        label=f"Piso ({min_frac:.0%}·b_max)")

        for y in years:
            if y in replaced:
                ax1.text(str(y), b_bar_vals[y], "Reemplazo", ha="center", va="bottom",
                          fontsize=9, color=replace_color, fontweight="bold")

        ax1.set_ylabel("Capacidad batería [kWh]", fontsize=11)
        ax1.set_title("Degradación de batería on-board (fleet-wide)", fontsize=14)
        ax1.grid(axis="y", linestyle="--", alpha=0.4)
        ax1.legend(loc="lower left", fontsize=9, framealpha=0.85)

        if self.df_N_ciclos is not None and not self.df_N_ciclos.empty:
            n_vals = self.df_N_ciclos.set_index("year")["N_ciclos"].reindex(years)
            ax2.bar([str(y) for y in years], n_vals.values, color=cycles_color, width=0.6, zorder=3)
            ax2.set_ylabel("Ciclos equivalentes\ndel año (N_ciclos)", fontsize=11)
            ax2.grid(axis="y", linestyle="--", alpha=0.4)
        else:
            ax2.text(0.5, 0.5, "No hay N_ciclos.json", ha="center", va="center", transform=ax2.transAxes)
        ax2.set_xlabel("Año", fontsize=12)

        fig.tight_layout()
        fig.savefig(os.path.join(self.plot_dir, "BatteryDegradation.png"), dpi=150, bbox_inches="tight")
        plt.close(fig)

    def create_all_plots(self):
        self.plot_charge_power_vs_price()
        #self.plot_node_extraction_vs_demand()
        self.plot_lhd_costs_bars()
        self.plot_lhd_soc_vs_price_and_states()
        self.plot_emissions_profiles_for_optimized_day()
        self.plot_material_extraction_by_point()
        self.plot_power_dispatch()
        self.plot_gen_capacity_profile()
        self.plot_bess_energy_state()
        self.plot_battery_degradation()
        print(f"✔ Plots guardados en '{self.plot_dir}'.")


# -------------------- CLI --------------------
def main():
    ap = argparse.ArgumentParser(description="Graficador para salidas JSON del modelo")
    ap.add_argument("--json_dir", required=True, help="Carpeta con *.json (variables + parameters.json)")
    ap.add_argument("--energy_price_scale", type=float, default=DEFAULT_ENERGY_PRICE_SCALE,
                    help="Escala para precio marginal (opcional)")
    args = ap.parse_args()

    plotter = JSONPlotter(args.json_dir, energy_price_scale=args.energy_price_scale)
    plotter.create_all_plots()

if __name__ == "__main__":
    main()