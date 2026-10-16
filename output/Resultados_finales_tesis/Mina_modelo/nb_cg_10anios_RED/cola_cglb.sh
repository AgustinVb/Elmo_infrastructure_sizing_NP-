#!/usr/bin/bash
# Nested Benders con --cg_lb a 10 años (6 h), cuando terminen la opcion 1 (cg_desde_nb)
# y la prueba de la heuristica IP con b_bar comun (prueba_ip_bbar): asi corre sola.
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
    powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { \$_.CommandLine -like '*cg_desde_nb*' -or \$_.CommandLine -like '*prueba_ip_bbar*' }) { exit 0 } else { exit 1 }"
}
sleep 120
while corriendo; do sleep 60; done
echo "nb_cglb $(date '+%F %T')" >> $O10/cola.estado
$PY -u setup.py $NB --n_years 10 --max_hours 6 --cg_lb --cg_block_timelimit 300 \
    --output_folder $O10/nb_cglb > $O10/nb_cglb.log 2>&1
echo "fin $(date '+%F %T')" >> $O10/cola.estado
