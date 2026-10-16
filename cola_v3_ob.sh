#!/bin/bash
# Version 3 OB, equivalente a cola_v3_swap.sh de battery_swapping_multiaño:
# perfiles de generacion corregidos, perfil interpolado y descuento del 8 %.
# Salida en output/.../Mina_modelo/v3/.
#
# Por escenario (P_red_gen_bat y P_red_gen, del mas rapido al mas lento):
#   1. Nested Benders, hasta 18 h o gap 1 %, con CORTE AUTOMATICO: si el UB lleva
#      4 iteraciones seguidas sin mejorar, se crea STOP apenas se cierra esa
#      iteracion (tras su backward) y Benders corta conservando la solucion.
#   2. mejorar_ub pasada 1 (años 1-10, 600 s) y pasada 2 (años 3-9, 900 s).
#   3. parameters + consumer sobre la mejor solucion y certificado (--evaluar_vivas).
# P_red (sin generacion: los perfiles no lo afectan) se copia de P_red_final y
# solo se le agrega la pasada 2 de mejorar_ub + certificado, igual que RED en swap.
# El corte de cargas en la cota operacional de swap (--corte_cargas_lhd) NO se
# usa: en OB la relajacion de la operacion diaria ya es exacta en las cargas.
#
#   bash cola_v3_ob.sh                         # todo, en ese orden
#   bash cola_v3_ob.sh P_red_gen               # solo algunos
#   SOLO_POST="P_red_gen" bash cola_v3_ob.sh P_red_gen   # sin repetir Benders
cd "$(dirname "$0")"
PY=${PY:-~/miniconda3/envs/py310/bin/python}   # en otro equipo: PY=<ruta a python> bash cola_v3_ob.sh
O=output/Resultados_finales_tesis/Mina_modelo
V=$O/v3
HORAS=18; GAP=0.01; ITER=50; SIN_MEJORA=4
FLAGS="--free_charging --free_maintenance"
ESCS=${@:-"P_red_gen_bat P_red_gen P_red"}
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

leer_benders() {  # "UB LB" de la linea final o de la ultima iteracion cerrada
  $PY -c "
import re;t=open('$1',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t) or re.findall(r'k=\d+  UB=([\d.]+)\s+LB=([\d.]+)',t)
print(*(m[-1] if m else ('','')))"
}
ub_resumen() {  # "solucion ub_final" de un resumen.json de mejorar_ub, o "- -"
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print(r.get('solucion') or '-', repr(r['ub_final']))" || echo "- -"
}

# P_red: copia del resultado final (Benders + mejorar_ub pasada 1), sin _mps/_monolitico.
if [[ " $ESCS " == *" P_red "* ]] && [ ! -d $V/P_red ]; then
  rsync -a --exclude _mps --exclude _monolitico $O/P_red_final/ $V/P_red/
  cp $O/P_red_final.log $V/P_red.log
  mkdir -p $V/mejorar_ub_P_red
  rsync -a $O/mejorar_ub_P_red_final/solucion $V/mejorar_ub_P_red/
  cp $O/mejorar_ub_P_red_final/resumen.json $O/mejorar_ub_P_red_final/dias.npz $V/mejorar_ub_P_red/
  cp $O/mejorar_ub_P_red_final.log $V/mejorar_ub_P_red.log
  msg P_red "copiado de P_red_final (no usa generacion)"
fi

for esc in $ESCS; do
  D=data/Resultados_finales_tesis/Mina_modelo/$esc; B=$V/$esc
  if [ "$esc" = "P_red" ] || [[ " $SOLO_POST " == *" $esc "* ]]; then
    msg $esc "sin Benders: se usa $B.log"
  else
    msg $esc "Nested Benders (max $HORAS h, gap $GAP, corte tras $SIN_MEJORA iteraciones sin mejora)"
    mkdir -p $B; rm -f $B/STOP
    vigia $B.log $B/STOP & VP=$!
    $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS \
        --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter $ITER \
        --max_hours $HORAS --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
        --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
        --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
        --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
        --polish_each_iter --skip_last_backward 2>&1 | tee $B.log
    kill $VP 2>/dev/null; wait $VP 2>/dev/null
  fi
  read UB0 LBOP < <(leer_benders $B.log)
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then msg $esc "Benders sin resultado: se salta"; continue; fi
  msg $esc "Benders: UB $UB0  LB $LBOP"

  if [ "$esc" != "P_red" ]; then
    msg $esc "mejorar_ub pasada 1 (años 1-10, 600 s)"
    $PY -u mejorar_ub.py --data_folder $D $FLAGS --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
        --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $V/mejorar_ub_$esc 2>&1 | tee $V/mejorar_ub_$esc.log
  fi
  read SOL UB < <(ub_resumen $V/mejorar_ub_$esc/resumen.json)
  [ "$esc" = "P_red" ] && SOL=$V/mejorar_ub_P_red/solucion
  if [ "$SOL" = "-" ]; then FUENTE="--respaldo $B/incumbente_respaldo.pkl"; UB=$UB0; SOL=""
  else FUENTE="--solucion $SOL"; fi
  msg $esc "tras pasada 1: UB $UB (${SOL:-respaldo de Benders})"

  msg $esc "mejorar_ub pasada 2 (años 3-9, 900 s)"
  if [ -n "$SOL" ]; then ORIG="--solucion $SOL"; else ORIG="--respaldo $B/incumbente_respaldo.pkl"; fi
  $PY -u mejorar_ub.py --data_folder $D $FLAGS $ORIG --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
      --timelimit 900 --jobs 4 --out $V/mejorar_ub_${esc}_2 2>&1 | tee $V/mejorar_ub_${esc}_2.log
  read SOL2 UB2 < <(ub_resumen $V/mejorar_ub_${esc}_2/resumen.json)
  if [ "$SOL2" != "-" ]; then SOL=$SOL2; UB=$UB2; FUENTE="--solucion $SOL"; fi
  msg $esc "tras pasada 2: UB $UB (${SOL:-respaldo de Benders})"
  if [ -n "$SOL" ]; then
    $PY -u escribir_parameters.py --data_folder $D $FLAGS $SOL 2>&1 | tee $V/consumer_$esc.log
    $PY consumer.py $SOL 2>&1 | tee -a $V/consumer_$esc.log
  fi

  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
  msg $esc "certificar (UB $UB, gap_objetivo $GO)"
  $PY -u certificar_inversiones.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB --gap_objetivo $GO \
      --evaluar_vivas --jobs 4 --out $V/certificado_$esc 2>&1 | tee $V/certificado_$esc.log
  msg $esc "$(grep -E 'LB CERTIFICADA' $V/certificado_$esc.log | tail -1)"
  msg $esc "fin"
done
