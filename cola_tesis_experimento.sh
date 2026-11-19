#!/bin/bash
# Experimento computacional de la tesis (mina modelo), 2026-11-17, acordado con la usuaria:
# comparacion de algoritmos en el CASO 2 (base, norte, 0,5C, red + generacion + BESS) de
# OB y de BS, con horizontes de 4 y 10 años (4 dias representativos por año), al estilo de
# Flores-Quiroz y Strunz (2021) y Lara et al. (2018). Corre DESPUES de
# cola_tesis_traccion.sh (la espera), en sus mismas condiciones: dos ranuras, una por
# socket (taskset), gurobi.env Threads 16, 12 h por corrida, gap objetivo 1 % en todos.
#
# Configuraciones (aceleracion = cota operacional + DP; en OB no hay DP, solo la cota):
#   mono     monolitico, MIPGap 1 %, 12 h de solver (--mono_timelimit; BS con McCormick)
#   nb_sin   Nested Benders sin cota operacional (LB del backward, caja centrada en la
#            primera solucion); BS ademas sin DP, sin corte de cargas y con R libre
#   nb_con   Nested Benders con la aceleracion (configuracion de la campaña)
#   ls_sin   L-shaped desde cero (BS con --sin_dp)
#   hibrido  BS: cota operacional + DP (iteracion 0 del NB) -> L-shaped (algoritmo final);
#            OB (sin DP): cota operacional + una iteracion del NB (primer forward) -> L-shaped
# En 10 años nb_con (OB y BS) e hibrido de BS salen de cola_tesis_traccion.sh (caso 2); el
# hibrido de OB se corre aparte (la campaña no guardo el incumbente de su primera iteracion).
# Cortes strengthened y estabilizacion activa en todos los NB. Corte automatico del NB
# tras 4 iteraciones sin mejora del UB (como la campaña).
#
#   nohup bash cola_tesis_experimento.sh > $E/experimento.out 2>&1 &
#   RANURAS=A ESPERAR=0 bash cola_tesis_experimento.sh            # socket A ya
#   RANURAS=B REINICIAR_COLA=0 bash cola_tesis_experimento.sh     # socket B se suma despues
#   TRABAJOS="OB:4:mono BS:4:nb_sin" bash cola_tesis_experimento.sh   # solo algunos
cd "$(dirname "$0")"
SW=$(pwd); OB=$SW/../Elmo_ob
PY=${PY:-$HOME/miniconda3/envs/py310/bin/python}
E=$SW/output/Resultados_finales_tesis/Mina_modelo/experimento_algoritmos
D_SW=$SW/data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte/05C/RED_GEN_BESS
D_OB=$OB/data/Resultados_finales_tesis/Mina_modelo_v4/Produccion_base/zona_norte/05C/P_red_gen_bat
HORAS=${HORAS:-12}; CPU_LS=${CPU_LS:-28}; GAP=0.01; SIN_MEJORA=4
CPU_A=${CPU_A:-0-62:2}; CPU_B=${CPU_B:-1-63:2}
# Orden por tamaño (4 años primero): si falta tiempo, quedan completas las instancias chicas.
TRABAJOS=${TRABAJOS:-"OB:4:mono BS:4:mono OB:4:nb_sin BS:4:nb_sin OB:4:nb_con BS:4:nb_con OB:4:ls_sin BS:4:ls_sin BS:4:hibrido OB:4:hibrido
OB:10:mono BS:10:mono OB:10:nb_sin BS:10:nb_sin OB:10:ls_sin BS:10:ls_sin OB:10:hibrido"}
NB_COMUN="--solver gurobi --days_per_year 4 --free_charging --free_maintenance --block_build_jobs 1 \
  --mode decomposed --gap_tol $GAP --max_iter 50 --no_monolithic_lp_bound --block_mip_focus 1 \
  --solve_timelimit 120 --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
  --polish_each_iter --skip_last_backward --stab_free_every 3 --stabilization box"
OPB="--operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400"

mkdir -p $E
LOG=$E/experimento.log
msg() { echo "[experimento $1] $(date '+%F %T') $2" | tee -a $LOG; }

# RANURAS="A" / "B" / "A B" (default): que sockets usa esta instancia. Dos instancias
# (p. ej. A ya y B al terminar la campaña) comparten la cola de trabajos: solo la
# primera la reinicia (REINICIAR_COLA=1); la otra se suma con REINICIAR_COLA=0.
# ESPERAR=1 (default) espera a que no quede ningun cola_tesis_traccion.sh.
RANURAS=${RANURAS:-A B}; ESPERAR=${ESPERAR:-1}; REINICIAR_COLA=${REINICIAR_COLA:-1}
if [ $ESPERAR = 1 ] && pgrep -f cola_tesis_traccion.sh > /dev/null; then
  msg "espera" "ranuras $RANURAS: cola_tesis_traccion.sh sigue corriendo, se espera a que termine"
  while pgrep -f cola_tesis_traccion.sh > /dev/null; do sleep 120; done
fi
# gurobi.env: se repone mientras corra alguna instancia (otras colas lo borran al terminar).
touch $E/.activa_$$
( while [ -f $E/.activa_$$ ]; do for r in $SW $OB; do [ -f $r/gurobi.env ] || echo "Threads 16" > $r/gurobi.env; done; sleep 30; done ) &

# STOP del NB tras $SIN_MEJORA iteraciones cerradas sin mejora del UB (o 2 sin solucion).
vigia() {
  local L=$1 STOPF=$2
  while true; do
    if $PY -c "
import re,sys
t=open('$L',encoding='utf-8',errors='replace').read()
if len(re.findall(r'k=\d+  UB=inf',t))>=2: sys.exit(0)
ub=[float(u) for u in re.findall(r'k=\d+  UB=([\d.]+)\s+LB=',t)]
s=0
for i in range(len(ub)-1,0,-1):
    if ub[i] < ub[i-1]-1e-6: break
    s+=1
sys.exit(0 if s>=$SIN_MEJORA else 1)" 2>/dev/null; then touch $STOPF; return; fi
    sleep 30
  done
}

nb() {  # nb <cpus> <repo> <datos> <salida> <horas> <flags...>
  local CPUS=$1 REPO=$2 DAT=$3 R=$4 H=$5; shift 5
  mkdir -p $R/benders; rm -f $R/benders/STOP
  vigia $R/benders.log $R/benders/STOP & local VP=$!
  (cd $REPO && taskset -c $CPUS $PY -u setup.py --data_folder $DAT --output_folder $R/benders --max_hours $H $NB_COMUN "$@") \
      > $R/benders.log 2>&1
  kill $VP 2>/dev/null; wait $VP 2>/dev/null
}

ub_final_nb() {  # UB final del benders.log: swap "[NestedBenders] termino en ... UB=1,234.5" u OB "NestedBenders: UB=1234.5"
  $PY -c "
import re;t=open('$1',encoding='utf-8',errors='replace').read()
m=re.findall(r'termino en[^\n]*: UB=([\d,.]+)',t) or re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)',t)
print(m[-1].replace(',','') if m else '')"
}

ls_desde_nb() {  # ls_desde_nb <T> <datos> <salida> <cpus> <t0> <tag> <flags del L-shaped...>
  local T=$1 DAT=$2 R=$3 CPUS=$4 t0=$5 TAG=$6; shift 6
  local ub=$(ub_final_nb $R/benders.log) resto=$($PY -c "print(round($HORAS - ($(date +%s) - $t0)/3600, 2))")
  if [ -z "$ub" ] || [ ! -f $R/benders/X.json ]; then msg "$TAG" "el NB no dejo solucion (ver benders.log)"; return; fi
  msg "$TAG" "incumbente del NB: UB $ub; L-shaped $resto h desde ahi"
  local REPO=$SW; [ $T = OB ] && REPO=$OB
  (cd $REPO && taskset -c $CPUS $PY -u lshaped.py --data_folder $DAT "$@" --solucion $R/benders --ub_esperado $ub \
      --out $R/lshaped --horas $resto --gap $GAP --cpu $CPU_LS --capacidad station_1=1) > $R/lshaped.log 2>&1
}

correr() {  # correr <OB|BS> <años> <config> <cpus>
  local T=$1 N=$2 C=$3 CPUS=$4 R=$E/$1/$2anios/$3 t0=$(date +%s)
  local TAG="$T ${N}a $C"
  if [ -f $R/FIN ]; then msg "$TAG" "ya hecho, se salta"; return; fi
  mkdir -p $R
  msg "$TAG" "inicio (cpus $CPUS)"
  local REPO=$SW DAT=$D_SW; [ $T = OB ] && { REPO=$OB; DAT=$D_OB; }
  case "$T:$C" in
    *:mono)
      local MC=""; [ $T = BS ] && MC="--mccormick_degradation"
      (cd $REPO && taskset -c $CPUS $PY -u setup.py --data_folder $DAT --solver gurobi --days_per_year 4 \
          --n_years $N --free_charging --free_maintenance --mode monolithic $MC --gap_tol $GAP \
          --mono_timelimit $((HORAS*3600)) --output_folder $R/monolitico) > $R/monolitico.log 2>&1 ;;
    OB:nb_sin) nb $CPUS $REPO $DAT $R $HORAS --n_years $N --stab_center first --box_delta_int 1 --strengthen --strengthened_timelimit 300 ;;
    BS:nb_sin) nb $CPUS $REPO $DAT $R $HORAS --n_years $N --stab_center first --box_delta_int 2 --cut_type strengthened --strengthened_timelimit 300 ;;
    OB:nb_con) nb $CPUS $REPO $DAT $R $HORAS --n_years $N $OPB --stab_center operational --box_delta_int 1 --strengthen --strengthened_timelimit 300 ;;
    BS:nb_con|BS:hibrido)
      local ITER=50; [ $C = hibrido ] && ITER=0
      [ -f $R/corte_cargas_lhd.json ] || taskset -c $CPUS $PY -u corte_cargas_op.py --data_folder $DAT --n_years $N \
          --out $R/corte_cargas_lhd.json --tmp $R/_corte_cargas_op > $R/corte_cargas_op.log 2>&1
      nb $CPUS $REPO $DAT $R $HORAS --n_years $N $OPB --corte_cargas_lhd $R/corte_cargas_lhd.json --stab_center operational \
          --box_delta_int 2 --cut_type strengthened --strengthened_timelimit 300 --fijar_R op --evaluar_trayectoria_op \
          --reparar_dp --max_iter $ITER
      [ $C = hibrido ] && ls_desde_nb BS $DAT $R $CPUS $t0 "$TAG" --n_years $N ;;
    OB:hibrido)
      nb $CPUS $REPO $DAT $R $HORAS --n_years $N $OPB --stab_center operational --box_delta_int 1 --strengthen \
          --strengthened_timelimit 300 --max_iter 1
      ls_desde_nb OB $DAT $R $CPUS $t0 "$TAG" --free_charging --free_maintenance --n_years $N ;;
    OB:ls_sin)
      (cd $OB && taskset -c $CPUS $PY -u lshaped.py --data_folder $DAT --free_charging --free_maintenance --n_years $N \
          --out $R/lshaped --horas $HORAS --gap $GAP --cpu $CPU_LS --capacidad station_1=1) > $R/lshaped.log 2>&1 ;;
    BS:ls_sin)
      taskset -c $CPUS $PY -u lshaped.py --data_folder $DAT --n_years $N --sin_dp --out $R/lshaped --horas $HORAS \
          --gap $GAP --cpu $CPU_LS --capacidad station_1=1 > $R/lshaped.log 2>&1 ;;
    *) msg "$TAG" "configuracion desconocida"; return ;;
  esac
  local h=$($PY -c "print(round(($(date +%s) - $t0)/3600, 2))")
  local res=$(grep -ah "^\[NestedBenders\] termino\|Best objective\|\[LS\] FINAL\|FINAL" $R/*.log 2>/dev/null | tail -1 | cut -c1-200)
  echo "$h h | $res" > $R/FIN
  msg "$TAG" "FIN en $h h: $res"
}

# Cola compartida: cada ranura toma el siguiente trabajo libre (flock sobre un contador).
read -r -a LISTA <<< "$(echo $TRABAJOS)"
[ $REINICIAR_COLA = 1 ] && echo 0 > $E/.siguiente
ranura() {  # ranura <nombre> <cpus>
  local nom=$1 cpus=$2 i
  while true; do
    i=$( (flock 9; n=$(cat $E/.siguiente); echo $((n+1)) > $E/.siguiente; echo $n) 9> $E/.lock )
    [ $i -ge ${#LISTA[@]} ] && break
    IFS=: read -r T N C <<< "${LISTA[$i]}"
    correr $T $N $C $cpus
  done
  msg "$nom" "ranura terminada"
}

msg "inicio" "ranuras $RANURAS | ${#LISTA[@]} trabajos: ${LISTA[*]} | $HORAS h por corrida, gap $GAP"
PIDS=""
for r in $RANURAS; do
  [ $r = A ] && { ranura A $CPU_A & PIDS="$PIDS $!"; }
  [ $r = B ] && { ranura B $CPU_B & PIDS="$PIDS $!"; }
done
wait $PIDS
rm -f $E/.activa_$$
if ls $E/.activa_* > /dev/null 2>&1; then
  msg "fin" "instancia de las ranuras $RANURAS terminada (queda otra instancia corriendo)"
else
  rm -f $SW/gurobi.env $OB/gurobi.env
  msg "fin" "experimento terminado (gurobi.env borrados)"
fi
