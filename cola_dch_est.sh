#!/bin/bash
# DCH swap (2 dias por año): recorte con estaciones SEPARADAS (s2 y s3 por su cuenta) y mas
# tiempo por dia (los rechazos del año 7 con --t 600 eran por tiempo, no infactibilidad).
# Uso: cola_dch_est.sh <1C|05C> <carpeta de la solucion incumbente>
cd "$(dirname "$0")"
C=$1; SOL=$2
B=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d/$C/RED
echo "[cola est $C] $(date -u '+%F %T') inicio desde $SOL" | tee -a $B/cola.log
~/miniconda3/envs/py310/bin/python -u recortar_holgada.py \
    --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/$C/RED \
    --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \
    --solucion $SOL --grupos 'station_1|station_2|station_3' --escalera '' --movs tope,ssee,retrasar \
    --t 1200 --gap 0.01 --horas 20 --jobs 4 --cpu 32 --out $B/recorte_est 2>&1 | tee $B/recorte_est.log
echo "[cola est $C] $(date -u '+%F %T') fin: $(grep -a 'FINAL' $B/recorte_est.log | tail -1)" | tee -a $B/cola.log
