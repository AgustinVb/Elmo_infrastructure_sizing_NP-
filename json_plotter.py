# json_plotter.py
# Autor: ELMO-UG (daniel)
# Ejecuta:  python json_plotter.py --json_dir /ruta/a/carpeta_con_json
# Salida:   /ruta/a/carpeta_con_json/plots/*.png

import os
import json
import argparse
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
            # Si axes["d"] es string no-numérico, es el lhd (d=lhd, t=day, i=interval)
            if isinstance(axes["d"], str):
                rows.append({
                    "lhd": str(axes["d"]),
                    "day": _numeric_or_str(axes["t"]),
                    "interval": _numeric_or_str(axes["i"]),
                    "value": float(value),
                })
            else:
                rows.append({
                    "lhd": axes["i"],
                    "day": _numeric_or_str(axes["d"]),
                    "interval": _numeric_or_str(axes["t"]),
                    "value": float(value),
                })

    if rows:
        return (pd.DataFrame(rows)
                .sort_values(["lhd", "day", "interval"])
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
            # Si axes["d"] es string no-numérico, es el lhd (d=lhd, t=node, i=day, j=interval)
            if isinstance(axes["d"], str):
                rows.append({
                    "lhd": str(axes["d"]),
                    "node": str(axes["t"]),
                    "day": _numeric_or_str(axes["i"]),
                    "interval": _numeric_or_str(axes["j"]),
                    "value": float(value),
                })
            else:
                rows.append({
                    "lhd": axes["i"],
                    "node": str(axes["j"]),
                    "day": _numeric_or_str(axes["d"]),
                    "interval": _numeric_or_str(axes["t"]),
                    "value": float(value),
                })

    if rows:
        return (pd.DataFrame(rows)
                .sort_values(["lhd", "node", "day", "interval"])
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
            row.update({
                "station": str(axes["k"]),
                "lhd": axes["i"],
                "day": _numeric_or_str(axes["d"]),
                "interval": _numeric_or_str(axes["t"]),
            })
        elif {"i", "j", "d", "t"}.issubset(axes):
            if isinstance(axes["d"], str):
                row.update({
                    "lhd": str(axes["d"]),
                    "node": str(axes["t"]),
                    "day": _numeric_or_str(axes["i"]),
                    "interval": _numeric_or_str(axes["j"]),
                })
            else:
                row.update({
                    "lhd": axes["i"],
                    "node": str(axes["j"]),
                    "day": _numeric_or_str(axes["d"]),
                    "interval": _numeric_or_str(axes["t"]),
                })
        elif {"i", "d", "t"}.issubset(axes):
            if isinstance(axes["d"], str):
                row.update({
                    "lhd": str(axes["d"]),
                    "day": _numeric_or_str(axes["t"]),
                    "interval": _numeric_or_str(axes["i"]),
                })
            else:
                row.update({
                    "lhd": axes["i"],
                    "day": _numeric_or_str(axes["d"]),
                    "interval": _numeric_or_str(axes["t"]),
                })
        else:
            row = {}

        if row:
            rows.append(row)

    if rows:
        df = pd.DataFrame(rows)
        cols = [c for c in ["lhd", "station", "node", "j", "day", "interval", "value"] if c in df.columns]
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
    cols = [c for c in ["lhd","station","node","j","day","interval","value"] if c in df.columns]
    return df[cols].sort_values([c for c in cols if c!="value"]).reset_index(drop=True)


def load_p_red_df(path: str) -> Optional[pd.DataFrame]:
    """Carga P_red.json → DataFrame con columnas [day, interval, value]."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"d", "t"}.issubset(axes):
            rows.append({
                "day":      _numeric_or_str(axes["d"]),
                "interval": _numeric_or_str(axes["t"]),
                "value":    float(val),
            })
    return pd.DataFrame(rows).sort_values(["day", "interval"]).reset_index(drop=True) if rows else None


def load_p_gen_df(path: str) -> Optional[pd.DataFrame]:
    """Carga P_gen.json → DataFrame con columnas [gen, day, interval, value]."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"g", "d", "t"}.issubset(axes):
            rows.append({
                "gen":      str(axes["g"]),
                "day":      _numeric_or_str(axes["d"]),
                "interval": _numeric_or_str(axes["t"]),
                "value":    float(val),
            })
    return pd.DataFrame(rows).sort_values(["gen", "day", "interval"]).reset_index(drop=True) if rows else None


# -------------------- Parámetros --------------------
class Parameters:
    def __init__(self, params_path: str, energy_price_scale: float = 1.0):
        self.path = params_path
        self.ok = False
        self.delta_t = 1.0
        self.energy_price_scale = float(energy_price_scale)

        self.m_j = None
        self.costo_marginal = None

        # Dos formas de emisiones:
        # a) por LHD (legacy): self.emissions  (cols: lhd, day, interval, emission_rate)
        # b) por tecnología (nuevo): self.emissions_electric / self.emissions_diesel (cols: day, interval, emission_rate)
        self.emissions = None
        self.emissions_electric = None
        self.emissions_diesel = None

        self.pe_i = None
        self.pd_i = None
        self.between_shifts = []
        self.meals = []
        self.maintenance = []
        # DET defaults
        self.shift_change = []
        self.forced_detention = []

        self._load()

    def _load(self):
        data = _load_json(self.path)
        if not data:
            print(f"⚠️ No se encontró parameters.json en {self.path}")
            return
        self.ok = True
        self.delta_t = float(data.get("delta_t", 1.0))

        # -------- m_j (demanda por nodo y día) --------
        m_j = {}
        for pex, blk in data.get("m_j", {}).get("_1", {}).items():
            if not isinstance(blk, dict):
                continue
            for day, val in blk.get("_2", {}).items():
                m_j.setdefault(pex, {})[_numeric_or_str(day)] = float(val)
        self.m_j = m_j if m_j else None

        # -------- costo_marginal por LHD → día → intervalo --------
        cm = []
        for lhd, blk1 in data.get("costo_marginal", {}).get("_1", {}).items():
            for day, t_blk in blk1.get("_2", {}).items():
                for interval, price in t_blk.get("_3", {}).items():
                    cm.append({
                        "lhd": lhd,
                        "day": _numeric_or_str(day),
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

        # -------- emisiones diésel (nuevo) --------
        em_d = []
        if "emissions_diesel" in data:
            for day, t_blk in data["emissions_diesel"].get("_1", {}).items():
                for interval, e in t_blk.get("_2", {}).items():
                    em_d.append({
                        "day": _numeric_or_str(day),
                        "interval": _numeric_or_str(interval),
                        "emission_rate": float(e),
                    })
        self.emissions_diesel = pd.DataFrame(em_d) if em_d else None

        # (opcional) guarda pe_i / pd_i si los usas en otros reportes
        self.pe_i = data.get("pe_i")
        self.pd_i = data.get("pd_i")

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

        # Parámetros
        self.params = Parameters(os.path.join(json_dir, "parameters.json"), energy_price_scale=energy_price_scale)
        self.delta_t = self.params.delta_t if self.params.ok else 1.0

        # Dominio
        self.days = self._detect_days()
        self.intervals = self._detect_intervals()
        self.special_intervals = self._load_special_intervals()

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

        start_hour = 9.0
        dt = float(self.delta_t)

        for d in self.days:
            p_day = (self.df_P.query("day == @d")[["interval", "value"]]
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
                peak_intervals = self._build_intervals_from_clock_windows(peak_windows, start_hour=9)
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
                handles = [line1, patch_peak_hatch, patch_between]
            else:
                handles = [line1, patch_between, patch_meal, patch_maint]
                if price_line is not None:
                    line_price = plt.Line2D([0], [0], color='red', linewidth=1.8, label='Energy Price')
                    handles.insert(1, line_price)

            month = self._rep_day_label(d)
            fig.suptitle(f"{month} – Total Charge Power vs Energy Price", fontsize=title_fs, y=1.06)

            fig.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, 0.99),
                       ncol=len(handles), fontsize=legend_fs, frameon=True)

            plt.tight_layout()
            fig.subplots_adjust(top=0.82)

            fig.savefig(
                os.path.join(self.plot_dir, f"ChargePower_vs_price_{month}.png"),
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
            for day in self.days:
                dfB_sel = (self.df_B.query("lhd == @lhd and day == @day")
                           if (self.df_B is not None and not self.df_B.empty) else pd.DataFrame())
                dfE_sel = (self.df_E.query("lhd == @lhd and day == @day")
                           if (self.df_E is not None and not self.df_E.empty) else pd.DataFrame())

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
                    series = (dfB_sel[["interval", "value"]]
                              .set_index("interval")["value"]
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
                    series = (dfE_sel[["interval", "value"]]
                              .set_index("interval")["value"]
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

                price_day = (self.params.costo_marginal
                             .query("lhd == @lhd and day == @day")[["interval", "price"]]
                             .set_index("interval")
                             .reindex(self.intervals)["price"]
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

                Y_filtered = (self.df_Y.query("lhd == @lhd and day == @day and value >= 0.5")
                              if (self.df_Y is not None and not self.df_Y.empty) else pd.DataFrame())
                P_filtered = (self.df_P.query("lhd == @lhd and day == @day")
                              if (self.df_P is not None and not self.df_P.empty) else pd.DataFrame())

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
                fig.suptitle(f"LHD {lhd} {month}", y=0.96, fontsize=18)

                fig.savefig(os.path.join(self.plot_dir, f"SoC_vs_price_LHD-{lhd}_day-{day}.png"), dpi=150, bbox_inches="tight")
                plt.close(fig)


    def plot_emissions_profiles_for_optimized_day(self):
        # Verificación de datos
        have_elec = (self.params.emissions_electric is not None and
                     not self.params.emissions_electric.empty)
        if not have_elec:
            print("ℹ️ No hay perfiles de emisiones eléctricas. Omitiendo 'Emission Profile'.")
            return


        for d in self.days:

            e_day = (self.params.emissions_electric
                    .query("day == @d")[["interval", "emission_rate"]]
                    .set_index("interval")
                    .reindex(self.intervals)["emission_rate"]
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
                        os.path.join(self.plot_dir, f"EmissionProfiles_{month}.png"),
                        bbox_inches="tight"
            )
            plt.close(fig)


    def plot_lhd_costs_bars(self):
        if self.params.costo_marginal is None or self.params.costo_marginal.empty:
            print("⚠️ No hay costo marginal. Omitiendo 'LHD_total_costs'.")
            return
        lhds = sorted(self.params.costo_marginal["lhd"].unique().tolist())
        costs = {l: 0.0 for l in lhds}

        if self.df_P is not None and not self.df_P.empty:
            for lhd in lhds:
                for d in self.days:
                    price = (self.params.costo_marginal
                             .query("lhd == @lhd and day == @d")[["interval","price"]].set_index("interval"))
                    p = (self.df_P.query("day == @d and lhd == @lhd")[["interval","value"]].set_index("interval"))
                    if not p.empty and not price.empty:
                        merged = p.join(price, how="inner")
                        costs[lhd] += float((merged["value"] * merged["price"] * self.delta_t).sum())
        elif self.df_E is not None and not self.df_E.empty:
            for lhd in lhds:
                for d in self.days:
                    price = (self.params.costo_marginal
                             .query("lhd == @lhd and day == @d")[["interval","price"]].set_index("interval"))
                    e = (self.df_E.query("day == @d and lhd == @lhd")[["interval","value"]].set_index("interval"))
                    if not e.empty and not price.empty:
                        merged = e.join(price, how="inner")
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

        for d in self.days:
            # Filtrar datos del dia
            df_day = self.df_M.query("day == @d")
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
            ax.set_title(f"Material Extraction - All Nodes - {month_label}", 
                        fontsize=14, pad=15)
            
            ax.grid(axis='y', linestyle=':', alpha=0.6)
            ax.legend(loc="upper right", fontsize=11, frameon=True).get_frame().set_edgecolor("#cccccc")
            
            fig.tight_layout()
            filename = f"Material_Extraction_M_AllNodes_{month_label.replace(' ', '_')}.png"
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
            
            ax.set_title(f"Material Extraction - Top {top_n} Nodes - {month_label}", 
                        fontsize=15, pad=15)
            
            ax.grid(axis='y', linestyle=':', alpha=0.6)
            ax.legend(loc="upper right", fontsize=12, frameon=True).get_frame().set_edgecolor("#cccccc")
            
            fig.tight_layout()
            filename = f"Material_Extraction_M_Top{top_n}_{month_label.replace(' ', '_')}.png"
            save_path = os.path.join(self.plot_dir, filename)
            fig.savefig(save_path, dpi=150, bbox_inches='tight')
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
        grid_color = "#90A4AE"

        dt = float(self.delta_t)
        start_hour = 9.0

        gen_names: List[str] = []
        if self.df_Pgen is not None and not self.df_Pgen.empty:
            gen_names = sorted(self.df_Pgen["gen"].unique().tolist())

        for color_idx, g in enumerate(gen_names):
            if g not in gen_colors:
                gen_colors[g] = fallback_colors[color_idx % len(fallback_colors)]

        for d in self.days:
            p_red_day = (
                self.df_Pred.query("day == @d")[["interval", "value"]]
                .set_index("interval")["value"]
                .reindex(self.intervals)
                .fillna(0.0)
            )

            # Eje X en horas absolutas (inicio del dia = 09:00)
            x_step = np.array([(t - 1) * dt + start_hour for t in self.intervals])
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
                        self.df_Pgen.query("day == @d and gen == @g")[["interval", "value"]]
                        .set_index("interval")["value"]
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

            # --- red electrica (banda superior) ---
            ax.fill_between(x_step,
                            _step(bottoms),
                            _step(bottoms + p_red_day.values),
                            step="post", alpha=0.88, color=grid_color, linewidth=0)
            handles.append(mpatches.Patch(color=grid_color, label="Red electrica"))

            # --- decoracion ---
            month = self._rep_day_label(d)
            ax.set_title(f"Despacho de potencia — {month}", fontsize=14)
            ax.set_xlabel("Hora", fontsize=12)
            ax.set_ylabel("Potencia [kW]", fontsize=12)
            ax.set_xlim(x_step[0], x_step[-1])
            ax.set_ylim(bottom=0)
            ax.legend(handles=handles, loc="upper right", fontsize=10, framealpha=0.8)
            ax.grid(axis="y", linestyle="--", alpha=0.4)

            ticks, labels = self._get_hourly_time_ticks(start_hour=int(start_hour))
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels, rotation=45, fontsize=9, ha="right")

            fig.tight_layout()
            fname = f"PowerDispatch_{month.replace(' ', '_')}.png"
            fig.savefig(os.path.join(self.plot_dir, fname), dpi=150, bbox_inches="tight")
            plt.close(fig)

    def create_all_plots(self):
        self.plot_charge_power_vs_price()
        #self.plot_node_extraction_vs_demand()
        self.plot_lhd_costs_bars()
        self.plot_lhd_soc_vs_price_and_states()
        self.plot_emissions_profiles_for_optimized_day()
        self.plot_material_extraction_by_point()
        self.plot_power_dispatch()
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