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
from matplotlib.ticker import MultipleLocator
import matplotlib.patheffects as path_effects
import matplotlib.patches as mpatches
from matplotlib.ticker import FixedLocator, FuncFormatter

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
    """Recorre un JSON anidado con claves de eje tipo {'d': {'1': {'t': {'1.0': val}}}}
    y devuelve lista de (tokens_tuple, value) donde tokens_tuple es ((axis_name, axis_value), ...)."""
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
    """Load B (battery SOC) from JSON with structure: i -> d -> t -> value"""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    # New structure: i (LHD) -> d (day) -> t (interval)
    i_dict = data.get("i", {})
    for lhd, d_block in i_dict.items():
        d_inner = d_block.get("d", {})
        for day, t_block in d_inner.items():
            t_inner = t_block.get("t", {})
            for interval, value in t_inner.items():
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
    """Load Y (trip binary). Soporta i->j->d->t y d->lhd->t->node->i->day->j->interval."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    cols = ["lhd", "node", "day", "interval", "value"]

    # Parche: estructura d->lhd->t->node->i->day->j->interval
    if "d" in data and "i" not in data:
        for lhd, t_block in data["d"].items():
            for node, i_block in t_block.get("t", {}).items():
                for day, j_block in i_block.get("i", {}).items():
                    for interval, val in j_block.get("j", {}).items():
                        try:
                            rows.append({"lhd": str(lhd), "node": str(node),
                                         "day": _numeric_or_str(day),
                                         "interval": _numeric_or_str(interval),
                                         "value": float(val)})
                        except Exception:
                            continue
        return (pd.DataFrame(rows).sort_values(["lhd","node","day","interval"])
                .reset_index(drop=True)) if rows else pd.DataFrame(columns=cols)

    # New structure: i (LHD) -> j (node) -> d (day) -> t (interval)
    i_dict = data.get("i", {})
    for lhd, j_block in i_dict.items():
        j_inner = j_block.get("j", {})
        for node, d_block in j_inner.items():
            d_inner = d_block.get("d", {})
            for day, t_block in d_inner.items():
                t_inner = t_block.get("t", {})
                for interval, val in t_inner.items():
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


def load_Bs_df(path: str) -> Optional[pd.DataFrame]:
    """Load B_s (battery SOC smooth) from JSON with structure: i -> d -> t -> value"""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    # Structure: i (LHD) -> d (day) -> t (interval)
    i_dict = data.get("i", {})
    for lhd, d_block in i_dict.items():
        d_inner = d_block.get("d", {})
        for day, t_block in d_inner.items():
            t_inner = t_block.get("t", {})
            for interval, value in t_inner.items():
                rows.append({
                    "lhd": lhd,
                    "day": _numeric_or_str(day),
                    "interval": _numeric_or_str(interval),
                    "value": float(value),
                })
    return (pd.DataFrame(rows)
            .sort_values(["lhd", "day", "interval"])
            .reset_index(drop=True)) if rows else pd.DataFrame(columns=["lhd", "day", "interval", "value"])


def load_Z_swap_df(path: str) -> Optional[pd.DataFrame]:
    """Load Z_swap (swap binary) from JSON with structure: k -> i -> d -> t -> value"""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    cols = ["station", "lhd", "day", "interval", "value"]

    # Parche: estructura _1->station->_2->lhd->_3->day->_4->interval
    if "_1" in data:
        for station, v2 in data["_1"].items():
            for lhd, v3 in v2.get("_2", {}).items():
                for day, v4 in v3.get("_3", {}).items():
                    for interval, val in v4.get("_4", {}).items():
                        try:
                            rows.append({"station": str(station), "lhd": str(lhd),
                                         "day": _numeric_or_str(day),
                                         "interval": _numeric_or_str(interval),
                                         "value": float(val)})
                        except Exception:
                            continue
        return (pd.DataFrame(rows).sort_values(["station","lhd","day","interval"])
                .reset_index(drop=True)) if rows else pd.DataFrame(columns=cols)

    k_dict = data.get("k", {})
    for station, i_block in k_dict.items():
        i_inner = i_block.get("i", {})
        for lhd, d_block in i_inner.items():
            d_inner = d_block.get("d", {})
            for day, t_block in d_inner.items():
                t_inner = t_block.get("t", {})
                for interval, value in t_inner.items():
                    rows.append({
                        "station": station,
                        "lhd": lhd,
                        "day": _numeric_or_str(day),
                        "interval": _numeric_or_str(interval),
                        "value": float(value),
                    })
    return (pd.DataFrame(rows)
            .sort_values(["station", "lhd", "day", "interval"])
            .reset_index(drop=True)) if rows else pd.DataFrame(columns=["station", "lhd", "day", "interval", "value"])


def load_Sv_df(path: str) -> Optional[pd.DataFrame]:
    """
    Carga Sv.json con estructura esperada:
      k (station) -> d (day) -> t (time interval) -> t_start (charging start interval) -> valor
    """
    data = _load_json(path)
    if not data:
        return None

    rows = []
    cols = ["station", "day", "interval", "start_interval", "value"]

    # Parche: estructura _1->station->_2->day->_3->interval->_4->start_interval
    if "_1" in data:
        for station, v2 in data["_1"].items():
            for day, v4 in v2.get("_2", {}).items():
                for interval, v6 in v4.get("_3", {}).items():
                    for start_interval, val in v6.get("_4", {}).items():
                        try:
                            rows.append({"station": str(station), "day": _numeric_or_str(day),
                                         "interval": _numeric_or_str(interval),
                                         "start_interval": _numeric_or_str(start_interval),
                                         "value": float(val)})
                        except Exception:
                            continue
        return (pd.DataFrame(rows).sort_values(["station","day","interval","start_interval"])
                .reset_index(drop=True)) if rows else pd.DataFrame(columns=cols)

    k_dict = data.get("k", {})
    if not isinstance(k_dict, dict):
        return pd.DataFrame(columns=cols)

    for station, d_block in k_dict.items():
        d_inner = d_block.get("d", {}) if isinstance(d_block, dict) else {}
        if not isinstance(d_inner, dict):
            continue
        for day, t_block in d_inner.items():
            t_inner = t_block.get("t", {}) if isinstance(t_block, dict) else {}
            if not isinstance(t_inner, dict):
                continue
            for t, t_start_block in t_inner.items():
                t_start_inner = t_start_block.get("t_start", {}) if isinstance(t_start_block, dict) else {}
                if not isinstance(t_start_inner, dict):
                    continue
                for t_start, val in t_start_inner.items():
                    try:
                        rows.append({
                            "station": str(station),
                            "day": _numeric_or_str(day),
                            "interval": _numeric_or_str(t),
                            "start_interval": _numeric_or_str(t_start),
                            "value": float(val),
                        })
                    except Exception:
                        continue

    return (pd.DataFrame(rows)
            .sort_values(["station", "day", "interval", "start_interval"])
            .reset_index(drop=True)) if rows else pd.DataFrame(columns=["station", "day", "interval", "start_interval", "value"])


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
    # Caso especial: M.json tipo _1 -> LHD -> _2 -> CXXXX -> _3 -> day
    # -------------------------------------------------------------------------
    if any(str(k).startswith("_") for k in data.keys()):
        for k1, v1 in data.items():            # _1
            for lhd, v2 in v1.items():         # LH518B_1
                for k2, v3 in v2.items():     # _2
                    for j, v4 in v3.items():  # C0044, C0045... nodo j
                        for k3, val_dict in v4.items():  # _3
                            for day, val in val_dict.items():
                                rows.append({
                                    "lhd": lhd,
                                    "j": j,
                                    "day": int(day),
                                    "value": float(val)
                                })

    # -------------------------------------------------------------------------
    # Caso especial M.json moderno: b -> LHD -> j -> nodes -> d -> day : value
    # -------------------------------------------------------------------------
    elif "b" in data and not any(str(k).startswith("_") for k in data.keys()):
        b_data = data.get("b", {})
        if isinstance(b_data, dict):
            for lhd, lhd_block in b_data.items():  # LH518B_1, LH518B_2, ...
                if isinstance(lhd_block, dict) and "j" in lhd_block:
                    for node, node_block in lhd_block["j"].items():  # node like "06_13F"
                        if isinstance(node_block, dict) and "d" in node_block:
                            for day, val in node_block["d"].items():  # day : value
                                rows.append({
                                    "lhd": lhd,
                                    "j": node,
                                    "day": int(day),
                                    "value": float(val)
                                })

    # -------------------------------------------------------------------------
    # Caso 1: Estructura con eje 'k' (P.json, etc.)
    # -------------------------------------------------------------------------
    elif "k" in data:
        for station, k_block in data["k"].items():
            if not isinstance(k_block, dict) or "d" not in k_block:
                continue
            d_data = k_block["d"]
            for lhd, t_block in d_data.items():
                if not isinstance(t_block, dict):
                    continue
                if "t" in t_block and isinstance(t_block["t"], dict):
                    for k1, v1 in t_block["t"].items():
                        if isinstance(v1, dict) and "i" in v1:
                            for interval, val in v1["i"].items():
                                rows.append({
                                    "lhd": lhd,
                                    "station": station,
                                    "day": _numeric_or_str(k1),
                                    "interval": _numeric_or_str(interval),
                                    "value": float(val),
                                })

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
    """Carga P_red.json → DataFrame [day, interval, value]."""
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
    """Carga P_gen.json → DataFrame [gen, day, interval, value]."""
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


def load_p_bat_df(path: str) -> Optional[pd.DataFrame]:
    """Carga P_bat.json → DataFrame [storage, day, interval, value].
    Positivo = descarga (inyecta a la red), negativo = carga (absorbe de la red).
    """
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"h", "d", "t"}.issubset(axes):
            rows.append({
                "storage":  str(axes["h"]),
                "day":      _numeric_or_str(axes["d"]),
                "interval": _numeric_or_str(axes["t"]),
                "value":    float(val),
            })
    return pd.DataFrame(rows).sort_values(["storage", "day", "interval"]).reset_index(drop=True) if rows else None


def load_a_h_df(path: str) -> Optional[pd.DataFrame]:
    """Carga A_h.json → DataFrame [storage, day, interval, value] en kWh."""
    data = _load_json(path)
    if not data:
        return None
    rows = []
    for tokens, val in _collect_named_leaf_records(data):
        axes = {n: v for n, v in tokens}
        if {"h", "d", "t"}.issubset(axes):
            rows.append({
                "storage":  str(axes["h"]),
                "day":      _numeric_or_str(axes["d"]),
                "interval": _numeric_or_str(axes["t"]),
                "value":    float(val),
            })
    return pd.DataFrame(rows).sort_values(["storage", "day", "interval"]).reset_index(drop=True) if rows else None


# -------------------- Parámetros --------------------
class Parameters:
    def __init__(self, params_path: str, energy_price_scale: float = 1.0):
        self.path = params_path
        self.ok = False
        self.delta_t = 1.0
        self.energy_price_scale = float(energy_price_scale)

        self.m_j = None
        self.costo_electricidad = None
        # Alias legacy: algunas funciones todavía referencian costo_marginal.
        self.costo_marginal = None

        # Dos formas de emisiones:
        # a) por LHD (legacy): self.emissions  (cols: lhd, day, interval, emission_rate)
        # b) por tecnología (nuevo): self.emissions_electric / self.emissions_diesel (cols: day, interval, emission_rate)
        self.emissions = None
        self.emissions_electric = None
        self.emissions_diesel = None

        self.pe_i = None
        self.pd_i = None
        self.between_shifts: List[int] = []
        self.meals: List[int] = []
        self.maintenance: List[int] = []
        self.meal_det: List[int] = []
        self.maintenance_det: List[int] = []
        self.road_clearing: List[int] = []

        self._load()

    def _load(self):
        data = _load_json(self.path)
        if not data:
            print(f"⚠️ No se encontró parameters.json en {self.path}")
            return
        self.ok = True
        self.delta_t = float(data.get("delta_t", 1.0))
        self.p_charger = float(data.get("p_charger", 150.0))

        # -------- m_j (demanda por nodo y día) --------
        m_j = {}
        for pex, blk in data.get("m_j", {}).get("_1", {}).items():
            if not isinstance(blk, dict):
                continue
            for day, val in blk.get("_2", {}).items():
                m_j.setdefault(pex, {})[_numeric_or_str(day)] = float(val)
        self.m_j = m_j if m_j else None

        # -------- costo_electricidad (nuevo): día → intervalo --------
        ce = []
        for day, t_blk in data.get("costo_electricidad", {}).get("_1", {}).items():
            for interval, price in t_blk.get("_2", {}).items():
                ce.append({
                    "day": _numeric_or_str(day),
                    "interval": _numeric_or_str(interval),
                    "price": float(price) * self.energy_price_scale,
                })

        # -------- costo_marginal (legacy): LHD → día → intervalo --------
        cm_legacy = []
        for lhd, blk1 in data.get("costo_marginal", {}).get("_1", {}).items():
            for day, t_blk in blk1.get("_2", {}).items():
                for interval, price in t_blk.get("_3", {}).items():
                    cm_legacy.append({
                        "lhd": lhd,
                        "day": _numeric_or_str(day),
                        "interval": _numeric_or_str(interval),
                        "price": float(price) * self.energy_price_scale,
                    })

        price_df = pd.DataFrame(ce) if ce else (pd.DataFrame(cm_legacy) if cm_legacy else None)
        self.costo_electricidad = price_df
        # Mantener alias para código legado.
        self.costo_marginal = price_df

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

        def _as_interval_list(raw: Any) -> List[int]:
            vals: List[int] = []
            if raw is None:
                return vals

            if isinstance(raw, list):
                iterable = raw
            elif isinstance(raw, dict):
                if "_1" in raw and isinstance(raw.get("_1"), dict):
                    iterable = list(raw.get("_1", {}).keys())
                else:
                    iterable = list(raw.keys())
            else:
                return vals

            for x in iterable:
                try:
                    vals.append(int(float(x)))
                except Exception:
                    continue
            return sorted(set(vals))

        self.between_shifts = _as_interval_list(data.get("time_intervals_between_shifts_set", []))
        self.meals = _as_interval_list(
            data.get("time_intervals_mid_shift_meal_set", data.get("time_intervals_meal_set", []))
        )
        self.maintenance = _as_interval_list(
            data.get("time_intevals_maintenance_set", data.get("time_intervals_maintenance_set", []))
        )

        self.meal_det = _as_interval_list(data.get("time_intervals_meal_det_set", []))
        self.maintenance_det = _as_interval_list(data.get("time_intervals_maintenance_det_set", []))
        self.road_clearing = _as_interval_list(data.get("time_intervals_road_clearing_det_set", []))

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
    # Paleta de sombreado para intervalos inactivos en modo DET: (color, alpha)
    DET_SHADE_COLORS = {
        "meal": ("red", 0.15),
        "road_clearing": ("gold", 0.20),
        "maintenance": ("gray", 0.20),
    }
    def __init__(self, json_dir: str, energy_price_scale: float = DEFAULT_ENERGY_PRICE_SCALE, mode: str = "DCH"):
        self.json_dir = json_dir
        self.mode = str(mode).upper() if mode else "DCH"

        # Guardar SIEMPRE en <json_dir>/plots
        self.plot_dir = os.path.join(self.json_dir, "plots")
        os.makedirs(self.plot_dir, exist_ok=True)

        # Variables
        self.df_B = load_B_df(os.path.join(json_dir, "B.json"))
        self.df_Bs = load_Bs_df(os.path.join(json_dir, "B_s.json"))
        self.df_Y = load_binary_Y_df(os.path.join(json_dir, "Y.json"))
        self.df_Z_swap = load_Z_swap_df(os.path.join(json_dir, "Z_swap.json"))
        self.df_Sv = load_Sv_df(os.path.join(json_dir, "Sv.json"))
        self.df_P = load_generic_variable_df(os.path.join(json_dir, "P.json"), "P")
        self.df_C = load_generic_variable_df(os.path.join(json_dir, "C.json"), "C")
        self.df_E = load_generic_variable_df(os.path.join(json_dir, "E.json"), "E")
        self.df_M    = load_generic_variable_df(os.path.join(json_dir, "M.json"), "M")
        self.df_Pred = load_p_red_df(os.path.join(json_dir, "P_red.json"))
        self.df_Pgen = load_p_gen_df(os.path.join(json_dir, "P_gen.json"))
        self.df_Pbat = load_p_bat_df(os.path.join(json_dir, "P_bat.json"))
        self.df_Ah   = load_a_h_df(os.path.join(json_dir, "A_h.json"))

        # Parámetros
        self.params = Parameters(os.path.join(json_dir, "parameters.json"), energy_price_scale=energy_price_scale)
        self.delta_t = self.params.delta_t if self.params.ok else 1.0

        # Dominio
        self.days = self._detect_days()
        self.intervals = self._detect_intervals()

    def _group_consecutive(self, values: List[int]) -> List[Tuple[int, int]]:
        cleaned = sorted(set(int(v) for v in values if v is not None))
        if not cleaned:
            return []
        groups = []
        start = cleaned[0]
        prev = cleaned[0]
        for current in cleaned[1:]:
            if current == prev + 1:
                prev = current
                continue
            groups.append((start, prev))
            start = current
            prev = current
        groups.append((start, prev))
        return groups

    def _special_mode_intervals(self, day: int) -> Dict[str, List[int]]:
        if self.mode == "DET":
            return {
                "meal": self.params.meal_det,
                "road_clearing": self.params.road_clearing,
                "maintenance": self.params.maintenance_det,
            }

        return {
            "between_shifts": self.params.between_shifts,
            "meal": self.params.meals,
            "maintenance": self.params.maintenance,
        }

    def _mode_plot_title(self) -> str:
        if self.mode == "DET":
            return "DET"
        return "DCH"

    def _detect_days(self) -> List[int]:
        sources = []
        for df in [self.df_B, self.df_Bs, self.df_Y, self.df_Z_swap, self.df_Sv, self.df_P, self.df_C, self.df_E]:
            if df is not None and "day" in df.columns and not df.empty:
                sources.append(sorted(df["day"].dropna().unique().tolist()))
        if self.params.costo_marginal is not None:
            sources.append(sorted(self.params.costo_marginal["day"].unique().tolist()))
        return sorted({d for lst in sources for d in lst}) if sources else []

    def _detect_intervals(self) -> List[int]:
        sources = []
        for df in [self.df_B, self.df_Bs, self.df_Y, self.df_Z_swap, self.df_Sv, self.df_P, self.df_C, self.df_E]:
            if df is not None and "interval" in df.columns and not df.empty:
                sources.append(sorted(df["interval"].dropna().unique().tolist()))
        if self.params.costo_marginal is not None:
            sources.append(sorted(self.params.costo_marginal["interval"].unique().tolist()))
        if not sources:
            return []
        ints = sorted({t for lst in sources for t in lst})
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

        # d es el numero absoluto de dia del año (1=1 enero, 32=1 feb, etc.)
        # Para escenarios multi-año: 366..730 = año 2, etc.
        day_of_year = ((di - 1) % 365) + 1

        month_lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        cum = 0
        for m, ml in enumerate(month_lengths, start=1):
            start = cum + 1
            end = cum + ml
            if start <= day_of_year <= end:
                day_of_month = day_of_year - cum
                month_name = self.MONTH_LABELS.get(m, f"Month{m}")
                return f"{day_of_month} {month_name}"
            cum += ml
        return f"Day {d}"

    def _season_label(self, d: int) -> str:
        """Devuelve estación del año (hemisferio sur) para el día dado."""
        try:
            di = int(d)
        except Exception:
            return ""

        if self.days and max(self.days) <= 12:
            month = di
        else:
            day_of_year = ((di - 1) % 365) + 1
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

    def _price_series(self, day: int, lhd: Optional[str] = None) -> pd.Series:
        """Retorna serie de precio por intervalo para un día.

        Soporta dos formatos en parameters.json:
        - Nuevo: costo_electricidad con columnas [day, interval, price]
        - Legacy: costo_marginal con columnas [lhd, day, interval, price]
        """
        if self.params.costo_marginal is None or self.params.costo_marginal.empty:
            return pd.Series(index=self.intervals, dtype=float)

        df_price = self.params.costo_marginal

        if lhd is not None and "lhd" in df_price.columns:
            filtered = df_price.query("lhd == @lhd and day == @day")
        else:
            filtered = df_price.query("day == @day")

        if filtered.empty:
            return pd.Series(index=self.intervals, dtype=float)

        return (
            filtered.groupby("interval")["price"]
            .mean()
            .reindex(self.intervals)
            .ffill()
            .bfill()
            .fillna(0.0)
        )

    def plot_battery_charging_power(self, charger_power: Optional[float] = None):
        """
        Grafica la potencia de carga calculada como:
        potencia_carga = Sv (cantidad de baterías cargando) × charger_power
        
        charger_power: potencia en kW por batería cargando (default 150 kW)
        """
        if self.df_Sv is None or self.df_Sv.empty:
            print("⚠️ No hay Sv.json. Omitiendo 'Battery Charging Power'.")
            return

        title_fs = 22
        axis_label_fs = 16
        tick_fs = 14
        legend_fs = 14

        start_hour = 9.0
        dt = float(self.delta_t)

        for d in self.days:
            # Obtener intervalos especiales para este día
            special_intervals = self._special_mode_intervals(d)

            # Extraer número de baterías cargando por intervalo tal como en
            # `plot_charging_batteries_vs_price`: agrupar por 'interval' y sumar 'value'.
            sv_day = (
                self.df_Sv.query("day == @d")[["interval", "value"]]
                .groupby("interval")["value"]
                .sum()
                .reindex(self.intervals)
                .fillna(0.0)
            )

            if sv_day.empty:
                continue

            # Determinar potencia por cargador: usar argumento si se pasa, sino leer de parameters.json
            if charger_power is None:
                charger_power = getattr(self.params, "p_charger", 150.0)

            # Calcular potencia: número de baterías cargando × potencia por cargador
            pcharge = sv_day.to_numpy(dtype=float) * float(charger_power)

            times = np.array([(t - 1) * dt for t in self.intervals])

            times_step = np.append(times, times[-1] + dt) + start_hour
            pcharge_step = np.append(pcharge, pcharge[-1])

            fig, ax1 = plt.subplots(figsize=(9, 5.6))

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

            if self.mode == "DET":
                meal_intervals_det = (
                    self.params.meal_det
                    if getattr(self.params, 'meal_det', None)
                    else special_intervals.get("meal", [])
                )
                road_clearing_intervals = (
                    self.params.road_clearing
                    if getattr(self.params, 'road_clearing', None)
                    else special_intervals.get("road_clearing", [])
                )
                maintenance_intervals_det = (
                    self.params.maintenance_det
                    if getattr(self.params, 'maintenance_det', None)
                    else special_intervals.get("maintenance", [])
                )
                shade_specs = [
                    (meal_intervals_det, *self.DET_SHADE_COLORS["meal"]),
                    (road_clearing_intervals, *self.DET_SHADE_COLORS["road_clearing"]),
                    (maintenance_intervals_det, *self.DET_SHADE_COLORS["maintenance"]),
                ]
                # Construir intervalos peak: 18:00-22:00 con start_hour=9
                # Convertir horas a intervalos: intervalo = ceil((hora - start_hour) / dt)
                peak_start_hour = 18.0
                peak_end_hour = 22.0
                peak_start_interval = int(np.ceil((peak_start_hour - start_hour) / dt))
                peak_end_interval = int(np.ceil((peak_end_hour - start_hour) / dt))
                peak_intervals = list(range(peak_start_interval, peak_end_interval + 1))
            else:
                between_shifts_intervals = (
                    self.params.between_shifts
                    if getattr(self.params, 'between_shifts', None)
                    else special_intervals.get("between_shifts", [])
                )
                meal_intervals = (
                    self.params.meals
                    if getattr(self.params, 'meals', None)
                    else special_intervals.get("meal", [])
                )
                maintenance_intervals = (
                    self.params.maintenance
                    if getattr(self.params, 'maintenance', None)
                    else special_intervals.get("maintenance", [])
                )

                shade_specs = [
                    (between_shifts_intervals, 'gray', 0.6),
                    (meal_intervals, 'gray', 0.15),
                    (maintenance_intervals, '#FF9999', 0.15),
                ]
            for intervals_list, color, alpha in shade_specs:
                for gs, ge in _group_consecutive(intervals_list):
                    x0 = start_hour + (gs - 1) * dt
                    x1 = start_hour + ge * dt
                    ax1.axvspan(x0, x1, color=color, alpha=alpha, linewidth=0)

            ax1.step(times_step, pcharge_step, where='post', label='Battery Charging Power', color='blue', linewidth=1.5)

            ax1.set_ylabel('Charging Power [kW]', color='black', fontsize=axis_label_fs)
            ax1.set_xlabel('Hour', fontsize=axis_label_fs)
            ax1.tick_params(axis='y', labelcolor='black', labelsize=tick_fs)
            ax1.set_ylim(0, 2500)
            ax1.set_xlim(times_step[0], times_step[-1])
            ax1.grid(False)

            ax2 = ax1.twinx()
            ax2.grid(False)
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
            ax2.set_axisbelow(False)

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

            patch_peak_hatch = mpatches.Patch(facecolor='none', edgecolor='#d62728', hatch='///', label='Peak hours')
            line1 = plt.Line2D([0], [0], color='blue', linewidth=1.5, label='Battery Charging Power')

            if self.mode == "DET":
                meal_color, meal_alpha = self.DET_SHADE_COLORS["meal"]
                road_color, road_alpha = self.DET_SHADE_COLORS["road_clearing"]
                maint_color, maint_alpha = self.DET_SHADE_COLORS["maintenance"]
                patch_meal_det = mpatches.Patch(color=meal_color, alpha=meal_alpha, label='Meal')
                patch_road = mpatches.Patch(color=road_color, alpha=road_alpha, label='Road Clearing')
                patch_maint_det = mpatches.Patch(color=maint_color, alpha=maint_alpha, label='Maintenance')
                handles = [line1, patch_peak_hatch, patch_meal_det, patch_road, patch_maint_det]
            else:
                patch_between = mpatches.Patch(color='gray', alpha=0.6, label='Between Shifts')
                patch_meal = mpatches.Patch(color='gray', alpha=0.15, label='Meal')
                patch_maint = mpatches.Patch(color='#FF9999', alpha=0.15, label='Maintenance')
                handles = [line1, patch_between, patch_meal, patch_maint]
                if price_line is not None:
                    handles.insert(1, plt.Line2D([0], [0], color='red', linewidth=1.8, label='Energy Price'))

            month = self._rep_day_label(d)
            fig.suptitle(f"{month} – Total Charge Power vs Energy Price", fontsize=title_fs, y=1.06)

            fig.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, 0.99),
                       ncol=3, fontsize=legend_fs, frameon=True)

            plt.tight_layout()
            fig.subplots_adjust(top=0.82)

            fig.savefig(
                os.path.join(self.plot_dir, f"BatteryChargingPower_{month}.png"),
                bbox_inches="tight", dpi=120
            )
            plt.close(fig)

    def plot_swaps_vs_price(self):
        if self.df_Z_swap is None or self.df_Z_swap.empty:
            print("⚠️ No hay Z_swap.json. Omitiendo 'Swaps_vs_price'.")
            return
        if self.params.costo_marginal is None or self.params.costo_marginal.empty:
            print("⚠️ No hay costo_electricidad en parameters.json. Omitiendo 'Swaps_vs_price'.")
            return

        title_fs = 22
        axis_label_fs = 16
        tick_fs = 14
        legend_fs = 14
        start_hour = 9.0
        dt = float(self.delta_t)

        for d in self.days:
            swap_day = (
                self.df_Z_swap.query("day == @d and value >= 0.5")[['interval', 'value']]
                .groupby('interval')['value']
                .sum()
                .reindex(self.intervals)
                .fillna(0.0)
            )
            if swap_day.empty:
                continue

            price_day = (
                self.params.costo_marginal.query("day == @d")[["interval", "price"]]
                .groupby("interval")["price"]
                .mean()
                .reindex(self.intervals)
                .ffill().bfill().fillna(0.0)
            )

            times = np.array([(t - 1) * dt for t in self.intervals], dtype=float)
            swap_counts = swap_day.values
            marginal = price_day.values

            times_step = np.append(times, times[-1] + dt) + start_hour
            swap_step = np.append(swap_counts, swap_counts[-1])
            marginal_step = np.append(marginal, marginal[-1])

            fig, ax1 = plt.subplots(figsize=(9, 5.6))

            interval_groups = self._special_mode_intervals(d)
            if self.mode == "DET":
                meal_color, meal_alpha = self.DET_SHADE_COLORS["meal"]
                road_color, road_alpha = self.DET_SHADE_COLORS["road_clearing"]
                maint_color, maint_alpha = self.DET_SHADE_COLORS["maintenance"]
                shade_specs = [
                    (interval_groups.get("meal", []), meal_color, meal_alpha, "Meal"),
                    (interval_groups.get("road_clearing", []), road_color, road_alpha, "Road Clearing"),
                    (interval_groups.get("maintenance", []), maint_color, maint_alpha, "Maintenance"),
                ]
            else:
                shade_specs = [
                    (interval_groups.get("between_shifts", []), "gray", 0.6, "Between Shifts"),
                    (interval_groups.get("meal", []), "gray", 0.15, "Meal"),
                    (interval_groups.get("maintenance", []), "#FF9999", 0.15, "Maintenance"),
                ]

            for intervals_list, color, alpha, _label in shade_specs:
                for gs, ge in self._group_consecutive(intervals_list):
                    x0 = start_hour + (gs - 1) * dt
                    x1 = start_hour + ge * dt
                    ax1.axvspan(x0, x1, color=color, alpha=alpha, linewidth=0)

            ax1.step(times_step, swap_step, where="post", label="Swaps", color="blue", linewidth=1.5)

            ax1.set_ylabel("Swaps [count]", color="black", fontsize=axis_label_fs)
            ax1.set_xlabel("Hour", fontsize=axis_label_fs)
            ax1.tick_params(axis="y", labelcolor="black", labelsize=tick_fs)
            ax1.set_ylim(0, max(1.0, float(np.nanmax(swap_step)) * 1.15 if len(swap_step) else 1.0))
            ax1.set_xlim(times_step[0], times_step[-1])
            ax1.grid(False)

            end = np.ceil(times_step[-1])
            xticks = np.arange(start_hour, end + 1, 4)
            ax1.xaxis.set_major_locator(FixedLocator(xticks))

            def hour_formatter(x, pos):
                h = int(np.floor(x)) % 24
                m = int(round((x - np.floor(x)) * 60)) % 60
                return f"{h:02d}:{m:02d}"

            ax1.xaxis.set_major_formatter(FuncFormatter(hour_formatter))
            ax1.xaxis.set_minor_locator(MultipleLocator(1))
            ax1.tick_params(axis="x", labelsize=tick_fs)

            ax2 = ax1.twinx()
            ax2.step(times_step, marginal_step, where="post", label="Energy Price", color="red", linewidth=1.5)
            ax2.set_ylabel("Energy Price [USD/kWh]", color="black", fontsize=axis_label_fs)
            ax2.tick_params(axis="y", labelcolor="black", labelsize=tick_fs)
            ax2.set_ylim(0.0, 0.30)
            ax2.grid(False)

            handles = [
                plt.Line2D([0], [0], color="blue", label="Swaps"),
                plt.Line2D([0], [0], color="red", label="Energy Price"),
            ]
            # Append unique shade patches by label to avoid duplicate legend entries
            seen_labels = set()
            for _vals, color, alpha, label in shade_specs:
                if label not in seen_labels:
                    handles.append(mpatches.Patch(color=color, alpha=alpha, label=label))
                    seen_labels.add(label)

            month = self._rep_day_label(d)
            fig.suptitle(f"{month} - {self._mode_plot_title()} Swaps vs Energy Price", fontsize=title_fs, y=1.06)
            fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.99), ncol=3, fontsize=legend_fs, frameon=True)

            plt.tight_layout()
            fig.subplots_adjust(top=0.82)
            fig.savefig(os.path.join(self.plot_dir, f"Swaps_vs_price_{month}.png"), bbox_inches="tight", dpi=120)
            plt.close(fig)

    def plot_charging_batteries_vs_price(self):
        if self.df_Sv is None or self.df_Sv.empty:
            print("⚠️ No hay Sv.json. Omitiendo 'ChargingBatteries_vs_price'.")
            return
        if self.params.costo_marginal is None or self.params.costo_marginal.empty:
            print("⚠️ No hay costo_electricidad en parameters.json. Omitiendo 'ChargingBatteries_vs_price'.")
            return

        dt = float(self.delta_t)

        for d in self.days:
            sv_day = (
                self.df_Sv.query("day == @d")[["interval", "value"]]
                .groupby("interval")["value"]
                .sum()
                .reindex(self.intervals)
                .fillna(0.0)
            )
            if sv_day.empty:
                continue

            price_day = self._price_series(day=d)

            # Misma construcción temporal del gráfico SoC:
            # x en [0,24] y serie extendida para cubrir el último intervalo.
            x_steps = np.array([0.0] + [t * dt for t in self.intervals], dtype=float)
            y_batt = np.concatenate([[sv_day.iloc[0] if len(sv_day) else 0.0], sv_day.to_numpy(dtype=float)])
            y_price = np.concatenate([[price_day.iloc[0] if len(price_day) else 0.0], price_day.to_numpy(dtype=float)])

            fig = plt.figure(figsize=(18, 6))
            fig.subplots_adjust(left=0.06, right=0.92, top=0.84, bottom=0.12)
            gs = gridspec.GridSpec(2, 1, height_ratios=[0.65, 4.2], hspace=0.08)
            legend_ax = fig.add_subplot(gs[0])
            legend_ax.axis("off")
            ax_main = fig.add_subplot(gs[1])
            ax_price = ax_main.twinx()

            batt_color = "#4F81BD"
            price_color = "#3366CC"

            batt_step = ax_main.step(
                x_steps,
                y_batt,
                where="post",
                color=batt_color,
                linewidth=2.8,
                label="Charging Batteries",
            )[0]
            ax_main.fill_between(x_steps, y_batt, step="post", alpha=0.25, color=batt_color)

            price_line = ax_price.plot(
                x_steps,
                y_price,
                color=price_color,
                linestyle="--",
                linewidth=2.6,
                alpha=0.9,
                label="Energy Price",
            )[0]

            handles = [
                plt.Line2D([0], [0], color=batt_color, lw=4, label="Charging Batteries"),
                plt.Line2D([0], [0], color=price_color, lw=3, ls="--", label="Energy Price"),
            ]
            ax_main.legend(
                handles=handles,
                loc="upper left",
                bbox_to_anchor=(0.15, 1.27),
                ncols=2,
                frameon=True,
                fontsize=16,
                framealpha=0.6,
            )

            ticks, labels = self._get_hourly_time_ticks(start_hour=9)
            ax_main.set_xticks(ticks)
            ax_main.set_xticklabels(labels, fontsize=13, rotation=0, ha="center")

            ax_main.set_xlim(0, 24)
            ax_price.set_xlim(0, 24)

            ax_main.set_xlabel("Time", fontsize=18)
            ax_main.set_ylabel("Batteries Charging [count]", fontsize=17, color=batt_color)
            ax_price.set_ylabel("Energy Price [USD/kWh]", fontsize=17, color=price_color)

            y_max = max(float(np.nanmax(y_batt)) if len(y_batt) else 0.0, 1.0)
            ax_main.set_ylim(0.0, y_max * 1.15)
            ax_main.yaxis.set_major_locator(MultipleLocator(max(1.0, round(y_max / 6.0))))

            ax_price.set_ylim(0.0, max(0.30, float(np.nanmax(y_price)) * 1.1 if len(y_price) else 0.30))
            ax_price.yaxis.set_major_locator(MultipleLocator(0.05))

            ax_main.tick_params(axis="y", labelsize=16)
            ax_price.tick_params(axis="y", labelsize=16)
            ax_main.tick_params(axis="x", labelsize=15.5)
            ax_main.grid(False)
            ax_main.grid(axis="x", which="major", linestyle="--", alpha=0.5)
            ax_main.yaxis.grid(False)
            ax_price.grid(False)
            legend_ax.set_xlim(0, 24)

            month = self._rep_day_label(d)
            fig.suptitle(f"Charging Batteries and Price - {month}", y=0.96, fontsize=18)

            fig.savefig(os.path.join(self.plot_dir, f"ChargingBatteries_vs_price_{month}.png"), dpi=150, bbox_inches="tight")
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
            print("⚠️ No hay costo_electricidad en parameters.json. Omitiendo 'SoC_vs_price_and_states'.")
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

                is_swap = (self.df_Z_swap is not None and not self.df_Z_swap.empty)
                charging_label = "Battery Swapping" if is_swap else "Charging"

                palette = {
                    "State-of-Charge": "#f78c11",
                    "Fuel": "#4169E1",
                    "Energy Price": "#3366CC",
                    "Charging": "#3366CC",
                    "Battery Swapping": "#3366CC",
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

                if not dfB_sel.empty:
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

                price_day = self._price_series(day=day, lhd=lhd).to_numpy(dtype=float)
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
                Z_filtered = (self.df_Z_swap.query("lhd == @lhd and day == @day and value >= 0.5")
                              if is_swap else pd.DataFrame())

                if self.mode == "DET":
                    for interval_list, color, alpha in [
                        (self.params.meal_det, *self.DET_SHADE_COLORS["meal"]),
                        (self.params.road_clearing, *self.DET_SHADE_COLORS["road_clearing"]),
                        (self.params.maintenance_det, *self.DET_SHADE_COLORS["maintenance"]),
                    ]:
                        for gs, ge in self._group_consecutive(interval_list):
                            x0 = (gs - 1) * delta_t
                            x1 = ge * delta_t
                            ax_main.axvspan(x0, x1, color=color, alpha=alpha, linewidth=0)
                            ax_task.axvspan(x0, x1, color=color, alpha=alpha, linewidth=0)

                states = []
                for t in self.intervals:
                    is_traveling = False
                    if not Y_filtered.empty and "interval" in Y_filtered.columns:
                        is_traveling = not Y_filtered.query("interval == @t").empty

                    is_charging = False
                    if is_swap:
                        if not Z_filtered.empty and "interval" in Z_filtered.columns:
                            is_charging = not Z_filtered.query("interval == @t").empty
                    elif is_electric and not P_filtered.empty and {"interval", "value"}.issubset(P_filtered.columns):
                        is_charging = not P_filtered.query("interval == @t and value > 1").empty

                    if is_traveling:
                        states.append("In-Transit")
                    elif is_charging:
                        states.append(charging_label)
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
                row2_names = [charging_label, "Inactive", "In-Transit"]
                row1_handles = [
                    plt.Line2D([0], [0], color=palette[name], lw=4, label=name)
                    for name in row1_names
                ]
                row2_handles = [
                    plt.Line2D([0], [0], color=palette[name], lw=6, label=name)
                    for name in row2_names
                ]

                legend1 = legend_ax.legend(
                    handles=row1_handles,
                    loc="center left",
                    bbox_to_anchor=(0.0, 0.5),
                    ncols=len(row1_handles),
                    frameon=True,
                    fontsize=16,
                    title_fontsize=17,
                    framealpha=0.6,
                )
                legend2 = legend_ax.legend(
                    handles=row2_handles,
                    loc="center right",
                    bbox_to_anchor=(1.0, 0.5),
                    ncols=len(row2_handles),
                    frameon=True,
                    fontsize=16,
                    title="Task States",
                    title_fontsize=17,
                    framealpha=0.6,
                )
                legend_ax.add_artist(legend1)
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
                fig.suptitle(f"LHD {lhd} {self._mode_plot_title()} - {month}", y=0.96, fontsize=18)

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
            print("⚠️ No hay costo_electricidad. Omitiendo 'LHD_total_costs'.")
            return

        if "lhd" in self.params.costo_marginal.columns:
            lhds = sorted(self.params.costo_marginal["lhd"].unique().tolist())
        else:
            lhds = []
            if self.df_P is not None and not self.df_P.empty and "lhd" in self.df_P.columns:
                lhds.extend(self.df_P["lhd"].dropna().unique().tolist())
            if self.df_E is not None and not self.df_E.empty and "lhd" in self.df_E.columns:
                lhds.extend(self.df_E["lhd"].dropna().unique().tolist())
            lhds = sorted(set(lhds))

        if not lhds:
            print("⚠️ No se pudieron detectar LHD para 'LHD_total_costs'.")
            return

        costs = {l: 0.0 for l in lhds}

        if self.df_P is not None and not self.df_P.empty:
            for lhd in lhds:
                for d in self.days:
                    price = (self.params.costo_marginal
                             .query("lhd == @lhd and day == @d")[["interval","price"]].set_index("interval")
                             if "lhd" in self.params.costo_marginal.columns
                             else self._price_series(day=d).to_frame(name="price"))
                    p = (self.df_P.query("day == @d and lhd == @lhd")[["interval","value"]].set_index("interval"))
                    if not p.empty and not price.empty:
                        merged = p.join(price, how="inner")
                        costs[lhd] += float((merged["value"] * merged["price"] * self.delta_t).sum())
        elif self.df_E is not None and not self.df_E.empty:
            for lhd in lhds:
                for d in self.days:
                    price = (self.params.costo_marginal
                             .query("lhd == @lhd and day == @d")[["interval","price"]].set_index("interval")
                             if "lhd" in self.params.costo_marginal.columns
                             else self._price_series(day=d).to_frame(name="price"))
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
        Genera un gráfico de barras AZULES mostrando el material total extraído (Variable M)
        por cada punto de extracción (nodo j) y compara con la demanda (m_j).
        """
        if self.df_M is None or self.df_M.empty:
            print("⚠️ No hay datos en M.json. Omitiendo 'Material Extraction by Point'.")
            return

        # Intentar detectar la columna del nodo ('node' o 'j')
        node_col = 'node' if 'node' in self.df_M.columns else 'j'
        if node_col not in self.df_M.columns:
            # Si no encuentra ninguna, intenta usar la segunda columna
            cols = self.df_M.columns
            if len(cols) > 1:
                node_col = cols[1]
            else:
                return

        mj = self.params.m_j or {}

        for d in self.days:
            # Filtrar datos del día
            df_day = self.df_M.query("day == @d")
            if df_day.empty:
                continue

            # Agrupar por punto de extracción (nodo) y sumar toneladas
            extr = (df_day.groupby(node_col)["value"]
                    .sum()
                    .rename("extracted_tons")
                    .to_frame()
                    .sort_index())

            if extr.empty:
                continue

            # --- Graficar ---
            fig, ax = plt.subplots(figsize=(max(10, 0.5 * len(extr)), 6))
            x = np.arange(len(extr.index))

            # Barras de material extraído (COLOR AZUL)
            ax.bar(x, extr["extracted_tons"].values, 
                   color='#1f77b4', edgecolor="black", alpha=0.85, width=0.6, 
                   label="Material Extracted (M)")

            # Línea de Demanda (m_j)
            if mj:
                demand_values = []
                # Buscar la demanda correspondiente a cada nodo en el eje X
                for node in extr.index:
                    # Intentar buscar como string (lo más común en JSON keys) o como el tipo original
                    val = mj.get(str(node), {}).get(d, np.nan)
                    if pd.isna(val):
                        val = mj.get(node, {}).get(d, np.nan)
                    demand_values.append(val)
                
                # Graficar demanda solo si hay datos válidos
                if any(not pd.isna(v) for v in demand_values):
                    ax.plot(x, demand_values, color='red', marker='D', linestyle='--', 
                            linewidth=2, markersize=6, label="Demand Target (m_j)")

            # Formato de ejes
            ax.set_xticks(x)
            ax.set_xticklabels(extr.index, rotation=45, ha='right', fontsize=11)
            
            ax.set_ylabel("Total Material [t]", fontsize=13)
            ax.set_xlabel("Extraction Point (j)", fontsize=13)
            
            month_label = self._rep_day_label(d)
            ax.set_title(f"Material Extraction vs Demand (M) — {month_label}", fontsize=16, pad=15)
            
            ax.grid(axis='y', linestyle=':', alpha=0.6)
            ax.legend(loc="upper right", fontsize=11, frameon=True).get_frame().set_edgecolor("#cccccc")

            fig.tight_layout()

            # Guardar archivo
            filename = f"Material_Extraction_M_Blue_{month_label.replace(' ', '_')}.png"
            save_path = os.path.join(self.plot_dir, filename)
            fig.savefig(save_path, dpi=150, bbox_inches='tight')
            plt.close(fig)

    def plot_gen_capacity_profile(self):
        """Perfil de capacidad disponible y despacho real por generador renovable (G_g * p_max_g * alpha_g)."""
        params_data = _load_json(os.path.join(self.json_dir, "parameters.json"))
        gg_data     = _load_json(os.path.join(self.json_dir, "G_g.json"))

        if not params_data or not gg_data:
            print("INFO: Faltan G_g.json o parameters.json — omitiendo perfil de capacidad solar.")
            return

        # G_g.json usa eje nombrado: {"g": {"Solar_PV": 2}} — navegar un nivel
        gg_inner = gg_data
        for _key in ("g", "_1"):
            if isinstance(gg_data, dict) and _key in gg_data and isinstance(gg_data[_key], dict):
                gg_inner = gg_data[_key]
                break
        gg: Dict[str, float] = {str(g): float(v) for g, v in gg_inner.items() if not isinstance(v, dict)} if isinstance(gg_inner, dict) else {}

        p_max_raw   = params_data.get("p_max_g", {})
        p_max_inner = p_max_raw.get("_1", p_max_raw) if isinstance(p_max_raw, dict) else {}
        p_max: Dict[str, float] = {str(g): float(v) for g, v in p_max_inner.items()} if isinstance(p_max_inner, dict) else {}

        alpha_root = params_data.get("alpha_g", {}).get("_1", {})
        gen_set    = [str(g) for g in params_data.get("gen_set", list(gg.keys()))]
        active_gens = [g for g in gen_set if gg.get(g, 0.0) > 0 and p_max.get(g, 0.0) > 0]

        if not active_gens:
            print("INFO: No hay generadores instalados — omitiendo perfil de capacidad.")
            return

        gen_colors = {"Solar_PV": "#F4A836", "Wind": "#4FC3F7", "Diesel": "#A5D6A7"}
        fallback   = ["#CE93D8", "#80CBC4", "#EF9A9A", "#FFF176"]
        for idx, g in enumerate(active_gens):
            if g not in gen_colors:
                gen_colors[g] = fallback[idx % len(fallback)]

        dt     = float(self.delta_t)
        x_rel  = np.array([(t - 1) * dt for t in self.intervals])
        x_step = np.append(x_rel, x_rel[-1] + dt)

        def _step(arr: np.ndarray) -> np.ndarray:
            return np.append(arr, arr[-1])

        for d in self.days:
            fig, ax = plt.subplots(figsize=(14, 5))
            handles = []

            for g in active_gens:
                units = gg.get(g, 0.0)
                pmax  = p_max.get(g, 0.0)
                alpha_day_raw = (
                    alpha_root.get(g, {}).get("_2", {}).get(str(d), {}).get("_3", {})
                )
                capacity = np.array([
                    units * pmax * float(alpha_day_raw.get(f"{float(t)}", alpha_day_raw.get(str(t), 0.0)))
                    for t in self.intervals
                ])
                color = gen_colors[g]
                ax.fill_between(x_step, 0, _step(capacity), step="post", color=color, alpha=0.35, linewidth=0)
                ax.step(x_step, _step(capacity), where="post", color=color, linewidth=2.0)
                handles.append(mpatches.Patch(color=color, alpha=0.5, label=f"{g} cap. (G·Pmax·α)"))

                if self.df_Pgen is not None and not self.df_Pgen.empty:
                    gen_day = (
                        self.df_Pgen.query("day == @d and gen == @g")[["interval", "value"]]
                        .set_index("interval")["value"].reindex(self.intervals).fillna(0.0).values
                    )
                    ax.step(x_step, _step(gen_day), where="post", color=color, linewidth=1.5, linestyle="--", alpha=0.9)
                    handles.append(plt.Line2D([0], [0], color=color, linewidth=1.5, linestyle="--",
                                              label=f"{g} despacho real (P_gen)"))

            month = self._rep_day_label(d)
            ax.set_title(f"Perfil de capacidad disponible — {month}", fontsize=14)
            ax.set_xlabel("Hora", fontsize=12)
            ax.set_ylabel("Potencia [kW]", fontsize=12)
            ax.set_xlim(0, 24)
            ax.set_ylim(bottom=0)
            ax.legend(handles=handles, loc="upper right", fontsize=10, framealpha=0.85)
            ax.grid(axis="y", linestyle="--", alpha=0.4)
            ticks, labels = self._get_hourly_time_ticks(start_hour=9)
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels, rotation=45, fontsize=9, ha="right")
            peak = max((gg.get(g, 0) * p_max.get(g, 0) for g in active_gens), default=0)
            ax.axhline(peak, color="gray", linewidth=0.8, linestyle=":", alpha=0.6)
            ax.text(0.5, peak, f"  Pico inst. {peak:,.0f} kW",
                    va="bottom", ha="left", fontsize=8, color="gray", transform=ax.get_yaxis_transform())
            fig.tight_layout()
            fname = f"GenCapacityProfile_{month.replace(' ', '_')}.png"
            fig.savefig(os.path.join(self.plot_dir, fname), dpi=150, bbox_inches="tight")
            plt.close(fig)

    def plot_power_dispatch(self):
        """Despacho de potencia apilado: generación + descarga BESS + red eléctrica."""
        if self.df_Pred is None or self.df_Pred.empty:
            print("INFO: No hay P_red.json — omitiendo gráfico de despacho de potencia.")
            return

        gen_colors   = {"Solar_PV": "#F4A836", "Wind": "#4FC3F7", "Diesel": "#A5D6A7"}
        fallback     = ["#CE93D8", "#80CBC4", "#EF9A9A", "#FFF176"]
        grid_color   = "#90A4AE"
        bess_color        = "#4CAF50"
        bess_charge_color = "#C8E6C9"

        dt = float(self.delta_t)
        gen_names: List[str]     = sorted(self.df_Pgen["gen"].unique().tolist())     if (self.df_Pgen is not None and not self.df_Pgen.empty) else []
        storage_names: List[str] = sorted(self.df_Pbat["storage"].unique().tolist()) if (self.df_Pbat is not None and not self.df_Pbat.empty) else []
        for idx, g in enumerate(gen_names):
            if g not in gen_colors:
                gen_colors[g] = fallback[idx % len(fallback)]

        for d in self.days:
            p_red_day = (
                self.df_Pred.query("day == @d")[["interval", "value"]]
                .set_index("interval")["value"].reindex(self.intervals).fillna(0.0)
            )
            x_step = np.append(np.array([(t - 1) * dt for t in self.intervals]),
                                (len(self.intervals)) * dt)

            def _step(s: np.ndarray) -> np.ndarray:
                return np.append(s, s[-1])

            fig, ax = plt.subplots(figsize=(14, 5))
            bottoms = np.zeros(len(self.intervals))
            handles = []

            for g in gen_names:
                gen_day = (
                    self.df_Pgen.query("day == @d and gen == @g")[["interval", "value"]]
                    .set_index("interval")["value"].reindex(self.intervals).fillna(0.0).values
                ) if self.df_Pgen is not None else np.zeros(len(self.intervals))
                color = gen_colors.get(g, "#CE93D8")
                ax.fill_between(x_step, _step(bottoms), _step(bottoms + gen_day), step="post", alpha=0.88, color=color, linewidth=0)
                handles.append(mpatches.Patch(color=color, label=f"Gen. {g}"))
                bottoms = bottoms + gen_day

            bess_added = False
            for h in storage_names:
                p_bat_raw = (
                    self.df_Pbat.query("day == @d and storage == @h")[["interval", "value"]]
                    .set_index("interval")["value"].reindex(self.intervals).fillna(0.0).values
                )
                discharge = np.clip(p_bat_raw, 0, None)
                charge    = np.clip(p_bat_raw, None, 0)
                if discharge.any():
                    ax.fill_between(x_step, _step(bottoms), _step(bottoms + discharge), step="post", alpha=0.88, color=bess_color, linewidth=0)
                    if not bess_added:
                        handles.append(mpatches.Patch(color=bess_color, label="BESS descarga"))
                    bottoms = bottoms + discharge
                    bess_added = True
                if charge.any():
                    charge_abs = np.abs(charge)
                    ax.fill_between(x_step, _step(bottoms + p_red_day.values),
                                    _step(bottoms + p_red_day.values + charge_abs),
                                    step="post", alpha=0.5, color=bess_charge_color, linewidth=0, hatch="///")
                    handles.append(mpatches.Patch(facecolor=bess_charge_color, hatch="///",
                                                  edgecolor="gray", alpha=0.6, label="BESS carga"))

            ax.fill_between(x_step, _step(bottoms), _step(bottoms + p_red_day.values),
                            step="post", alpha=0.88, color=grid_color, linewidth=0)
            handles.append(mpatches.Patch(color=grid_color, label="Red eléctrica"))

            month = self._rep_day_label(d)
            ax.set_title(f"Despacho de potencia — {month}", fontsize=14)
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
            fname = f"PowerDispatch_{month.replace(' ', '_')}.png"
            fig.savefig(os.path.join(self.plot_dir, fname), dpi=150, bbox_inches="tight")
            plt.close(fig)

    def plot_bess_energy_state(self):
        """Estado de energía (A_h) del BESS con P_bat superpuesta en eje derecho."""
        if self.df_Ah is None or self.df_Ah.empty:
            print("INFO: No hay A_h.json — omitiendo gráfico de estado BESS.")
            return

        bess_color   = "#4CAF50"
        charge_color = "#1565C0"
        dt = float(self.delta_t)
        storage_names = sorted(self.df_Ah["storage"].unique().tolist())

        for h in storage_names:
            for d in self.days:
                a_day = (
                    self.df_Ah.query("day == @d and storage == @h")[["interval", "value"]]
                    .set_index("interval")["value"].reindex(self.intervals).fillna(0.0)
                )
                x_rel  = np.array([(t - 1) * dt for t in self.intervals])
                x_step = np.append(x_rel, x_rel[-1] + dt)

                fig, ax1 = plt.subplots(figsize=(14, 5))
                ax2 = ax1.twinx()
                ax2.grid(False)

                soc_vals = np.append(a_day.values, a_day.values[-1])
                ax1.fill_between(x_step, 0, soc_vals, step="post", color=bess_color, alpha=0.35, linewidth=0)
                ax1.step(x_step, soc_vals, where="post", color=bess_color, linewidth=2.0, label="Estado energía A_h")

                if self.df_Pbat is not None and not self.df_Pbat.empty:
                    p_bat = (
                        self.df_Pbat.query("day == @d and storage == @h")[["interval", "value"]]
                        .set_index("interval")["value"].reindex(self.intervals).fillna(0.0)
                    )
                    p_step = np.append(p_bat.values, p_bat.values[-1])
                    ax2.step(x_step, p_step, where="post", color=charge_color, linewidth=1.8,
                             linestyle="--", alpha=0.85, label="P_bat (+desc / -carga)")
                    ax2.axhline(0, color="gray", linewidth=0.6, linestyle=":")
                    ax2.set_ylabel("Potencia BESS [kW]", fontsize=11, color=charge_color)
                    ax2.tick_params(axis="y", labelcolor=charge_color, labelsize=10)

                month = self._rep_day_label(d)
                ax1.set_title(f"Estado de energía BESS — {h} — {month}", fontsize=14)
                ax1.set_xlabel("Hora", fontsize=12)
                ax1.set_ylabel("Energía almacenada [kWh]", fontsize=12, color=bess_color)
                ax1.tick_params(axis="y", labelcolor=bess_color, labelsize=10)
                ax1.set_xlim(0, 24)
                ax1.set_ylim(bottom=0)
                ax1.grid(axis="y", linestyle="--", alpha=0.4)
                ticks, labels = self._get_hourly_time_ticks(start_hour=9)
                ax1.set_xticks(ticks)
                ax1.set_xticklabels(labels, rotation=45, fontsize=9, ha="right")
                lines1, lbl1 = ax1.get_legend_handles_labels()
                lines2, lbl2 = ax2.get_legend_handles_labels() if self.df_Pbat is not None else ([], [])
                ax1.legend(lines1 + lines2, lbl1 + lbl2, loc="upper right", fontsize=10, framealpha=0.85)
                fig.tight_layout()
                fname = f"BESS_EnergyState_{h}_{month.replace(' ', '_')}.png"
                fig.savefig(os.path.join(self.plot_dir, fname), dpi=150, bbox_inches="tight")
                plt.close(fig)

    def create_all_plots(self):
        self.plot_swaps_vs_price()
        self.plot_battery_charging_power()
        self.plot_charging_batteries_vs_price()
        #self.plot_node_extraction_vs_demand()
        self.plot_lhd_costs_bars()
        self.plot_lhd_soc_vs_price_and_states()
        self.plot_emissions_profiles_for_optimized_day()
        self.plot_material_extraction_by_point()
        self.plot_power_dispatch()
        self.plot_gen_capacity_profile()
        self.plot_bess_energy_state()
        print(f"✔ Plots guardados en '{self.plot_dir}'.")


# -------------------- CLI --------------------
def main():
    ap = argparse.ArgumentParser(description="Graficador para salidas JSON del modelo")
    ap.add_argument("--json_dir", required=True, help="Carpeta con *.json (variables + parameters.json)")
    ap.add_argument("--mode", choices=["DCH", "DET"], default="DCH", help="Modo de graficado")
    ap.add_argument("--energy_price_scale", type=float, default=DEFAULT_ENERGY_PRICE_SCALE,
                    help="Escala para costo_electricidad (opcional)")
    args = ap.parse_args()

    plotter = JSONPlotter(args.json_dir, energy_price_scale=args.energy_price_scale, mode=args.mode)
    plotter.create_all_plots()

if __name__ == "__main__":
    main()
