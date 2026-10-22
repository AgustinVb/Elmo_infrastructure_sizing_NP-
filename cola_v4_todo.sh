#!/bin/bash
# Cola completa de la v4: ronda 1 de todos los casos y despues ronda 2 de los que quedaron
# en >= 1 %. Cada ronda se corre dos veces: la segunda pasada solo retoma lo que haya quedado
# a medias si el PC se cayo entre pasos (lo terminado se salta solo).
#   bash cola_v4_todo.sh > output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/cola.out 2>&1
cd "$(dirname "$0")"
export PY=${PY:-.venv_elmo/Scripts/python.exe}
for ronda in 1 2; do
  for pasada in 1 2; do
    RONDA=$ronda bash cola_tesis_ob_v4.sh
  done
done
