# batch_plotter.py
# Recorre una carpeta raíz con subcarpetas de escenarios y ejecuta json_plotter.py en cada una.
# Guarda los gráficos en <escenario>/plots/
#
# Ejemplo (Windows):
#   python batch_plotter.py --root_dir "C:\ruta\al\output" --depth 2 --energy_price_scale 1.0
#
from __future__ import annotations
from pathlib import Path
import argparse
import sys
import importlib.util
from typing import List

def _import_plotter(plotter_path: Path):
    spec = importlib.util.spec_from_file_location("json_plotter", str(plotter_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader, "No se pudo cargar json_plotter.py"
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod

def _looks_like_scenario_dir(d: Path) -> bool:
    if not d.is_dir():
        return False
    # criterio: si tiene parameters.json y al menos un *.json de variables
    if (d / "parameters.json").exists():
        # ¿hay algún otro .json además de parameters.json?
        other_jsons = [p for p in d.glob("*.json") if p.name.lower() != "parameters.json"]
        return len(other_jsons) > 0
    return False

def _find_scenario_dirs(root: Path, depth: int) -> List[Path]:
    scenarios: List[Path] = []
    if _looks_like_scenario_dir(root):
        scenarios.append(root)

    level = 0
    current = [root]
    
    while current and level < depth: 
        nxt = []
        for base in current:
            try:
                for child in base.iterdir():
                    if child.is_dir():
                        if _looks_like_scenario_dir(child):
                            scenarios.append(child)
                        else:
                            nxt.append(child)
            except PermissionError:
                print(f"⚠️ Permiso denegado en: {base}")
                continue
                
        current = nxt
        level += 1
        
    return scenarios

def main():
    ap = argparse.ArgumentParser(description="Batch runner para json_plotter.py sobre subcarpetas de escenarios")
    ap.add_argument("--root_dir", required=True, help="Carpeta 'output' que contiene subcarpetas por escenario")
    ap.add_argument("--depth", type=int, default=2, help="Profundidad máxima a explorar (default: 2)")
    ap.add_argument("--energy_price_scale", type=float, default=1.0, help="Escala para precio marginal")
    ap.add_argument("--mode", choices=["DCH","DET"], default="DCH", help="Modo de detenciones para graficar: DCH (default) o DET")
    args = ap.parse_args()

    root = Path(args.root_dir).expanduser()
    if not root.exists() or not root.is_dir():
        print(f"❌ La carpeta raíz no existe o no es carpeta: {root}")
        sys.exit(1)

    plotter_mod = _import_plotter(Path(__file__).resolve().parent / "json_plotter.py")
    JSONPlotter = getattr(plotter_mod, "JSONPlotter", None)
    if JSONPlotter is None:
        print("❌ No se encontró la clase JSONPlotter en json_plotter.py")
        sys.exit(1)

    scenarios = _find_scenario_dirs(root, args.depth)
    if not scenarios:
        print("⚠️ No se encontraron subcarpetas de escenarios bajo", root)
        sys.exit(0)

    print(f"🔎 Encontrados {len(scenarios)} escenarios:")
    for s in scenarios:
        print(" •", s)

    any_error = False
    for sdir in scenarios:
        print(f"\n📊 Procesando escenario: {sdir}")
        try:
            plotter = JSONPlotter(str(sdir), energy_price_scale=args.energy_price_scale, mode=args.mode)
            plotter.create_all_plots()
            print(f"✅ Listo: {sdir / 'plots'}")
        except Exception as e:
            any_error = True
            print(f"❌ Error en {sdir}: {e}")
            # Mostrar traceback resumido para ayudar al debug
            import traceback
            traceback.print_exc(limit=2)

    if any_error:
        sys.exit(2)

if __name__ == "__main__":
    main()
