#!/bin/bash
# 2C (DCH v4, carga libre, pausas DCH): mas iteraciones de Nested Benders partiendo del respaldo de
# la ronda 1 (iteracion 2: UB 11.709.336, LB 10.263.748; incumbente, cortes y cota validos, sin
# recalcular la cota operacional). Mismos parametros que cola_dch_ob_v4.sh. Hasta HORAS h; corta si el
# UB lleva SIN_MEJORA iteraciones sin mejorar; si el proceso muere, reintenta desde su propio respaldo.
# Si el UB baja del de mejorar_ub (11.708.379,17), escribe parameters y corre consumer sobre la solucion.
#   Lanzar: correr_2c_r2.cmd (o bash correr_2c_r2.sh). Cortar a mano: crear r2/benders/STOP.
cd "$(dirname "$0")/../../../.."
export PYTHONIOENCODING=utf-8
PY=${PY:-.venv_elmo/Scripts/python.exe}
D=data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo/2C/P_red
R1=output/Resultados_finales_tesis/DCH/casos_finales_v4_Carga_libre/2C/P_red
R=$R1/r2; B=$R/benders
HORAS=${HORAS:-8}; SIN_MEJORA=4; GAP=0.01; ITER=50
UB_R1=11708379.1736
FLAGS="--free_charging --pause_scheme dch"
COMUN="--n_years 11 --consumption_model wp2"
mkdir -p $B
LOG=$R/r2.log
[ -f $R/respaldo_r1.pkl ] || cp $R1/benders/incumbente_respaldo.pkl $R/respaldo_r1.pkl
INC="--incumbente_inicial $R/respaldo_r1.pkl"
msg() { echo "[dch 2C r2] $(date '+%F %T') $1" | tee -a $LOG; }

# Crea STOP cuando el UB lleva $SIN_MEJORA iteraciones cerradas sin mejorar (igual que la cola).
vigia() {
  while true; do
    if $PY -c "
import re,sys
t=open('$R/benders.log',encoding='utf-8',errors='replace').read()
ub=[float(u) for u in re.findall(r'k=\d+  UB=([\d.]+)\s+LB=',t)]
s=0
for i in range(len(ub)-1,0,-1):
    if ub[i] < ub[i-1]-1e-6: break
    s+=1
sys.exit(0 if s>=$SIN_MEJORA else 1)" 2>/dev/null; then
      touch $B/STOP; msg "$SIN_MEJORA iteraciones sin mejora del UB: STOP"; return
    fi
    sleep 30
  done
}

FIN=$(( $(date +%s) + HORAS * 3600 ))
msg "inicio: Benders desde el respaldo de la ronda 1, hasta $HORAS h (hasta $(date -d @$FIN '+%F %T'))"
for intento in 1 2 3 4 5 6; do
  H=$(awk -v q=$(( FIN - $(date +%s) )) 'BEGIN{printf "%.2f", q/3600}')
  if awk -v h=$H 'BEGIN{exit !(h<0.5)}'; then msg "sin tiempo para otro intento ($H h)"; break; fi
  msg "Nested Benders intento $intento (max $H h) $INC"
  rm -f $B/STOP
  vigia & VP=$!
  $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS $COMUN \
      --block_build_jobs 1 --output_folder $B --mode decomposed --gap_tol $GAP --max_iter $ITER \
      --max_hours $H --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
      --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
      --stab_free_every 3 --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
      --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
      --polish_each_iter --skip_last_backward $INC 2>&1 | tee -a $R/benders.log
  kill $VP 2>/dev/null; wait $VP 2>/dev/null
  # Terminado solo si la ultima linea final no es un corte por error: con la CPU inestable el
  # Benders a veces cae en una excepcion absurda (TypeError), la atrapa, imprime igual su linea
  # final "cortado por error" y sale con codigo 0. Eso se reintenta como una caida.
  ULT=$(grep -E '^NestedBenders' $R/benders.log | tail -1)
  if [ -n "$ULT" ] && [[ "$ULT" != *"cortado por error"* ]]; then break; fi
  if [ -f $B/incumbente_respaldo.pkl ]; then
    cp $B/incumbente_respaldo.pkl $B/respaldo_intento$intento.pkl
    INC="--incumbente_inicial $B/respaldo_intento$intento.pkl"
  fi
  msg "Benders murio en el intento $intento; se reintenta desde su respaldo"
done

read -r UB LB < <($PY -c "
import re;t=open('$R/benders.log',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t) or re.findall(r'k=\d+  UB=([\d.]+)\s+LB=([\d.]+)',t)
print(*(m[-1] if m else ('','')))" | tr -d '\r')
if [ -z "$UB" ]; then msg "FIN sin resultado"; exit 1; fi
G=$(awk -v u=$UB -v l=$LB 'BEGIN{printf "%.3f", 100*(u-l)/u}')
if awk -v u=$UB -v r=$UB_R1 'BEGIN{exit !(u < r - 1e-6)}'; then
  $PY -u escribir_parameters.py --data_folder $D $FLAGS $COMUN $B 2>&1 | tee $R/consumer.log
  $PY consumer.py $B 2>&1 | tee -a $R/consumer.log
  echo "UB $UB  LB $LB  gap $G %  solucion $B" > $R/resultado.txt
  msg "FIN: UB $UB (mejor que la ronda 1, $UB_R1)  LB $LB  gap $G %"
else
  echo "UB $UB  LB $LB  gap $G % (el UB no mejora el de mejorar_ub de la ronda 1, $UB_R1)" > $R/resultado.txt
  msg "FIN: UB $UB sin mejorar la ronda 1 ($UB_R1)  LB $LB  gap $G %"
fi
