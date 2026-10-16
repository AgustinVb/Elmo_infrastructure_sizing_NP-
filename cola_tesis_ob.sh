#!/bin/bash
# Casos de estudio finales de la tesis, OB (Mina_modelo), en cola y por prioridad.
# Metodo: el de cola_v3_swap.sh de battery_swapping_multiaño (igual que cola_v3_ob.sh):
#   1. Nested Benders hasta gap 1 % o HORAS (6 h), con CORTE AUTOMATICO: si el UB
#      lleva 4 iteraciones cerradas sin mejorar, se crea STOP y Benders corta
#      conservando la solucion.
#   2. mejorar_ub pasada 1 (años 1-10, 600 s) y pasada 2 (años 3-9, 900 s).
#   3. parameters + consumer sobre la mejor solucion y certificado (--evaluar_vivas).
# El caso 1 (base/norte/05C/P_red, sin generacion) se copia de P_red_final: mismos
# datos salvo GenProfiles, que P_red no usa. Solo se le agrega la pasada 2 + certificado.
#
# RONDA 2 (RONDA=2): relanza solo los casos cuyo certificado de la ronda 1 quedo en
# gap >= 1 %. Benders retoma el incumbente_respaldo.pkl de la ronda 1 (UB, cortes,
# LB y centro de la caja) por HORAS_R2 (12 h) y repite mejorar_ub + certificado.
# Salida en <caso>/r2/. El resumen final usa la ronda 2 cuando existe.
#
# Tiempo: tope global TOPE_H (66 h) por ronda, desde la primera vez que se lanza
# (en $V/.inicio_r<ronda>; borrarlo reinicia el reloj). Cada Benders recibe
# min(HORAS, horas que quedan - RESERVA_H); si no alcanza MIN_H, el caso se salta.
#
# Se retoma solo: un caso con certificado se salta, y un Benders terminado no se repite.
#   PY=.venv_elmo/Scripts/python.exe bash cola_tesis_ob.sh          # ronda 1, todo
#   PY=... bash cola_tesis_ob.sh 3 4                                # solo los casos 3 y 4
#   RONDA=2 PY=... bash cola_tesis_ob.sh                            # relanza los >= 1 %
#   Para cortar un Benders a mano: crear el archivo STOP en su carpeta benders/.
cd "$(dirname "$0")"
PY=${PY:-.venv_elmo/Scripts/python.exe}
DATA=data/Resultados_finales_tesis/Mina_modelo
O=output/Resultados_finales_tesis/Mina_modelo
V=$O/casos_finales
RONDA=${RONDA:-1}
HORAS=${HORAS:-6}; HORAS_R2=${HORAS_R2:-12}
TOPE_H=${TOPE_H:-66}; RESERVA_H=${RESERVA_H:-2.5}; MIN_H=${MIN_H:-2}
GAP=0.01; ITER=50; SIN_MEJORA=4
FLAGS="--free_charging --free_maintenance"

# En orden de prioridad (relativos a $DATA). "|copia": se toma de P_red_final.
CASOS=(
  "Produccion_base/zona_norte/05C/P_red|copia"
  "Produccion_base/zona_norte/05C/P_red_gen_bat"
  "Produccion_base/zona_norte/05C/P_red_gen"
  "Produccion_escalada/zona_norte/05C/P_red"
  "Produccion_escalada/zona_norte/05C/P_red_gen_bat"
  "Produccion_base/zona_centro/05C/P_red_gen_bat"
  "Produccion_base/zona_norte/1C/P_red_gen_bat"
  "Produccion_base/zona_norte/2C/P_red_gen_bat"
)
SEL=${@:-$(seq 1 ${#CASOS[@]})}

mkdir -p $V
LOG=$V/cola.log
[ -f $V/.inicio_r$RONDA ] || date +%s > $V/.inicio_r$RONDA
INICIO=$(cat $V/.inicio_r$RONDA); FIN=$((INICIO + ${TOPE_H%.*} * 3600))
horas() { awk -v s=$1 'BEGIN{printf "%.1f", s/3600}'; }
msg() {
  local ahora=$(date +%s)
  echo "[cola r$RONDA $1] $(date '+%F %T') (llevo $(horas $((ahora-INICIO))) h, quedan $(horas $((FIN-ahora))) h) $2" | tee -a $LOG
}

# Crea STOP cuando el UB lleva $SIN_MEJORA iteraciones cerradas sin mejorar.
vigia() {
  local L=$1 STOPF=$2 TAG=$3
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
      touch $STOPF; msg "$TAG" "$SIN_MEJORA iteraciones sin mejora del UB: STOP"
      return
    fi
    sleep 30
  done
}
leer_benders() {  # "UB LB" de la linea final o de la ultima iteracion cerrada
  $PY -c "
import re;t=open('$1',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t) or re.findall(r'k=\d+  UB=([\d.]+)\s+LB=([\d.]+)',t)
print(*(m[-1] if m else ('','')))" | tr -d '\r'
}
ub_resumen() {  # "solucion ub_final" de un resumen.json de mejorar_ub, o "- -"
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print(r.get('solucion') or '-', repr(r['ub_final']))" | tr -d '\r' || echo "- -"
}
certificado() { grep -E 'LB CERTIFICADA' $1 2>/dev/null | tail -1 | sed 's/^\[cert\] //'; }
gap_de() { certificado $1 | sed -E 's/.*gap ([0-9.]+)%.*/\1/'; }

resumen_final() {
  {
    echo "== Resumen ($(date '+%F %T'))"
    local i=0
    for c in "${CASOS[@]}"; do
      i=$((i+1)); local caso=${c%%|*} R=$V/${c%%|*} r=r1
      local f=$R/certificado.log
      [ -n "$(certificado $R/r2/certificado.log)" ] && { f=$R/r2/certificado.log; r=r2; }
      local linea=$(certificado $f)
      if [ -z "$linea" ]; then printf "%d. %-45s  sin certificado\n" $i $caso; continue; fi
      local ok=$(awk -v g=$(gap_de $f) 'BEGIN{print (g<1.0)?"OK <1%":"NO cierra 1%"}')
      printf "%d. %-45s  %s %-12s %s\n" $i $caso $r "$ok" "$linea"
    done
  } | tee $V/resumen.txt
}

msg "inicio" "casos $(echo $SEL | tr '\n' ' ')| Benders $([ $RONDA = 2 ] && echo $HORAS_R2 || echo $HORAS) h por caso, tope global $TOPE_H h (hasta $(date -d @$FIN '+%F %T'))"

for n in $SEL; do
  c=${CASOS[$((n-1))]}; [ -z "$c" ] && { msg "?" "no existe el caso $n"; continue; }
  caso=${c%%|*}; COPIA=0; [[ "$c" == *"|copia" ]] && COPIA=1
  D=$DATA/$caso; R1=$V/$caso; TAG="$n/${#CASOS[@]} $caso"
  INC=""; H_CASO=$HORAS
  if [ $RONDA = 2 ]; then
    G1=$(gap_de $R1/certificado.log)
    if [ -z "$G1" ]; then msg "$TAG" "sin certificado de la ronda 1: se salta"; continue; fi
    if awk -v g=$G1 'BEGIN{exit !(g<1.0)}'; then msg "$TAG" "ronda 1 ya cerro ($G1 %): se salta"; continue; fi
    R=$R1/r2; INC="--incumbente_inicial $R1/benders/incumbente_respaldo.pkl"; H_CASO=$HORAS_R2; COPIA=0
    msg "$TAG" "ronda 1 quedo en $G1 %: se relanza desde su incumbente"
  else
    R=$R1
  fi
  B=$R/benders
  mkdir -p $R
  if [ -n "$(certificado $R/certificado.log)" ]; then msg "$TAG" "ya certificado, se salta: $(certificado $R/certificado.log)"; continue; fi

  # --- 1. Benders -----------------------------------------------------------
  if [ $COPIA = 1 ]; then
    if [ ! -f $B/incumbente_respaldo.pkl ]; then
      mkdir -p $B $R/mejorar_ub_1
      cp -r $O/P_red_final/. $B/; rm -rf "${B:?}"/_mps "${B:?}"/_monolitico
      cp $O/P_red_final.log $R/benders.log
      cp -r $O/mejorar_ub_P_red_final/. $R/mejorar_ub_1/
      cp $O/mejorar_ub_P_red_final.log $R/mejorar_ub_1.log
      msg "$TAG" "copiado de P_red_final y mejorar_ub_P_red_final (P_red no usa generacion)"
    fi
  elif grep -qE '^NestedBenders' $R/benders.log 2>/dev/null && [ -f $B/incumbente_respaldo.pkl ]; then
    msg "$TAG" "Benders ya terminado, se usa $R/benders.log"
  else
    QUEDAN=$(( FIN - $(date +%s) ))
    H=$(awk -v q=$QUEDAN -v r=$RESERVA_H -v t=$H_CASO 'BEGIN{h=q/3600-r; if(h>t)h=t; printf "%.2f", h}')
    if awk -v h=$H -v m=$MIN_H 'BEGIN{exit !(h<m)}'; then
      msg "$TAG" "SALTADO: solo quedarian $H h para Benders (minimo $MIN_H h)"; continue
    fi
    msg "$TAG" "Nested Benders (max $H h, gap $GAP, corte tras $SIN_MEJORA iteraciones sin mejora)"
    mkdir -p $B; rm -f $B/STOP
    vigia $R/benders.log $B/STOP "$TAG" & VP=$!
    $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS \
        --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter $ITER \
        --max_hours $H --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
        --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
        --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
        --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
        --polish_each_iter --skip_last_backward $INC 2>&1 | tee $R/benders.log
    kill $VP 2>/dev/null; wait $VP 2>/dev/null
  fi
  read UB0 LBOP < <(leer_benders $R/benders.log)
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then msg "$TAG" "Benders sin resultado: se salta"; continue; fi
  msg "$TAG" "Benders: UB $UB0  LB $LBOP"

  # --- 2. mejorar_ub --------------------------------------------------------
  if [ $COPIA = 0 ] && [ ! -f $R/mejorar_ub_1/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 1 (años 1-10, 600 s)"
    $PY -u mejorar_ub.py --data_folder $D $FLAGS --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
        --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1 2>&1 | tee $R/mejorar_ub_1.log
  fi
  read SOL UB < <(ub_resumen $R/mejorar_ub_1/resumen.json)
  [ $COPIA = 1 ] && SOL=$R/mejorar_ub_1/solucion
  if [ "$SOL" = "-" ]; then FUENTE="--respaldo $B/incumbente_respaldo.pkl"; UB=$UB0; SOL=""
  else FUENTE="--solucion $SOL"; fi
  msg "$TAG" "tras pasada 1: UB $UB (${SOL:-respaldo de Benders})"

  if [ ! -f $R/mejorar_ub_2/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 2 (años 3-9, 900 s)"
    $PY -u mejorar_ub.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
        --timelimit 900 --jobs 4 --out $R/mejorar_ub_2 2>&1 | tee $R/mejorar_ub_2.log
  fi
  read SOL2 UB2 < <(ub_resumen $R/mejorar_ub_2/resumen.json)
  if [ "$SOL2" != "-" ]; then SOL=$SOL2; UB=$UB2; FUENTE="--solucion $SOL"; fi
  msg "$TAG" "tras pasada 2: UB $UB (${SOL:-respaldo de Benders})"
  if [ -n "$SOL" ]; then
    $PY -u escribir_parameters.py --data_folder $D $FLAGS $SOL 2>&1 | tee $R/consumer.log
    $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log
  fi

  # --- 3. certificado -------------------------------------------------------
  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))" | tr -d '\r')
  msg "$TAG" "certificar (UB $UB, gap_objetivo $GO)"
  $PY -u certificar_inversiones.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB --gap_objetivo $GO \
      --evaluar_vivas --jobs 4 --out $R/certificado 2>&1 | tee $R/certificado.log
  msg "$TAG" "FIN: $(certificado $R/certificado.log)"
  resumen_final > /dev/null
done

resumen_final | tee -a $LOG
msg "fin" "ronda $RONDA terminada; resumen en $V/resumen.txt"
[ $RONDA = 1 ] && grep -q "NO cierra" $V/resumen.txt && msg "fin" "hay casos >= 1 %: relanzarlos con  RONDA=2 PY=$PY bash cola_tesis_ob.sh"
