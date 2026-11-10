#!/bin/bash
# DCH swap (2 dias por año): recorte con MACROBLOQUES por estacion (--por_estacion): operar
# (misma inversion, operacion por estacion), macro (algoritmo de un año: Fase 1 por
# macrobloque, agregacion, Fase 2) y despues tope/ssee/retrasar con cada estacion sola.
# Uso: cola_dch_macro.sh <1C|05C> <carpeta de la solucion incumbente>
cd "$(dirname "$0")"
C=$1; SOL=$2
B=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d/$C/RED
echo "[cola macro $C] $(date -u '+%F %T') inicio desde $SOL" | tee -a $B/cola.log
~/miniconda3/envs/py310/bin/python -u recortar_holgada.py \
    --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/$C/RED \
    --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \
    --solucion $SOL --por_estacion --grupos 'station_1|station_2|station_3' --escalera '' \
    --movs operar,macro,tope,ssee,retrasar \
    --t 300 --gap 0.01 --horas 20 --jobs 11 --cpu 32 --out $B/recorte_macro 2>&1 | tee $B/recorte_macro.log
echo "[cola macro $C] $(date -u '+%F %T') fin: $(grep -a 'FINAL' $B/recorte_macro.log | tail -1)" | tee -a $B/cola.log
