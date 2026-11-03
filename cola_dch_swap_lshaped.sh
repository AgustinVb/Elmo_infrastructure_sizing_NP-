#!/bin/bash
# Casos DCH multiaño en SWAP (0,5C / 1C / 2C, caso red) resueltos con el L-shaped entero
# (lshaped.py) en vez de Nested Benders, partiendo de cero. Datos y condiciones: ver
# cola_dch_swap_v4.sh (pausas DCH con --pause_scheme dch, swap libre fuera de mantencion,
# 11 años, WP2, produccion y cotas iguales a OB).
#
# Por caso: Fase 1 (maestro continuo y entero, cortes LP; con 3 estaciones la UB sale de
# las inversiones de la etapa 2, porque la DP de UB inicial es de una estacion), Fase 2
# (branch-and-cut con cortes enteros) y mejorar UB. Salida en $V/<tasa>/lshaped y
# $V/<tasa>/lshaped.log; un caso con resumen.json se salta.
#
#   bash cola_dch_swap_lshaped.sh            # los 3 casos, HORAS h cada uno
#   bash cola_dch_swap_lshaped.sh 2C 1C      # solo algunos, en ese orden
#   HORAS=6 bash cola_dch_swap_lshaped.sh
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
PY=${PY:-.venv_elmo/Scripts/python.exe}
DATA=data/Tesis_final/DCH/Bateria_482/Costo_fijo
V=${V:-output/Resultados_finales_tesis/DCH/casos_finales_v4_swap}
HORAS=${HORAS:-10}; JOBS=${JOBS:-4}; MAX_IT=${MAX_IT:-8,60}
REG="--n_years 11 --consumption_model wp2 --pause_scheme dch"
CASOS=${@:-05C 1C 2C}

mkdir -p $V
LOG=$V/cola_lshaped.log
msg() { echo "[lshaped $1] $(date '+%F %T') $2" | tee -a $LOG; }

msg inicio "casos $CASOS | $HORAS h por caso, $JOBS dias en paralelo, max_it $MAX_IT"
for tasa in $CASOS; do
  R=$V/$tasa/RED
  mkdir -p $R
  if [ -f $R/lshaped/resumen.json ]; then
    msg $tasa "ya terminado, se salta: $(grep -a 'FINAL' $R/lshaped.log | tail -1)"
    continue
  fi
  msg $tasa "L-shaped ($HORAS h)"
  $PY -u lshaped.py --data_folder $DATA/$tasa/RED $REG --jobs $JOBS --horas $HORAS \
      --max_it $MAX_IT --out $R/lshaped 2>&1 | tee $R/lshaped.log
  msg $tasa "fin: $(grep -a '\[LS\] FINAL' $R/lshaped.log | tail -1)"
done
msg fin "resumen:"
for tasa in $CASOS; do
  echo "  $tasa: $(grep -a '\[LS\] FINAL' $V/$tasa/RED/lshaped.log 2>/dev/null | tail -1)" | tee -a $LOG
done
