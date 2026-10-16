#!/usr/bin/bash
# CG sola en RED_GEN a 10 años (6 h), con la relajacion operacional al mismo margen que en
# Nested Benders (--op_bound_timelimit 5400, gap 1e-4: 2.072.483,88 en 13-27 min). Con el
# default (600 s, gap 1e-3) se corto en 1.853.621: cota 10 % mas debil y peor punto de
# partida para la heuristica. Reemplaza el paso "cg" de cola.sh (cortado a los 12 min).
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
O=output/Resultados_finales_tesis/Mina_modelo/nb_cg_RED_GEN
D=data/Tesis_final/Mina_modelo/RED_GEN
echo "cg $(date '+%F %T')" >> $O/cola.estado
$PY -u cg_propia.py --data_folder $D --n_years 10 --out $O/cg --tiempo_max 21600 --ub_cada 15 \
    --tl_relajacion 2400 --gap_relajacion 1e-4 --cota_operacional 2072483.88 > $O/cg.log 2>&1
echo "fin $(date '+%F %T')" >> $O/cola.estado
