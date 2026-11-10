#!/bin/bash
# L-shaped (Fase 1 + branch-and-cut con cortes enteros + mejorar UB) para subir la LB de
# DCH swap 1C y 0,5C (2 dias por año), partiendo de la mejor incumbente del recorte
# (recortar_holgada.py). Espera a que terminen los recortes de 0,5C.
#   bash cola_dch_lshaped.sh            # 1C y 05C
#   HORAS=6 bash cola_dch_lshaped.sh 1C
cd "$(dirname "$0")"
PY=${PY:-$HOME/miniconda3/envs/py310/bin/python}
V=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d
HORAS=${HORAS:-10}
CASOS=${@:-1C 05C}
LOG=$V/cola_lshaped.log
msg() { echo "[lshaped $1] $(date -u '+%F %T') $2" | tee -a $LOG; }

sleep 60
while tmux has-session -t dch_05c 2>/dev/null || tmux has-session -t dch_05c_vigia 2>/dev/null \
      || tmux has-session -t dch_05c_rec2 2>/dev/null; do sleep 60; done
msg inicio "casos $CASOS, $HORAS h cada uno"
for tasa in $CASOS; do
  R=$V/$tasa/RED
  # Mejor incumbente: la menor UB de los resumen.json de recorte*/.
  read SOL UB < <($PY - "$R" <<'PYEOF'
import glob, json, os, sys
mejor = min(((json.load(open(f))["ub"], os.path.dirname(f))
             for f in glob.glob(os.path.join(sys.argv[1], "recorte*", "resumen.json"))), default=None)
print(f"{mejor[1]}/solucion {mejor[0]!r}" if mejor else "- -")
PYEOF
)
  if [ "$SOL" = "-" ]; then msg $tasa "sin incumbente, se salta"; continue; fi
  if [ -f $R/lshaped/resumen.json ]; then msg $tasa "ya terminado, se salta"; continue; fi
  msg $tasa "L-shaped desde $SOL (UB $UB)"
  $PY -u lshaped.py --data_folder data/Tesis_final/DCH/Bateria_482/Costo_fijo/$tasa/RED \
      --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch \
      --solucion $SOL --ub_esperado $UB --horas $HORAS --jobs 11 --cpu 60 --hilos_maestro 8 \
      --out $R/lshaped 2>&1 | tee $R/lshaped.log
  msg $tasa "fin: $(grep -a '\[LS\] FINAL\|ABORTA' $R/lshaped.log | tail -1)"
done
msg fin "listo"
