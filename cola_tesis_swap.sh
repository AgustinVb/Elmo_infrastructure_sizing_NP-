#!/bin/bash
# Casos de estudio finales de la tesis, SWAP (Mina_modelo), en cola y por
# prioridad: el equivalente de cola_tesis_ob.sh de carga_ob_multiaño, con los
# mismos 8 casos y el mismo metodo:
#   1. Nested Benders hasta gap 1 % o HORAS, con CORTE AUTOMATICO: si el UB
#      lleva 4 iteraciones cerradas sin mejorar, se crea STOP y Benders corta
#      conservando la solucion.
#   2. mejorar_ub pasada 1 (años 1-10, 600 s) y pasada 2 (años 3-9, 900 s).
#   3. parameters + consumer sobre la mejor solucion y certificado.
# Diferencias con OB (propias de swap):
#   - HORAS = 10 por caso en la ronda 1 (OB: 6), igual que los casos base de la v3:
#     las iteraciones de swap duran ~70 min (OB ~45) y las mejoras grandes del UB
#     llegaron hasta k=5 (~6,9 h en RED_GEN_BESS v3). Los que queden >= 1 % se
#     siguen en la ronda 2 desde su respaldo (12 h). Tope global 68 h: 5 casos
#     nuevos x (10 h + ~3 h) = 65 h.
#   - Casos 1-3 (base/norte/05C): si la cola v3 (cola_v3_swap.sh) ya los dejo
#     certificados en $O/v3, se COPIAN de ahi (misma data; Benders de hasta 18 h).
#     Si no, se corren como los demas.
#   - Corte de cargas por LHD en la cota operacional (--corte_cargas_lhd; CORTE=0
#     lo apaga): se precalcula POR CASO con corte_cargas_op.py, porque depende de
#     la produccion (escalada) y de la degradacion (1C/2C).
#   - Benders con --cut_type strengthened (el --strengthen de OB); mejorar_ub,
#     certificar y escribir_parameters sin flags de regimen (los fijan ellos), y
#     el certificado solo acepta --solucion (sin --evaluar_vivas).
#
# RONDA 2 (RONDA=2): relanza los casos cuyo certificado de la ronda 1 quedo en
# gap >= 1 %, retomando su incumbente_respaldo.pkl (--incumbente_inicial) por
# HORAS_R2 (12 h). Salida en <caso>/r2/. El resumen usa la ronda 2 si existe.
# Tope global TOPE_H por ronda desde la primera vez ($V/.inicio_r<ronda>).
#
#   bash cola_tesis_swap.sh                 # ronda 1, todos los casos
#   bash cola_tesis_swap.sh 4 5             # solo los casos 4 y 5
#   RONDA=2 bash cola_tesis_swap.sh         # relanza los >= 1 %
#   Para cortar un Benders a mano: crear el archivo STOP en su carpeta benders/.
cd "$(dirname "$0")"
PY=${PY:-~/miniconda3/envs/py310/bin/python}
DATA=${DATA:-data/Tesis_final/Mina_modelo}   # DATA=data/Tesis_final/Mina_modelo_v4 para la v4
O=output/Resultados_finales_tesis/Mina_modelo
V=${V:-$O/casos_finales}   # V=<carpeta> para pruebas aparte (p. ej. v4)
V3=$O/v3
RONDA=${RONDA:-1}
HORAS=${HORAS:-10}; HORAS_R2=${HORAS_R2:-12}
HORAS_ESCALADA_BESS=${HORAS_ESCALADA_BESS:-24}
TOPE_H=${TOPE_H:-68}; RESERVA_H=${RESERVA_H:-3}; MIN_H=${MIN_H:-2}
CORTE=${CORTE:-1}
GAP=0.01; ITER=50; SIN_MEJORA=4
FLAGS="--free_charging --free_maintenance"

# En orden de prioridad (relativos a $DATA). "|v3:<ESC>": se copia de la cola v3 si
# esta certificado ahi. "|alt:<caso>": si Benders no encuentra solucion factible
# (ningun UB tras 2 iteraciones, o sin resultado), se corre <caso> en su lugar
# (escalado al 20 %: primero con 0,5C; con 1C solo si 0,5C no es factible).
CASOS=(
  "Produccion_base/zona_norte/05C/RED|v3:RED"
  "Produccion_base/zona_norte/05C/RED_GEN_BESS|v3:RED_GEN_BESS"
  "Produccion_base/zona_norte/05C/RED_GEN|v3:RED_GEN"
  "Produccion_escalada20/zona_norte/05C/RED|alt:Produccion_escalada20/zona_norte/1C/RED"
  "Produccion_escalada20/zona_norte/05C/RED_GEN_BESS|alt:Produccion_escalada20/zona_norte/1C/RED_GEN_BESS"
  "Produccion_base/zona_centro/05C/RED_GEN_BESS"
  "Produccion_base/zona_norte/1C/RED_GEN_BESS"
  "Produccion_base/zona_norte/2C/RED_GEN_BESS"
)
# CASOS_ENV="<caso> [<caso> ...]": reemplaza la lista (pruebas aparte, p. ej. v4).
[ -n "$CASOS_ENV" ] && read -r -a CASOS <<< "$CASOS_ENV"
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
if len(re.findall(r'k=\d+  UB=inf',t))>=2: sys.exit(0)   # sin solucion factible
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
print(*(m[-1] if m else ('','')))"
}
ub_resumen() {  # "solucion ub_final" de un resumen.json de mejorar_ub, o "- -"
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print(r.get('solucion') or '-', repr(r['ub_final']))" || echo "- -"
}
certificado() { grep -E 'LB CERTIFICADA' $1 2>/dev/null | tail -1 | sed 's/^\[cert\] //'; }
gap_de() { certificado $1 | sed -E 's/.*gap ([0-9.]+)%.*/\1/'; }

# Copia un escenario certificado de la cola v3 a la carpeta del caso.
copiar_v3() {
  local E=$1 R=$2
  mkdir -p $R
  rsync -a --exclude _mps --exclude _monolitico $V3/$E/ $R/benders/
  cp $V3/$E.log $R/benders.log
  for p in "" _2; do
    local n=$([ -z "$p" ] && echo 1 || echo 2)
    if [ -d $V3/mejorar_ub_$E$p ]; then
      rsync -a --exclude _mps --exclude _monolitico $V3/mejorar_ub_$E$p/ $R/mejorar_ub_$n/
      cp $V3/mejorar_ub_$E$p.log $R/mejorar_ub_$n.log 2>/dev/null
    fi
  done
  rsync -a --exclude _mps --exclude _monolitico $V3/certificado_$E/ $R/certificado/
  cp $V3/certificado_$E.log $R/certificado.log
  cp $V3/consumer_$E.log $R/consumer.log 2>/dev/null
}

resumen_final() {
  {
    echo "== Resumen ($(date '+%F %T'))"
    local i=0
    for c in "${CASOS[@]}"; do
      i=$((i+1)); local caso=${c%%|*} R=$V/${c%%|*} r=r1
      if [[ "$c" == *"|alt:"* ]] && [ -z "$(certificado $R/certificado.log)" ] \
         && [ -n "$(certificado $V/${c##*|alt:}/certificado.log)" ]; then
        caso=${c##*|alt:}; R=$V/$caso
      fi
      local f=$R/certificado.log
      [ -n "$(certificado $R/r2/certificado.log)" ] && { f=$R/r2/certificado.log; r=r2; }
      local linea=$(certificado $f)
      if [ -z "$linea" ]; then printf "%d. %-45s  sin certificado\n" $i $caso; continue; fi
      local ok=$(awk -v g=$(gap_de $f) 'BEGIN{print (g<1.0)?"OK <1%":"NO cierra 1%"}')
      printf "%d. %-45s  %s %-12s %s\n" $i $caso $r "$ok" "$linea"
    done
  } | tee $V/resumen.txt
}

msg "inicio" "casos $(echo $SEL | tr '\n' ' ')| Benders $([ $RONDA = 2 ] && echo $HORAS_R2 || echo $HORAS) h por caso, tope global $TOPE_H h (hasta $(date -d @$FIN '+%F %T')), corte de cargas $([ $CORTE = 1 ] && echo si || echo no)"

for n in $SEL; do
  c=${CASOS[$((n-1))]}; [ -z "$c" ] && { msg "?" "no existe el caso $n"; continue; }
  ALT=""; [[ "$c" == *"|alt:"* ]] && ALT=${c##*|alt:}
  while :; do
  caso=${c%%|*}; E3=""; [[ "$c" == *"|v3:"* ]] && E3=${c##*|v3:}
  D=$DATA/$caso; R1=$V/$caso; TAG="$n/${#CASOS[@]} $caso"
  INC=""; H_CASO=$HORAS
  [ "$caso" = "Produccion_escalada20/zona_norte/05C/RED_GEN_BESS" ] && H_CASO=$HORAS_ESCALADA_BESS
  # Ronda 2 de un caso que en la ronda 1 se resolvio con su alternativa (1C).
  if [ $RONDA = 2 ] && [ -n "$ALT" ] && [ -z "$(certificado $R1/certificado.log)" ] \
     && [ -n "$(certificado $V/$ALT/certificado.log)" ]; then c=$ALT; ALT=""; continue; fi
  if [ $RONDA = 2 ]; then
    G1=$(gap_de $R1/certificado.log)
    if [ -z "$G1" ]; then msg "$TAG" "sin certificado de la ronda 1: se salta"; continue 2; fi
    if awk -v g=$G1 'BEGIN{exit !(g<1.0)}'; then msg "$TAG" "ronda 1 ya cerro ($G1 %): se salta"; continue 2; fi
    R=$R1/r2; INC="--incumbente_inicial $R1/benders/incumbente_respaldo.pkl"; H_CASO=$HORAS_R2; E3=""
    [ "$caso" = "Produccion_escalada20/zona_norte/05C/RED_GEN_BESS" ] && H_CASO=$HORAS_ESCALADA_BESS
    msg "$TAG" "ronda 1 quedo en $G1 %: se relanza desde su incumbente"
  else
    R=$R1
  fi
  B=$R/benders
  mkdir -p $R
  if [ -n "$(certificado $R/certificado.log)" ]; then msg "$TAG" "ya certificado, se salta: $(certificado $R/certificado.log)"; continue 2; fi

  # --- 0. caso base de la cola v3 ----------------------------------------------
  if [ -n "$E3" ] && [ -n "$(certificado $V3/certificado_$E3.log)" ]; then
    copiar_v3 $E3 $R
    msg "$TAG" "copiado de v3/$E3: $(certificado $R/certificado.log)"
    resumen_final > /dev/null; continue 2
  fi

  # --- 1. Benders -------------------------------------------------------------
  if grep -qE '^\[NestedBenders\] termino en' $R/benders.log 2>/dev/null && [ -f $B/incumbente_respaldo.pkl ]; then
    msg "$TAG" "Benders ya terminado, se usa $R/benders.log"
  else
    QUEDAN=$(( FIN - $(date +%s) ))
    H=$(awk -v q=$QUEDAN -v r=$RESERVA_H -v t=$H_CASO 'BEGIN{h=q/3600-r; if(h>t)h=t; printf "%.2f", h}')
    if awk -v h=$H -v m=$MIN_H 'BEGIN{exit !(h<m)}'; then
      msg "$TAG" "SALTADO: solo quedarian $H h para Benders (minimo $MIN_H h)"; continue 2
    fi
    EXTRA=""
    if [ $CORTE = 1 ]; then
      [ -f $R/corte_cargas_lhd.json ] || $PY -u corte_cargas_op.py --data_folder $D --out $R/corte_cargas_lhd.json \
          --tmp $R/_corte_cargas_op > $R/corte_cargas_op.log 2>&1
      if [ -f $R/corte_cargas_lhd.json ]; then EXTRA="--corte_cargas_lhd $R/corte_cargas_lhd.json"; rm -rf $R/_corte_cargas_op
      else msg "$TAG" "corte_cargas_op fallo (ver corte_cargas_op.log): Benders sin el corte"; fi
    fi
    msg "$TAG" "Nested Benders (max $H h, gap $GAP, corte tras $SIN_MEJORA iteraciones sin mejora${EXTRA:+, corte de cargas})"
    mkdir -p $B; rm -f $B/STOP
    vigia $R/benders.log $B/STOP "$TAG" & VP=$!
    $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS \
        --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter $ITER \
        --max_hours $H --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
        --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
        --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
        --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
        --polish_each_iter --skip_last_backward $EXTRA $INC 2>&1 | tee $R/benders.log
    kill $VP 2>/dev/null; wait $VP 2>/dev/null
  fi
  read UB0 LBOP < <(leer_benders $R/benders.log)
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then
    if [ -n "$ALT" ] && [ $RONDA = 1 ]; then
      msg "$TAG" "SIN SOLUCION FACTIBLE: se corre la alternativa $ALT"; c=$ALT; ALT=""; continue
    fi
    msg "$TAG" "Benders sin resultado: se salta"; break
  fi
  msg "$TAG" "Benders: UB $UB0  LB $LBOP"

  # --- 2. mejorar_ub ----------------------------------------------------------
  if [ ! -f $R/mejorar_ub_1/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 1 (años 1-10, 600 s)"
    $PY -u mejorar_ub.py --data_folder $D --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
        --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1 2>&1 | tee $R/mejorar_ub_1.log
  fi
  read SOL UB < <(ub_resumen $R/mejorar_ub_1/resumen.json)
  [ "$SOL" = "-" ] && { SOL=$B; UB=$UB0; }     # sin mejora: los JSON de Benders
  msg "$TAG" "tras pasada 1: UB $UB ($SOL)"

  if [ ! -f $R/mejorar_ub_2/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 2 (años 3-9, 900 s)"
    $PY -u mejorar_ub.py --data_folder $D --solucion $SOL --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
        --timelimit 900 --jobs 4 --out $R/mejorar_ub_2 2>&1 | tee $R/mejorar_ub_2.log
  fi
  read SOL2 UB2 < <(ub_resumen $R/mejorar_ub_2/resumen.json)
  if [ "$SOL2" != "-" ]; then SOL=$SOL2; UB=$UB2; fi
  msg "$TAG" "tras pasada 2: UB $UB ($SOL)"
  $PY -u escribir_parameters.py $D $SOL 2>&1 | tee $R/consumer.log
  $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log

  # --- 3. certificado ---------------------------------------------------------
  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
  msg "$TAG" "certificar (UB $UB, gap_objetivo $GO)"
  $PY -u certificar_inversiones.py --data_folder $D --solucion $SOL --ub_esperado $UB --gap_objetivo $GO \
      --jobs 4 --out $R/certificado 2>&1 | tee $R/certificado.log
  msg "$TAG" "FIN: $(certificado $R/certificado.log)"
  resumen_final > /dev/null
  break
  done
done

resumen_final | tee -a $LOG
msg "fin" "ronda $RONDA terminada; resumen en $V/resumen.txt"
[ $RONDA = 1 ] && grep -q "NO cierra" $V/resumen.txt && msg "fin" "hay casos >= 1 %: relanzarlos con  RONDA=2 bash cola_tesis_swap.sh"
