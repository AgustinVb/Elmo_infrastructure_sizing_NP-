"""Escribe parameters.json en carpetas de solucion que no lo traen, para que
consumer.py (y cost_comparisons/) las puedan leer.

Las carpetas solucion/ de mejorar_ub.py solo tienen los JSON de variables
(Printer.write_variables_jsons); consumer.py necesita ademas los parametros del
escenario. Se arman con el mismo Printer.write_parameters_json() de una corrida
normal: dependen solo de los datos, no de la solucion. Verificado: el de
RED_GEN_BESS sale identico al que escribio la corrida v6.

    python -u escribir_parameters.py data/Tesis_final/Mina_modelo/RED_GEN \
        output/Resultados_finales_tesis/Mina_modelo/mejorar_ub_red_gen/solucion
    python consumer.py output/Resultados_finales_tesis/Mina_modelo/mejorar_ub_red_gen/solucion

Acepta varios pares <data_folder> <carpeta>. Si la carpeta ya tiene
parameters.json no lo toca. Opciones (antes de los pares) para escenarios que no
son la mina modelo, p. ej. DCH: --n_years 11 --consumption_model wp2
--pause_scheme dch.
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


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, choices=[1, 2, 4], default=4,
                    help="dias representativos por año (setup.py): 4 = 15, 105, 196, 288; 2 = 15 y 196 (sin y con cobro de punta)")
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp1")
    ap.add_argument("--pause_scheme", choices=["det", "dch"], default="det")
    a, args = ap.parse_known_args(argv)
    if not args or len(args) % 2:
        print(__doc__)
        return 1
    for data, carpeta in zip(args[::2], args[1::2]):
        destino = os.path.join(carpeta, "parameters.json")
        if os.path.exists(destino):
            print(f"ya existe {destino}", flush=True)
            continue
        # Mismo armado que mejorar_ub.py y certificar_inversiones.py.
        ns = Namespace(data_folder=data, model="elmo_data.xlsx", series="time_series.xlsx",
                       consumption_model=a.consumption_model, wp2_consumption_json=None, n_years=a.n_years,
                       days_per_year=a.days_per_year)
        _s, ms, ts = build_mine(ns)
        om = OptModel(ms, ts, os.path.join(tempfile.gettempdir(), "escribir_parameters"),
                      mccormick_degradation=True, free_charging=True, free_maintenance=True,
                      pause_scheme=a.pause_scheme)
        Printer(om, carpeta, ts, ms).write_parameters_json()
        print(f"escrito {destino} ({os.path.getsize(destino) / 1e6:.1f} MB) desde {data}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
