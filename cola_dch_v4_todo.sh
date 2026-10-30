#!/bin/bash
# Cola completa de DCH v4: ronda 1 de los 3 casos y despues ronda 2 de los que quedaron en
# >= 1 %. Cada ronda se corre dos veces: la segunda pasada solo retoma lo que haya quedado a
# medias si el PC se cayo entre pasos (lo terminado se salta solo).
#   bash cola_dch_v4_todo.sh > output/Resultados_finales_tesis/DCH/casos_finales_v4_Carga_libre/cola.out 2>&1
cd "$(dirname "$0")"
export PY=${PY:-.venv_elmo/Scripts/python.exe}
for ronda in 1 2; do
  for pasada in 1 2; do
    RONDA=$ronda bash cola_dch_ob_v4.sh
  done
done
