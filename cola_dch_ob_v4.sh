#!/bin/bash
# Casos DCH multiaño, OB, caso red (P_red), Bateria_482, costo fijo, tasas de carga 0,5C / 1C
# / 2C. Datos en data/Resultados_finales_tesis/DCH (armados por armar_datos_dch.py sobre la
# plantilla Mina_modelo_v4: plan DCH 2035-2045 de 11 años, consumo WP2, degradacion y
# cargadores de la mina modelo, limites fisicos DCH).
#
# Mismo esquema que cola_tesis_ob_v4.sh: Benders -> (si no cierra bajo GAP) mejorar_ub
# pasadas 1 y 2 -> parameters + consumer -> certificado; ronda 2 para los casos >= 1 %.
# Lo que cambia respecto de la mina modelo:
#   - pausas DCH, las del modelo anual de carga_on_board (DCH septiembre, carga libre):
#     --pause_scheme dch (mantenciones sin operar ni cargar, colacion por grupos) en vez de
#     las ventanas DET con --free_maintenance. Las cotas de produccion son las de la mina
#     modelo (floor-1..ceil+2). La primera corrida, con pausas DET, quedo en
#     DCH/casos_v4_det_cotas_anchas;
#   - se respeta la colacion DCH todos los años (FleetByYear.opera_en_colacion = 0): 1C y 2C son
#     factibles en el año 7 sin operar en colacion;
#   - 11 años y consumo WP2 en TODOS los pasos ($COMUN): mejorar_ub, certificar y
#     escribir_parameters traen por defecto 10 años y wp1;
#   - X por defecto: las 3 estaciones abiertas desde el año 1, aunque ese año solo opera
#     LH518B_1. Es la regla del modelo (apertura_solo_primer_anio en functions.py: una
#     estacion se abre en el año 1 o nunca); con un X que abre estaciones despues, el UB del
#     Benders no las cobra y mejorar_ub aborta porque el pulido no calza;
#   - no hay solucion previa: el Benders parte de cero, con HORAS = 6 (como la v3 y el caso 9);
#   - mejorar_ub: pasada 1 en los años 1-11, pasada 2 en los años 3-9 (9 a 12 LHD).
#
#   bash cola_dch_v4_todo.sh                         # ronda 1 y despues ronda 2 (lo normal)
#   PY=... bash cola_dch_ob_v4.sh 2 3                # solo algunos casos, ronda 1
#   RONDA=2 PY=... bash cola_dch_ob_v4.sh            # solo la ronda 2
#   Para cortar un Benders a mano: crear el archivo STOP en su carpeta benders/.
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
PY=${PY:-.venv_elmo/Scripts/python.exe}
DATA=${DATA:-data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo}
V=${V:-output/Resultados_finales_tesis/DCH/casos_finales_v4_Carga_libre}
RONDA=${RONDA:-1}
NY=${NY:-11}; ANIOS_1=${ANIOS_1:-1,2,3,4,5,6,7,8,9,10,11}; ANIOS_2=${ANIOS_2:-3,4,5,6,7,8,9}
HORAS=${HORAS:-6}; HORAS_R2=${HORAS_R2:-8}
if [ $RONDA = 2 ]; then TOPE_H=${TOPE_H:-30}; else TOPE_H=${TOPE_H:-36}; fi
RESERVA_H=${RESERVA_H:-2.5}; MIN_H=${MIN_H:-1}
GAP=${GAP:-0.01}; ITER=50; SIN_MEJORA=4
FLAGS="--free_charging --pause_scheme dch"
COMUN="--n_years $NY --consumption_model wp2"

CASOS=(
  "05C/P_red"
  "1C/P_red"
  "2C/P_red"
)
# Por defecto solo 1C y 2C: con pausas DCH, 0,5C es infactible en el año 7 (2 cargadores de 241 kW
# por estacion no recargan 4 LHD a 54.441 t/dia; probado 2026-10-29). Para intentarlo igual:
# bash cola_dch_ob_v4.sh 1
SEL=${@:-2 3}

mkdir -p $V
LOG=$V/cola.log
[ -f $V/.inicio_r$RONDA ] || date +%s > $V/.inicio_r$RONDA
INICIO=$(cat $V/.inicio_r$RONDA); FIN=$((INICIO + ${TOPE_H%.*} * 3600))
horas() { awk -v s=$1 'BEGIN{printf "%.1f", s/3600}'; }
msg() {
  local ahora=$(date +%s)
  echo "[dch v4 r$RONDA $1] $(date '+%F %T') (llevo $(horas $((ahora-INICIO))) h, quedan $(horas $((FIN-ahora))) h) $2" | tee -a $LOG
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
import json;r=json.load(open('$1')); print((r.get('solucion') or '-').replace(chr(92), '/'), repr(r['ub_final']))" | tr -d '\r' || echo "- -"
}
mejor_solucion() {  # mejor_solucion <carpeta_benders> <ub_benders> <dir_caso>...: "carpeta UB" de menor UB
  local best=$1 ub=$2 dir p s u; shift 2
  for dir in "$@"; do for p in mejorar_ub_2 mejorar_ub_1; do
    read -r s u < <(ub_resumen $dir/$p/resumen.json)
    if [ "$s" != "-" ] && [ -n "$s" ] && awk -v a=$u -v b=$ub 'BEGIN{exit !(a<b-1e-6)}'; then best=$s; ub=$u; fi
  done; done
  echo "$best $ub"
}
certificado() { grep -E 'LB CERTIFICADA' $1 2>/dev/null | tail -1 | sed 's/^\[cert\] //'; }
gap_de() { certificado $1 | sed -E 's/.*gap ([0-9.]+)%.*/\1/'; }

# Corre un paso hasta 3 veces si el proceso muere. El codigo 1 es un aborto propio de
# mejorar_ub/certificar (el incumbente no se reconstruye): repetirlo no sirve.
reintentar() {  # reintentar <log> <comando...>
  local LOGF=$1; shift; local k rc
  : > "$LOGF"
  for k in 1 2 3; do
    "$@" 2>&1 | tee -a "$LOGF"; rc=${PIPESTATUS[0]}
    [ "$rc" = 0 ] && return 0
    [ "$rc" = 1 ] && { msg "$TAG" "$(basename "$LOGF"): aborto (codigo 1), no se reintenta"; return 1; }
    msg "$TAG" "$(basename "$LOGF"): el proceso murio (codigo $rc, intento $k); se reintenta"
  done
  return 1
}

# Nested Benders con reintentos: si el proceso muere sin la linea final, se relanza desde
# el respaldo de ESTA corrida (cortes y cota validos, sin recalcular la cota operacional).
benders() {  # benders <datos> <salida> <horas> "<--incumbente_inicial ...>"
  local D=$1 R=$2 HMAX=$3 INC=$4 B=$2/benders OP="--operational_bound" intento H
  local FIN_CASO=$(( $(date +%s) + $(awk -v h=$HMAX 'BEGIN{printf "%d", h*3600}') ))
  mkdir -p $B
  for intento in 1 2 3 4 5 6; do
    H=$(awk -v a=$((FIN_CASO - $(date +%s))) -v b=$((FIN - $(date +%s))) -v r=$RESERVA_H \
        'BEGIN{h=a/3600; g=b/3600-r; if(g<h)h=g; printf "%.2f", h}')
    if awk -v h=$H 'BEGIN{exit !(h<0.5)}'; then msg "$TAG" "sin tiempo para otro intento de Benders ($H h)"; return 1; fi
    msg "$TAG" "Nested Benders intento $intento (max $H h, gap $GAP, corte tras $SIN_MEJORA iteraciones sin mejora) ${INC:-desde cero}"
    rm -f $B/STOP
    vigia $R/benders.log $B/STOP "$TAG" & local VP=$!
    $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS $COMUN \
        --block_build_jobs 1 --output_folder $B --mode decomposed --gap_tol $GAP --max_iter $ITER \
        --max_hours $H $OP --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
        --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
        --stab_free_every 3 --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
        --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
        --polish_each_iter --skip_last_backward $INC 2>&1 | tee -a $R/benders.log
    kill $VP 2>/dev/null; wait $VP 2>/dev/null
    grep -qE '^NestedBenders' $R/benders.log && return 0
    if [ -f $B/incumbente_respaldo.pkl ]; then
      cp $B/incumbente_respaldo.pkl $B/respaldo_intento$intento.pkl
      INC="--incumbente_inicial $B/respaldo_intento$intento.pkl"; OP=""
    fi
    msg "$TAG" "Benders murio en el intento $intento; se reintenta desde su respaldo"
  done
  return 1
}

resumen_final() {
  {
    echo "== Resumen DCH v4 ($(date '+%F %T'))"
    local i=0
    for c in "${CASOS[@]}"; do
      i=$((i+1)); local caso=$c R=$V/$c r=r1
      local f=$R/certificado.log s=$R/sin_certificado.txt
      if [ -n "$(certificado $R/r2/certificado.log)" ]; then f=$R/r2/certificado.log; r=r2
      elif [ -f $R/r2/sin_certificado.txt ]; then f=""; s=$R/r2/sin_certificado.txt; r=r2
      elif [ -z "$(certificado $f)" ] && [ -f $s ]; then f=""
      fi
      if [ -z "$f" ]; then
        printf "%d. %-12s  %s %-12s %s\n" $i $caso $r "OK <1%" "sin certificado, brecha del Benders: $(cat $s)"; continue
      fi
      local linea=$(certificado $f)
      if [ -z "$linea" ]; then printf "%d. %-12s  sin certificado\n" $i $caso; continue; fi
      local ok=$(awk -v g=$(gap_de $f) 'BEGIN{print (g<1.0)?"OK <1%":"NO cierra 1%"}')
      printf "%d. %-12s  %s %-12s %s\n" $i $caso $r "$ok" "$linea"
    done
  } | tee $V/resumen.txt
}

msg "inicio" "casos $(echo $SEL | tr '\n' ' ')| $NY años | Benders $([ $RONDA = 2 ] && echo $HORAS_R2 || echo $HORAS) h por caso, tope global $TOPE_H h (hasta $(date -d @$FIN '+%F %T'))"

for n in $SEL; do
  caso=${CASOS[$((n-1))]}; [ -z "$caso" ] && { msg "?" "no existe el caso $n"; continue; }
  D=$DATA/$caso; R1=$V/$caso; TAG="$n/${#CASOS[@]} $caso"
  if [ $RONDA = 2 ]; then
    if [ -f $R1/sin_certificado.txt ]; then msg "$TAG" "ronda 1 cerro con el Benders bajo 1 % (sin certificado): se salta"; continue; fi
    G1=$(gap_de $R1/certificado.log)
    if [ -z "$G1" ]; then msg "$TAG" "sin certificado de la ronda 1: se salta"; continue; fi
    if awk -v g=$G1 'BEGIN{exit !(g<1.0)}'; then msg "$TAG" "ronda 1 ya cerro ($G1 %): se salta"; continue; fi
    R=$R1/r2; INC="--incumbente_inicial $R1/benders/incumbente_respaldo.pkl"; H_CASO=$HORAS_R2
    msg "$TAG" "ronda 1 quedo en $G1 %: se relanza desde su respaldo"
  else
    R=$R1; INC=""; H_CASO=$HORAS
  fi
  B=$R/benders
  mkdir -p $R
  if [ -n "$(certificado $R/certificado.log)" ]; then msg "$TAG" "ya certificado, se salta: $(certificado $R/certificado.log)"; continue; fi
  if [ -f $R/sin_certificado.txt ]; then msg "$TAG" "ya cerrado con el Benders bajo 1 % (sin certificado), se salta"; continue; fi

  # --- 1. Benders -----------------------------------------------------------
  if grep -qE '^NestedBenders' $R/benders.log 2>/dev/null && [ -f $B/incumbente_respaldo.pkl ]; then
    msg "$TAG" "Benders ya terminado, se usa $R/benders.log"
  else
    if [ -f $B/incumbente_respaldo.pkl ]; then   # una cola anterior murio a mitad del Benders
      cp $B/incumbente_respaldo.pkl $B/respaldo_cola.pkl
      INC="--incumbente_inicial $B/respaldo_cola.pkl"
      msg "$TAG" "Benders a medias de una cola anterior: se retoma su respaldo"
    fi
    H=$(awk -v q=$((FIN - $(date +%s))) -v r=$RESERVA_H -v t=$H_CASO 'BEGIN{h=q/3600-r; if(h>t)h=t; printf "%.2f", h}')
    if awk -v h=$H -v m=$MIN_H 'BEGIN{exit !(h<m)}'; then
      msg "$TAG" "SALTADO: solo quedarian $H h para Benders (minimo $MIN_H h)"; continue
    fi
    benders $D $R $H "$INC"
  fi
  read -r UB0 LBOP < <(leer_benders $R/benders.log)
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then msg "$TAG" "Benders sin resultado: se salta"; continue; fi
  msg "$TAG" "Benders: UB $UB0  LB $LBOP"

  # --- Benders ya bajo GAP: sin mejorar_ub ni certificado (regla de la usuaria) ---
  if awk -v u=$UB0 -v l=$LBOP -v g=$GAP 'BEGIN{exit !((u-l)/u < g)}'; then
    DIRS=$R; [ $RONDA = 2 ] && DIRS="$R $R1"
    read -r SOL UB < <(mejor_solucion $B $UB0 $DIRS)
    $PY -u escribir_parameters.py --data_folder $D $FLAGS $COMUN $SOL 2>&1 | tee $R/consumer.log
    $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log
    G=$(awk -v u=$UB -v l=$LBOP 'BEGIN{printf "%.3f", 100*(u-l)/u}')
    echo "UB $UB  LB $LBOP  gap $G %  solucion $SOL" > $R/sin_certificado.txt
    msg "$TAG" "FIN sin mejorar_ub ni certificado (Benders bajo 1 %): UB $UB  LB $LBOP  gap $G %"
    resumen_final > /dev/null
    continue
  fi

  # --- 2. mejorar_ub --------------------------------------------------------
  if [ ! -f $R/mejorar_ub_1/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 1 (años $ANIOS_1, 600 s)"
    reintentar $R/mejorar_ub_1.log $PY -u mejorar_ub.py --data_folder $D $FLAGS $COMUN --respaldo $B/incumbente_respaldo.pkl \
        --ub_esperado $UB0 --anios $ANIOS_1 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1
  fi
  read -r SOL UB < <(ub_resumen $R/mejorar_ub_1/resumen.json)
  if [ "$SOL" = "-" ] || [ -z "$SOL" ]; then FUENTE="--respaldo $B/incumbente_respaldo.pkl"; UB=$UB0; SOL=""
  else FUENTE="--solucion $SOL"; fi
  msg "$TAG" "tras pasada 1: UB $UB (${SOL:-respaldo de Benders})"

  if [ ! -f $R/mejorar_ub_2/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 2 (años $ANIOS_2, 900 s)"
    reintentar $R/mejorar_ub_2.log $PY -u mejorar_ub.py --data_folder $D $FLAGS $COMUN $FUENTE --ub_esperado $UB \
        --anios $ANIOS_2 --timelimit 900 --jobs 4 --out $R/mejorar_ub_2
  fi
  read -r SOL2 UB2 < <(ub_resumen $R/mejorar_ub_2/resumen.json)
  if [ "$SOL2" != "-" ] && [ -n "$SOL2" ]; then SOL=$SOL2; UB=$UB2; FUENTE="--solucion $SOL"; fi
  msg "$TAG" "tras pasada 2: UB $UB (${SOL:-respaldo de Benders})"
  if [ $RONDA = 2 ]; then   # la ronda 2 parte del respaldo del Benders (por los cortes), no
    # de la mejor solucion de la ronda 1: no puede terminar certificando un UB peor que ese
    read -r SOLR1 UBR1 < <(ub_resumen $R1/mejorar_ub_2/resumen.json)
    { [ "$SOLR1" = "-" ] || [ -z "$SOLR1" ]; } && read -r SOLR1 UBR1 < <(ub_resumen $R1/mejorar_ub_1/resumen.json)
    if [ "$SOLR1" != "-" ] && [ -n "$SOLR1" ] && awk -v a=$UBR1 -v b=$UB 'BEGIN{exit !(a<b-1e-6)}'; then
      SOL=$SOLR1; UB=$UBR1; FUENTE="--solucion $SOL"
      msg "$TAG" "la ronda 1 tenia mejor UB ($UB): se certifica esa solucion"
    fi
  fi
  if [ -n "$SOL" ]; then
    $PY -u escribir_parameters.py --data_folder $D $FLAGS $COMUN $SOL 2>&1 | tee $R/consumer.log
    $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log
  fi

  # --- 3. certificado -------------------------------------------------------
  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))" | tr -d '\r')
  msg "$TAG" "certificar (UB $UB, gap_objetivo $GO)"
  reintentar $R/certificado.log $PY -u certificar_inversiones.py --data_folder $D $FLAGS $COMUN $FUENTE --ub_esperado $UB \
      --gap_objetivo $GO --evaluar_vivas --jobs 4 --out $R/certificado
  msg "$TAG" "FIN: $(certificado $R/certificado.log)"
  resumen_final > /dev/null
done

resumen_final | tee -a $LOG
msg "fin" "ronda $RONDA terminada; resumen en $V/resumen.txt"
