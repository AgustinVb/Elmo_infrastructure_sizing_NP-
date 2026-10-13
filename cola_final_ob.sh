#!/bin/bash
# Version final OB: los 3 escenarios en serie, del mas rapido al mas lento en la
# version 1 (P_red 1,6 h; P_red_gen_bat 2,4 h; P_red_gen 11,25 h).
# Por escenario: Nested Benders (hasta 24 h o gap 1 %) -> mejorar_ub ->
# (parameters + consumer si mejora) -> certificar_inversiones.
#
#   bash cola_final_ob.sh                       # los tres, en ese orden
#   bash cola_final_ob.sh P_red_gen             # solo algunos
#
# Todo se ve en la consola y queda en output/.../<archivo>.log. Para cortar
# Benders conservando la solucion: touch output/.../<ESC>_final/STOP
cd "$(dirname "$0")"
PY=~/miniconda3/envs/py310/bin/python
O=output/Resultados_finales_tesis/Mina_modelo
HORAS=24
GAP=0.01
ESCS=${@:-"P_red P_red_gen_bat P_red_gen"}
LOG=$O/cola_final_ob.log

msg() { echo "[cola $1] $(date '+%F %T') $2" | tee -a $LOG; }

for esc in $ESCS; do
  D=data/Resultados_finales_tesis/Mina_modelo/$esc; B=$O/${esc}_final

  msg $esc "Nested Benders (max $HORAS h, gap $GAP)"
  $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
      --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter 50 \
      --max_hours $HORAS --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
      --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
      --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
      --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
      --polish_each_iter --skip_last_backward 2>&1 | tee $B.log

  read UB0 LBOP < <($PY -c "
import re;t=open('$B.log',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t); print(*(m[-1] if m else ('','')))")
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then
    msg $esc "Benders sin resultado: se salta el escenario"; continue
  fi
  msg $esc "Benders: UB $UB0  LB $LBOP -> mejorar_ub"

  $PY -u mejorar_ub.py --data_folder $D --free_charging --free_maintenance \
      --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 --anios 1,2,3,4,5,6,7,8,9,10 \
      --timelimit 600 --jobs 4 --out $O/mejorar_ub_${esc}_final 2>&1 | tee $O/mejorar_ub_${esc}_final.log

  SOL=""; UB=$UB0
  if [ -f $O/mejorar_ub_${esc}_final/resumen.json ]; then
    read SOL UB < <($PY -c "
import json;r=json.load(open('$O/mejorar_ub_${esc}_final/resumen.json')); print(r.get('solucion') or '-', repr(r['ub_final']))")
    [ "$SOL" = "-" ] && SOL=""
  fi
  if [ -n "$SOL" ]; then
    msg $esc "mejorar_ub: UB $UB -> parameters + consumer"
    $PY -u escribir_parameters.py --data_folder $D --free_charging --free_maintenance $SOL 2>&1 | tee $O/consumer_${esc}_final.log
    $PY consumer.py $SOL 2>&1 | tee -a $O/consumer_${esc}_final.log
    FUENTE="--solucion $SOL"
  else
    msg $esc "mejorar_ub sin mejora: se certifica el respaldo de Benders"; UB=$UB0
    FUENTE="--respaldo $B/incumbente_respaldo.pkl"
  fi

  # gap_objetivo bajo el gap contra la cota operacional (si no, no hay candidatas)
  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
  msg $esc "certificar (UB $UB, gap_objetivo $GO)"
  $PY -u certificar_inversiones.py --data_folder $D --free_charging --free_maintenance $FUENTE \
      --ub_esperado $UB --gap_objetivo $GO --evaluar_vivas --jobs 4 --out $O/certificado_${esc}_final 2>&1 \
      | tee $O/certificado_${esc}_final.log
  msg $esc "$(grep -E 'LB CERTIFICADA' $O/certificado_${esc}_final.log | tail -1)"
  msg $esc "fin"
done
