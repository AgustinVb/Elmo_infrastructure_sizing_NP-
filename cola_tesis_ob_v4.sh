#!/bin/bash
# Casos de estudio finales de la tesis, OB, VERSION 4 de los datos (Mina_modelo_v4):
# la v3 con
#   - perfil eolico optimista: norte 7,56 h-eq/dia (6,63 / 4,55 / 11,31 / 7,74) y centro
#     5,33 (3,46 / 4,41 / 4,96 / 8,49); la solar no cambia;
#   - topes de generacion (solar y eolica) y BESS en 10, en la practica libres;
#   - valor residual lineal de la generacion y el BESS al final del horizonte (columna
#     vida_util: solar 35, eolica 25, BESS 20 años; ver ObjectiveRules._net_investment_factor);
#   - tasa de descuento 8 %, la misma de la v3 y de la rama swap.
#
# Mismo metodo que cola_tesis_ob.sh (v3): Benders -> mejorar_ub (pasadas 1 y 2) ->
# parameters + consumer -> certificado. Cambia lo que enseño la v3:
#   - Benders parte de la solucion final v3 de cada caso (--incumbente_inicial con la
#     carpeta JSON: se pule con los datos v4; sin cortes ni cota de la v3, que no valen
#     con otro objetivo). La cota operacional se recalcula. Por eso basta con HORAS = 4
#     (en la v3 casi todos agotaban las 6 h partiendo de cero).
#   - --stab_free_every 3: una iteracion sin caja cada 3 (en la v3 el caso 5 quedo
#     atrapado por la caja de estabilizacion).
#   - Reintentos: este PC se cae (access violation, CPU inestable). Benders retoma su
#     propio respaldo (cortes y cota validos); mejorar_ub y el certificado se repiten.
#   - Orden: primero los que deberian cerrar rapido; 5 y 3 (los dificiles de la v3) al
#     final, para que no frenen a los demas.
#
# Reutiliza lo que no cambia (copiado en $V antes de lanzar, con un REUTILIZADO_*.txt):
#   - casos 1 y 4 (P_red): sin generacion ni BESS (g_max = h_max = 0), el modelo v4 es
#     identico al v3; se toma su resultado certificado y la cola los salta;
#   - caso 6: su Benders ya se corrio con estos mismos datos (casos_finales_v4_vr, 0,77 %);
#     la cola solo agrega mejorar_ub y el certificado.
#
# Regla de la usuaria (2026-10-21, desde el caso 7): si el Benders cierra bajo GAP, NO se
# corren mejorar_ub ni el certificado. Se toma la mejor solucion que ya exista (mejorar_ub
# terminado o la del Benders), se le corre el consumer y queda <caso>/sin_certificado.txt
# con la brecha del Benders (cota operacional, valida). Los casos 2 y 6 ya tenian certificado.
#
# RONDA=2: relanza los casos cuyo certificado de la ronda 1 quedo en >= 1 %, desde el
# respaldo de la ronda 1 (UB, cortes, cota), con HORAS_R2 de Benders. Salida en <caso>/r2/.
#   bash cola_v4_todo.sh                          # ronda 1 y despues ronda 2 (lo normal)
#   PY=... bash cola_tesis_ob_v4.sh 2 7           # solo algunos casos, ronda 1
#   RONDA=2 PY=... bash cola_tesis_ob_v4.sh       # solo la ronda 2
#   Para cortar un Benders a mano: crear el archivo STOP en su carpeta benders/.
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
PY=${PY:-.venv_elmo/Scripts/python.exe}
DATA=${DATA:-data/Resultados_finales_tesis/Mina_modelo_v4}
O=output/Resultados_finales_tesis/Mina_modelo
V=${V:-$O/casos_finales_v4}
V3=$O/casos_finales
RONDA=${RONDA:-1}
HORAS=${HORAS:-4}; HORAS_R2=${HORAS_R2:-8}
if [ $RONDA = 2 ]; then TOPE_H=${TOPE_H:-30}; else TOPE_H=${TOPE_H:-44}; fi
RESERVA_H=${RESERVA_H:-2.5}; MIN_H=${MIN_H:-1}
GAP=0.01; ITER=50; SIN_MEJORA=4
FLAGS="--free_charging --free_maintenance"

# Numeracion de la tesis (la misma de la v3). "|sol=<carpeta>": solucion final v3 de la
# que parte el Benders de la ronda 1 (relativa a $V3).
CASOS=(
  "Produccion_base/zona_norte/05C/P_red"
  "Produccion_base/zona_norte/05C/P_red_gen_bat|sol=Produccion_base/zona_norte/05C/P_red_gen_bat/mejorar_ub_2/solucion"
  "Produccion_base/zona_norte/05C/P_red_gen|sol=Produccion_base/zona_norte/05C/P_red_gen/topes10/mejorar_ub_2/solucion"
  "Produccion_escalada20/zona_norte/1C/P_red"
  "Produccion_escalada20/zona_norte/1C/P_red_gen_bat|sol=Produccion_escalada20/zona_norte/1C/P_red_gen_bat/topes10/mejorar_ub_2/solucion"
  "Produccion_base/zona_centro/05C/P_red_gen_bat|sol=Produccion_base/zona_centro/05C/P_red_gen_bat/mejorar_ub_2/solucion"
  "Produccion_base/zona_norte/1C/P_red_gen_bat|sol=Produccion_base/zona_norte/1C/P_red_gen_bat/mejorar_ub_2/solucion"
  "Produccion_base/zona_norte/2C/P_red_gen_bat|sol=Produccion_base/zona_norte/2C/P_red_gen_bat/mejorar_ub_2/solucion"
)
SEL=${@:-6 2 7 8 5 3 1 4}

mkdir -p $V
LOG=$V/cola.log
[ -f $V/.inicio_r$RONDA ] || date +%s > $V/.inicio_r$RONDA
INICIO=$(cat $V/.inicio_r$RONDA); FIN=$((INICIO + ${TOPE_H%.*} * 3600))
horas() { awk -v s=$1 'BEGIN{printf "%.1f", s/3600}'; }
msg() {
  local ahora=$(date +%s)
  echo "[v4 r$RONDA $1] $(date '+%F %T') (llevo $(horas $((ahora-INICIO))) h, quedan $(horas $((FIN-ahora))) h) $2" | tee -a $LOG
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
    $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS \
        --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol $GAP --max_iter $ITER \
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
    echo "== Resumen v4 ($(date '+%F %T'))"
    local i=0
    for c in "${CASOS[@]}"; do
      i=$((i+1)); local caso=${c%%|*} R=$V/${c%%|*} r=r1
      local f=$R/certificado.log s=$R/sin_certificado.txt
      if [ -n "$(certificado $R/r2/certificado.log)" ]; then f=$R/r2/certificado.log; r=r2
      elif [ -f $R/r2/sin_certificado.txt ]; then f=""; s=$R/r2/sin_certificado.txt; r=r2
      elif [ -z "$(certificado $f)" ] && [ -f $s ]; then f=""
      fi
      if [ -z "$f" ]; then
        printf "%d. %-50s  %s %-12s %s\n" $i $caso $r "OK <1%" "sin certificado, brecha del Benders: $(cat $s)"; continue
      fi
      local linea=$(certificado $f)
      if [ -z "$linea" ]; then printf "%d. %-50s  sin certificado\n" $i $caso; continue; fi
      local ok=$(awk -v g=$(gap_de $f) 'BEGIN{print (g<1.0)?"OK <1%":"NO cierra 1%"}')
      printf "%d. %-50s  %s %-12s %s\n" $i $caso $r "$ok" "$linea"
    done
  } | tee $V/resumen.txt
}

msg "inicio" "casos $(echo $SEL | tr '\n' ' ')| Benders $([ $RONDA = 2 ] && echo $HORAS_R2 || echo $HORAS) h por caso, tope global $TOPE_H h (hasta $(date -d @$FIN '+%F %T'))"

for n in $SEL; do
  c=${CASOS[$((n-1))]}; [ -z "$c" ] && { msg "?" "no existe el caso $n"; continue; }
  caso=${c%%|*}; SOLV3=""; [[ "$c" == *"|sol="* ]] && SOLV3=$V3/${c#*|sol=}
  D=$DATA/$caso; R1=$V/$caso; TAG="$n/${#CASOS[@]} $caso"
  if [ $RONDA = 2 ]; then
    if [ -f $R1/sin_certificado.txt ]; then msg "$TAG" "ronda 1 cerro con el Benders bajo 1 % (sin certificado): se salta"; continue; fi
    G1=$(gap_de $R1/certificado.log)
    if [ -z "$G1" ]; then msg "$TAG" "sin certificado de la ronda 1: se salta"; continue; fi
    if awk -v g=$G1 'BEGIN{exit !(g<1.0)}'; then msg "$TAG" "ronda 1 ya cerro ($G1 %): se salta"; continue; fi
    R=$R1/r2; INC="--incumbente_inicial $R1/benders/incumbente_respaldo.pkl"; H_CASO=$HORAS_R2
    msg "$TAG" "ronda 1 quedo en $G1 %: se relanza desde su respaldo"
  else
    R=$R1; INC=""; [ -n "$SOLV3" ] && INC="--incumbente_inicial $SOLV3"; H_CASO=$HORAS
  fi
  B=$R/benders
  mkdir -p $R
  if [ -n "$(certificado $R/certificado.log)" ]; then msg "$TAG" "ya certificado, se salta: $(certificado $R/certificado.log)"; continue; fi
  if [ -f $R/sin_certificado.txt ]; then msg "$TAG" "ya cerrado con el Benders bajo 1 % (sin certificado), se salta"; continue; fi
  if [ -n "$SOLV3" ] && [ $RONDA = 1 ] && [ ! -d "$SOLV3" ]; then msg "$TAG" "no existe la solucion v3 $SOLV3: se salta"; continue; fi

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
    $PY -u escribir_parameters.py --data_folder $D $FLAGS $SOL 2>&1 | tee $R/consumer.log
    $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log
    G=$(awk -v u=$UB -v l=$LBOP 'BEGIN{printf "%.3f", 100*(u-l)/u}')
    echo "UB $UB  LB $LBOP  gap $G %  solucion $SOL" > $R/sin_certificado.txt
    msg "$TAG" "FIN sin mejorar_ub ni certificado (Benders bajo 1 %): UB $UB  LB $LBOP  gap $G %"
    resumen_final > /dev/null
    continue
  fi

  # --- 2. mejorar_ub --------------------------------------------------------
  if [ ! -f $R/mejorar_ub_1/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 1 (años 1-10, 600 s)"
    reintentar $R/mejorar_ub_1.log $PY -u mejorar_ub.py --data_folder $D $FLAGS --respaldo $B/incumbente_respaldo.pkl \
        --ub_esperado $UB0 --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1
  fi
  read -r SOL UB < <(ub_resumen $R/mejorar_ub_1/resumen.json)
  if [ "$SOL" = "-" ] || [ -z "$SOL" ]; then FUENTE="--respaldo $B/incumbente_respaldo.pkl"; UB=$UB0; SOL=""
  else FUENTE="--solucion $SOL"; fi
  msg "$TAG" "tras pasada 1: UB $UB (${SOL:-respaldo de Benders})"

  if [ ! -f $R/mejorar_ub_2/resumen.json ]; then
    msg "$TAG" "mejorar_ub pasada 2 (años 3-9, 900 s)"
    reintentar $R/mejorar_ub_2.log $PY -u mejorar_ub.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB \
        --anios 3,4,5,6,7,8,9 --timelimit 900 --jobs 4 --out $R/mejorar_ub_2
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
    $PY -u escribir_parameters.py --data_folder $D $FLAGS $SOL 2>&1 | tee $R/consumer.log
    $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log
  fi

  # --- 3. certificado -------------------------------------------------------
  GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))" | tr -d '\r')
  msg "$TAG" "certificar (UB $UB, gap_objetivo $GO)"
  reintentar $R/certificado.log $PY -u certificar_inversiones.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB \
      --gap_objetivo $GO --evaluar_vivas --jobs 4 --out $R/certificado
  msg "$TAG" "FIN: $(certificado $R/certificado.log)"
  resumen_final > /dev/null
done

resumen_final | tee -a $LOG
msg "fin" "ronda $RONDA terminada; resumen en $V/resumen.txt"
