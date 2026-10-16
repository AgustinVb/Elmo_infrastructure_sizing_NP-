#!/usr/bin/bash
# CG con mejoras (variante fases, respaldo con precio de inversion) a 10 años, RED, 6 h.
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
O=output/Resultados_finales_tesis/Mina_modelo/cg_10anios_RED
echo "cg $(date '+%F %T')" >> $O/cola.estado
.venv_elmo/Scripts/python.exe -u cg_propia.py --data_folder data/Tesis_final/Mina_modelo/RED \
    --n_years 10 --out $O --tiempo_max 21600 --ub_cada 15 > $O/cg.log 2>&1
echo "fin $(date '+%F %T')" >> $O/cola.estado
