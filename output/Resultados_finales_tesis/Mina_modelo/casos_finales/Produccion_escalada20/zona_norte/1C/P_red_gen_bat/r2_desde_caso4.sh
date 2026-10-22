#!/usr/bin/bash
# Caso 5 (escalada20/1C/P_red_gen_bat), ronda 2 alternativa: Nested Benders desde la solucion
# del caso 4 (escalada20/1C/P_red: 2 cargadores, 3 reemplazos), que al cargarse se pule con las
# continuas libres (solar, BESS, potencia de punta, degradacion). El incumbente de la ronda 1
# (5.258.470, 3 cargadores) quedo atrapado por la caja de estabilizacion.
# - --stab_free_every 3: una iteracion sin caja cada 3, para poder alejarse del centro.
# - --lb_inicial: la cota operacional ya calculada (4.662.046,72), sin recalcularla.
# - Reintentos: el CPU de este equipo se cae seguido; si Benders muere sin la linea final, se
#   relanza desde el incumbente_respaldo.pkl de esta corrida, con el tiempo que quede de las 12 h.
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
D=data/Resultados_finales_tesis/Mina_modelo/Produccion_escalada20/zona_norte/1C/P_red_gen_bat
C=output/Resultados_finales_tesis/Mina_modelo/casos_finales
R=$C/Produccion_escalada20/zona_norte/1C/P_red_gen_bat/r2
B=$R/benders
SOL4=$C/Produccion_escalada20/zona_norte/1C/P_red/mejorar_ub_2/solucion
FLAGS="--free_charging --free_maintenance"
LOG=$C/cola.log
msg() { echo "[caso5 r2b] $(date '+%F %T') $1" | tee -a $LOG; }

mkdir -p $B
[ -f $R/benders.log ] && mv $R/benders.log $R/benders_caida_ronda2.log
FIN=$(( $(date +%s) + 12*3600 ))
INC="--incumbente_inicial $SOL4"
for intento in 1 2 3 4 5 6 7 8; do
  QUEDAN=$(( FIN - $(date +%s) ))
  H=$(awk -v q=$QUEDAN 'BEGIN{printf "%.2f", q/3600}')
  if awk -v h=$H 'BEGIN{exit !(h<1)}'; then msg "sin tiempo para otro intento ($H h)"; break; fi
  msg "Benders intento $intento ($H h) desde $INC"
  $PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 $FLAGS \
      --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol 0.01 --max_iter 50 \
      --max_hours $H --lb_inicial 4662046.72 \
      --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
      --stab_free_every 3 --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
      --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
      --polish_each_iter --skip_last_backward $INC 2>&1 | tee -a $R/benders.log
  if grep -qE '^NestedBenders' $R/benders.log; then msg "Benders terminado"; break; fi
  if [ -f $B/incumbente_respaldo.pkl ]; then
    cp $B/incumbente_respaldo.pkl $B/respaldo_intento$intento.pkl
    INC="--incumbente_inicial $B/respaldo_intento$intento.pkl"
  fi
  msg "Benders se cayo en el intento $intento; se reintenta"
done
grep -E '^NestedBenders|k=[0-9]+  UB=' $R/benders.log | tail -2 | tee -a $LOG
