#!/usr/bin/bash
# CG con las mejoras (corrida original desde un worktree; hoy en el arbol principal), mismo caso y mismo
# presupuesto que cola.sh. Espera a que termine la linea base para no competir
# por CPU.
O=/c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-/output/Resultados_finales_tesis/Mina_modelo/comparacion_cg_2anios
PY=/c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-/.venv_elmo/Scripts/python.exe
until grep -q "^fin" $O/cola.estado 2>/dev/null; do sleep 30; done
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
echo "cg_nueva $(date '+%F %T')" >> $O/cola.estado
$PY -u cg_propia.py --data_folder data/Tesis_final/Mina_modelo/RED --n_years 2 \
    --out $O/cg_nueva --tiempo_max 3600 > $O/cg_nueva.log 2>&1
echo "fin_cg_nueva $(date '+%F %T')" >> $O/cola.estado
