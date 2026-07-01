"""
Grafica todo lo de output/DCH_taller_julio/:
1) Para carpetas _Descomp y otras desacopladas: corre combine_macrobloques_for_plot.py
   -> genera combined/day_X/ con las 3 estaciones sumadas, luego las plotea.
2) Para carpetas monoliticas (parameters.json en raiz): JSONPlotter directo.
Omite subcarpetas _stage1 y station_X_dY individuales (demasiado granular).
"""
from __future__ import annotations
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path("output/DCH_taller_julio")
MODE = "DCH"

# ── helpers ──────────────────────────────────────────────────────────────────

def _import_plotter():
    spec = importlib.util.spec_from_file_location(
        "json_plotter", str(Path(__file__).parent / "json_plotter.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "JSONPlotter")


def _is_descomp(d: Path) -> bool:
    """Carpeta desacoplada: no tiene parameters.json propio pero sus hijos sí."""
    if (d / "parameters.json").exists():
        return False
    return any(
        c.is_dir() and (c / "parameters.json").exists()
        for c in d.iterdir()
        if not c.name.endswith("_stage1") and c.name != "combined"
    )


def _is_monolithic(d: Path) -> bool:
    if not (d / "parameters.json").exists():
        return False
    other = [p for p in d.glob("*.json") if p.name != "parameters.json"]
    return len(other) > 0


def run_combine(folder: Path):
    print(f"\n── Combinando: {folder.name} ──")
    result = subprocess.run(
        [sys.executable, "combine_macrobloques_for_plot.py",
         "--root_dir", str(folder),
         "--mode", MODE,
         "--no_plot"],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"  ⚠️  combine falló en {folder.name}")


def plot_folder(JSONPlotter, folder: Path):
    print(f"  📊 {folder}")
    try:
        p = JSONPlotter(str(folder), mode=MODE)
        p.create_all_plots()
        print(f"  ✅ plots -> {folder / 'plots'}")
    except Exception as e:
        print(f"  ❌ {e}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    JSONPlotter = _import_plotter()

    monolithic: list[Path] = []
    descomp: list[Path] = []

    for cost_type in sorted(ROOT.iterdir()):
        if not cost_type.is_dir():
            continue
        for scenario in sorted(cost_type.iterdir()):
            if not scenario.is_dir():
                continue
            if _is_descomp(scenario):
                descomp.append(scenario)
            elif _is_monolithic(scenario):
                monolithic.append(scenario)

    print(f"Monolíticos encontrados: {len(monolithic)}")
    for d in monolithic:
        print(f"  • {d.relative_to(ROOT)}")

    print(f"\nDescompuestos encontrados: {len(descomp)}")
    for d in descomp:
        print(f"  • {d.relative_to(ROOT)}")

    # 1) Combinar todos los Descomp
    print("\n" + "="*60)
    print("PASO 1: Combinando carpetas desacopladas")
    print("="*60)
    for d in descomp:
        run_combine(d)

    # 2) Plotear monolíticos
    print("\n" + "="*60)
    print("PASO 2: Graficando carpetas monolíticas")
    print("="*60)
    for d in monolithic:
        plot_folder(JSONPlotter, d)

    # 3) Plotear combined/ de cada Descomp
    print("\n" + "="*60)
    print("PASO 3: Graficando resultados combinados (Descomp)")
    print("="*60)
    for d in descomp:
        combined = d / "combined"
        if not combined.exists():
            print(f"  ⚠️  {d.name}/combined no existe (combine falló?)")
            continue
        # sub-carpetas por día (parallel_days) o carpeta plana
        day_dirs = [c for c in combined.iterdir() if c.is_dir() and _is_monolithic(c)]
        if day_dirs:
            print(f"\n  {d.name} ({len(day_dirs)} días combinados):")
            for dd in sorted(day_dirs):
                plot_folder(JSONPlotter, dd)
        elif _is_monolithic(combined):
            print(f"\n  {d.name} (combinado plano):")
            plot_folder(JSONPlotter, combined)
        else:
            print(f"  ⚠️  {d.name}/combined no tiene estructura reconocible")

    print("\n✅ Listo.")


if __name__ == "__main__":
    main()
