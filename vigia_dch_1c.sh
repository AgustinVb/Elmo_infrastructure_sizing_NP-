#!/bin/bash
# Al empezar la ronda 2 del recorte 1C (codigo sin reparar/equipo), lo corta y lo relanza
# desde su mejor solucion con el codigo nuevo (tmux dch_1c_rec2, sin escalera).
cd "$(dirname "$0")"
B=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d/1C/RED
until grep -q "ronda 2\|FINAL" $B/recorte.log || ! tmux has-session -t dch_1c_rec 2>/dev/null; do sleep 30; done
# Esperar a que no se este escribiendo el JSON de una mejora (lo escribe antes de seguir).
sleep 30
tmux kill-session -t dch_1c_rec 2>/dev/null
echo "[vigia] $(date -u '+%F %T') corte del recorte 1C: $(grep -a 'UB' $B/recorte.log | grep '\*\*\*' | tail -1)" | tee -a $B/vigia.log
cp -r $B/recorte/solucion $B/recorte_r1_solucion
tmux new-session -d -s dch_1c_rec2 "cd $(pwd) && ~/miniconda3/envs/py310/bin/python -u recortar_holgada.py --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/1C/RED --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch --solucion $B/recorte_r1_solucion --grupos 'station_1|station_2,station_3' --escalera '' --t 600 --gap 0.01 --horas 10 --jobs 11 --out $B/recorte2 2>&1 | tee $B/recorte2.log"
echo "[vigia] $(date -u '+%F %T') lanzado dch_1c_rec2" | tee -a $B/vigia.log
