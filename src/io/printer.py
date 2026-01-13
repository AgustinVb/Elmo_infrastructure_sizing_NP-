# printer.py
# -*- coding: utf-8 -*-
"""
Printer SIN GRÁFICOS para ELMO-UG.

Salidas:
1) Un JSON por CADA VARIABLE del modelo, con anidación forzada por variable.
   - Binarias: se omiten entradas con 0.
2) Un JSON con TODOS los PARÁMETROS del modelo (parameters.json).
3) Copia/guarda el LOG de Gurobi (gurobi.log) si existe.
4) Un TXT (summary.txt) con: tiempo, COSTO, PRODUCCIÓN, gap, etc.
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
    """Inserta value en dict anidado usando la secuencia de keys."""
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
    m = _BRACKET_RE.match(var_name)
    if not m:
        return var_name, []
    base = m.group("base")
    raw = m.group("idx")
    parts = [p.strip() for p in raw.split(",")] if raw else []
    return base, parts

def _parse_index_token(tok: str) -> Tuple[Optional[str], str]:
    if "=" in tok:
        k, v = tok.split("=", 1)
        return k.strip(), v.strip()
    return None, tok.strip()

def _normalize_axis_name(name: str) -> str:
    kk = str(name).lower()
    if kk in ("day", "d"):
        return "d"
    if kk in ("interval", "t", "time", "slot"):
        return "t"
    return name

def _reorder_named_pairs(named_pairs: List[Tuple[str, str]],
                         preferred_order: Optional[List[str]] = None) -> List[Tuple[str, str]]:
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
    parsed = [_parse_index_token(t) for t in tokens]

    if all(n is not None for n, _ in parsed):
        named = [(n, v) for (n, v) in parsed]
        return _reorder_named_pairs(named, preferred_order)

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
    def __init__(self, opt_model, path: str, time_series, mine_system):
        self.opt_model = opt_model
        self.model = opt_model.model
        self.output_mgr = getattr(opt_model, 'output_manager', None)
        self.path = path
        self.time_series = time_series
        self.mine_system = mine_system

        _ensure_dir(self.path)

        self.var_index_schema: Dict[str, List[str]] = {
            "Z":        ["d", "t", "i"],
            "Z_charge": ["k","d", "t", "i"],
            "Y":        ["d", "t", "i", "j"],
            "P":        ["d", "t", "i"],
            "B":        ["d", "t", "i"],
            "N_chargers": ["k"],
            "X":         ["k"],
            "StartCharge": ["k", "i", "d", "t"],
            "EndCharge":   ["k", "i", "d", "t"],
        }
        self.var_axis_order: Dict[str, List[str]] = {
            "Z":        ["d", "t", "i"],
            "Z_charge": ["k","d", "t", "i"],
            "Y":        ["d", "t", "i", "j"],
            "P":        ["d", "t", "i"],
            "B":        ["d", "t", "i"],
            "N_chargers": ["k"],
            "X":         ["k"],
            "StartCharge": ["d", "t", "i", "k"],
            "EndCharge":   ["d", "t", "i", "k"],
        }

        self.float_tol: float = 1e-9

    # ----------- (1) VARIABLES → JSON anidados por variable -----------

    def _export_single_variable_json(self, var_comp: Var) -> None:
        base_name = str(var_comp.name)
        is_binary = _is_binary_var_component(var_comp)

        tree: Dict[str, Any] = {}
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

            if abs(xv) < self.float_tol:
                xv = 0.0

            if is_binary and abs(xv) < self.float_tol:
                continue

            vname = vd.name
            base, tokens = _split_base_and_indices(vname)
            pairs = _infer_pairs_from_tokens(base, tokens, schema, preferred_order=preferred)

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
        out_path = os.path.join(self.path, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    # ----------- (3) LOG de Gurobi -----------

    def save_gurobi_log(self, filename: str = "gurobi.log") -> None:
        dst = os.path.join(self.path, filename)
        candidate = getattr(self.opt_model, "gurobi_log_path", None)
        if candidate and os.path.exists(candidate):
            if os.path.abspath(candidate) != os.path.abspath(dst):
                _ensure_dir(os.path.dirname(dst))
                shutil.copy2(candidate, dst)
            return

        if os.path.exists(dst):
            return

        cwd_candidate = os.path.join(os.getcwd(), filename)
        if os.path.exists(cwd_candidate):
            _ensure_dir(os.path.dirname(dst))
            shutil.copy2(cwd_candidate, dst)
            return

        legacy = os.path.join(os.getcwd(), "ELMO_log.txt")
        if os.path.exists(legacy):
            shutil.copy2(legacy, dst)
            return

        print(f"⚠️ No se encontró '{filename}'.")

    # ----------- (4) SUMMARY TXT (MODIFICADO) -----------

    def write_summary_txt(self, filename: str = "summary.txt") -> None:
        """
        Escribe: Costo total, Producción, Tiempos, MIP gap, etc.
        """
        lines: List[str] = []
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"Generated: {now}")
        lines.append("-" * 30)

        # 1. RESULTADOS PRINCIPALES (Costo y Producción)
        # Se buscan los atributos en opt_model
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

        lines.append("-" * 30)

        # 2. TIEMPOS
        t_form = getattr(self.opt_model, "time_formulation", None)
        t_solve = getattr(self.opt_model, "time_total", None)

        if t_solve is not None:
            lines.append(f"Solve time [s]: {float(t_solve):.3f}")
        else:
            lines.append("Solve time [s]: (not provided)")
        
        if t_form is not None:
            lines.append(f"Formulation time [s]: {float(t_form):.3f}")

        # 3. ESTADÍSTICAS DEL MODELO
        try:
            nvars = _count_vars(self.model)
            lines.append(f"Variables: {int(nvars)}")
        except:
            pass
            
        try:
            ncons = _count_constraints(self.model)
            lines.append(f"Constraints: {int(ncons)}")
        except:
            pass

        # 4. MIP GAP
        mip_gap = getattr(self.opt_model, "mip_gap", None)
        if mip_gap is not None:
            try:
                lines.append(f"MIP Gap: {float(mip_gap):.6f}")
            except:
                pass

        out_path = os.path.join(self.path, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ----------- ORQUESTADOR -----------

    def create_all_plots(self):
        self.write_variables_jsons()
        self.write_parameters_json()
        self.save_gurobi_log()
        self.write_summary_txt()
        print(f"✔ JSONs, log y summary guardados en '{self.path}'.")

    def run_all(self, write_params: bool = True, log_filename: str = "gurobi.log", summary_filename: str = "summary.txt") -> None:
        self.write_variables_jsons()
        if write_params:
            self.write_parameters_json()
        self.save_gurobi_log(filename=log_filename)
        self.write_summary_txt(filename=summary_filename)