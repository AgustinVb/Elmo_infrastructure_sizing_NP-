#!/usr/bin/bash
# Resto de la cola de 10 años (reemplaza el final de comparacion_cg_4anios/cola_hibridos.sh,
# cuyo bash se detuvo para cambiar la ultima prueba sin editar un script en ejecucion).
# Espera a que termine Nested Benders solo (ya en curso, nb/), y despues:
#   opcion 1   cg_desde_nb  CG desde el incumbente de Nested Benders (6 h)
#   NB+DW      nb_cglb      Nested Benders con la cota del año 1 por CG + B&P (--cg_lb),
#                           forward con Gurobi como Nested Benders solo: un unico cambio (6 h)
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
O10=output/Resultados_finales_tesis/Mina_modelo/nb_cg_10anios_RED
D=data/Tesis_final/Mina_modelo/RED
NB="--data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
    --block_build_jobs 1 --mode decomposed --gap_tol 1e-4 --max_iter 50 \
    --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 1800 \
    --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
    --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
    --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
    --polish_each_iter --skip_last_backward"
corriendo() {
    powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { \$_.CommandLine -like '*nb_cg_10anios_RED/nb' }) { exit 0 } else { exit 1 }"
}
while corriendo; do sleep 60; done
echo "nb_fin $(date '+%F %T')" >> $O10/cola.estado

echo "cg_desde_nb $(date '+%F %T')" >> $O10/cola.estado
$PY -u cg_propia.py --data_folder $D --n_years 10 --solucion $O10/nb --variantes_inicio ninguna \
    --out $O10/cg_desde_nb --tiempo_max 21600 --ub_cada 15 > $O10/cg_desde_nb.log 2>&1

echo "nb_cglb $(date '+%F %T')" >> $O10/cola.estado
$PY -u setup.py $NB --n_years 10 --max_hours 6 --cg_lb --cg_block_timelimit 300 \
    --output_folder $O10/nb_cglb > $O10/nb_cglb.log 2>&1
echo "fin $(date '+%F %T')" >> $O10/cola.estado
