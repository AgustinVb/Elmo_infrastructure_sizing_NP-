# printer.py
# -*- coding: utf-8 -*-
"""
Printer SIN GRÁFICOS para ELMO-UG.

Salidas:
1) Un JSON por CADA VARIABLE del modelo, con anidación forzada por variable.
   - Binarias: se omiten entradas con 0.
2) Un JSON con TODOS los PARÁMETROS del modelo (parameters.json).
3) Copia/guarda el LOG de Gurobi (gurobi.log) si existe.
4) Un TXT (summary.txt) con: Costo, Producción, Tiempos, MIP gap, etc.

Firma compatible con tu flujo:
    Printer(opt_model, path, time_series, mine_system)
y el orquestador:
    printer.create_all_plots()
"""

import os
import re
import json
import time
import shutil
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
from pyomo.environ import Var, Param, Constraint, value
# (No usamos pandas)

# ------------------------ utilidades genéricas ------------------------

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def _coerce_json_val(x: Any) -> Any:
    """Convierte objetos a tipos JSON-serializables."""
    if x is None:
        return None
    if isinstance(x, (str, int, float, bool)):
        return x
    try:
        import numpy as np
        if isinstance(x, np.integer):
            return int(x)
        if isinstance(x, np.floating):
            return float(x)
        if isinstance(x, np.bool_):
            return bool(x)
    except Exception:
        pass
    if isinstance(x, (list, tuple)):
        return [_coerce_json_val(t) for t in x]
    if isinstance(x, dict):
        return {str(k): _coerce_json_val(v) for k, v in x.items()}
    if hasattr(x, "__dict__"):
        return {k: _coerce_json_val(v) for k, v in x.__dict__.items() if not k.startswith("_")}
    return str(x)

def _insert_nested(d: dict, keys: List[Union[str, int]], value_: Any) -> None:
    """Inserta value en dict anidado usando la secuencia de keys (todas se guardan como str)."""
    if not keys:
        raise ValueError("No se puede insertar un valor con ruta de claves vacía")
    cur = d
    for k in keys[:-1]:
        ks = str(k)
        if ks not in cur or not isinstance(cur[ks], dict):
            cur[ks] = {}
        cur = cur[ks]
    cur[str(keys[-1])] = value_

# ------------------------ parsing de nombres e índices ------------------------

_BRACKET_RE = re.compile(r"^(?P<base>[^\[]+)\[(?P<idx>.*)\]$")

def _split_base_and_indices(var_name: str) -> Tuple[str, List[str]]:
    """
    Soporta:
      base[i,j,k]
      base[day=1,t=2,i=A]
    Devuelve (base, [tokens crudos]).
    """
    m = _BRACKET_RE.match(var_name)
    if not m:
        return var_name, []
    base = m.group("base")
    raw = m.group("idx")
    parts = [p.strip() for p in raw.split(",")] if raw else []
    return base, parts

def _parse_index_token(tok: str) -> Tuple[Optional[str], str]:
    """'day=183' -> ('day','183'); '42' -> (None,'42'); 'LHD1' -> (None,'LHD1')."""
    if "=" in tok:
        k, v = tok.split("=", 1)
        return k.strip(), v.strip()
    return None, tok.strip()

def _normalize_axis_name(name: str) -> str:
    """Normaliza alias de ejes: 'day'→'d'; 'interval'/'time'→'t'."""
    kk = str(name).lower()
    if kk in ("day", "d"):
        return "d"
    if kk in ("interval", "t", "time", "slot"):
        return "t"
    return name

def _reorder_named_pairs(named_pairs: List[Tuple[str, str]],
                         preferred_order: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """
    Reordena pares (k,v) según un orden preferido si se entrega.
    Si no hay orden preferido, aplica fallback: 'd' primero, 't' segundo, resto igual.
    """
    def norm(a: str) -> str:
        return _normalize_axis_name(a)

    normed = [(norm(k), v) for (k, v) in named_pairs]

    if preferred_order:
        order = [norm(x) for x in preferred_order]
        rank = {name: i for i, name in enumerate(order)}
        head, tail = [], []
        for kv in normed:
            if kv[0] in rank:
                head.append(kv)
            else:
                tail.append(kv)
        head.sort(key=lambda kv: rank[kv[0]])
        return head + tail

    # fallback clásico: d -> t -> otros
    d_items = [(k, v) for (k, v) in normed if k == "d"]
    t_items = [(k, v) for (k, v) in normed if k == "t"]
    others = [(k, v) for (k, v) in normed if k not in ("d", "t")]
    return d_items + t_items + others

def _infer_pairs_from_tokens(base: str,
                             tokens: List[str],
                             var_index_schema: Optional[Dict[str, List[str]]] = None,
                             preferred_order: Optional[List[str]] = None
                             ) -> List[Tuple[str, str]]:
    """
    Devuelve lista de (nombre_idx, valor_idx) asignando nombres posicionales si faltan
    y reordenando según preferred_order cuando se provee.
    """
    parsed = [_parse_index_token(t) for t in tokens]

    if all(n is not None for n, _ in parsed):
        named = [(n, v) for (n, v) in parsed]
        return _reorder_named_pairs(named, preferred_order)

    # Alguno sin nombre → asignar por schema si está
    positional_names: List[str] = []
    if var_index_schema and base in var_index_schema:
        positional_names = list(var_index_schema[base])
    if not positional_names:
        positional_names = [f"_{i+1}" for i in range(len(parsed))]

    named: List[Tuple[str, str]] = []
    for i, (n, v) in enumerate(parsed):
        if n is not None:
            named.append((n, v))
        else:
            name = positional_names[i] if i < len(positional_names) else f"_{i+1}"
            named.append((name, v))
    return _reorder_named_pairs(named, preferred_order)

# ------------------------ helpers Pyomo ------------------------

def _is_binary_var_component(v: Var) -> bool:
    """Heurística para saber si un componente Var es binario."""
    try:
        for vd in v.values():
            if vd.is_binary():
                return True
        for vd in v.values():
            lb = vd.lb if vd.lb is not None else -np.inf
            ub = vd.ub if vd.ub is not None else np.inf
            if lb >= 0 and ub <= 1 and (vd.is_integer() or vd.is_binary()):
                return True
    except Exception:
        pass
    return False

def _iter_all_vardata(model) -> Iterable:
    for comp in model.component_objects(Var, active=True):
        for vd in comp.values():
            yield vd

def _count_vars(model) -> int:
    return sum(1 for _ in _iter_all_vardata(model))

def _count_constraints(model) -> int:
    n = 0
    for c in model.component_objects(Constraint, active=True):
        n += sum(1 for _ in c.values())
    return n

# ------------------------ Clase principal ------------------------

class Printer:
    """
    Reemplazo de Printer con exportación JSON/TXT/LOG y sin gráficos.

    __init__(opt_model, path, time_series, mine_system) se mantiene igual.
    """

    def __init__(self, opt_model, path: str, time_series, mine_system):
        self.opt_model = opt_model
        self.model = opt_model.model
        self.output_mgr = getattr(opt_model, 'output_manager', None)
        self.path = path
        self.time_series = time_series
        self.mine_system = mine_system

        _ensure_dir(self.path)

        # ---------- SCHEMA POSICIONAL Y ORDEN DE ANIDACIÓN POR VARIABLE ----------
        # CRITICAL: Order MUST match variable definition in functions.py
        # Variable definitions (functions.py lines ~351-389):
        # Y: (i,j,d,t) via Y_INDEX; Z: (i,d,t); Z_swap: (k,i,d,t); B: (i,d,t); B_s: (b,d,t)
        # S,Sv,X_dch,X_ini,W: (k,d,t); Sv has extra 4th dimension: (k,d,t,t_start)
        # N_chargers,X,N_batteries: (k); M: (b,j,d); F: (j,d); F_seg: (j,d,seg)
        self.var_index_schema: Dict[str, List[str]] = {
            "Z":        ["i", "d", "t"],
            "Z_swap":   ["k", "i", "d", "t"],
            "Y":        ["i", "j", "d", "t"],
            "B":        ["i", "d", "t"],
            "B_s":      ["b", "d", "t"],
            "N_chargers": ["k"],
            "X":        ["k"],
            "N_batteries": ["k"],
            "S":        ["k", "d", "t"],
            "Sv":       ["k", "d", "t", "t_start"],
            "X_dch":    ["k", "d", "t"],
            "X_ini":    ["k", "d", "t"],
            "W":        ["k", "d", "t"],
            "M":        ["b", "j", "d"],
            "F":        ["j", "d"],
            "F_seg":    ["j", "d", "seg"],
        }
        self.var_axis_order: Dict[str, List[str]] = {
            "Z":        ["i", "d", "t"],
            "Z_swap":   ["k", "i", "d", "t"],
            "Y":        ["i", "j", "d", "t"],
            "B":        ["i", "d", "t"],
            "B_s":      ["b", "d", "t"],
            "N_chargers": ["k"],
            "X":        ["k"],
            "N_batteries": ["k"],
            "S":        ["k", "d", "t"],
            "Sv":       ["k", "d", "t", "t_start"],
            "X_dch":    ["k", "d", "t"],
            "X_ini":    ["k", "d", "t"],
            "W":        ["k", "d", "t"],
            "M":        ["b", "j", "d"],
            "F":        ["j", "d"],
            "F_seg":    ["j", "d", "seg"],
        }

        # tolerancia para considerar "casi cero" como cero
        self.float_tol: float = 1e-9

    # ----------- (1) VARIABLES → JSON anidados por variable -----------

    def _export_single_variable_json(self, var_comp: Var) -> None:
        """
        Exporta un componente de variable de Pyomo (Var) a un archivo JSON.
        """
        base_name = str(var_comp.name)
        is_binary = _is_binary_var_component(var_comp)

        if not var_comp.is_indexed():
            vd = next(iter(var_comp.values()), None)
            if vd is None:
                return

            x = vd.value
            if x is None:
                return
            try:
                xv = float(x)
            except Exception:
                return

            try:
                if vd.is_integer() or vd.is_binary():
                    xv = int(round(xv))
                elif abs(xv) < self.float_tol:
                    xv = 0.0
            except Exception:
                if abs(xv) < self.float_tol:
                    xv = 0.0

            if is_binary and abs(float(xv)) < self.float_tol:
                return

            out_path = os.path.join(self.path, f"{base_name}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(_coerce_json_val(xv), f, ensure_ascii=False, indent=2)
            return

        tree: Dict[str, Any] = {}

        # Orden preferido y schema posicional para esta variable (si existen)
        preferred = self.var_axis_order.get(base_name)
        schema = self.var_index_schema

        for vd in var_comp.values():
            x = vd.value
            if x is None:
                continue
            try:
                xv = float(x)
            except Exception:
                continue

            # Si la variable es entera/binaria, redondear al entero más cercano
            try:
                if vd.is_integer() or vd.is_binary():
                    xv = int(round(xv))
                else:
                    # Redondeo pequeño a 0 para continuas
                    if abs(xv) < self.float_tol:
                        xv = 0.0
            except Exception:
                # fallback: aplicar tolerancia sólo
                if abs(xv) < self.float_tol:
                    xv = 0.0

            # Para binarias → omitir 0
            if is_binary and abs(float(xv)) < self.float_tol:
                continue

            vname = vd.name
            base, tokens = _split_base_and_indices(vname)

            pairs = _infer_pairs_from_tokens(
                base, tokens, schema, preferred_order=preferred
            )

            key_path: List[str] = []
            for k, v in pairs:
                key_path.extend([_normalize_axis_name(k), str(v)])

            _insert_nested(tree, key_path, xv)

        out_path = os.path.join(self.path, f"{base_name}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False, indent=2)


    def write_variables_jsons(self) -> None:
        for var_comp in self.model.component_objects(Var, active=True):
            self._export_single_variable_json(var_comp)

    # ----------- (2) PARÁMETROS → parameters.json -----------

    def _param_payload(self, p: Param) -> Any:
        if not p.is_indexed():
            try:
                return _coerce_json_val(value(p))
            except Exception:
                return _coerce_json_val(None)

        tree: Dict[str, Any] = {}
        preferred = self.var_axis_order.get(str(p.name))

        for key, pdata in p.items():
            try:
                val = value(pdata)
            except Exception:
                val = None
            val = _coerce_json_val(val)

            key_tuple = key if isinstance(key, tuple) else (key,)

            pairs: List[Tuple[str, str]] = []
            for i, v in enumerate(key_tuple):
                kname = f"_{i+1}"
                sval = str(v)
                if isinstance(v, str) and '=' in v:
                    k, vv = _parse_index_token(v)
                    if k is not None:
                        kname, sval = k, vv
                pairs.append((kname, sval))

            pairs = _reorder_named_pairs(pairs, preferred_order=preferred)

            key_path: List[str] = []
            for k, v in pairs:
                key_path.extend([_normalize_axis_name(k), str(v)])
            _insert_nested(tree, key_path, val)

        return tree

    def write_parameters_json(self, filename: str = "parameters.json") -> None:
        payload: Dict[str, Any] = {}
        for p in self.model.component_objects(Param, active=True):
            payload[str(p.name)] = self._param_payload(p)

        def _export_set_values(*candidate_names: str) -> List[Any]:
            for name in candidate_names:
                if not hasattr(self.model, name):
                    continue
                try:
                    vals = [_coerce_json_val(v) for v in getattr(self.model, name)]
                    # ordenar numéricamente si aplica
                    try:
                        vals = sorted(vals, key=lambda x: float(x))
                    except Exception:
                        pass
                    return vals
                except Exception:
                    continue
            return []

        # Exportar sets de pausas para trazado en json_plotter
        payload["time_intervals_between_shifts_set"] = _export_set_values("time_intervals_between_shifts_set")
        payload["time_intervals_mid_shift_meal_set"] = _export_set_values(
            "time_intervals_mid_shift_meal_set",
            "time_intervals_meal_set",
        )
        payload["time_intevals_maintenance_set"] = _export_set_values(
            "time_intevals_maintenance_set",
            "time_intervals_maintenance_set",
        )

        out_path = os.path.join(self.path, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    # ----------- (3) LOG de Gurobi -----------

    def save_gurobi_log(self, filename: str = "gurobi.log") -> None:
        """
        Intenta copiar un archivo de log de Gurobi al directorio de salida.
        """
        dst = os.path.join(self.path, filename)

        # 1) Si OptimizationModel almacenó la ruta:
        candidate = getattr(self.opt_model, "gurobi_log_path", None)
        if candidate and os.path.exists(candidate):
            if os.path.abspath(candidate) != os.path.abspath(dst):
                _ensure_dir(os.path.dirname(dst))
                shutil.copy2(candidate, dst)
            return

        # 2) Si ya existe en la carpeta de salida, no hacemos nada
        if os.path.exists(dst):
            return

        # 3) Buscar en cwd
        cwd_candidate = os.path.join(os.getcwd(), filename)
        if os.path.exists(cwd_candidate):
            _ensure_dir(os.path.dirname(dst))
            shutil.copy2(cwd_candidate, dst)
            return

        # 4) Intento adicional: archivo anterior que usabas
        legacy = os.path.join(os.getcwd(), "ELMO_log.txt")
        if os.path.exists(legacy):
            shutil.copy2(legacy, dst)
            return

        print(f"⚠️ No se encontró '{filename}'. ")

    # ----------- (4) SUMMARY TXT (ACTUALIZADO CON KPIs) -----------

    def write_summary_txt(self, filename: str = "summary.txt") -> None:
        """
        Escribe: COSTO, PRODUCCIÓN, tiempo de resolución, MIP gap, etc.
        """
        lines: List[str] = []
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"Generated: {now}")
        lines.append("-" * 35)

        # --- SECCIÓN NUEVA: KPIs principales (Costo y Producción) ---
        # Leemos los atributos que guardaste en opt_model
        cost_val = getattr(self.opt_model, "opt_cost_result", None)
        prod_val = getattr(self.opt_model, "total_production", None)

        if cost_val is not None:
            lines.append(f"Total Cost (Objective): {float(cost_val):,.2f}")
        else:
            lines.append("Total Cost: (not available)")

        if prod_val is not None:
            lines.append(f"Total Production: {float(prod_val):,.2f}")
        else:
            lines.append("Total Production: (not available)")
        
        lines.append("-" * 35)
        # ------------------------------------------------------------

        # tiempos
        t_form = getattr(self.opt_model, "time_formulation", None)
        t_solve = getattr(self.opt_model, "time_total", None)

        if t_form is not None:
            lines.append(f"Formulation time [s]: {float(t_form):.3f}")
        else:
            lines.append("Formulation time [s]: (not provided)")

        if t_solve is not None:
            lines.append(f"Solve time [s]: {float(t_solve):.3f}")
        else:
            lines.append("Solve time [s]: (not provided)")

        # conteos
        try:
            nvars = _count_vars(self.model)
        except Exception:
            nvars = None
        try:
            ncons = _count_constraints(self.model)
        except Exception:
            ncons = None

        lines.append(f"Variables: {int(nvars) if nvars is not None else '(unknown)'}")
        lines.append(f"Constraints: {int(ncons) if ncons is not None else '(unknown)'}")

        # MIP gap
        mip_gap = getattr(self.opt_model, "mip_gap", None)
        if mip_gap is not None:
            try:
                lines.append(f"MIP Gap: {float(mip_gap):.6f}")
            except Exception:
                lines.append("MIP Gap: (unavailable)")
        else:
            lines.append("MIP Gap: (n/a)")

        out_path = os.path.join(self.path, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ----------- ORQUESTADOR (reemplaza create_all_plots) -----------

    def create_all_plots(self):
        """
        Compatibilidad: ejecuta el pipeline de exportación JSON/TXT/LOG (sin gráficos).
        """
        self.write_variables_jsons()
        self.write_parameters_json()
        self.save_gurobi_log()
        self.write_summary_txt()
        print(f"✔ JSON de variables y parámetros, log y summary guardados en '{self.path}'.")

    # ----------- helpers opcionales -----------

    def run_all(self,
                write_params: bool = True,
                log_filename: str = "gurobi.log",
                summary_filename: str = "summary.txt") -> None:
        self.write_variables_jsons()
        if write_params:
            self.write_parameters_json()
        self.save_gurobi_log(filename=log_filename)
        self.write_summary_txt(filename=summary_filename)