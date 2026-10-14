#!/usr/bin/bash
# Comparacion a presupuesto igual (2 h c/u, en serie para no competir por CPU):
# CG con mejoras, Nested Benders y monolitico. Mismo caso que comparacion_cg_2anios
# pero con 4 años (RED, 4 dias por año, regimenes liberados, McCormick).
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
D=data/Tesis_final/Mina_modelo/RED
O=output/Resultados_finales_tesis/Mina_modelo/comparacion_cg_4anios
T=7200
COMUN="--data_folder $D --solver gurobi --days_per_year 4 --n_years 4 --free_charging --free_maintenance"

echo "cg $(date '+%F %T')" >> $O/cola.estado
$PY -u cg_propia.py --data_folder $D --n_years 4 --out $O/cg --tiempo_max $T > $O/cg.log 2>&1

echo "nb $(date '+%F %T')" >> $O/cola.estado
$PY -u setup.py $COMUN --block_build_jobs 1 --output_folder $O/nb --mode decomposed \
    --gap_tol 1e-4 --max_iter 50 --max_hours 2 --operational_bound --op_bound_gap 1e-4 \
    --op_bound_timelimit 1800 --no_monolithic_lp_bound --stabilization box \
    --stab_center operational --box_delta_int 1 --cut_type strengthened \
    --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
    --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
    --polish_each_iter --skip_last_backward > $O/nb.log 2>&1

echo "mono $(date '+%F %T')" >> $O/cola.estado
$PY -u setup.py $COMUN --mccormick_degradation --mode monolithic --gap_tol 1e-4 \
    --mono_timelimit $T --output_folder $O/mono > $O/mono.log 2>&1

echo "fin $(date '+%F %T')" >> $O/cola.estado
