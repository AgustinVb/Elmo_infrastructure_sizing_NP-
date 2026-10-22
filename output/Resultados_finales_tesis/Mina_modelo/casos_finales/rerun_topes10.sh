#!/usr/bin/bash
# Re-corre con los topes de generacion y BESS en 10 (en la practica libres) los casos de la v3 que
# quedaron en el tope de 2:
#   caso 5  escalada20/1C/P_red_gen_bat   BESS 2,00 de 2   (incumbente 4.774.205, desde el caso 4)
#   caso 6  base/centro/05C/P_red_gen_bat solar 1,98 de 2  (final v3 2.726.476)
# Parten de soluciones en JSON (sin cortes ni cota del modelo con tope 2, que podrian no valer con
# topes mas holgados); la cota operacional se recalcula. Por caso: Benders (4 iteraciones como
# maximo, una libre cada 3, reintentos desde su propio respaldo si el CPU lo tumba) -> mejorar_ub
# (pasadas 1 y 2) -> certificado. Salida en <caso>/topes10/.
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
DATA=data/Resultados_finales_tesis/Mina_modelo
C=output/Resultados_finales_tesis/Mina_modelo/casos_finales
FLAGS="--free_charging --free_maintenance"
LOG=$C/cola.log

ub_resumen() {
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print((r.get('solucion') or '-').replace(chr(92), '/'), repr(r['ub_final']))" | tr -d '\r' || echo "- -"
}

correr() {  # correr <etiqueta> <datos, relativo a DATA> <salida> <solucion inicial (carpeta JSON)>
  local TAG=$1 D=$DATA/$2 R=$3 B=$3/benders INC="--incumbente_inicial $4" OP="--operational_bound"
  msg() { echo "[topes10 $TAG] $(date '+%F %T') $1" | tee -a $LOG; }
  mkdir -p $B
  local FIN=$(( $(date +%s) + 6*3600 )) intento
  for intento in 1 2 3 4 5 6; do
    local H=$(awk -v q=$(( FIN - $(date +%s) )) 'BEGIN{printf "%.2f", q/3600}')
    if awk -v h=$H 'BEGIN{exit !(h<0.75)}'; then msg "sin tiempo para otro intento"; break; fi
    msg "Benders intento $intento ($H h, max 4 iteraciones) desde $INC"
    $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS \
        --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol 0.01 --max_iter 4 \
        --max_hours $H $OP --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
        --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
        --stab_free_every 3 --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
        --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
        --polish_each_iter --skip_last_backward $INC 2>&1 | tee -a $R/benders.log
    grep -qE '^NestedBenders' $R/benders.log && break
    if [ -f $B/incumbente_respaldo.pkl ]; then           # respaldo de ESTA corrida: cortes validos
      cp $B/incumbente_respaldo.pkl $B/respaldo_intento$intento.pkl
      INC="--incumbente_inicial $B/respaldo_intento$intento.pkl"; OP=""
    fi
    msg "Benders se cayo en el intento $intento; se reintenta"
  done
  local UB0 LBOP
  read -r UB0 LBOP < <($PY -c "
import re;t=open('$R/benders.log',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t) or re.findall(r'k=\d+  UB=([\d.]+)\s+LB=([\d.]+)',t)
print(*(m[-1] if m else ('','')))" | tr -d '\r')
  if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then msg "Benders sin resultado"; return; fi
  msg "Benders: UB $UB0  LB $LBOP"

  msg "mejorar_ub pasada 1 (años 1-10, 600 s)"
  $PY -u mejorar_ub.py --data_folder $D $FLAGS --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
      --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1 2>&1 | tee $R/mejorar_ub_1.log
  local SOL UB FUENTE SOL2 UB2
  read -r SOL UB < <(ub_resumen $R/mejorar_ub_1/resumen.json)
  if [ "$SOL" = "-" ] || [ -z "$SOL" ]; then FUENTE="--respaldo $B/incumbente_respaldo.pkl"; UB=$UB0; SOL=""
  else FUENTE="--solucion $SOL"; fi
  msg "tras pasada 1: UB $UB"
  msg "mejorar_ub pasada 2 (años 3-9, 900 s)"
  $PY -u mejorar_ub.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
      --timelimit 900 --jobs 4 --out $R/mejorar_ub_2 2>&1 | tee $R/mejorar_ub_2.log
  read -r SOL2 UB2 < <(ub_resumen $R/mejorar_ub_2/resumen.json)
  if [ "$SOL2" != "-" ] && [ -n "$SOL2" ]; then SOL=$SOL2; UB=$UB2; FUENTE="--solucion $SOL"; fi
  msg "tras pasada 2: UB $UB"
  if [ -n "$SOL" ]; then
    $PY -u escribir_parameters.py --data_folder $D $FLAGS $SOL 2>&1 | tee $R/consumer.log
    $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log
  fi
  local GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))" | tr -d '\r')
  msg "certificar (UB $UB, gap_objetivo $GO)"
  $PY -u certificar_inversiones.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB --gap_objetivo $GO \
      --evaluar_vivas --jobs 4 --out $R/certificado 2>&1 | tee $R/certificado.log
  msg "FIN: $(grep -E 'LB CERTIFICADA' $R/certificado.log | tail -1 | sed 's/^\[cert\] //')"
}

correr "caso 5" Produccion_escalada20/zona_norte/1C/P_red_gen_bat \
       $C/Produccion_escalada20/zona_norte/1C/P_red_gen_bat/topes10 \
       $C/Produccion_escalada20/zona_norte/1C/P_red/mejorar_ub_2/solucion
correr "caso 6" Produccion_base/zona_centro/05C/P_red_gen_bat \
       $C/Produccion_base/zona_centro/05C/P_red_gen_bat/topes10 \
       $C/Produccion_base/zona_centro/05C/P_red_gen_bat/mejorar_ub_2/solucion
echo "[topes10] $(date '+%F %T') fin" | tee -a $LOG
