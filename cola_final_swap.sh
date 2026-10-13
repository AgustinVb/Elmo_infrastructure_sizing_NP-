#!/bin/bash
# Version final SWAP, equivalente a cola_final_ob.sh de carga_ob_multiaño: los 3
# escenarios en serie, del que menos demoro al que mas en la version 1 de swap
# (Benders de la v1: RED_GEN ~8,6 h; RED ~12-13 h, P_red_only -> r2 -> r3 -> r4;
# RED_GEN_BESS ~37,2 h, v4 -> v5 -> v6). Tope de Benders: 36 h por escenario.
# Datos: perfil de generacion interpolado y tasa de descuento del 8 %.
# Por escenario: Nested Benders (hasta 36 h o gap 1 %; --max_iter 50 como en
# el comando de OB) -> mejorar_ub
# (años 1-10, 600 s) -> (parameters + consumer si mejora) -> certificar_inversiones.
#
#   bash cola_final_swap.sh                     # los tres, en ese orden
#   bash cola_final_swap.sh RED_GEN             # solo algunos
#
# Todo se ve en la consola y queda en output/.../<archivo>.log. Para cortar
# Benders conservando la solucion: touch output/.../<ESC>_final/STOP
cd "$(dirname "$0")"
PY=~/miniconda3/envs/py310/bin/python
O=output/Resultados_finales_tesis/Mina_modelo
declare -A HORAS=([RED_GEN]=36 [RED]=36 [RED_GEN_BESS]=36)
GAP=0.01
ITER=50
ESCS=${@:-"RED_GEN RED RED_GEN_BESS"}
LOG=$O/cola_final_swap.log

msg() { echo "[cola $1] $(date '+%F %T') $2" | tee -a $LOG; }

for esc in $ESCS; do
  D=data/Tesis_final/Mina_modelo/$esc; B=$O/${esc}_final

  H=${HORAS[$esc]:-24}
  msg $esc "Nested Benders (max $ITER iteraciones / $H h, gap $GAP)"
  $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
      --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter $ITER \
      --max_hours $H --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
      --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
      --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
      --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
      --polish_each_iter --skip_last_backward 2>&1 | tee $B.log

  read UB0 LBOP < <($PY -c "
import re;t=open('$B.log',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t); print(*(m[-1] if m else ('','')))")
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then
    msg $esc "Benders sin resultado: se salta el escenario"; continue
  fi
  msg $esc "Benders: UB $UB0  LB $LBOP -> mejorar_ub"

  $PY -u mejorar_ub.py --data_folder $D --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
      --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $O/mejorar_ub_${esc}_final 2>&1 \
      | tee $O/mejorar_ub_${esc}_final.log

  SOL=""; UB=$UB0
  if [ -f $O/mejorar_ub_${esc}_final/resumen.json ]; then
    read SOL UB < <($PY -c "
import json;r=json.load(open('$O/mejorar_ub_${esc}_final/resumen.json')); print(r.get('solucion') or '-', repr(r['ub_final']))")
    [ "$SOL" = "-" ] && SOL=""
  fi
  if [ -n "$SOL" ]; then
    msg $esc "mejorar_ub: UB $UB -> parameters + consumer"
  else
    # En swap certificar_inversiones solo acepta --solucion: la carpeta de Benders
    # trae los JSON del incumbente.
    msg $esc "mejorar_ub sin mejora: se certifica la solucion de Benders"; UB=$UB0; SOL=$B
  fi
  $PY -u escribir_parameters.py $D $SOL 2>&1 | tee $O/consumer_${esc}_final.log
  $PY consumer.py $SOL 2>&1 | tee -a $O/consumer_${esc}_final.log

  # gap_objetivo bajo el gap contra la cota operacional (si no, no hay candidatas)
  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
  msg $esc "certificar (UB $UB, gap_objetivo $GO)"
  $PY -u certificar_inversiones.py --data_folder $D --solucion $SOL --ub_esperado $UB --gap_objetivo $GO \
      --jobs 4 --out $O/certificado_${esc}_final 2>&1 | tee $O/certificado_${esc}_final.log
  msg $esc "$(grep -E 'LB CERTIFICADA' $O/certificado_${esc}_final.log | tail -1)"
  msg $esc "fin"
done
