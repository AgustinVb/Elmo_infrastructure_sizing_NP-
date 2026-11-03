#!/bin/bash
# Version 5 de los resultados swap (2026-10-31): los casos base norte 0,5C (RED,
# RED_GEN, RED_GEN_BESS) con dos cambios de la operacion de swap, por entorno
# (ver functions.py, swap_soc_max y carga_capacidad_real; v4 no cambia):
#   ELMO_SWAP_SOC_MAX=0.30        swap solo si el LHD llega con SOC <= 30 %;
#   ELMO_CARGA_CAPACIDAD_REAL=1   la carga dura lo que pide la capacidad del año.
# L-shaped completo (lshaped.py, arbol + mejorar UB) desde cero, en
# casos_finales_v5/<caso>/lshaped/, y validacion en el monolitico con el mismo
# modelo. Mismos datos que v4 (Mina_modelo_v4).
#
# Primera vuelta (31-oct, tope 25 %, 5 h los tres; borrada): el tope encarecia el
# pool y RED_GEN_BESS quedo suboptimo (gap 8,2 %, 2 modulos de subestacion): la
# UB inicial gasto 1,7 h sin solucion y el arbol alcanzo 41 callbacks. Ahora tope
# 30 % y RED_GEN_BESS con HORAS_RGB (10 h) en una ranura propia.
#
# Segunda vuelta (31-oct, tope 30 %; movida a casos_finales_v5_sin_carga_parcial/):
# RED cerro el arbol a la 1,25 h con gap 31 % porque casi ninguna inversion
# tenia UB: los dos cambios acotan b_bar por arriba para una operacion fija y
# b_y_link_lower la fijaba en D[y-1], asi que el pulido salia infactible. Desde
# el 1-nov la v5 quita b_y_link_lower (functions.carga_parcial) y la UB resuelve
# los dias con una trayectoria de capacidad que cumple la degradacion con su
# energia (LShaped._b_bar_v5). Esa vuelta (en <caso>/lshaped/) cerro RED con
# gap 3,5 %, y RED_GEN y RED_GEN_BESS con cotas corruptas: Gurobi no actualiza
# las cotas restauradas hasta update() (descartadas en _lshaped_bug_cotas/).
#
# Tercera vuelta (1-nov, la que vale, en <caso>/lshaped_r/): update() tras cada
# restauracion de cotas, energia minima ENTERA de los dias en el maestro
# (rango_presupuestos(entero_tl)) y reparacion con la DP de las inversiones
# aceptadas cuya UB sale infactible (LShaped._reparar_v5). RED 6 h desde la
# solucion de la segunda vuelta, RED_GEN 7 h y RED_GEN_BESS 8 h, en paralelo
# (21 nucleos). Resultados en resultados_lshaped.md §6.
#
#   tmux new -d -s cola_v5 "bash cola_lshaped_v5.sh > $V5/cola_v5.out 2>&1"
PY=${PY:-~/miniconda3/envs/py310/bin/python}
V5=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v5
D=data/Tesis_final/Mina_modelo_v4
SUB=${SUB:-lshaped_r}
HORAS=${HORAS:-7}
HORAS_RGB=${HORAS_RGB:-8}
CPU=${CPU:-21}
ESPERAR=${ESPERAR:-}
export ELMO_SWAP_SOC_MAX=${ELMO_SWAP_SOC_MAX:-0.30}
export ELMO_CARGA_CAPACIDAD_REAL=1
cd "$(dirname "$0")"
mkdir -p $V5

msg() { echo "[lshaped v5 r640-01] $(date '+%F %T') $*" | tee -a $V5/cola.log; }

# correr <caso> [horas] [solucion inicial]
correr() {
  local c=$1 h=${2:-$HORAS} out=$V5/$1/$SUB ub ini=()
  if [ -n "$3" ]; then
    ub=$($PY -c "import json; print(json.load(open('$(dirname $3)/resumen.json'))['ub_final'])")
    ini=(--solucion $3 --ub_esperado $ub)
  fi
  mkdir -p $out
  msg "inicio $c ($h h, $CPU nucleos, SOC max $ELMO_SWAP_SOC_MAX, carga con capacidad real${3:+, desde $3}) -> $out"
  $PY -u lshaped.py --data_folder $D/$c --out $out --horas $h --cpu $CPU \
      --capacidad station_1=1 "${ini[@]}" > $out/lshaped.log 2>&1
  msg "fin $c: $(grep -aE 'FINAL|Traceback|Error' $out/lshaped.log | tail -1)"
  [ -d $out/solucion ] || return
  ub=$($PY -c "import json; print(json.load(open('$out/resumen.json'))['ub_final'])")
  $PY -u lshaped.py --data_folder $D/$c --solucion $out/solucion --ub_esperado $ub \
      --out $out/validacion --solo_validar --cpu 4 > $out/validacion.log 2>&1
  msg "validacion $c (ub_final $ub): $(grep -aE 'VALIDADA|ABORTA|Error' $out/validacion.log | tail -1)"
}

if [ -n "$ESPERAR" ]; then
  msg "esperando que termine tmux $ESPERAR"
  while tmux has-session -t $ESPERAR 2>/dev/null; do sleep 60; done
fi
msg "inicio cola v5"
{ correr Produccion_base/zona_norte/05C/RED_GEN_BESS $HORAS_RGB; } &
{ correr Produccion_base/zona_norte/05C/RED_GEN; } &
{ correr Produccion_base/zona_norte/05C/RED 6 $V5/Produccion_base/zona_norte/05C/RED/lshaped/solucion; } &
wait

# Comparacion con on-board (ajuste de energia + redimensionamiento): parameters.json
# y consumer.log de cada solucion, y cost_comparisons/comparar_swap_ob_v5.py.
for c in RED RED_GEN RED_GEN_BESS; do
  s=$V5/Produccion_base/zona_norte/05C/$c/$SUB/solucion
  [ -d $s ] || continue
  $PY -u escribir_parameters.py $D/Produccion_base/zona_norte/05C/$c $s > /dev/null 2>&1
  $PY consumer.py $s > $(dirname $s)/consumer.log 2>&1
done
$PY -u cost_comparisons/comparar_swap_ob_v5.py --sub $SUB 2>&1 | grep -aE '^\[|ok ->|Error|SystemExit' | tee -a $V5/cola.log
msg "cola v5 terminada"
