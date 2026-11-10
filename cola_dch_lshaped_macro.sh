#!/bin/bash
# L-shaped con MACROBLOQUES por estacion (lshaped.py --por_estacion) desde la UB del
# recorte con macrobloques (recorte_macro). Uso: cola_dch_lshaped_macro.sh <1C|05C> [cpu]
cd "$(dirname "$0")"
C=$1; CPU=${2:-32}
R=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d/$C/RED
PY=~/miniconda3/envs/py310/bin/python
UB=$($PY -c "import json; print(repr(json.load(open('$R/recorte_macro/resumen.json'))['ub']))")
echo "[cola lshaped macro $C] $(date -u '+%F %T') inicio, UB $UB" | tee -a $R/cola.log
$PY -u lshaped.py --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/$C/RED \
    --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch --por_estacion \
    --solucion $R/recorte_macro/solucion --ub_esperado $UB --horas 10 --jobs 11 --cpu $CPU \
    --hilos_maestro 8 --out $R/lshaped_macro 2>&1 | tee $R/lshaped_macro.log
echo "[cola lshaped macro $C] $(date -u '+%F %T') fin: $(grep -a '\[LS\] FINAL\|ABORTA' $R/lshaped_macro.log | tail -1)" | tee -a $R/cola.log
