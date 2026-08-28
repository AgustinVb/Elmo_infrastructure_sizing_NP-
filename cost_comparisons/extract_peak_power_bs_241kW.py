#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrae la potencia peak (max P_red[y,d,t]) por año, escenario
battery-swapping 241kW. Correr con la rama battery_swapping_multiano
checked out.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT = REPO_ROOT / "output" / "Monolithic_241kW_2dias_mccormick"


def max_p_red_by_year(p_red_path: Path) -> dict:
    with open(p_red_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    years = data.get("y", data)
    result = {}
    for y, y_node in years.items():
        days = y_node.get("d", {}) if isinstance(y_node, dict) else {}
        mx = 0.0
        for _, d_node in days.items():
            intervals = d_node.get("t", {}) if isinstance(d_node, dict) else {}
            for _, val in intervals.items():
                if val is not None and float(val) > mx:
                    mx = float(val)
        result[y] = mx
    return result


def main():
    p_red_path = ROOT / "P_red.json"
    peak_by_year = max_p_red_by_year(p_red_path)

    result = {
        "scenario": "battery_swapping_241kW",
        "root": str(ROOT),
        "peak_power_kw_by_year": peak_by_year,
    }

    out_path = Path(__file__).resolve().parent / "bs_241kW_peak_power.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nGuardado en {out_path}")


if __name__ == "__main__":
    main()
