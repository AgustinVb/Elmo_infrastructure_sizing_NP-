#!/bin/bash
# Casos DCH multiaño, SWAP, caso red (RED), Bateria_482, costo fijo, tasas de carga 0,5C / 1C
# / 2C: el espejo de cola_dch_ob_v4.sh de carga_ob_multiaño, con el metodo de swap de
# cola_tesis_swap.sh (Benders strengthened + corte de cargas + estabilizacion, mejorar_ub
# pasadas 1 y 2, parameters + consumer, certificado; ronda 2 para los >= 1 %).
#
# Datos en data/Tesis_final/DCH (armar_datos_dch_swap.py): plan DCH 2035-2045 de 11 años,
# produccion, extraccion por calle, turnos y FleetByYear IGUALES a los de OB (time_series.xlsx
# copiado de carga_ob_multiaño), consumo WP2, cargadores y degradacion de la mina modelo swap
# por tasa, estaciones de la campaña swap DCH (3 estaciones, 4 bahias, 2 baterias y 2
# cargadores por bahia, 1.500 kW) y p_peak 5.000 kW.
#
# Mismas condiciones que OB:
#   - pausas DCH (--pause_scheme dch): mantenciones con los LHD detenidos (sin operar ni
#     swap; los cargadores de la estacion siguen cargando baterias), colacion por grupos de
#     LHD y entre turnos de la hoja Shifts (2 turnos);
#   - swap libre fuera de mantencion (--free_charging, el "carga libre" de OB);
#   - cotas de produccion floor-1..ceil+2 por punto (ConstraintRules.production, igual en
#     las dos ramas);
#   - 11 años y WP2 en TODOS los pasos (REG); mejorar_ub pasada 1 en los años 1-11 y pasada
#     2 en los años 3-9 (9 a 12 LHD), como OB.
# A diferencia de OB se corre tambien 0,5C: alla fue infactible en el año 7 porque 2
# cargadores de 241 kW por estacion no recargaban 4 LHD; en swap la carga es en el rack.
#
#   bash cola_dch_swap_v4.sh                # ronda 1, los 3 casos (0,5C, 1C, 2C)
#   bash cola_dch_swap_v4.sh 2 3            # solo algunos
#   RONDA=2 bash cola_dch_swap_v4.sh        # relanza los >= 1 %
#   Para cortar un Benders a mano: crear el archivo STOP en su carpeta benders/.
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PY=${PY:-.venv_elmo/Scripts/python.exe}
export DATA=data/Tesis_final/DCH/Bateria_482/Costo_fijo
export V=${V:-output/Resultados_finales_tesis/DCH/casos_finales_v4_swap}
export V3=
export CASOS_ENV="05C/RED 1C/RED 2C/RED"
export FLAGS="--free_charging"
export REG="--n_years 11 --consumption_model wp2 --pause_scheme dch"
export ANIOS_1=1,2,3,4,5,6,7,8,9,10,11 ANIOS_2=3,4,5,6,7,8,9
export TOPE_H=${TOPE_H:-45}
exec bash cola_tesis_swap.sh "$@"
