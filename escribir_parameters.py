"""Escribe parameters.json en carpetas de solucion que no lo traen, para que
consumer.py (y cost_comparisons/) las puedan leer. Portado de
battery_swapping_multiaño.

Las carpetas solucion/ de mejorar_ub.py solo tienen los JSON de variables
(Printer.write_variables_jsons); consumer.py necesita ademas los parametros del
escenario. Se arman con el mismo Printer.write_parameters_json() de una corrida
normal: dependen solo de los datos, no de la solucion (en swap, el de
RED_GEN_BESS salio identico al que escribio la corrida v6).

    python -u escribir_parameters.py --data_folder data/Resultados_finales_tesis/Mina_modelo/P_red \
        --free_charging --free_maintenance output/.../mejorar_ub_<escenario>/solucion
    python consumer.py output/.../mejorar_ub_<escenario>/solucion

Acepta varias carpetas del mismo escenario. Si una ya tiene parameters.json no
la toca.
"""
import argparse
import os
import sys
import tempfile
from argparse import Namespace

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from setup import build_mine                                     # noqa: E402
from src.io.printer import Printer                               # noqa: E402
from src.optimization.opt_model import OptModel                  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("carpetas", nargs="+")
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--model", default="elmo_data.xlsx")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, choices=[2, 4], default=4)
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp1")
    ap.add_argument("--wp2_consumption_json", default=None)
    ap.add_argument("--free_charging", action="store_true")
    ap.add_argument("--free_maintenance", action="store_true")
    a = ap.parse_args()

    pendientes = [c for c in a.carpetas
                  if not os.path.exists(os.path.join(c, "parameters.json"))]
    for c in set(a.carpetas) - set(pendientes):
        print(f"ya existe {os.path.join(c, 'parameters.json')}", flush=True)
    if not pendientes:
        return 0
    # Mismo armado que mejorar_ub.py.
    ns = Namespace(data_folder=a.data_folder, model=a.model, series="time_series.xlsx",
                   consumption_model=a.consumption_model,
                   wp2_consumption_json=a.wp2_consumption_json, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(tempfile.gettempdir(), "escribir_parameters"),
                  mccormick_degradation=True, free_charging=a.free_charging,
                  free_maintenance=a.free_maintenance)
    for carpeta in pendientes:
        Printer(om, carpeta, ts, ms).write_parameters_json()
        destino = os.path.join(carpeta, "parameters.json")
        print(f"escrito {destino} ({os.path.getsize(destino) / 1e6:.1f} MB) desde "
              f"{a.data_folder}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
