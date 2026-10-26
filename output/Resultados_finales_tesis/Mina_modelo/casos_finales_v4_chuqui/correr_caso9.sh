#!/usr/bin/bash
# Caso 9: v4 norte/05C/P_red_gen_bat (caso 2) con precio de red variable:
# Profile_Chuquicamata de los casos antiguos (ya viene desplazado al inicio del turno), que el
# modelo reescala a media diaria 0,1 USD/kWh (solo cambia la forma respecto del caso 2).
# 11 horas vacias del perfil (dias 46, 56 y 365, fuera de los dias muestreados) rellenadas.
# Mismo esquema que correr_machali.sh: Nested Benders sin incumbente inicial hasta gap 1 %,
# reintentos desde el respaldo si el proceso se cae, y consumer al final.
cd /home/avilches/Elmo_ob
export PYTHONIOENCODING=utf-8
PY=${PY:-~/miniconda3/envs/py310/bin/python}
D=data/Resultados_finales_tesis/Mina_modelo_v4_chuqui/Produccion_base/zona_norte/05C/P_red_gen_bat
C=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4_chuqui
R=$C/Produccion_base/zona_norte/05C/P_red_gen_bat
B=$R/benders
LOG=$C/cola.log
FLAGS="--free_charging --free_maintenance"
msg() { echo "[v4 caso9 chuqui] $(date '+%F %T') $1" | tee -a $LOG; }
mkdir -p $B
FIN=$(( $(date +%s) + ${HORAS:-6}*3600 ))
INC=""; OP="--operational_bound"
for intento in 1 2 3 4 5 6; do
  H=$(awk -v q=$(( FIN - $(date +%s) )) 'BEGIN{printf "%.2f", q/3600}')
  if awk -v h=$H 'BEGIN{exit !(h<0.75)}'; then msg "sin tiempo para otro intento"; break; fi
  msg "Benders intento $intento ($H h) $INC"
  $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS \
      --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol 0.01 --max_iter 50 \
      --max_hours $H $OP --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
      --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
      --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
      --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
      --polish_each_iter --skip_last_backward $INC 2>&1 | tee -a $R/benders.log
  grep -qE '^NestedBenders' $R/benders.log && break
  if [ -f $B/incumbente_respaldo.pkl ]; then
    cp $B/incumbente_respaldo.pkl $B/respaldo_intento$intento.pkl
    INC="--incumbente_inicial $B/respaldo_intento$intento.pkl"; OP=""
  fi
  msg "Benders se cayo en el intento $intento; se reintenta"
done
msg "$(grep -E '^NestedBenders|k=[0-9]+  UB=' $R/benders.log | tail -1)"
$PY -u escribir_parameters.py --data_folder $D $FLAGS $B 2>&1 | tee $R/consumer.log
$PY consumer.py $B 2>&1 | tee -a $R/consumer.log
msg "FIN instalado: $($PY -c "
import json; g=json.load(open('$B/G_g.json'))['g']; h=json.load(open('$B/H.json'))
print('solar', round(g.get('Solar_PV',0),3), 'eolica', round(g.get('Wind',0),3), 'BESS', round(h,3))")"
