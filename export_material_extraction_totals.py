"""
Exporta, para cada escenario que usa descomposicion por macrobloques, un CSV
con el material total extraido (variable M) por punto de extraccion (nodo),
agregando LHDs e intervalos de un dia representativo (la extraccion es la
misma en todos los dias/meses representativos, asi que basta con uno).

Reusa:
  - combine_macrobloques_for_plot.py: para generar combined/day_X/ en los
    escenarios que aun no lo tienen.
  - json_plotter.JSONPlotter: para cargar M.json exactamente igual que lo hace
    plot_material_extraction_by_point (misma deteccion de columna de nodo),
    garantizando que el CSV sea consistente con el grafico
    Material_Extraction_M_AllNodes_<Mes>.png.

Salida: <escenario>/combined/Material_Extraction_Totals.csv
(CSV con sep=";" y decimal="," para Excel en configuracion regional español).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path("output/DCH_taller_julio")
MODE = "DCH"

_DAY_RE = re.compile(r"^day_(\d+)$")
_STATION_RE = re.compile(r"^station_\d+(_d\d+)?(_stage1)?$")


def _import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_scenario_root(d: Path) -> bool:
    """Carpeta desacoplada (por macrobloques/dia): no tiene parameters.json
    propio, y sus hijos directos son subcarpetas "station_<n>" o
    "station_<n>_d<dia>" (con su propio parameters.json) — el patron real de
    salida de run_macrobloques_decomposicion.py. No basta con "cualquier hijo
    con parameters.json": carpetas contenedoras como Costo_fijo/Costo_variable
    tambien tienen hijos con parameters.json (escenarios monoliticos sueltos,
    p.ej. Swap_320kW) sin ser ellas mismas un escenario descompuesto."""
    if d.name == "combined" or d.name.endswith("_stage1") or _STATION_RE.match(d.name):
        return False
    if (d / "parameters.json").exists():
        return False
    return any(
        c.is_dir() and _STATION_RE.match(c.name) and (c / "parameters.json").exists()
        for c in d.iterdir()
    )


def find_descomp_scenarios(root: Path) -> list[Path]:
    """Todas las carpetas de escenario desacopladas (por descomposicion de
    macrobloques), en cualquier profundidad bajo root."""
    return sorted(d for d in root.rglob("*") if d.is_dir() and _is_scenario_root(d))


def ensure_combined(combine_mod, scenario: Path) -> Path:
    combined_dir = scenario / "combined"
    if combined_dir.exists():
        return combined_dir

    print(f"  -> combinando estaciones ({scenario.name}) ...")
    subfolders = combine_mod.find_macrobloque_subfolders(scenario)
    if not subfolders:
        raise RuntimeError(f"No se encontraron subcarpetas con parameters.json en {scenario}")

    day_groups = combine_mod.group_by_day(subfolders)
    if day_groups is None:
        combine_mod.combine_many(subfolders, combined_dir)
    else:
        for day, day_subfolders in sorted(day_groups.items(), key=lambda kv: int(kv[0])):
            combine_mod.combine_many(day_subfolders, combined_dir / f"day_{day}")

    return combined_dir


def pick_representative_day_dir(combined_dir: Path) -> Path:
    day_dirs = []
    for c in combined_dir.iterdir():
        if not c.is_dir():
            continue
        m = _DAY_RE.match(c.name)
        if m and (c / "parameters.json").exists():
            day_dirs.append((int(m.group(1)), c))

    if day_dirs:
        day_dirs.sort(key=lambda t: t[0])
        return day_dirs[0][1]

    # Carpeta "plana" (sin subcarpetas day_X): usar combined/ directamente.
    if (combined_dir / "parameters.json").exists():
        return combined_dir

    raise RuntimeError(f"No se encontro un dia representativo valido en {combined_dir}")


def detect_node_col(df_M) -> str:
    """Misma logica que json_plotter.plot_material_extraction_by_point."""
    standard_cols = {"lhd", "day", "interval", "value", "station"}
    cols = list(df_M.columns)
    if "node" in cols:
        return "node"
    if "j" in cols:
        return "j"
    node_candidates = [c for c in cols if c not in standard_cols]
    if node_candidates:
        return node_candidates[0]
    if len(cols) > 1:
        return cols[1]
    raise RuntimeError("No se pudo determinar la columna de nodo en M.json")


def main():
    plotter_mod = _import_module("json_plotter", Path(__file__).resolve().parent / "json_plotter.py")
    combine_mod = _import_module("combine_macrobloques_for_plot", Path(__file__).resolve().parent / "combine_macrobloques_for_plot.py")

    scenarios = find_descomp_scenarios(ROOT)
    print(f"Escenarios desacoplados encontrados: {len(scenarios)}")
    for s in scenarios:
        print(f"  - {s.relative_to(ROOT)}")

    ok, failed = 0, 0
    for scenario in scenarios:
        print(f"\n=== {scenario.relative_to(ROOT)} ===")
        try:
            combined_dir = ensure_combined(combine_mod, scenario)
            day_dir = pick_representative_day_dir(combined_dir)

            plotter = plotter_mod.JSONPlotter(str(day_dir), mode=MODE)
            if plotter.df_M is None or plotter.df_M.empty:
                print(f"  ⚠️  No hay datos en M.json para {day_dir.name}. Omitiendo.")
                failed += 1
                continue

            node_col = detect_node_col(plotter.df_M)
            extr = (plotter.df_M.groupby(node_col)["value"]
                    .sum()
                    .rename("total_extracted_tons")
                    .sort_values(ascending=False)
                    .reset_index()
                    .rename(columns={node_col: "extraction_point"}))

            total_tons = extr["total_extracted_tons"].sum()

            out_path = combined_dir / "Material_Extraction_Totals.csv"
            # "." para decimales y "," para miles (formato US). pandas
            # to_csv no soporta thousands= al escribir, asi que se formatea
            # la columna como texto antes de guardar; sep=";" evita ambiguedad
            # con la coma de miles dentro de los numeros.
            extr_out = extr.copy()
            extr_out["total_extracted_tons"] = extr_out["total_extracted_tons"].map(lambda v: f"{v:,.2f}")
            extr_out.to_csv(out_path, index=False, sep=";")

            print(f"  dia representativo: {day_dir.name}")
            print(f"  nodos: {len(extr)}  |  total extraido: {total_tons:.1f} t")
            print(f"  ✅ {out_path}")
            ok += 1
        except Exception as e:
            print(f"  ❌ {e}")
            failed += 1

    print(f"\nListo. OK={ok} Fallidos={failed}")


if __name__ == "__main__":
    main()
