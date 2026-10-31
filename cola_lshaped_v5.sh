#!/bin/bash
# Version 5 de los resultados swap (2026-10-31): los casos base norte 0,5C (RED,
# RED_GEN, RED_GEN_BESS) con dos cambios de la operacion de swap, por entorno
# (ver functions.py, swap_soc_max y carga_capacidad_real; v4 no cambia):
#   ELMO_SWAP_SOC_MAX=0.25        swap solo si el LHD llega con SOC <= 25 %;
#   ELMO_CARGA_CAPACIDAD_REAL=1   la carga dura lo que pide la capacidad del año.
# L-shaped completo (lshaped.py, 5 h de arbol + mejorar UB) desde cero, en
# casos_finales_v5/<caso>/lshaped/, y validacion en el monolitico con el mismo
# modelo. Mismos datos que v4 (Mina_modelo_v4). Arranca cuando termina la cola
# v4 (tmux cola_A4: caso 8 y sus validaciones).
#
#   tmux new -d -s cola_v5 "bash cola_lshaped_v5.sh > $V5/cola_v5.out 2>&1"
PY=${PY:-~/miniconda3/envs/py310/bin/python}
V5=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v5
D=data/Tesis_final/Mina_modelo_v4
HORAS=${HORAS:-5}
CPU=${CPU:-28}
ESPERAR=${ESPERAR:-cola_A4}
export ELMO_SWAP_SOC_MAX=${ELMO_SWAP_SOC_MAX:-0.25}
export ELMO_CARGA_CAPACIDAD_REAL=1
cd "$(dirname "$0")"
mkdir -p $V5

msg() { echo "[lshaped v5 r640-01] $(date '+%F %T') $*" | tee -a $V5/cola.log; }

correr() {
  local c=$1 out=$V5/$1/lshaped ub
  mkdir -p $out
  msg "inicio $c ($HORAS h, $CPU nucleos, SOC max $ELMO_SWAP_SOC_MAX, carga con capacidad real) -> $out"
  $PY -u lshaped.py --data_folder $D/$c --out $out --horas $HORAS --cpu $CPU \
      --capacidad station_1=1 > $out/lshaped.log 2>&1
  msg "fin $c: $(grep -aE 'FINAL|Traceback|Error' $out/lshaped.log | tail -1)"
  [ -d $out/solucion ] || return
  ub=$($PY -c "import json; print(json.load(open('$out/resumen.json'))['ub_final'])")
  $PY -u lshaped.py --data_folder $D/$c --solucion $out/solucion --ub_esperado $ub \
      --out $out/validacion --solo_validar --cpu 4 > $out/validacion.log 2>&1
  msg "validacion $c (ub_final $ub): $(grep -aE 'VALIDADA|ABORTA|Error' $out/validacion.log | tail -1)"
}

msg "esperando que termine la cola v4 (tmux $ESPERAR)"
while tmux has-session -t $ESPERAR 2>/dev/null; do sleep 60; done
msg "inicio cola v5"
{ correr Produccion_base/zona_norte/05C/RED_GEN_BESS; correr Produccion_base/zona_norte/05C/RED; } &
{ correr Produccion_base/zona_norte/05C/RED_GEN; } &
wait
msg "cola v5 terminada"
