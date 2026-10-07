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
parameters.json no lo toca.
"""
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


def main(args):
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
                       consumption_model="wp1", wp2_consumption_json=None, n_years=10,
                       days_per_year=4)
        _s, ms, ts = build_mine(ns)
        om = OptModel(ms, ts, os.path.join(tempfile.gettempdir(), "escribir_parameters"),
                      mccormick_degradation=True, free_charging=True, free_maintenance=True)
        Printer(om, carpeta, ts, ms).write_parameters_json()
        print(f"escrito {destino} ({os.path.getsize(destino) / 1e6:.1f} MB) desde {data}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
