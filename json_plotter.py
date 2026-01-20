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

    # Caso 1: Estructura con eje 'k' (P.json, etc.)
    if "k" in data:
        for station, k_block in data["k"].items():
            if not isinstance(k_block, dict) or "d" not in k_block:
                continue
            d_data = k_block["d"]
            for lhd, t_block in d_data.items():
                if not isinstance(t_block, dict):
                    continue
                if "t" in t_block and isinstance(t_block["t"], dict):
                    for k1, v1 in t_block["t"].items():
                        # Caso similar a C/E: day -> i(interval)
                        if isinstance(v1, dict) and "i" in v1:
                            for interval, val in v1["i"].items():
                                rows.append({
                                    "lhd": lhd,
                                    "day": _numeric_or_str(k1),
                                    "interval": _numeric_or_str(interval),
                                    "value": float(val),
                                })

    # Caso 2: Estructura directa con 'd' (estructura original)
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
                    # heuristic: if inner keys look like LHD ids (start with 'LH')
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
                        # here lhd_key is the actual LHD id, station is outer_key
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
                        # other patterns under station wrapper (rare) are ignored for now
                else:
                    # original assumption: outer_key is LHD
                    lhd = outer_key
                    t_block = outer_block
                    for k1, v1 in t_block["t"].items():
                        # Caso Y-like: node -> i(day) -> j(interval)
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
                        # Caso B-like: day -> b(interval)
                        elif isinstance(v1, dict) and "b" in v1:
                            for interval, val in v1["b"].items():
                                rows.append({
                                    "lhd": lhd,
                                    "day": _numeric_or_str(k1),
                                    "interval": _numeric_or_str(interval),
                                    "value": float(val),
                                })
                        # Caso C/E-like: day -> i(interval)
                        elif isinstance(v1, dict) and "i" in v1:
                            for interval, val in v1["i"].items():
                                rows.append({
                                    "lhd": lhd,
                                    "day": _numeric_or_str(k1),
                                    "interval": _numeric_or_str(interval),
                                    "value": float(val),
                                })
                        else:
                            # Fallback genérico
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
    cols = [c for c in ["lhd","station","node","day","interval","value"] if c in df.columns]
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
        - Si los días del dataset parecen meses (max(self.days) <= 12), trata d como índice de mes.
        - Si no, interpreta d como día absoluto y calcula el día-del-año relativo a min(self.days),
          luego asigna mes usando longitudes reales de meses (no bisiesto).
        """
        try:
            di = int(d)
        except Exception:
            return f"Day {d}"

        # Caso: el dataset ya usa 1..12 como meses
        if self.days and max(self.days) <= 12:
            return self.MONTH_LABELS.get(di, f"Month {di}")

        # No asumimos que min(self.days) == 1 de enero: tomamos min(self.days) como inicio "virtual" del año
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
            if d in (12, 1, 2):
                return "Summer"
            if d in (3, 4, 5):
                return "Autumn"
            if d in (6, 7, 8):
                return "Winter"
            return "Spring"


        def _time_labels_every_4h(intervals: list[int]) -> tuple[list[int], list[str]]:
            if not intervals: return [], []
            step = 8  # 4h con Δt=0.5h
            tick_pos, tick_lbl = [], []
            for k, t in enumerate(intervals, start=1):
                if (k - 1) % step == 0:
                    total_min = 8*60 + (k-1)*30  # arranca 08:00
                    hh = (total_min // 60) % 24
                    mm = total_min % 60
                    tick_pos.append(t); tick_lbl.append(f"{hh:02d}:{mm:02d}")
            return tick_pos, tick_lbl

        for d in self.days:
            season = _season_for_day(int(d))

            p_day = (self.df_P.query("day == @d")[["interval", "value"]]
                        .groupby("interval")["value"].sum()
                        .reindex(self.intervals).fillna(0.0))

            price_day = (self.params.costo_marginal.query("day == @d")
                            .groupby("interval")["price"].mean()
                            .reindex(self.intervals).ffill().bfill().fillna(0.0))

            import matplotlib.gridspec as gridspec
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

            # ----- Curvas (etiquetas sin unidades) -----
            ax1.plot(self.intervals, p_day.values, linewidth=3.2,
                    label="Charge Power", drawstyle="steps-post", color="C0")
            ax2.plot(self.intervals, price_day.values, linewidth=3.2, linestyle="--",
                    label="Energy Price", color="red")
            # Límites y márgenes
            ax1.set_ylim(0, 1500)
            ax2.set_ylim(0, 0.30)
            ax1.set_xlim(self.intervals[0], self.intervals[-1])
            ax1.margins(x=0.0)
            for ax in (ax1, ax2, ax3): ax.grid(False)

            # Etiquetas y tamaños de fuente
            ax1.set_xlabel("Time", fontsize=22)
            ax1.set_ylabel("Charge Power [kW]", fontsize=22)
            ax2.set_ylabel("Energy Price [USD/kWh]", fontsize=22)

            ticks, labels = _time_labels_every_4h(self.intervals)
            ax1.set_xticks(ticks)
            ax1.set_xticklabels(labels, fontsize=18)
            ax1.tick_params(axis="y", labelsize=18)
            ax2.tick_params(axis="y", labelsize=18)

            # Título (SIN negrita) y más grande
            ax_top.set_axis_off()
            
            month = self._rep_day_label(d)
            ax_top.text(
                       0.5, 0.78,
                       f"{month} – Total Charge Power vs Energy Price ({season})",
                       ha="center", va="center", fontsize=32
            )


            # Leyenda: en panel superior con borde gris claro
            h1, l1 = ax1.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            h3, l3 = ax3.get_legend_handles_labels()
            handles = h1 + h2 + h3
            labels_ = l1 + l2 + l3
            leg = ax_top.legend(handles, labels_, loc="center", ncols=3, fontsize=18,
                                frameon=True, fancybox=False, framealpha=1.0,
                                bbox_to_anchor=(0.5, 0.18))
            # borde gris claro
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
        if self.df_Y is None or self.df_Y.empty:
            print("⚠️ No hay Y.json. Omitiendo 'Extraction vs Demand'.")
            return
        mj = self.params.m_j or {}

        for d in self.days:
            y_day = self.df_Y.query("day == @d")
            if y_day.empty:
                continue
            extr = (y_day.groupby("node")["value"].sum().rename("assignments").to_frame())
            if mj:
                tons = []
                for node, cnt in extr["assignments"].items():
                    base = mj.get(node, {}).get(d, np.nan)
                    tons.append(np.nan if pd.isna(base) else cnt * base)
                extr["extracted_tons"] = tons

            fig, ax = plt.subplots(figsize=(max(8, 0.35*len(extr)), 5))
            x = np.arange(len(extr.index))
            bar = extr["extracted_tons"] if "extracted_tons" in extr.columns else extr["assignments"]
            ax.bar(x, bar.values, edgecolor="black")
            import sys; sys.stdout.flush()  # Force output to appear immediately

            if mj:
                demand = [mj.get(node, {}).get(d, np.nan) for node in extr.index]
                ax.plot(x, demand, linestyle="--", linewidth=2, label="Demand")

            ax.set_xticks(x); ax.set_xticklabels(extr.index, rotation=90)
            ax.set_ylabel("Tons" if "extracted_tons" in extr.columns else "Assignments")
            ax.set_title(f"Extraction vs Demand – {self._rep_day_label(d)}")
            if mj: ax.legend(loc="upper left")
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
            if d in (12, 1, 2):
                return "Summer"
            if d in (3, 4, 5):
                return "Autumn"
            if d in (6, 7, 8):
                return "Winter"
            return "Spring"

        def _time_ticks_labels_every_4h(intervals: list[int]):
            if not intervals:
                return [], []
            step = 8  # 4h con Δt=0.5h
            tick_pos, tick_lbl = [], []
            for k, t in enumerate(intervals, start=1):
                if (k - 1) % step == 0:
                    total_min = 8*60 + (k-1)*30
                    hh = (total_min // 60) % 24
                    mm = total_min % 60
                    tick_pos.append(t); tick_lbl.append(f"{hh:02d}:{mm:02d}")
            return tick_pos, tick_lbl

        # Detectar LHDs (desde B o E)
        lhds = set()
        if self.df_B is not None and not self.df_B.empty:
            lhds.update(self.df_B["lhd"].unique().tolist())
        if self.df_E is not None and not self.df_E.empty:
            lhds.update(self.df_E["lhd"].unique().tolist())
        lhds = sorted(lhds)

        import matplotlib.gridspec as gridspec
        from matplotlib.ticker import MultipleLocator, FormatStrFormatter

        for lhd in lhds:
            for day in self.days:
                dfB_sel = (self.df_B.query("lhd == @lhd and day == @day")
                        if (self.df_B is not None and not self.df_B.empty) else pd.DataFrame())
                dfE_sel = (self.df_E.query("lhd == @lhd and day == @day")
                        if (self.df_E is not None and not self.df_E.empty) else pd.DataFrame())
                is_electric = not dfB_sel.empty
                if dfB_sel.empty and dfE_sel.empty:
                    continue

                # ---- Layout más compacto y equilibrado ----
                fig = plt.figure(figsize=(14, 8))
                # Más espacio al gráfico principal; leyendas compactas
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

                # ===== Serie principal → normalizar a % =====
                line1 = None

                if is_electric:
                    soc_series = (dfB_sel[["interval", "value"]]
                                .set_index("interval")["value"]
                                .reindex(self.intervals)
                                .ffill().bfill().fillna(0.0))
                    soc_ini = float(soc_series.iloc[0]) if len(soc_series) else 0.0
                    y_raw = np.array([soc_ini] + soc_series.tolist(), dtype=float)
                    max_ref = float(np.nanmax(y_raw)) if y_raw.size else 1.0
                    max_ref = max(max_ref, 1e-6)
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
                    max_ref = float(np.nanmax(y_raw)) if y_raw.size else 1.0
                    max_ref = max(max_ref, 1e-6)
                    y_pct = (y_raw / max_ref) * 100.0
                    x_hrs = np.array([0] + [t * delta_t for t in self.intervals], dtype=float)

                    line1, = ax1.plot(x_hrs, y_pct, color='#4169E1', lw=2.4, label='Fuel')
                    y_label = 'Fuel Level [%]'

                ax1.set_ylabel(y_label, fontsize=22, labelpad=10)

                # ===== Precio (0..0.30; ticks cada 0.05) =====
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

                # ===== X: 0..24, ticks cada 4h desde 08:00, sin márgenes =====
                ax1.set_xlabel('Time', fontsize=22, labelpad=10)
                ax1.set_xlim(0, 24)
                ax1.margins(x=0.0)
                tick_pos_intervals, tick_lbls = _time_ticks_labels_every_4h(self.intervals)
                tick_pos_hours = [((self.intervals.index(t) + 1) * delta_t) for t in tick_pos_intervals]
                ax1.set_xticks(tick_pos_hours)
                ax1.set_xticklabels(tick_lbls, fontsize=20)
                # ===== Y izquierdo: 0–100% + pequeño margen superior; ticks cada 20% =====
                top_pct = 105.0
                ax1.set_ylim(-12.0, top_pct)
                ax1.yaxis.set_major_locator(MultipleLocator(20.0))
                ax1.set_yticks([0, 20, 40, 60, 80, 100])

                # <-- Asegura el tamaño de fuente aquí
                ax1.tick_params(axis='y', labelsize=20, pad=10)

                # ===== Y derecho: 0–0.30, ticks cada 0.05; alinear 0 con eje izquierdo =====
                ax2.set_ylim(0.0, 0.30)
                ax2.yaxis.set_major_locator(MultipleLocator(0.05))
                # Alinear 0: calcular mapeo de 0% (izq) a 0 en der preservando banda negativa
                Lmin, Lmax = ax1.get_ylim()  # e.g., -12 .. 105
                Rmax_fixed = 0.30
                if Lmax != Lmin:
                    r = (0.0 - Lmin) / (Lmax - Lmin)
                    Rmin_align = (r * Rmax_fixed) / (r - 1.0) if 0.0 < r < 1.0 else 0.0
                else:
                    Rmin_align = 0.0
                ax2.set_ylim(Rmin_align, Rmax_fixed)
                ax2.tick_params(axis='y', labelsize=20, pad=10)

                # ===== Barra de estados en banda negativa =====
                colors = {'Travel': '#FF8C00', 'Charging': 'steelblue', 'Parked': 'gray'}
                handles_tasks, labels_tasks = [], []

                Y_filtered = (self.df_Y.query("lhd == @lhd and day == @day and value >= 0.5")
                            if (self.df_Y is not None and not self.df_Y.empty) else pd.DataFrame())
                P_filtered = (self.df_P.query("lhd == @lhd and day == @day")
                            if (self.df_P is not None and not self.df_P.empty) else pd.DataFrame())

                bottom_band = -12.0
                y_min, y_max = bottom_band, bottom_band * 0.01


                                # ===== Barra de estados en banda negativa (CONTINUA) =====
                colors = {'Travel': 'gold', 'Charging': 'blue', 'Parked': 'gray'}
                handles_tasks, labels_tasks = [], []

                Y_filtered = (
                    self.df_Y.query("lhd == @lhd and day == @day and value >= 0.5")
                    if (self.df_Y is not None and not self.df_Y.empty)
                    else pd.DataFrame()
                )

                P_filtered = (
                    self.df_P.query("lhd == @lhd and day == @day")
                    if (self.df_P is not None and not self.df_P.empty)
                    else pd.DataFrame()
                )

                bottom_band = -12.0
                y_min, y_max = bottom_band, bottom_band * 0.01

                # ---------- Determinar estado por intervalo ----------
                states = []

                for t in self.intervals:
                    is_traveling = False
                    if not Y_filtered.empty and 'interval' in Y_filtered.columns:
                        is_traveling = not Y_filtered.query("interval == @t").empty

                    is_charging = False
                    if (
                        is_electric
                        and not P_filtered.empty
                        and {'interval', 'value'}.issubset(P_filtered.columns)
                    ):
                        is_charging = not P_filtered.query(
                            "interval == @t and value > 1"
                        ).empty

                    if is_traveling:
                        states.append('Travel')
                    elif is_charging:
                        states.append('Charging')
                    else:
                        states.append('Parked')

                # ---------- Agrupar tramos consecutivos ----------
                segments = []
                start_idx = 0
                current_state = states[0]

                for i in range(1, len(states)):
                    if states[i] != current_state:
                        segments.append((start_idx, i, current_state))
                        start_idx = i
                        current_state = states[i]

                segments.append((start_idx, len(states), current_state))

                # ---------- Dibujar bloques continuos ----------
                for start, end, state in segments:
                    x_start = start * delta_t
                    x_end = end * delta_t

                    h = ax1.fill_between(
                        [x_start, x_end],
                        y_min,
                        y_max,
                        color=colors[state],
                        label=state if state not in labels_tasks else ""
                    )

                    if state not in labels_tasks:
                        handles_tasks.append(h)
                        labels_tasks.append(state)


                # ===== Sin grids =====
                ax1.grid(False); ax2.grid(False)

                # ===== Título y leyendas (tamaños y bordes) =====
                season = _season_for_day(int(day))
                

                
                month = self._rep_day_label(day)
                ax_title.text(
                    0.5, 0.75,
                    f"LHD {lhd} – {month} ({season})",
                    ha="center", va="center", fontsize=32
                )


                # Leyenda de líneas
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

                # Leyenda de estados (título con mismo tamaño que labels)
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
                        # subir font size del título al mismo tamaño que los labels
                        leg2.set_title('Task States')
                        leg2.get_title().set_fontsize(18)

                # ===== Guardar =====
                fig_path = os.path.join(self.plot_dir, f'SoC_vs_price_LHD-{lhd}_day-{day}.png')
                fig.savefig(fig_path, dpi=150, bbox_inches='tight')
                plt.close(fig)




    def plot_emissions_profiles_for_optimized_day(self):

        # Verificación de datos
        have_elec = (self.params.emissions_electric is not None and
                    not self.params.emissions_electric.empty)
        if not have_elec:
            print("ℹ️ No hay perfiles de emisiones eléctricas. Omitiendo 'Emission Profile'.")
            return

        # Helpers locales (mismos criterios que el gráfico anterior)
        def _season_for_day(d: int) -> str:
            if 1 <= d <= 90:   return "Winter"
            if 91 <= d <= 180: return "Spring"
            if 181 <= d <= 270:return "Summer"
            return "Autumn"

        def _time_labels_every_4h(intervals: list[int]) -> tuple[list[int], list[str]]:
            if not intervals:
                return [], []
            step = 8  # 4h con Δt=0.5h
            tick_pos, tick_lbl = [], []
            for k, t in enumerate(intervals, start=1):
                if (k - 1) % step == 0:
                    total_min = 8*60 + (k-1)*30  # arranca 08:00
                    hh = (total_min // 60) % 24
                    mm = total_min % 60
                    tick_pos.append(t)
                    tick_lbl.append(f"{hh:02d}:{mm:02d}")
            return tick_pos, tick_lbl

        import matplotlib.gridspec as gridspec

        for d in self.days:
            season = _season_for_day(int(d))

            # Serie por día (electric only)
            e_day = (self.params.emissions_electric
                    .query("day == @d")[["interval", "emission_rate"]]
                    .set_index("interval")
                    .reindex(self.intervals)["emission_rate"]
                    .ffill().bfill().fillna(0.0))

            if e_day.isna().all() or (e_day == 0).all():
                # Si no hay datos útiles ese día, seguimos
                continue

            # Layout con espacio exclusivo para título + leyenda
            fig = plt.figure(figsize=(14, 8))
            gs = gridspec.GridSpec(2, 1, height_ratios=[0.20, 0.80], hspace=0.08)
            ax_top = fig.add_subplot(gs[0])   # título + leyenda
            ax = fig.add_subplot(gs[1])       # gráfico principal

            # Curva principal (eléctrica)
            ax.plot(self.intervals, e_day.values, linewidth=3.0,
                    label="Electric Emissions", color="C2")

            # Estilo de ejes
            ax.set_ylim(0.0, 0.5)                       # Y fijo
            ax.set_xlim(self.intervals[0], self.intervals[-1])
            ax.margins(x=0.0)                           # sin espacios en blanco laterales
            ax.grid(False)                               # sin grids

            # Etiquetas y fuentes (mismo set que el gráfico anterior)
            ax.set_xlabel("Time", fontsize=22, labelpad=10)
            ax.set_ylabel("Emission Rate [kgCO2/kWh]", fontsize=22, labelpad=10)

            # Ticks y pads
            ticks, labels = _time_labels_every_4h(self.intervals)
            ax.set_xticks(ticks)
            ax.set_xticklabels(labels)
            ax.tick_params(axis="x", labelsize=20, pad=8)
            ax.tick_params(axis="y", labelsize=20, pad=20)

            # Título (estación, sin negrita) y grande
            ax_top.set_axis_off()
            
            month = self._rep_day_label(d)
            ax_top.text(
                0.5, 0.78,
                f"{month} – Electric Emission Profile ({season})",
                ha="center", va="center", fontsize=32
            )
            

          
            # Leyenda debajo del título, sin unidades en el label, con borde gris claro
            handles, labels_ = ax.get_legend_handles_labels()
            leg = ax_top.legend(handles, labels_, loc="center", ncols=1, fontsize=18,
                                frameon=True, fancybox=False, framealpha=1.0,
                                bbox_to_anchor=(0.5, 0.18))
            leg.get_frame().set_edgecolor("#cccccc")
            leg.get_frame().set_linewidth(1.2)
            leg.get_frame().set_facecolor("white")

            # Guardar            
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

    def create_all_plots(self):
        self.plot_charge_power_vs_price()
        self.plot_node_extraction_vs_demand()
        self.plot_lhd_costs_bars()
        self.plot_lhd_soc_vs_price_and_states()
        self.plot_emissions_profiles_for_optimized_day()
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