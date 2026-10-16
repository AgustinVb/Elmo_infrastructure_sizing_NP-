#!/usr/bin/bash
# Hibridos Nested Benders + CG (RED), en serie, despues de la CG de 10 años en curso.
#   NB+DW, 4 años      nb_cg              Nested Benders con Dantzig-Wolfe dentro: CG en los
#                                         bloques del forward (--block_solver cg), cota del
#                                         año 1 con los cortes por CG + B&P (--cg_lb) y cortes
#                                         de Dantzig-Wolfe (--dw_cuts) (2 h)
#   opcion 1, 4 años   cg_desde_nb        chequeo corto del flujo (10 min): que la solucion
#                                         de Nested Benders se cargue, se reconstruya y entre
#                                         como columnas antes de las 12 h de 10 años
#   opcion 1, 10 años  nb_cg_10anios_RED  Nested Benders genera el incumbente (6 h) y la
#                                         CG arranca de el (6 h)
#   NB+DW, 10 años     nb_cg_10anios_RED/nb_dw  Nested Benders con Dantzig-Wolfe dentro
#                                         (--block_solver cg --cg_lb, sin --dw_cuts, que a 2
#                                         años no subieron la LB) (6 h)
# En la opcion 1 la CG no corre su heuristica inicial (--variantes_inicio ninguna):
# la UB de arranque es la de Nested Benders.
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
M=output/Resultados_finales_tesis/Mina_modelo
O=$M/comparacion_cg_4anios
O10=$M/nb_cg_10anios_RED
mkdir -p $O10
D=data/Tesis_final/Mina_modelo/RED
NB="--data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
    --block_build_jobs 1 --mode decomposed --gap_tol 1e-4 --max_iter 50 \
    --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 1800 \
    --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
    --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
    --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
    --polish_each_iter --skip_last_backward"
until grep -q "^fin" $M/cg_10anios_RED/cola.estado 2>/dev/null; do sleep 60; done

echo "nb_cg $(date '+%F %T')" >> $O/cola.estado
$PY -u setup.py $NB --n_years 4 --max_hours 2 --dw_cuts --dw_timelimit 300 \
    --block_solver cg --cg_lb --cg_block_timelimit 300 \
    --output_folder $O/nb_cg > $O/nb_cg.log 2>&1

echo "cg_desde_nb $(date '+%F %T')" >> $O/cola.estado
$PY -u cg_propia.py --data_folder $D --n_years 4 --solucion $O/nb --variantes_inicio ninguna \
    --out $O/cg_desde_nb --tiempo_max 600 --sin_bp > $O/cg_desde_nb.log 2>&1
echo "fin_4anios $(date '+%F %T')" >> $O/cola.estado

echo "nb $(date '+%F %T')" >> $O10/cola.estado
$PY -u setup.py $NB --n_years 10 --max_hours 6 --output_folder $O10/nb > $O10/nb.log 2>&1

echo "cg_desde_nb $(date '+%F %T')" >> $O10/cola.estado
$PY -u cg_propia.py --data_folder $D --n_years 10 --solucion $O10/nb --variantes_inicio ninguna \
    --out $O10/cg_desde_nb --tiempo_max 21600 --ub_cada 15 > $O10/cg_desde_nb.log 2>&1

echo "nb_dw $(date '+%F %T')" >> $O10/cola.estado
$PY -u setup.py $NB --n_years 10 --max_hours 6 --block_solver cg --cg_lb \
    --cg_block_timelimit 300 --output_folder $O10/nb_dw > $O10/nb_dw.log 2>&1
echo "fin $(date '+%F %T')" >> $O10/cola.estado
