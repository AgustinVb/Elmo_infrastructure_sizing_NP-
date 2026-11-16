#!/bin/bash
# Campaña final de la tesis, mina modelo (8 casos), OB y battery swapping en IGUALES
# condiciones en este equipo (r640-01, 2 sockets x 16 nucleos x 2 hilos), 2026-11-16.
#
# Motivo: OB cuenta ahora los ciclos de degradacion con la energia de traccion (Elmo_ob
# 9bdbcf5f7; antes 10,8 % mas ciclos que swap). Todos los resultados OB v4 quedaron
# viejos, y se rehacen los dos lados con el mismo presupuesto.
#
# Dos ranuras en paralelo, una por socket (taskset), con los MISMOS casos para OB y BS:
# el caso n de OB y el de BS corren en el mismo socket. Cada ranura hace, en orden:
#   1. OB: Nested Benders desde cero (cola_tesis_ob_v4.sh, config. v4: caja +-1 libre
#      cada 3, cota operacional, strengthen), HORAS_NB h, corte tras 4 iteraciones sin
#      mejora; SOLO_BENDERS=1 (sin mejorar_ub ni certificado) -> parameters + consumer.
#   2. BS: Nested Benders desde cero (cola_tesis_swap.sh, config. v4: corte de cargas,
#      caja +-2 libre cada 3, --fijar_R op --evaluar_trayectoria_op --reparar_dp),
#      HORAS_NB h (tambien el caso 5), SOLO_BENDERS=1 -> parameters + consumer.
#   3. BS: L-shaped (lshaped.py) desde la incumbente del Benders, HORAS_LS h (incluye
#      su mejorar UB final) -> validacion en el monolitico + parameters + consumer.
#      La LB de BS es la del L-shaped (sin certificado).
#
# Hilos: Gurobi no respeta taskset, asi que gurobi.env (Threads 16 = nucleos fisicos
# de un socket) va en la raiz de los dos repos mientras dura la campaña, y los dias
# del forward reparten los nucleos de la afinidad (day_blocks.py: 4 dias x 8 hilos).
#
#   nohup bash cola_tesis_traccion.sh > $V/campana.out 2>&1 &
#   RANURA_A="1 3" RANURA_B="" bash cola_tesis_traccion.sh     # solo algunos casos
#   FASES="ls" ...                                               # solo algunas fases (ob bs ls)
cd "$(dirname "$0")"
SW=$(pwd)
OB=${OB:-$SW/../Elmo_ob}
PY=${PY:-$HOME/miniconda3/envs/py310/bin/python}
V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_traccion   # en los dos repos
DATA_SW=data/Tesis_final/Mina_modelo_v4
HORAS_NB=${HORAS_NB:-12}; HORAS_LS=${HORAS_LS:-10}; CPU_LS=${CPU_LS:-28}
CPU_A=${CPU_A:-0-62:2}; CPU_B=${CPU_B:-1-63:2}          # NUMA node0 / node1
RANURA_A=${RANURA_A-"1 3 5 7"}; RANURA_B=${RANURA_B-"2 4 6 8"}
FASES=${FASES:-ob bs ls}
ESTAB_SW="--stabilization box --stab_center operational --box_delta_int 2 --stab_free_every 3"
EXTRA_SW="--fijar_R op --evaluar_trayectoria_op --reparar_dp"

# Numeracion de cola_tesis_swap.sh (4 y 5: alternativa 1C si 0,5C no tiene solucion).
SW_CASOS=(
  "Produccion_base/zona_norte/05C/RED"
  "Produccion_base/zona_norte/05C/RED_GEN_BESS"
  "Produccion_base/zona_norte/05C/RED_GEN"
  "Produccion_escalada20/zona_norte/05C/RED|alt:Produccion_escalada20/zona_norte/1C/RED"
  "Produccion_escalada20/zona_norte/05C/RED_GEN_BESS|alt:Produccion_escalada20/zona_norte/1C/RED_GEN_BESS"
  "Produccion_base/zona_centro/05C/RED_GEN_BESS"
  "Produccion_base/zona_norte/1C/RED_GEN_BESS"
  "Produccion_base/zona_norte/2C/RED_GEN_BESS"
)

mkdir -p $SW/$V $OB/$V
LOG=$SW/$V/campana.log
msg() { echo "[traccion $1] $(date '+%F %T') $2" | tee -a $LOG; }

for r in $SW $OB; do
  if [ -f $r/gurobi.env ] && ! grep -qx "Threads 16" $r/gurobi.env; then
    msg "-" "ABORTO: $r/gurobi.env ya existe con otro contenido"; exit 1
  fi
  echo "Threads 16" > $r/gurobi.env
done

ub_benders() {  # UB final del benders.log (linea "termino en"), sin separador de miles
  $PY -c "
import re,sys;t=open(sys.argv[1],encoding='utf-8',errors='replace').read()
m=re.findall(r'termino en[^\n]*: UB=([\d,.]+)',t); print(m[-1].replace(',','') if m else '')" "$1"
}

lshaped() {  # lshaped <n> <cpus>
  local c=${SW_CASOS[$(($1-1))]} caso R ub cap
  caso=${c%%|*}; R=$SW/$V/$caso; ub=$(ub_benders $R/benders.log)
  if [ -z "$ub" ] && [[ "$c" == *"|alt:"* ]]; then caso=${c##*|alt:}; R=$SW/$V/$caso; ub=$(ub_benders $R/benders.log); fi
  local TAG="LS $1 $caso"
  if [ -z "$ub" ] || [ ! -f $R/benders/X.json ]; then msg "$TAG" "sin solucion del Benders: se salta"; return; fi
  if [ -f $R/lshaped/resumen.json ]; then msg "$TAG" "ya hecho, se salta"; return; fi
  cap=$(grep -ao 'cotas impuestas al anio 1: station_1: >= [0-9]*' $R/benders.log | tail -1 | grep -o '[0-9]*$')
  msg "$TAG" "L-shaped desde el Benders (UB $ub), $HORAS_LS h, --cpu $CPU_LS, capacidad station_1>=${cap:-1}"
  mkdir -p $R/lshaped
  taskset -c $2 $PY -u lshaped.py --data_folder $DATA_SW/$caso --solucion $R/benders --ub_esperado $ub \
      --out $R/lshaped --horas $HORAS_LS --cpu $CPU_LS --capacidad station_1=${cap:-1} > $R/lshaped.log 2>&1
  local sol=$R/lshaped/solucion ubf
  ubf=$($PY -c "import json;print(repr(json.load(open('$R/lshaped/resumen.json'))['ub_final']))" 2>/dev/null)
  if [ ! -d $sol ] || [ -z "$ubf" ]; then msg "$TAG" "el L-shaped no dejo solucion/ ni resumen.json (ver lshaped.log)"; return; fi
  taskset -c $2 $PY -u lshaped.py --data_folder $DATA_SW/$caso --solucion $sol --ub_esperado $ubf \
      --out $R/lshaped/validacion --solo_validar --cpu 4 > $R/lshaped/validacion.log 2>&1
  $PY -u escribir_parameters.py --n_years 10 $DATA_SW/$caso $sol > $R/lshaped/escribir_parameters.log 2>&1
  $PY consumer.py $sol > $R/lshaped/consumer.log 2>&1
  msg "$TAG" "FIN: $(grep -a 'FINAL' $R/lshaped.log | tail -1) | $(grep -a 'VALIDADA\|desvio' $R/lshaped/validacion.log | tail -1)"
}

ranura() {  # ranura <nombre> <cpus> <casos...>
  local nom=$1 cpus=$2; shift 2
  [ $# = 0 ] && return
  if [[ " $FASES " == *" ob "* ]]; then
    msg "$nom" "OB Benders casos $* (cpus $cpus)"
    (cd $OB && taskset -c $cpus env PY=$PY V=$V HORAS=$HORAS_NB TOPE_H=400 SIN_SOL=1 SOLO_BENDERS=1 \
        bash cola_tesis_ob_v4.sh "$@") > $OB/$V/cola_$nom.out 2>&1
  fi
  if [[ " $FASES " == *" bs "* ]]; then
    msg "$nom" "BS Benders casos $* (cpus $cpus)"
    taskset -c $cpus env PY=$PY DATA=$DATA_SW V=$V V3= HORAS=$HORAS_NB HORAS_ESCALADA_BESS=$HORAS_NB \
        TOPE_H=400 ESTAB="$ESTAB_SW" BENDERS_EXTRA="$EXTRA_SW" SOLO_BENDERS=1 \
        bash cola_tesis_swap.sh "$@" > $SW/$V/cola_$nom.out 2>&1
  fi
  if [[ " $FASES " == *" ls "* ]]; then
    for n in "$@"; do lshaped $n $cpus; done
  fi
  msg "$nom" "ranura terminada"
}

msg "inicio" "fases: $FASES | Benders $HORAS_NB h, L-shaped $HORAS_LS h | A ($CPU_A): $RANURA_A | B ($CPU_B): $RANURA_B"
ranura A $CPU_A $RANURA_A &
ranura B $CPU_B $RANURA_B &
wait
rm -f $SW/gurobi.env $OB/gurobi.env
msg "fin" "campaña terminada (gurobi.env borrados)"
