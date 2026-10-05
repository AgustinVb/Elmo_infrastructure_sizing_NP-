#!/usr/bin/bash
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
M=output/Resultados_finales_tesis/Mina_modelo
.venv_elmo/Scripts/python.exe -u cg_bloque_anual.py --year 6 --esquemas actual,gurobi,cg --timelimit 1800 --out $M/cg_bloque_y6 > $M/cg_bloque_y6.log 2>&1
.venv_elmo/Scripts/python.exe -u cg_propia.py --n_years 4 --out $M/cg_frio_4anios --cota_operacional 1824373.30 --estab barrier+wentges --jobs 4 --tiempo_max 10800 --ub_cada 5 > $M/cg_frio_4anios.log 2>&1
echo FIN > $M/cg_experimentos.fin
