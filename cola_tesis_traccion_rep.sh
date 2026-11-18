#!/bin/bash
# Repeticion del L-shaped BS de los casos 1-5 de cola_tesis_traccion.sh con el hibrido
# corregido (2026-11-18, pedido de la usuaria): lshaped.py ahora corre la DP de la UB
# inicial (inversion del maestro de la Fase 1 + pasadas con la energia real) tambien
# cuando parte de la solucion del NB. Los casos 6-8 ya corrieron con el codigo nuevo
# dentro de la campaña; las corridas anteriores de 1-3 quedaron en <caso>/lshaped_v1 y
# las interrumpidas de 4-5 en <caso>/lshaped_interrumpido.
#
# Cada socket repite sus casos apenas termina su parte de la campaña (misma asignacion
# de sockets por caso: A = 1 3 5, B = 2 4). Lanzar con un argumento que contenga
# "cola_tesis_traccion.sh", para que cola_tesis_experimento.sh siga esperando:
#   tmux new -d -s rep "bash cola_tesis_traccion_rep.sh espera-cola_tesis_traccion.sh"
cd "$(dirname "$0")"
SW=$(pwd); OB=$SW/../Elmo_ob
V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_traccion
LOG=$SW/$V/campana.log
msg() { echo "[traccion rep $1] $(date '+%F %T') $2" | tee -a $LOG; }

# gurobi.env: la campaña lo borra al terminar y cada instancia de la cola tambien; se
# repone mientras dure la repeticion.
( while true; do for r in $SW $OB; do [ -f $r/gurobi.env ] || echo "Threads 16" > $r/gurobi.env; done; sleep 30; done ) &
GE=$!

esperar_ranura() { until grep -q "^\[traccion $1\] .* ranura terminada" $LOG; do sleep 60; done; }
msg "inicio" "espera a cada socket de la campaña; repite L-shaped A: 1 3 5, B: 2 4"
( esperar_ranura A; msg A "repite L-shaped 1 3 5"
  RANURA_A="1 3 5" RANURA_B="" FASES=ls bash cola_tesis_traccion.sh > $SW/$V/rep_A.out 2>&1 ) &
PA=$!
( esperar_ranura B; msg B "repite L-shaped 2 4"
  RANURA_A="" RANURA_B="2 4" FASES=ls bash cola_tesis_traccion.sh > $SW/$V/rep_B.out 2>&1 ) &
PB=$!
wait $PA $PB
kill $GE 2>/dev/null
rm -f $SW/gurobi.env $OB/gurobi.env
msg "fin" "repeticion terminada (gurobi.env borrados)"
