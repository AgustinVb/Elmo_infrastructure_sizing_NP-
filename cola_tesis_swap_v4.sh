#!/bin/bash
# Casos de estudio finales de la tesis, SWAP, VERSION 4 (data/Tesis_final/Mina_modelo_v4),
# el equivalente de cola_tesis_ob_v4.sh de carga_ob_multiaño. Respecto de la v3:
#   - perfil eolico optimista (el de OB v4): norte 7,56 h-eq/dia (6,63 / 4,55 / 11,31 /
#     7,74) y centro 5,33 (3,46 / 4,41 / 4,96 / 8,49); la solar no cambia;
#   - topes de solar, eolica y BESS en 10 (ya estaban en la v3 de swap);
#   - valor residual lineal de la generacion y el BESS (columna vida_util: solar 35,
#     eolica 25, BESS 20 años; ObjectiveRules._net_investment_factor);
#   - degradacion de baterias corregida (build_mccormick_degradation_block): flota exacta
#     y McCormick por tramos de b_bar. La v3 contaba 43-67 % de los ciclos fisicos.
# Los casos 4 y 5 siguen en 0,5C, como en la v3 de swap (con la alternativa 1C si 0,5C
# no tiene solucion factible).
#
# Todos los casos parten de cero: las soluciones v3 no son factibles con la degradacion
# corregida (necesitan reemplazos de baterias que no hicieron), y los casos solo-red
# (1 y 4) tambien cambian por la degradacion. Mismo metodo y opciones que
# cola_tesis_swap.sh, sin copiar nada de la v3.
#
#   bash cola_tesis_swap_v4.sh              # ronda 1, los 8 casos
#   bash cola_tesis_swap_v4.sh 2 7          # solo algunos
#   RONDA=2 bash cola_tesis_swap_v4.sh      # relanza los >= 1 %
cd "$(dirname "$0")"
export DATA=data/Tesis_final/Mina_modelo_v4
export V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4
export V3=
exec bash cola_tesis_swap.sh "$@"
