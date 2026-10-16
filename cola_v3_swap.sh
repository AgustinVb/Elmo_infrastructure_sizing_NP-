#!/bin/bash
# Version 3 SWAP: perfiles de generacion corregidos (commit f92c97b4c), perfil
# interpolado y descuento del 8 %. Salida en output/.../Mina_modelo/v3/.
#
# Por escenario (RED_GEN_BESS y RED_GEN, del mas rapido al mas lento):
#   1. Nested Benders, hasta 10 h o gap 1 %, con CORTE AUTOMATICO: si el UB lleva
#      4 iteraciones seguidas sin mejorar, se crea STOP apenas se cierra esa
#      iteracion (tras su backward) y Benders corta conservando la solucion.
#   2. mejorar_ub pasada 1 (años 1-10, 600 s) y pasada 2 (años 3-9, 900 s).
#   3. parameters + consumer sobre la mejor solucion y certificado.
# RED (sin generacion: los perfiles no lo afectan) se copio de RED_final y solo
# se le agrega la pasada 2 de mejorar_ub + certificado, para que los tres
# escenarios tengan el mismo metodo.
#
#   bash cola_v3_swap.sh                    # todo, en ese orden
#   bash cola_v3_swap.sh RED_GEN            # solo algunos
#   SOLO_POST="RED_GEN" bash cola_v3_swap.sh RED_GEN   # sin repetir Benders
#   CORTE_CARGAS=data/Tesis_final/Mina_modelo/corte_cargas_lhd.json bash cola_v3_swap.sh RED_GEN
#        # agrega a la cota operacional el corte de cargas por LHD condicional
#        # al reemplazo (corte_cargas_op.py, setup.py --corte_cargas_lhd)
cd "$(dirname "$0")"
PY=~/miniconda3/envs/py310/bin/python
V=output/Resultados_finales_tesis/Mina_modelo/v3
HORAS=10; GAP=0.01; ITER=50; SIN_MEJORA=4
EXTRA_BENDERS=""
[ -n "$CORTE_CARGAS" ] && EXTRA_BENDERS="--corte_cargas_lhd $CORTE_CARGAS"
ESCS=${@:-"RED_GEN_BESS RED_GEN RED"}
LOG=$V/cola_v3.log
mkdir -p $V
msg() { echo "[v3 $1] $(date '+%F %T') $2" | tee -a $LOG; }

# Crea STOP cuando el UB lleva $SIN_MEJORA iteraciones cerradas sin mejorar.
vigia() {
  local L=$1 STOPF=$2
  while true; do
    if $PY -c "
import re,sys
t=open('$L',encoding='utf-8',errors='replace').read()
ub=[float(u) for u in re.findall(r'k=\d+  UB=([\d.]+)\s+LB=',t)]
s=0
for i in range(len(ub)-1,0,-1):
    if ub[i] < ub[i-1]-1e-6: break
    s+=1
sys.exit(0 if s>=$SIN_MEJORA else 1)" 2>/dev/null; then
      touch $STOPF; echo "[v3 vigia] $(date '+%F %T') $SIN_MEJORA iteraciones sin mejora: STOP en $STOPF" | tee -a $LOG
      return
    fi
    sleep 30
  done
}

leer_benders() {  # imprime "UB LB" de la ultima iteracion cerrada (o de la linea final)
  $PY -c "
import re;t=open('$1',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t) or re.findall(r'k=\d+  UB=([\d.]+)\s+LB=([\d.]+)',t)
print(*(m[-1] if m else ('','')))"
}
ub_resumen() {  # "solucion ub_final" de un resumen.json de mejorar_ub, o "- -"
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print(r.get('solucion') or '-', repr(r['ub_final']))" || echo "- -"
}

for esc in $ESCS; do
  D=data/Tesis_final/Mina_modelo/$esc; B=$V/$esc
  if [ "$esc" = "RED" ] || [[ " $SOLO_POST " == *" $esc "* ]]; then
    msg $esc "sin Benders: se usa $B.log"
  else
    msg $esc "Nested Benders (max $HORAS h, gap $GAP, corte tras $SIN_MEJORA iteraciones sin mejora${CORTE_CARGAS:+, corte de cargas en la cota operacional})"
    mkdir -p $B; rm -f $B/STOP
    vigia $B.log $B/STOP & VP=$!
    $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
        --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter $ITER \
        --max_hours $HORAS --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
        --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
        --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
        --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
        --polish_each_iter --skip_last_backward $EXTRA_BENDERS 2>&1 | tee $B.log
    kill $VP 2>/dev/null; wait $VP 2>/dev/null
  fi
  read UB0 LBOP < <(leer_benders $B.log)
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then msg $esc "Benders sin resultado: se salta"; continue; fi
  msg $esc "Benders: UB $UB0  LB $LBOP"

  # mejorar_ub, pasada 1 (RED ya la tiene: se copio de RED_final)
  if [ "$esc" != "RED" ]; then
    msg $esc "mejorar_ub pasada 1 (años 1-10, 600 s)"
    $PY -u mejorar_ub.py --data_folder $D --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
        --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $V/mejorar_ub_$esc 2>&1 | tee $V/mejorar_ub_$esc.log
  fi
  read SOL UB < <(ub_resumen $V/mejorar_ub_$esc/resumen.json)
  [ "$SOL" = "-" ] && { SOL=$B; UB=$UB0; }
  [ "$esc" = "RED" ] && SOL=$V/mejorar_ub_RED/solucion   # la copia de RED_final
  msg $esc "tras pasada 1: UB $UB ($SOL)"

  # mejorar_ub, pasada 2
  msg $esc "mejorar_ub pasada 2 (años 3-9, 900 s)"
  $PY -u mejorar_ub.py --data_folder $D --solucion $SOL --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
      --timelimit 900 --jobs 4 --out $V/mejorar_ub_${esc}_2 2>&1 | tee $V/mejorar_ub_${esc}_2.log
  read SOL2 UB2 < <(ub_resumen $V/mejorar_ub_${esc}_2/resumen.json)
  if [ "$SOL2" != "-" ]; then SOL=$SOL2; UB=$UB2; fi
  msg $esc "tras pasada 2: UB $UB ($SOL) -> parameters + consumer"
  $PY -u escribir_parameters.py $D $SOL 2>&1 | tee $V/consumer_$esc.log
  $PY consumer.py $SOL 2>&1 | tee -a $V/consumer_$esc.log

  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
  msg $esc "certificar (UB $UB, gap_objetivo $GO)"
  $PY -u certificar_inversiones.py --data_folder $D --solucion $SOL --ub_esperado $UB --gap_objetivo $GO \
      --jobs 4 --out $V/certificado_$esc 2>&1 | tee $V/certificado_$esc.log
  msg $esc "$(grep -E 'LB CERTIFICADA' $V/certificado_$esc.log | tail -1)"
  msg $esc "fin"
done
