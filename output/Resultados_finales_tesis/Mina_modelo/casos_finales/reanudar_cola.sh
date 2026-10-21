#!/bin/bash
# Reanuda la cola tras los reinicios del 21/10 (caida ~15:06 y reinicio 18:10),
# en el mismo orden que tenia:
#   1. caso 8 (2C): cola_tesis_swap.sh salta Benders y mejorar_ub pasada 1 (ya
#      hechos) y sigue desde la pasada 2 y el certificado.
#   2. caso 6 con tope 10 (centro; datos ya en tope 10, el de tope 2 quedo en
#      RED_GEN_BESS_tope2), lo que antes lanzaba centro_tope10.sh.
#   3. caso 5 desde la solucion del caso 4 (caso5_inc_caso4.sh).
#   4. caso 7: mejorar_ub pasada 3 (caso7_mejorar_ub3.sh).
cd /home/avilches/Elmo_infrastructure_sizing_NP-
O=output/Resultados_finales_tesis/Mina_modelo/casos_finales
echo "[cola reanudar] $(date '+%F %T') reinicio de la maquina: se reanuda (8 -> 6 tope 10 -> 5 inc caso 4 -> 7 ub3)" | tee -a $O/cola.log
TOPE_H=200 bash cola_tesis_swap.sh 8 2>&1 | tee -a cola_tesis_swap.out
TOPE_H=200 bash cola_tesis_swap.sh 6 2>&1 | tee cola_centro10.out
bash $O/caso5_inc_caso4.sh 2>&1 | tee $O/caso5_inc_caso4.log
bash $O/caso7_mejorar_ub3.sh 2>&1 | tee $O/caso7_mejorar_ub3.log
echo "[cola reanudar] $(date '+%F %T') cola terminada" | tee -a $O/cola.log
