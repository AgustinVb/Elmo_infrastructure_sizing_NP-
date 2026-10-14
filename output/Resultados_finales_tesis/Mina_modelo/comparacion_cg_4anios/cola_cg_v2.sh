#!/usr/bin/bash
# CG con la heuristica del maestro corregida (b_bar coherente con R redondeado,
# R fijo en el pulido). Espera a que termine cola.sh para no competir por CPU.
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
O=output/Resultados_finales_tesis/Mina_modelo/comparacion_cg_4anios
until grep -q "^fin" $O/cola.estado 2>/dev/null; do sleep 30; done
echo "cg_v2 $(date '+%F %T')" >> $O/cola.estado
.venv_elmo/Scripts/python.exe -u cg_propia.py --data_folder data/Tesis_final/Mina_modelo/RED \
    --n_years 4 --out $O/cg_v2 --tiempo_max 7200 > $O/cg_v2.log 2>&1
echo "fin_cg_v2 $(date '+%F %T')" >> $O/cola.estado
