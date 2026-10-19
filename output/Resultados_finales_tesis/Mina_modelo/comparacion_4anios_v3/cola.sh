#!/usr/bin/bash
# Comparacion de algoritmos a 4 años con los datos v3 (perfiles de generacion corregidos),
# RED_GEN_BESS y RED_GEN, en serie en este PC. Completa la de RED (comparacion_cg_4anios,
# a la que la v3 no afecta: solo cambio GenProfiles).
#
# PRESUPUESTO IGUAL: 2 h de RELOJ desde el arranque de cada proceso, lectura de datos y
# armado incluidos, y los resultados se leen a t = 7200 s:
#   - Nested Benders: --max_hours 2 (cuenta desde el arranque; no empieza una iteracion
#     que no alcanza a terminar).
#   - CG: --tiempo_max 6600, para que la heuristica final (hasta ~10-20 min) quepa en las
#     2 h; lo que encuentre despues de 7200 s no cuenta.
#   - Monolitico: --mono_timelimit 7200 --mono_reloj (descuenta el armado del modelo).
# Mismas tolerancias para todos (gap 1e-4) y la misma cota operacional: Nested Benders
# con --op_bound_timelimit 2400 --op_bound_gap 1e-4 y la CG con --tl_relajacion 2400
# --gap_relajacion 1e-4 (con el default de 600 s la CG no llegaba a la operacional en
# RED_GEN a 10 años).
# --cg_block_timelimit 600 (no 300 como en RED): en RED_GEN el pricing es mas lento y la
# CG del año 1 necesito ~330 s; para apenas converge, asi que no gasta de mas.
# El monolitico va al final de todo: en RED a 4 años no encontro solucion factible.
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
O=output/Resultados_finales_tesis/Mina_modelo/comparacion_4anios_v3
T=7200
NB="--solver gurobi --days_per_year 4 --n_years 4 --free_charging --free_maintenance \
    --block_build_jobs 1 --mode decomposed --gap_tol 1e-4 --max_iter 50 --max_hours 2 \
    --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 2400 \
    --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
    --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
    --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
    --polish_each_iter --skip_last_backward"
CG="--n_years 4 --tiempo_max 6600 --tl_relajacion 2400 --gap_relajacion 1e-4"
MONO="--solver gurobi --days_per_year 4 --n_years 4 --free_charging --free_maintenance \
    --mccormick_degradation --mode monolithic --gap_tol 1e-4 --mono_timelimit $T --mono_reloj"

paso() {  # paso <nombre> <comando...>: registra inicio y fin de reloj en cola.estado
  local n=$1; shift
  echo "$n inicio $(date '+%F %T')" >> $O/cola.estado
  "$@"
  local rc=$?
  echo "$n fin $(date '+%F %T') (salida $rc)" >> $O/cola.estado
}

mkdir -p $O/RED_GEN_BESS $O/RED_GEN
# Prueba de humo: la CG con BESS (H) a 1 año, 10 min.
paso humo_cg_bess_1anio $PY -u cg_propia.py --data_folder data/Tesis_final/Mina_modelo/RED_GEN_BESS \
    --n_years 1 --out $O/RED_GEN_BESS/humo_cg_1anio --tiempo_max 600 \
    > $O/RED_GEN_BESS/humo_cg_1anio.log 2>&1

for esc in RED_GEN_BESS RED_GEN; do
  D=data/Tesis_final/Mina_modelo/$esc; E=$O/$esc
  paso $esc/nb $PY -u setup.py --data_folder $D $NB --output_folder $E/nb > $E/nb.log 2>&1
  paso $esc/nb_cglb $PY -u setup.py --data_folder $D $NB --cg_lb --cg_block_timelimit 600 \
      --output_folder $E/nb_cglb > $E/nb_cglb.log 2>&1
  paso $esc/cg $PY -u cg_propia.py --data_folder $D $CG --out $E/cg > $E/cg.log 2>&1
done
for esc in RED_GEN_BESS RED_GEN; do
  D=data/Tesis_final/Mina_modelo/$esc; E=$O/$esc
  paso $esc/mono $PY -u setup.py --data_folder $D $MONO --output_folder $E/mono > $E/mono.log 2>&1
done
echo "fin $(date '+%F %T')" >> $O/cola.estado
