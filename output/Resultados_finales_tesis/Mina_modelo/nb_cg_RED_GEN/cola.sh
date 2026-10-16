#!/usr/bin/bash
# RED_GEN a 10 años: configuraciones que superaron a Nested Benders normal en RED.
# Linea base (otro equipo, mismos ajustes de NB): 26,6 h, 21 iteraciones de 64-81 min,
# UB 2.231.895,24 (desde k=13), LB 2.073.809,96 (operacional 2.072.483,88 hasta k=13),
# gap 7,08 %; a ~6 h (k=4-5) UB ~2.254.553 y LB operacional (~8 %).
#   nb_cglb  Nested Benders con la cota del año 1 por CG + B&P (--cg_lb), un unico
#            cambio respecto de la linea base (6 h); 600 s porque el pricing con
#            generacion es mas lento (humo a 1 año: 114 s por dia en la ronda exacta)
#   cg       CG propia sola (6 h), la mejor UB en RED a 4 y 10 años
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
O=output/Resultados_finales_tesis/Mina_modelo/nb_cg_RED_GEN
D=data/Tesis_final/Mina_modelo/RED_GEN
# Mismos ajustes que cola_final_swap.sh (la linea base), salvo el tope de horas.
NB="--data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
    --block_build_jobs 1 --mode decomposed --gap_tol 0.01 --max_iter 50 \
    --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 \
    --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
    --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
    --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
    --polish_each_iter --skip_last_backward"

echo "nb_cglb $(date '+%F %T')" >> $O/cola.estado
$PY -u setup.py $NB --n_years 10 --max_hours 6 --cg_lb --cg_block_timelimit 600 \
    --output_folder $O/nb_cglb > $O/nb_cglb.log 2>&1

echo "cg $(date '+%F %T')" >> $O/cola.estado
$PY -u cg_propia.py --data_folder $D --n_years 10 --out $O/cg --tiempo_max 21600 --ub_cada 15 \
    > $O/cg.log 2>&1
echo "fin $(date '+%F %T')" >> $O/cola.estado
