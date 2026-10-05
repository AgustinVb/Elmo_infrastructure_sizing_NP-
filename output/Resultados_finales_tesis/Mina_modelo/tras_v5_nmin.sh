#!/usr/bin/bash
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
M=output/Resultados_finales_tesis/Mina_modelo
until grep -aq "JSONs, log y summary" $M/v5_estab.log || grep -aq "Traceback" $M/v5_estab.err; do sleep 60; done
sleep 30
.venv_elmo/Scripts/python.exe -u cortes_cargas.py --n_years 10 --milp_s 300 --jobs 8 --out $M/cortes_cargas_milp_10anios.json > $M/cortes_cargas_milp_10anios.log 2>&1
ELMO_N_MIN_JSON=$M/cortes_cargas_milp_10anios.json .venv_elmo/Scripts/python.exe -u cota_operacional_variantes.py --variante base --n_years 10 --mipgap 1e-4 --timelimit 5400 --out $M/cota_op_nmin_milp_10anios > $M/cota_op_nmin_milp_10anios.log 2>&1
echo FIN > $M/tras_v5_nmin.fin
