#!/bin/bash
# DCH swap 2C (2 dias por año), desde cero: recorte con escalera 2..6 (recortar_holgada.py),
# recorte con macrobloques por estacion desde esa UB y L-shaped desde la mejor incumbente.
cd "$(dirname "$0")"
PY=~/miniconda3/envs/py310/bin/python
C=2C
DATA=data/Tesis_final/DCH/Bateria_482/Costo_fijo/$C/RED
B=output/Resultados_finales_tesis/DCH/casos_finales_v4_swap_2d/$C/RED
COMUN="--data_folder $DATA --n_years 11 --days_per_year 2 --consumption_model wp2 --pause_scheme dch"
mkdir -p $B
msg() { echo "[cola $C] $(date -u '+%F %T') $1" | tee -a $B/cola.log; }

msg "recorte desde cero"
$PY -u recortar_holgada.py $COMUN --grupos 'station_1|station_2,station_3' --escalera 2,3,4,5,6 \
    --t 600 --gap 0.01 --horas 14 --jobs 11 --out $B/recorte 2>&1 | tee $B/recorte.log
msg "fin recorte: $(grep -a 'FINAL' $B/recorte.log | tail -1)"
[ -f $B/recorte/resumen.json ] || { msg "sin incumbente, aborta"; exit 1; }

msg "recorte macro desde $B/recorte/solucion"
$PY -u recortar_holgada.py $COMUN --solucion $B/recorte/solucion --por_estacion \
    --grupos 'station_1|station_2|station_3' --escalera '' --movs operar,macro,tope,ssee,retrasar \
    --t 300 --gap 0.01 --horas 10 --jobs 11 --cpu 60 --out $B/recorte_macro 2>&1 | tee $B/recorte_macro.log
msg "fin recorte macro: $(grep -a 'FINAL' $B/recorte_macro.log | tail -1)"

read SOL UB < <($PY - "$B" <<'PYEOF'
import glob, json, os, sys
mejor = min((json.load(open(f))["ub"], os.path.dirname(f))
            for f in glob.glob(os.path.join(sys.argv[1], "recorte*", "resumen.json")))
print(f"{mejor[1]}/solucion {mejor[0]!r}")
PYEOF
)
msg "L-shaped desde $SOL (UB $UB)"
$PY -u lshaped.py $COMUN --solucion $SOL --ub_esperado $UB --horas 10 --jobs 11 --cpu 60 \
    --hilos_maestro 8 --out $B/lshaped 2>&1 | tee $B/lshaped.log
msg "fin L-shaped: $(grep -a '\[LS\] FINAL\|ABORTA' $B/lshaped.log | tail -1)"
