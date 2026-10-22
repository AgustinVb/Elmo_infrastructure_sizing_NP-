#!/bin/bash
# Monolitico (Gurobi) del caso 3 v4 (norte 05C, P_red_gen) con warm start desde la
# solucion de mejorar_ub_2 (UB 2.487.888) y la cota certificada como fila obj >= LB.
#   bash correr_mono_caso3.sh        # MIPFocus 1 (prioriza incumbentes)
#   bash correr_mono_caso3.sh 3      # MIPFocus 3 (prioriza la cota)
# Ctrl+C detiene Gurobi y exporta la mejor solucion encontrada.
cd "$(dirname "$0")"
FOCO=${1:-1}
C=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/05C/P_red_gen
OUT=$C/monolitico_focus$FOCO
PYTHONIOENCODING=utf-8 .venv_elmo/Scripts/python.exe -u setup.py \
  --data_folder data/Resultados_finales_tesis/Mina_modelo_v4/Produccion_base/zona_norte/05C/P_red_gen \
  --solver gurobi --days_per_year 4 --n_years 10 --free_charging --free_maintenance \
  --mode monolithic --mccormick_degradation \
  --warm_start_folder $C/mejorar_ub_2/solucion --warm_start_solo_enteras \
  --mono_lb 2453109.79 --mono_timelimit 86400 --mip_focus $FOCO \
  --output_folder $OUT 2>&1 | tee $OUT.log
