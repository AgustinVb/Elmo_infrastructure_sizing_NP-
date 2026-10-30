#!/bin/bash
# Cola del L-shaped entero (lshaped.py, con el cambio A: corte de cargas funcion
# de b_bar y niveles de capacidad en el maestro) en los casos swap v4, desde
# cero (sin incumbente). Dos ranuras en paralelo con CPU nucleos cada una.
#
#   HORAS=5 CPU=28 nohup bash cola_lshaped.sh > $V/cola_lshaped.out 2>&1 &
#
# Segunda vuelta (lshaped_A2/): con las cotas de n_ssee_k (1,5 MW) y de P_pot
# (p_peak); la primera (lshaped_A/) tuvo big-M de 1e78 por P_pot sin cota.
# Casos 5 (sin problema numerico), 3 y 8 ya tienen corrida en lshaped_A/.
# Desde el 2026-10-29 16:30 la UB re-resuelve con P_pot libre los dias que
# fallan con la P_pot del maestro (caso 4 relanzado; la corrida anterior quedo
# en lshaped_A2_ppot_fijo/).
#
# LISTA_A / LISTA_B en el entorno reemplazan las listas de abajo ("" deja la
# ranura vacia), p. ej. para relanzar una sola ranura.
#
# Cada linea de una ranura: caso, mejor UB conocida, mejor LB conocida ("-" si
# no hay); solo sirven para comparar y para la alarma de cota invalida.
PY=${PY:-~/miniconda3/envs/py310/bin/python}
V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4
D=data/Tesis_final/Mina_modelo_v4
HORAS=${HORAS:-5}
CPU=${CPU:-28}
SUB=${SUB:-lshaped_A2}
cd "$(dirname "$0")"

msg() { echo "[lshaped A r640-01] $(date '+%F %T') $*" | tee -a $V/cola.log; }

resultado() {
  local out=$V/$1/$SUB
  msg "fin $1: $(grep -a 'FINAL' $out/lshaped.log | tail -1) $(grep -a 'contra lo mejor' $out/lshaped.log | tail -1 | sed 's/.*|//')"
}

correr() {
  local c=$1 ub=$2 lb=$3 out=$V/$1/$SUB extra=""
  mkdir -p $out
  [ "$ub" != "-" ] && extra="$extra --mejor_ub $ub"
  [ "$lb" != "-" ] && extra="$extra --mejor_lb $lb"
  # Cota de la subestacion del presolve de las corridas anteriores del caso
  # (2 modulos con cargadores 2C), como en cola_tesis_swap.sh; sin ella el
  # maestro propone n_ssee_k = 1 y los dias salen infactibles (caso 8).
  local cap=$(grep -aoh 'cotas impuestas al anio 1: station_1: >= [0-9]*' $V/$c/*/benders.log \
              2>/dev/null | grep -o '[0-9]*$' | sort -n | tail -1)
  extra="$extra --capacidad station_1=${cap:-1}"
  msg "inicio $c (L-shaped desde cero, $HORAS h, $CPU nucleos; mejor conocido UB $ub LB $lb) -> $out"
  $PY -u lshaped.py --data_folder $D/$c --out $out --horas $HORAS --cpu $CPU $extra \
      > $out/lshaped.log 2>&1
  resultado $c
}

ranura() { while read -r c ub lb; do [ -n "$c" ] && correr $c $ub $lb; done; }

# ESPERAR_A / ESPERAR_B = "pid caso": una corrida ya lanzada; la ranura espera
# que termine (y registra su resultado) antes de seguir con sus casos.
esperar() {
  [ -z "$1" ] && return
  while kill -0 $1 2>/dev/null; do sleep 60; done
  resultado $2
}

# Mejor conocido al 2026-10-29 (cola_swap_v4_algoritmo.md, todos certificados).
LISTA_A=${LISTA_A-"Produccion_escalada20/zona_norte/05C/RED 4090093.05 3884866
Produccion_base/zona_norte/1C/RED_GEN_BESS 2743895.04 2636527.63
Produccion_base/zona_centro/05C/RED_GEN_BESS 2767909.36 2695859.54
Produccion_base/zona_norte/05C/RED_GEN 2550277.54 2482036"}
LISTA_B=${LISTA_B-"Produccion_base/zona_norte/05C/RED_GEN_BESS 2493040.16 2440748.17
Produccion_base/zona_norte/05C/RED 3294143.94 3263609.06
Produccion_base/zona_norte/2C/RED_GEN_BESS 4949249.55 4703215"}

{ esperar $ESPERAR_A; ranura <<< "$LISTA_A"; } &
{ esperar $ESPERAR_B; ranura <<< "$LISTA_B"; } &
wait
msg "cola terminada"
