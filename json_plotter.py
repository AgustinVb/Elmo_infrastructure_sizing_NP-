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


# -------------------- Loaders --------------------
def load_B_df(path: str) -> Optional[pd.DataFrame]:
    data = _load_json(path)
    if not data:
        return None
    rows = []
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
    def __init__(self, json_dir: str, energy_price_scale: float = DEFAULT_ENERGY_PRICE_SCALE):
        self.json_dir = json_dir

        # Guardar SIEMPRE en <json_dir>/plots
        self.plot_dir = os.path.join(self.json_dir, "plots")
        os.makedirs(self.plot_dir, exist_ok=True)

        # Variables
        self.df_B = load_B_df(os.path.join(json_dir, "B.json"))
        self.df_Y = load_binary_Y_df(os.path.join(json_dir, "Y.json"))
        self.df_P = load_generic_variable_df(os.path.join(json_dir, "P.json"), "P")
        self.df_C = load_generic_variable_df(os.path.join(json_dir, "C.json"), "C")
        self.df_E = load_generic_variable_df(os.path.join(json_dir, "E.json"), "E")
        self.df_M = load_generic_variable_df(os.path.join(json_dir, "M.json"), "M")

        # Parámetros
        self.params = Parameters(os.path.join(json_dir, "parameters.json"), energy_price_scale=energy_price_scale)
        self.delta_t = self.params.delta_t if self.params.ok else 1.0

        # Dominio
        self.days = self._detect_days()
        self.intervals = self._detect_intervals()

    def _detect_days(self) -> List[int]:
        sources = []
        for df in [self.df_B, self.df_Y, self.df_P, self.df_C, self.df_E]:
            if df is not None and "day" in df.columns and not df.empty:
                sources.append(sorted(df["day"].dropna().unique().tolist()))
        if self.params.costo_marginal is not None:
            sources.append(sorted(self.params.costo_marginal["day"].unique().tolist()))
        return sorted({d for lst in sources for d in lst}) if sources else []

    def _detect_intervals(self) -> List[int]:
        sources = []
        for df in [self.df_B, self.df_Y, self.df_P, self.df_C, self.df_E]:
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

        # No asumimos que min(self.days) == 1 de enero
        min_day = min(self.days) if self.days else 1
        day_of_year = ((di - min_day) % 365) + 1

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
    
    # -------------------- NUEVO MÉTODO DE TIEMPO FIJO --------------------
    def _get_fixed_time_ticks(self, mode="interval"):
        """
        Devuelve ticks y etiquetas FIJAS para: 08:00, 12:00, 16:00, 20:00, 00:00, 04:00, 08:00(+1).
        mode="hour": devuelve posiciones 0, 4, 8... (para gráficos con eje X en horas)
        mode="interval": devuelve posiciones 1, 1+step... (para gráficos con eje X en intervalos)
        """
        # Horas relativas desde el inicio (0h = 08:00 AM)
        rel_hours = [0, 4, 8, 12, 16, 20, 24]
        labels = ["08:00", "12:00", "16:00", "20:00", "00:00", "04:00", "08:00"]
        
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

    # ---------- Plots ----------
    def plot_charge_power_vs_price(self):
        if self.df_P is None or self.df_P.empty:
            print("⚠️ No hay P.json. Omitiendo 'ChargePower_vs_price'.")
            return
        if self.params.costo_marginal is None or self.params.costo_marginal.empty:
            print("⚠️ No hay costo marginal en parameters.json. Omitiendo 'ChargePower_vs_price'.")
            return
        
        def _season_for_day(d: int) -> str:
            if d in (12, 1, 2): return "Summer"
            if d in (3, 4, 5): return "Autumn"
            if d in (6, 7, 8): return "Winter"
            return "Spring"

        for d in self.days:
            season = _season_for_day(int(d))

            p_day = (self.df_P.query("day == @d")[["interval", "value"]]
                        .groupby("interval")["value"].sum()
                        .reindex(self.intervals).fillna(0.0))

            price_day = (self.params.costo_marginal.query("day == @d")
                            .groupby("interval")["price"].mean()
                            .reindex(self.intervals).ffill().bfill().fillna(0.0))

            fig = plt.figure(figsize=(14, 8))
            gs = gridspec.GridSpec(2, 1, height_ratios=[0.20, 0.80], hspace=0.08)

            ax_top = fig.add_subplot(gs[0])   # espacio título + leyenda
            ax1 = fig.add_subplot(gs[1])      # gráfico principal
            ax2 = ax1.twinx()
            ax3 = ax1.twinx()

            # Eje “invisible” del costo acumulado
            ax3.spines["right"].set_visible(False)
            ax3.yaxis.set_ticks([])
            ax3.set_ylabel("")
            ax3.set_ylim(0, 600)

            # ----- Curvas -----
            ax1.plot(self.intervals, p_day.values, linewidth=3.2,
                    label="Charge Power", drawstyle="steps-post", color="C0")
            ax2.plot(self.intervals, price_day.values, linewidth=3.2, linestyle="--",
                    label="Energy Price", color="red")
            
            # Límites y márgenes
            ax1.set_ylim(0, 1500)
            ax2.set_ylim(0, 0.30)
            
            # --- Ticks fijos ---
            ticks, labels = self._get_fixed_time_ticks(mode="interval")
            ax1.set_xticks(ticks)
            ax1.set_xticklabels(labels, fontsize=18)
            ax1.set_xlim(ticks[0], ticks[-1])
            # -------------------

            ax1.margins(x=0.0)
            for ax in (ax1, ax2, ax3): ax.grid(False)

            # Etiquetas y tamaños de fuente
            ax1.set_xlabel("Time", fontsize=22)
            ax1.set_ylabel("Charge Power [kW]", fontsize=22)
            ax2.set_ylabel("Energy Price [USD/kWh]", fontsize=22)
            ax1.tick_params(axis="y", labelsize=18)
            ax2.tick_params(axis="y", labelsize=18)

            # Título 
            ax_top.set_axis_off()
            month = self._rep_day_label(d)
            ax_top.text(0.5, 0.78,
                        f"{month} – Total Charge Power vs Energy Price ({season})",
                        ha="center", va="center", fontsize=32)

            # Leyenda
            h1, l1 = ax1.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            h3, l3 = ax3.get_legend_handles_labels()
            handles = h1 + h2 + h3
            labels_ = l1 + l2 + l3
            leg = ax_top.legend(handles, labels_, loc="center", ncols=3, fontsize=18,
                                frameon=True, fancybox=False, framealpha=1.0,
                                bbox_to_anchor=(0.5, 0.18))
            leg.get_frame().set_edgecolor("#cccccc")
            leg.get_frame().set_linewidth(1.2)
            leg.get_frame().set_facecolor("white")

            month = self._rep_day_label(d)
            fig.savefig(
                        os.path.join(self.plot_dir, f"ChargePower_vs_price_{month}.png"),
                        bbox_inches="tight"
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
        def _season_for_day(d: int) -> str:
            if d in (12, 1, 2): return "Summer"
            if d in (3, 4, 5): return "Autumn"
            if d in (6, 7, 8): return "Winter"
            return "Spring"

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

                # ---- Layout más compacto y equilibrado ----
                fig = plt.figure(figsize=(14, 8))
                gs = gridspec.GridSpec(
                    nrows=4, ncols=1,
                    height_ratios=[0.08, 0.11, 0.10, 0.71],  # título, leyenda líneas, leyenda estados, gráfico
                    hspace=0.05
                )
                ax_title  = fig.add_subplot(gs[0]); ax_title.axis('off')
                ax_leg_lines  = fig.add_subplot(gs[1]); ax_leg_lines.axis('off')
                ax_leg_states = fig.add_subplot(gs[2]); ax_leg_states.axis('off')
                ax1 = fig.add_subplot(gs[3])
                ax2 = ax1.twinx()

                # ===== Serie principal =====
                line1 = None
                if is_electric:
                    soc_series = (dfB_sel[["interval", "value"]]
                                .set_index("interval")["value"]
                                .reindex(self.intervals)
                                .ffill().bfill().fillna(0.0))
                    soc_ini = float(soc_series.iloc[0]) if len(soc_series) else 0.0
                    y_raw = np.array([soc_ini] + soc_series.tolist(), dtype=float)
                    max_ref = max(float(np.nanmax(y_raw)), 1e-6)
                    y_pct = (y_raw / max_ref) * 100.0
                    x_hrs = np.array([0] + [t * delta_t for t in self.intervals], dtype=float)

                    line1, = ax1.plot(x_hrs, y_pct, color='orange', lw=2.4, label='SoC')
                    y_label = 'State of Charge [%]'
                else:
                    fuel_series = (dfE_sel[["interval", "value"]]
                                .set_index("interval")["value"]
                                .reindex(self.intervals)
                                .ffill().bfill().fillna(0.0))
                    fuel_ini = float(fuel_series.iloc[0]) if len(fuel_series) else 0.0
                    y_raw = np.array([fuel_ini] + fuel_series.tolist(), dtype=float)
                    max_ref = max(float(np.nanmax(y_raw)), 1e-6)
                    y_pct = (y_raw / max_ref) * 100.0
                    x_hrs = np.array([0] + [t * delta_t for t in self.intervals], dtype=float)

                    line1, = ax1.plot(x_hrs, y_pct, color='#4169E1', lw=2.4, label='Fuel')
                    y_label = 'Fuel Level [%]'

                ax1.set_ylabel(y_label, fontsize=22, labelpad=10)

                # ===== Precio =====
                price_day = (self.params.costo_marginal
                            .query("lhd == @lhd and day == @day")[["interval", "price"]]
                            .set_index("interval")
                            .reindex(self.intervals)["price"]
                            .ffill().bfill().fillna(0.0)
                            .to_numpy(dtype=float))
                y_price = np.concatenate([[price_day[0] if len(price_day) else 0.0], price_day])
                x_price = np.array([0] + [t * delta_t for t in self.intervals], dtype=float)

                line2, = ax2.step(x_price, y_price, where='post',
                                linestyle='--', linewidth=2.2, alpha=0.95,
                                color='red', label='Energy Price')
                ax2.set_ylabel('Energy Price [USD/kWh]', fontsize=22, labelpad=10)

                # ===== Ejes X con Ticks Fijos =====
                ticks, labels = self._get_fixed_time_ticks(mode="hour")
                ax1.set_xlabel('Time', fontsize=22, labelpad=10)
                ax1.set_xticks(ticks)
                ax1.set_xticklabels(labels, fontsize=20)
                ax1.set_xlim(0, 24)
                ax1.margins(x=0.0)

                # ===== Y limits =====
                ax1.set_ylim(-12.0, 105.0)
                ax1.yaxis.set_major_locator(MultipleLocator(20.0))
                ax1.set_yticks([0, 20, 40, 60, 80, 100])
                ax1.tick_params(axis='y', labelsize=20, pad=10)

                ax2.set_ylim(0.0, 0.30)
                ax2.yaxis.set_major_locator(MultipleLocator(0.05))
                # Alineación del cero
                Lmin, Lmax = ax1.get_ylim()
                Rmax_fixed = 0.30
                if Lmax != Lmin:
                    r = (0.0 - Lmin) / (Lmax - Lmin)
                    Rmin_align = (r * Rmax_fixed) / (r - 1.0) if 0.0 < r < 1.0 else 0.0
                else:
                    Rmin_align = 0.0
                ax2.set_ylim(Rmin_align, Rmax_fixed)
                ax2.tick_params(axis='y', labelsize=20, pad=10)

                # ===== Estados =====
                Y_filtered = (self.df_Y.query("lhd == @lhd and day == @day and value >= 0.5")
                            if (self.df_Y is not None and not self.df_Y.empty) else pd.DataFrame())
                P_filtered = (self.df_P.query("lhd == @lhd and day == @day")
                            if (self.df_P is not None and not self.df_P.empty) else pd.DataFrame())

                colors = {'Travel': 'gold', 'Charging': 'blue', 'Parked': 'gray'}
                handles_tasks, labels_tasks = [], []
                bottom_band = -12.0
                y_min, y_max = bottom_band, bottom_band * 0.01

                # Determinar estados por intervalo
                states = []
                for t in self.intervals:
                    is_traveling = False
                    if not Y_filtered.empty and 'interval' in Y_filtered.columns:
                        is_traveling = not Y_filtered.query("interval == @t").empty

                    is_charging = False
                    if is_electric and not P_filtered.empty and {'interval', 'value'}.issubset(P_filtered.columns):
                        is_charging = not P_filtered.query("interval == @t and value > 1").empty

                    if is_traveling:
                        states.append('Travel')
                    elif is_charging:
                        states.append('Charging')
                    else:
                        states.append('Parked')

                # Dibujar bloques continuos
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

                    for start, end, state in segments:
                        x_start = start * delta_t
                        x_end = end * delta_t
                        h = ax1.fill_between(
                            [x_start, x_end], y_min, y_max,
                            color=colors[state],
                            label=state if state not in labels_tasks else ""
                        )
                        if state not in labels_tasks:
                            handles_tasks.append(h)
                            labels_tasks.append(state)

                ax1.grid(False); ax2.grid(False)

                # Título y Leyendas
                season = _season_for_day(int(day))
                month = self._rep_day_label(day)
                ax_title.text(
                    0.5, 0.75,
                    f"LHD {lhd} – {month} ({season})",
                    ha="center", va="center", fontsize=32
                )

                ax_leg_lines.legend(
                    [line1, line2],
                    [line1.get_label(), 'Energy Price'],
                    loc='center', ncol=2, frameon=True, framealpha=1.0,
                    fontsize=18, bbox_to_anchor=(0.5, 0.5)
                )
                leg1 = ax_leg_lines.get_legend()
                if leg1:
                    leg1.get_frame().set_edgecolor("#cccccc")
                    leg1.get_frame().set_linewidth(1.2)
                    leg1.get_frame().set_facecolor("white")

                if handles_tasks:
                    ax_leg_states.legend(
                        handles_tasks, labels_tasks,
                        loc='center', ncol=len(labels_tasks),
                        frameon=True, framealpha=1.0, fontsize=18,
                        bbox_to_anchor=(0.5, 0.5), title='Task States'
                    )
                    leg2 = ax_leg_states.get_legend()
                    if leg2:
                        leg2.get_frame().set_edgecolor("#cccccc")
                        leg2.get_frame().set_linewidth(1.2)
                        leg2.get_frame().set_facecolor("white")
                        leg2.set_title('Task States')
                        leg2.get_title().set_fontsize(18)

                fig.savefig(os.path.join(self.plot_dir, f'SoC_vs_price_LHD-{lhd}_day-{day}.png'), dpi=150, bbox_inches='tight')
                plt.close(fig)


    def plot_emissions_profiles_for_optimized_day(self):
        # Verificación de datos
        have_elec = (self.params.emissions_electric is not None and
                     not self.params.emissions_electric.empty)
        if not have_elec:
            print("ℹ️ No hay perfiles de emisiones eléctricas. Omitiendo 'Emission Profile'.")
            return

        def _season_for_day(d: int) -> str:
            if 1 <= d <= 90:   return "Winter"
            if 91 <= d <= 180: return "Spring"
            if 181 <= d <= 270:return "Summer"
            return "Autumn"

        for d in self.days:
            season = _season_for_day(int(d))

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
                f"{month} – Electric Emission Profile ({season})",
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

    def create_all_plots(self):
        self.plot_charge_power_vs_price()
        #self.plot_node_extraction_vs_demand()
        self.plot_lhd_costs_bars()
        self.plot_lhd_soc_vs_price_and_states()
        self.plot_emissions_profiles_for_optimized_day()
        self.plot_material_extraction_by_point()
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