#!/bin/bash
# Variante de P_red_gen (version final OB) con el CORTE LAGRANGEANO de Lara et
# al. (2018) ec. (62) -- --strengthen_max_iter 3 -- en vez del Strengthened
# Benders cut ec. (63) (max_iter 1) de cola_final_ob.sh. Pregunta: los cortes
# mas ajustados guian al forward a un mejor incumbente?
#
# Espera a que termine cola_final_ob.sh, retoma P_red_gen desde el respaldo de
# esa corrida (UB, centro de la caja, cortes y LB: --incumbente_inicial) y corre
# HORAS de Benders -> mejorar_ub -> certificar. Salida en *_lagr3, sin tocar
# las carpetas *_final.
#
#   nohup bash cola_lagr_ob.sh > output/.../cola_lagr_ob.log 2>&1 &
cd "$(dirname "$0")"
PY=~/miniconda3/envs/py310/bin/python
O=output/Resultados_finales_tesis/Mina_modelo
esc=P_red_gen; SUF=lagr3; HORAS=8; GAP=0.01; NLAGR=3
D=data/Resultados_finales_tesis/Mina_modelo/$esc; B=$O/${esc}_${SUF}
RESP0=$O/${esc}_final/incumbente_respaldo.pkl

msg() { echo "[lagr $esc] $(date '+%F %T') $1"; }

# Espera por el PID del proceso cuyo comando es EXACTAMENTE "bash cola_final_ob.sh"
# (pgrep -f con el texto suelto tambien encontraba la shell que lanzo este script,
# porque su linea de comando contiene ese texto, y la espera no terminaba nunca).
while pgrep -x bash -a | grep -qE "^[0-9]+ bash (\./)?cola_final_ob\.sh( |$)"; do sleep 60; done
if [ ! -f $RESP0 ]; then msg "no existe $RESP0: no hay de donde retomar"; exit 1; fi

msg "Nested Benders con --strengthen_max_iter $NLAGR, retomando $RESP0 (max $HORAS h)"
$PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
    --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter 50 \
    --max_hours $HORAS --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
    --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
    --strengthen --strengthen_max_iter $NLAGR --strengthened_timelimit 300 --block_mip_focus 1 \
    --solve_timelimit 120 --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
    --polish_each_iter --skip_last_backward --incumbente_inicial $RESP0 > $B.log 2>&1

read UB0 LBOP < <($PY -c "
import re;t=open('$B.log',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t); print(*(m[-1] if m else ('','')))")
if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then msg "Benders sin resultado"; exit 1; fi
msg "Benders: UB $UB0  LB $LBOP -> mejorar_ub"

$PY -u mejorar_ub.py --data_folder $D --free_charging --free_maintenance \
    --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 --anios 1,2,3,4,5,6,7,8,9,10 \
    --timelimit 600 --jobs 4 --out $O/mejorar_ub_${esc}_${SUF} > $O/mejorar_ub_${esc}_${SUF}.log 2>&1
SOL=""; UB=$UB0
if [ -f $O/mejorar_ub_${esc}_${SUF}/resumen.json ]; then
  read SOL UB < <($PY -c "
import json;r=json.load(open('$O/mejorar_ub_${esc}_${SUF}/resumen.json')); print(r.get('solucion') or '-', repr(r['ub_final']))")
  [ "$SOL" = "-" ] && SOL=""
fi
if [ -n "$SOL" ]; then
  msg "mejorar_ub: UB $UB -> parameters + consumer"
  $PY -u escribir_parameters.py --data_folder $D --free_charging --free_maintenance $SOL > $O/consumer_${esc}_${SUF}.log 2>&1
  $PY consumer.py $SOL >> $O/consumer_${esc}_${SUF}.log 2>&1
  FUENTE="--solucion $SOL"
else
  msg "mejorar_ub sin mejora: se certifica el respaldo de Benders"; UB=$UB0
  FUENTE="--respaldo $B/incumbente_respaldo.pkl"
fi
GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
msg "certificar (UB $UB, gap_objetivo $GO)"
$PY -u certificar_inversiones.py --data_folder $D --free_charging --free_maintenance $FUENTE \
    --ub_esperado $UB --gap_objetivo $GO --evaluar_vivas --jobs 4 --out $O/certificado_${esc}_${SUF} \
    > $O/certificado_${esc}_${SUF}.log 2>&1
msg "$(grep -E 'LB CERTIFICADA' $O/certificado_${esc}_${SUF}.log | tail -1)"
msg "fin"
