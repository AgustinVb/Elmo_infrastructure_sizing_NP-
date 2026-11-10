#!/bin/bash
# L-shaped de DCH swap 1C y 0,5C (2 dias por año) desde la UB del recorte con macrobloques
# (recorte_macro), los dos en paralelo con 30 nucleos cada uno. Espera a que termine la
# cola del 2C (tmux dch_2c). Salida en <caso>/RED/lshaped_v2.
cd "$(dirname "$0")"
PY=~/miniconda3/envs/py310/bin/python
V=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d
sleep 60
while tmux has-session -t dch_2c 2>/dev/null; do sleep 60; done
corre() {
  C=$1; R=$V/$C/RED
  UB=$($PY -c "import json; print(repr(json.load(open('$R/recorte_macro/resumen.json'))['ub']))")
  echo "[cola lshaped v2 $C] $(date -u '+%F %T') inicio, UB $UB" | tee -a $R/cola.log
  $PY -u lshaped.py --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/$C/RED \
      --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \
      --solucion $R/recorte_macro/solucion --ub_esperado $UB --horas 10 --jobs 11 --cpu 30 \
      --hilos_maestro 4 --out $R/lshaped_v2 > $R/lshaped_v2.log 2>&1
  echo "[cola lshaped v2 $C] $(date -u '+%F %T') fin: $(grep -a '\[LS\] FINAL\|ABORTA' $R/lshaped_v2.log | tail -1)" | tee -a $R/cola.log
}
corre 1C &
sleep 30
corre 05C &
wait
