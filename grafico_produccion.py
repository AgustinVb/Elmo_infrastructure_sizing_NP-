"""Grafico de produccion anual de un caso (barras).

Produccion anual = suma de la meta DIARIA de todos los puntos de extraccion
(hoja 'ExtractionGoal' de time_series.xlsx) x 365.

    python grafico_produccion.py --data_folder data/Resultados_finales_tesis/Mina_modelo/P_red
"""
import argparse
import os

import matplotlib.pyplot as plt
import pandas as pd

AZUL = '#2a78d6'


def leer_produccion(data_folder):
    meta = pd.read_excel(os.path.join(data_folder, 'time_series.xlsx'), sheet_name='ExtractionGoal')
    anios = [c for c in meta.columns if isinstance(c, int)]
    produccion = (meta[anios].sum() * 365 / 1e6).astype(float)  # Mt/año
    produccion.index = produccion.index.astype(int)
    return produccion


def graficar(produccion, titulo, salida, y_max=10):
    fig, ax = plt.subplots(figsize=(7.1, 3.85), dpi=200)
    x = produccion.index.values
    ax.bar(x, produccion.values, width=0.7, color=AZUL, zorder=2)

    ax.set_ylim(0, y_max)
    ax.set_yticks(range(0, y_max + 1, 2))
    ax.tick_params(axis='both', length=0, labelsize=8, colors='#444444')
    for lado in ('top', 'left', 'right'):
        ax.spines[lado].set_visible(False)
    ax.spines['bottom'].set_color('#cccccc')
    ax.grid(axis='y', color='#e6e6e6', lw=0.8, zorder=0)
    ax.set_axisbelow(True)

    ax.set_xticks(x)
    ax.set_xlim(x[0] - 0.75, x[-1] + 0.75)
    ax.set_xlabel('Año', fontsize=9, color='#444444')
    ax.set_ylabel('Producción anual (Mtpa)', fontsize=9, color='#444444')
    ax.set_title(titulo, fontsize=11.5, pad=12)

    fig.tight_layout()
    fig.savefig(salida, facecolor='white')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_folder', default='data/Resultados_finales_tesis/Mina_modelo/P_red')
    parser.add_argument('--titulo', default='Producción anual Mina modelo')
    parser.add_argument('--salida', default='output/Resultados_finales_tesis/Mina_modelo/produccion_anual.png')
    args = parser.parse_args()

    produccion = leer_produccion(args.data_folder)
    print(produccion.round(3).to_string())
    os.makedirs(os.path.dirname(args.salida), exist_ok=True)
    graficar(produccion, args.titulo, args.salida)
    print(f'Guardado en {args.salida}')


if __name__ == '__main__':
    main()
