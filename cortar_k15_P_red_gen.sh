#!/bin/bash
# Crea STOP en P_red_gen_final apenas termina la iteracion 15 de Nested Benders.
L=/home/avilches/Elmo_infrastructure_sizing_NP-/output/Resultados_finales_tesis/Mina_modelo/P_red_gen_final.log
until grep -q "k=15  UB=" $L; do
  pgrep -f "bash cola_final_ob.sh" > /dev/null || { echo "$(date '+%F %T') la cola termino antes de k=15"; exit 0; }
  sleep 20
done
touch /home/avilches/Elmo_infrastructure_sizing_NP-/output/Resultados_finales_tesis/Mina_modelo/P_red_gen_final/STOP
echo "$(date '+%F %T') STOP creado tras: $(grep 'k=15  UB=' $L | cut -c1-120)"
