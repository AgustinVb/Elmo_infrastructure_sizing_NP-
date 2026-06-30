"""
Combina los outputs JSON de varios macrobloques (ej. output/.../320kW_Desc/station_1,
station_2, station_3) en una sola carpeta, para poder graficarlos juntos con
json_plotter.py / batch_plotter.py.

Reglas de combinacion:
- parameters.json: merge "primero gana" en colisiones de hoja (los parametros
  fisicos compartidos como delta_t, p_peak, costo_electricidad son iguales en
  los 3 macrobloques, asi que NO se suman; los parametros indexados por
  estacion (station_cost_k, etc.) se unen sin colision porque cada estacion
  tiene su propio nombre).
- Cualquier otra variable (*.json): merge con SUMA en colisiones de hoja.
  Las variables por LHD/estacion (Y, Z, Z_swap, N_chargers, etc.) no colisionan
  entre macrobloques (cada uno tiene LHDs/estaciones distintas), asi que el
  merge es una union simple. La variable que SI colisiona en cada hoja
  (mismo dia, mismo intervalo) es P_red -> ahi se suma la demanda de red de
  las 3 estaciones, que es justamente lo que se pidio.

Despues de combinar, corre JSONPlotter sobre la carpeta combinada para generar
los graficos en <output_dir>/plots/.
"""
import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
import sys
import importlib.util

_STATION_DAY_RE = re.compile(r"^(.+)_d(\d+)$")


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def merge_sum(a, b):
    """Merge profundo: en colision de hoja numerica, suma. En colision de
    hoja no numerica (str, etc.), conserva el valor de `a`."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            if k in out:
                out[k] = merge_sum(out[k], v)
            else:
                out[k] = v
        return out
    if _is_number(a) and _is_number(b):
        return a + b
    return a


def merge_first(a, b):
    """Merge profundo: en colision de hoja, conserva el valor de `a` (no suma)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            if k in out:
                out[k] = merge_first(out[k], v)
            else:
                out[k] = v
        return out
    return a


def find_macrobloque_subfolders(root: Path):
    """Subcarpetas con su propio parameters.json, excluyendo las intermedias
    de Fase 1 (sufijo _stage1 de --parallel_days), que no son resultados finales."""
    subfolders = []
    for child in sorted(root.iterdir()):
        if child.name == "combined" or child.name.endswith("_stage1"):
            continue
        if child.is_dir() and (child / "parameters.json").exists():
            subfolders.append(child)
    return subfolders


def parse_station_day(name: str) -> Optional[tuple]:
    """Si la carpeta sigue el patron <estacion>_d<dia> (salida de
    run_macrobloques_decomposicion.py --parallel_days), devuelve (estacion, dia)."""
    m = _STATION_DAY_RE.match(name)
    if not m:
        return None
    return m.group(1), m.group(2)


def group_by_day(subfolders: List[Path]) -> Optional[Dict[str, List[Path]]]:
    """Agrupa subcarpetas <estacion>_d<dia> por dia. Devuelve None si alguna
    subcarpeta no sigue ese patron (carpeta "plana" station_1/2/3, sin dia)."""
    groups: Dict[str, List[Path]] = {}
    for sub in subfolders:
        parsed = parse_station_day(sub.name)
        if parsed is None:
            return None
        _, day = parsed
        groups.setdefault(day, []).append(sub)
    return groups


def combine_many(subfolders: List[Path], output_dir: Path):
    """Combina una lista explicita de subcarpetas (cada una con su propio
    parameters.json) en output_dir, sumando variables y conservando el primer
    valor en colisiones de parametros compartidos."""
    output_dir.mkdir(parents=True, exist_ok=True)

    all_names = set()
    for sub in subfolders:
        for f in sub.glob("*.json"):
            all_names.add(f.name)

    for name in sorted(all_names):
        merged = None
        merge_fn = merge_first if name == "parameters.json" else merge_sum
        for sub in subfolders:
            fpath = sub / name
            if not fpath.exists():
                continue
            with open(fpath, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except Exception:
                    continue
            merged = data if merged is None else merge_fn(merged, data)

        if merged is not None:
            with open(output_dir / name, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)

    return output_dir


def combine(root: Path, output_dir: Path):
    subfolders = find_macrobloque_subfolders(root)
    if not subfolders:
        raise SystemExit(f"No se encontraron subcarpetas con parameters.json en {root}")

    print(f"Macrobloques detectados: {[s.name for s in subfolders]}")
    combine_many(subfolders, output_dir)
    for f in sorted(output_dir.glob("*.json")):
        print(f"  combinado: {f.name}")
    return output_dir


def _import_plotter(plotter_path: Path):
    spec = importlib.util.spec_from_file_location("json_plotter", str(plotter_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader, "No se pudo cargar json_plotter.py"
    spec.loader.exec_module(mod)
    return mod


def _plot_folder(output_dir: Path, mode: str, energy_price_scale: float):
    plotter_mod = _import_plotter(Path(__file__).resolve().parent / "json_plotter.py")
    JSONPlotter = getattr(plotter_mod, "JSONPlotter", None)
    if JSONPlotter is None:
        print("No se encontro la clase JSONPlotter en json_plotter.py")
        sys.exit(1)

    plotter = JSONPlotter(str(output_dir), energy_price_scale=energy_price_scale, mode=mode)
    plotter.create_all_plots()
    print(f"Listo: {output_dir / 'plots'}")


def main():
    ap = argparse.ArgumentParser(description="Combina (sumando potencias) los outputs de varios macrobloques en una sola carpeta y grafica.")
    ap.add_argument("--root_dir", required=True, help="Carpeta con subcarpetas station_1, station_2, station_3, etc. "
                                                        "o, si vienen de --parallel_days, station_1_d1, station_2_d1, ...")
    ap.add_argument("--output_dir", default=None, help="Carpeta de salida combinada (default: <root_dir>/combined)")
    ap.add_argument("--mode", choices=["DCH", "DET"], default="DCH")
    ap.add_argument("--costo_electricidad_scale", "--energy_price_scale", dest="energy_price_scale", type=float, default=1.0)
    ap.add_argument("--no_plot", action="store_true", help="Solo combinar JSONs, no generar graficos")
    args = ap.parse_args()

    root = Path(args.root_dir).expanduser()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Carpeta no valida: {root}")

    subfolders = find_macrobloque_subfolders(root)
    if not subfolders:
        raise SystemExit(f"No se encontraron subcarpetas con parameters.json en {root}")

    day_groups = group_by_day(subfolders)

    if day_groups is None:
        # Carpeta plana (station_1, station_2, station_3): un solo combinado.
        print(f"Macrobloques detectados (sin separar por dia): {[s.name for s in subfolders]}")
        output_dir = Path(args.output_dir).expanduser() if args.output_dir else (root / "combined")
        combine_many(subfolders, output_dir)
        print(f"  combinado en: {output_dir}")
        if not args.no_plot:
            _plot_folder(output_dir, args.mode, args.energy_price_scale)
        return

    # Salida de --parallel_days (<estacion>_d<dia>): agrupar primero por dia
    # (sumar P_red solo entre estaciones del MISMO dia) y combinar cada grupo
    # por separado, para no sumar potencias de dias distintos sin sentido fisico.
    base_output = Path(args.output_dir).expanduser() if args.output_dir else (root / "combined")
    print(f"Detectados {len(day_groups)} dias: {sorted(day_groups.keys(), key=int)}")
    for day, day_subfolders in sorted(day_groups.items(), key=lambda kv: int(kv[0])):
        print(f"  dia {day}: {[s.name for s in day_subfolders]}")
        day_output_dir = base_output / f"day_{day}"
        combine_many(day_subfolders, day_output_dir)
        print(f"    combinado en: {day_output_dir}")
        if not args.no_plot:
            _plot_folder(day_output_dir, args.mode, args.energy_price_scale)


if __name__ == "__main__":
    main()
