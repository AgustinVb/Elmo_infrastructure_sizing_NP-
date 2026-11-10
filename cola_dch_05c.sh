#!/bin/bash
# DCH swap 0,5C (2 dias por año): espera a que terminen los recortes de 1C (tmux
# dch_1c_rec y dch_1c_rec2) y corre recortar_holgada.py desde cero con la escalera 2..6.
cd "$(dirname "$0")"
B=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d/05C/RED
mkdir -p $B
sleep 120
while tmux has-session -t dch_1c_rec 2>/dev/null || tmux has-session -t dch_1c_rec2 2>/dev/null \
      || tmux has-session -t dch_1c_vigia 2>/dev/null; do sleep 60; done
echo "[cola 05C] $(date -u '+%F %T') inicio" | tee -a $B/cola.log
~/miniconda3/envs/py310/bin/python -u recortar_holgada.py \
    --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/05C/RED \
    --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \
    --grupos 'station_1|station_2,station_3' --escalera 2,3,4,5,6 \
    --t 600 --gap 0.01 --horas 14 --jobs 11 --out $B/recorte 2>&1 | tee $B/recorte.log
echo "[cola 05C] $(date -u '+%F %T') fin: $(grep -a 'FINAL' $B/recorte.log | tail -1)" | tee -a $B/cola.log
