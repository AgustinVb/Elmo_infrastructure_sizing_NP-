#!/bin/bash
# Relevo del socket A (2026-11-19, pedido de la usuaria): cuando termina la corrida en curso
# del experimento en el socket A, se detiene esa instancia del experimento (y la corrida que
# alcance a tomar, que vuelve a la cola compartida), se re-corre el caso 8 de OB (2C) con la
# carga forzada cerca de la tasa nominal (ELMO_CARGA_MIN=0.9: conectado => P >= 0,9 p_cargador;
# antes OB 2C cargaba a ~0,9C con la degradacion de 2C, con 1 modulo de 500 kW) y despues el
# socket A vuelve al experimento.
cd "$(dirname "$0")"
SW=$(pwd); OB=$SW/../Elmo_ob
PY=$HOME/miniconda3/envs/py310/bin/python
E=$SW/output/Resultados_finales_tesis/Mina_modelo/experimento_algoritmos
LOG=$E/experimento.log
CPU_A=0-62:2
msg() { echo "[experimento relevo A] $(date '+%F %T') $1" | tee -a $LOG; }
INST_A=${INST_A:?pid de la instancia A del experimento}
LISTA=(OB:4:mono BS:4:mono OB:4:nb_sin BS:4:nb_sin OB:4:nb_con BS:4:nb_con OB:4:ls_sin BS:4:ls_sin BS:4:hibrido OB:4:hibrido
       OB:10:mono BS:10:mono OB:10:nb_sin BS:10:nb_sin OB:10:ls_sin BS:10:ls_sin OB:10:hibrido)

msg "espera a que termine la corrida en curso del socket A (OB 4a nb_sin)"
until grep -q "^\[experimento OB 4a nb_sin\] .* FIN" $LOG; do sleep 20; done
# La instancia A toma enseguida el siguiente trabajo: se espera su linea de inicio,
# posterior a la del FIN.
nfin=$(grep -n "^\[experimento OB 4a nb_sin\] .* FIN" $LOG | tail -1 | cut -d: -f1)
until linea=$(tail -n +$((nfin + 1)) $LOG | grep "inicio (cpus $CPU_A)" | head -1) && [ -n "$linea" ]; do sleep 2; done
job=$(echo "$linea" | sed -E 's/^\[experimento ([A-Z]+) ([0-9]+)a ([a-z_]+)\].*/\1:\2:\3/')
msg "la instancia A tomo $job: se detiene y el trabajo vuelve a la cola"
desc() { local p; for p in $(pgrep -P $1); do desc $p; echo $p; done; }
kill -TERM $(desc $INST_A) $INST_A 2>/dev/null
tmux kill-session -t exp_A 2>/dev/null
sleep 5
rm -f $E/.activa_$INST_A
IFS=: read -r T N C <<< "$job"
rm -rf $E/$T/${N}anios/$C
idx=-1; for i in "${!LISTA[@]}"; do [ "${LISTA[$i]}" = "$job" ] && idx=$i; done
(
  flock 9
  s=$(cat $E/.siguiente)
  if [ "$s" = "$((idx + 1))" ]; then echo $idx > $E/.siguiente; echo "devuelto (indice $idx)"
  else echo "NO devuelto: la cola ya va en $s (indice del trabajo $idx); correrlo a mano"; fi
) 9> $E/.lock | while read -r l; do msg "$job: $l"; done

msg "caso 8 OB (2C) con carga forzada >= 90 % del cargador (ELMO_CARGA_MIN=0.9) en el socket A"
(cd $OB && taskset -c $CPU_A env PY=$PY V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_traccion/carga90 \
   HORAS=12 TOPE_H=400 SIN_SOL=1 SOLO_BENDERS=1 ELMO_CARGA_MIN=0.9 bash cola_tesis_ob_v4.sh 8) \
   > $OB/output/Resultados_finales_tesis/Mina_modelo/casos_finales_traccion_carga90.out 2>&1
msg "caso 8 OB terminado: $(cat $OB/output/Resultados_finales_tesis/Mina_modelo/casos_finales_traccion/carga90/Produccion_base/zona_norte/2C/P_red_gen_bat/sin_certificado.txt 2>/dev/null)"
msg "el socket A vuelve al experimento"
RANURAS=A ESPERAR=0 REINICIAR_COLA=0 exec bash cola_tesis_experimento.sh
