#!/bin/bash
# Cierre de la cola del L-shaped (lshaped_A2/), para dejar los 8 casos swap v4
# con la misma version del codigo y las UB validadas:
#
#   1. caso 5 (escalada20 norte 05C RED_GEN_BESS) con el codigo final, en la
#      ranura B cuando esta termine (la corrida de lshaped_A/ es anterior a las
#      cotas de P_pot y al arreglo del b_bar, y su LB supera a la certificada);
#   2. en paralelo, cada caso que termina con solucion/ se valida en el
#      monolitico (lshaped.py --solo_validar: todas las enteras fijas, LP, el
#      costo debe ser la ub_final de resumen.json), hasta que no quede nada
#      corriendo.
#
#   ESPERAR_B=<pid de la subshell de la ranura B> nohup bash cola_lshaped_cierre.sh &
PY=${PY:-~/miniconda3/envs/py310/bin/python}
V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4
D=data/Tesis_final/Mina_modelo_v4
SUB=${SUB:-lshaped_A2}
cd "$(dirname "$0")"

msg() { echo "[lshaped cierre r640-01] $(date '+%F %T') $*" | tee -a $V/cola.log; }

caso5() {
  while kill -0 $ESPERAR_B 2>/dev/null; do sleep 60; done
  LISTA_A="" LISTA_B="Produccion_escalada20/zona_norte/05C/RED_GEN_BESS 3098875.18 2983268.87" \
    HORAS=${HORAS:-5} CPU=${CPU:-28} SUB=$SUB bash cola_lshaped.sh
}

validar() {
  local d=$1 caso=${1#$V/} ub
  caso=${caso%/$SUB}
  ub=$($PY -c "import json; print(json.load(open('$d/resumen.json'))['ub_final'])")
  $PY -u lshaped.py --data_folder $D/$caso --solucion $d/solucion --ub_esperado $ub \
      --out $d/validacion --solo_validar --cpu 4 > $d/validacion.log 2>&1
  msg "validacion $caso (ub_final $ub): $(grep -aE 'VALIDADA|ABORTA|Error' $d/validacion.log | tail -1)"
}

pendientes() {
  for r in $V/*/*/*/*/$SUB/resumen.json; do
    local d=$(dirname $r)
    [ -d $d/solucion ] && [ ! -f $d/validacion.log ] && echo $d
  done
}

corriendo() {
  pgrep -f "lshaped.py .*--out $V/.*/$SUB( |$)" > /dev/null || kill -0 $C5 2>/dev/null \
    || tmux has-session -t cola_ls4 2>/dev/null || tmux has-session -t cola_ls5 2>/dev/null
}

msg "inicio: caso 5 tras la ranura B (pid $ESPERAR_B); validacion de cada solucion/ nueva"
caso5 &
C5=$!
while true; do
  for d in $(pendientes); do validar $d; done
  corriendo || break
  sleep 600
done
for d in $(pendientes); do validar $d; done
msg "cierre terminado: casos en $SUB con resumen: $(ls $V/*/*/*/*/$SUB/resumen.json 2>/dev/null | wc -l)"
