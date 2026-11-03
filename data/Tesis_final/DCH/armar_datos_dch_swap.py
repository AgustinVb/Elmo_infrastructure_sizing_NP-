"""Arma los datos multianio de DCH para SWAP (caso red), espejo de los de carga on-board.

Uso (desde la raiz del repo, en la rama battery_swapping_multiaño):
    python data/Tesis_final/DCH/armar_datos_dch_swap.py

Genera data/Tesis_final/DCH/Bateria_482/Costo_fijo/{05C,1C,2C}/RED con elmo_data.xlsx,
time_series.xlsx y electric_routes_within_time.json (WP2).

Lo comun con OB se toma TAL CUAL de los datos DCH de la rama carga_ob_multiaño
(data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo/<tasa>/P_red, armados por su
armar_datos_dch.py): time_series.xlsx entero (plan DCH 2035-2045 de 11 anios, extraccion
por calle, NodeAssignment, StationAssignment, Shifts de 2 turnos), extraction_nodes,
discharge_nodes, FleetByYear (con opera_en_colacion, que swap no lee: hoy es 0 en todos los
anios), el peso y la velocidad del LH518B y el WP2. Asi la produccion y sus cotas
(floor-1..ceil+2 por punto, ConstraintRules.production) son las mismas en las dos ramas.

Lo propio de swap sale de la mina modelo v4 de esta rama
(data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte):
- plantilla 05C/RED: LHD (tecnologia Battery_swap, swap_time), Generators/Storage del caso
  red (g_max = h_max = 0) y costos unitarios de las estaciones;
- por tasa: hoja chargers (241 / 482 / 964 kW, battery_cost) y BatteryDegradation (gamma
  0,0188 / 0,0273 / 0,0451) de 05C/RED, 1C/RED_GEN_BESS y 2C/RED_GEN_BESS;
- limites fisicos de la campania swap DCH (data/DCH/160kW_2dias): 3 estaciones, 4 bahias,
  2 baterias y 2 cargadores por bahia, p_max_ssee 1.500 kW; p_peak_dist 5.000 kW (el de OB).

Correr con --n_years 11 --days_per_year 4 --consumption_model wp2 --free_charging
--pause_scheme dch (pausas DCH; el rack sigue cargando en mantencion).
"""
import io
import os
import subprocess

import openpyxl
import pandas as pd

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
MM = os.path.join(RAIZ, 'data', 'Tesis_final', 'Mina_modelo_v4', 'Produccion_base', 'zona_norte')
PLANTILLA = os.path.join(MM, '05C', 'RED')
FUENTE_TASA = {'05C': os.path.join(MM, '05C', 'RED'),
               '1C': os.path.join(MM, '1C', 'RED_GEN_BESS'),
               '2C': os.path.join(MM, '2C', 'RED_GEN_BESS')}
OB = 'carga_ob_multiaño:data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo/{tasa}/P_red/{f}'
SALIDA = os.path.join(RAIZ, 'data', 'Tesis_final', 'DCH', 'Bateria_482', 'Costo_fijo')

N_ESTACIONES = 3
MAX_BAYS = 4
MAX_BATTERIES_PER_BAY = 2
MAX_CHARGERS_PER_BAY = 2
P_MAX_SSEE = 1500
P_PEAK_DIST = 5000


def de_ob(tasa, f):
    return subprocess.run(['git', 'show', OB.format(tasa=tasa, f=f)], cwd=RAIZ, check=True,
                          capture_output=True).stdout


def reescribir(ws, filas, desde):
    """Borra desde la fila `desde` hacia abajo y escribe `filas` ahi."""
    if ws.max_row >= desde:
        ws.delete_rows(desde, ws.max_row - desde + 1)
    for i, fila in enumerate(filas):
        for j, valor in enumerate(fila):
            ws.cell(row=desde + i, column=j + 1, value=valor)


def encabezado(ws):
    return [c.value for c in ws[1]]


def copiar_hoja(ws, ws_ob):
    """Reemplaza ws por el contenido completo de ws_ob (encabezado y unidades incluidos)."""
    reescribir(ws, [[c.value for c in fila] for fila in ws_ob.iter_rows()], 1)


def armar_elmo_data(destino, tasa):
    wb = openpyxl.load_workbook(os.path.join(PLANTILLA, 'elmo_data.xlsx'))
    ob = openpyxl.load_workbook(io.BytesIO(de_ob(tasa, 'elmo_data.xlsx')))
    fuente = openpyxl.load_workbook(os.path.join(FUENTE_TASA[tasa], 'elmo_data.xlsx'))

    # LHD: la fila de swap de la plantilla para cada LHD de OB, con su peso y velocidad.
    ws = wb['LHD']
    cols = encabezado(ws)
    base = dict(zip(cols, [c.value for c in ws[3]]))
    cols_ob = encabezado(ob['LHD'])
    filas = []
    for fila_ob in ob['LHD'].iter_rows(min_row=3, values_only=True):
        r = dict(zip(cols_ob, fila_ob))
        if not r.get('name'):
            continue
        fila = dict(base)
        fila.update(id=r['id'], name=r['name'], weight=r['weight'], speed=r['speed'])
        filas.append([fila[c] for c in cols])
    reescribir(ws, filas, 3)

    # stations: costos de la mina modelo swap, limites de la campania swap DCH.
    ws = wb['stations']
    cols = encabezado(ws)
    base = dict(zip(cols, [c.value for c in ws[3]]))
    filas = []
    for k in range(1, N_ESTACIONES + 1):
        fila = dict(base)
        fila.update(station_name=f'station_{k}', max_bays=MAX_BAYS,
                    max_batteries_per_bay=MAX_BATTERIES_PER_BAY,
                    max_chargers_per_bay=MAX_CHARGERS_PER_BAY, p_max_ssee=P_MAX_SSEE)
        filas.append([fila[c] for c in cols])
    reescribir(ws, filas, 3)

    # chargers y BatteryDegradation de la tasa; tope de red DCH.
    ws = wb['chargers']
    cols = encabezado(ws)
    ch = dict(zip(encabezado(fuente['chargers']), [c.value for c in fuente['chargers'][3]]))
    ch['p_peak_dist'] = P_PEAK_DIST
    reescribir(ws, [[ch[c] for c in cols]], 3)
    reescribir(wb['BatteryDegradation'], [[c.value for c in fuente['BatteryDegradation'][3]]], 3)

    # Plan y geometria DCH, iguales a OB.
    for hoja in ('extraction_nodes', 'discharge_nodes', 'FleetByYear'):
        copiar_hoja(wb[hoja], ob[hoja])

    wb.save(os.path.join(destino, 'elmo_data.xlsx'))


def main():
    for tasa in FUENTE_TASA:
        destino = os.path.join(SALIDA, tasa, 'RED')
        os.makedirs(destino, exist_ok=True)
        armar_elmo_data(destino, tasa)
        for f in ('time_series.xlsx', 'electric_routes_within_time.json'):
            with open(os.path.join(destino, f), 'wb') as out:
                out.write(de_ob(tasa, f))
        print('listo', os.path.relpath(destino, RAIZ))


if __name__ == '__main__':
    main()
