#!/bin/bash
# Caso 5 (escalada20/norte/05C/RED_GEN_BESS) desde la solucion del caso 4 (misma
# produccion, solo RED): el incumbente de la ronda 1 compra una nave (año 6) y una
# bateria (año 7) de mas que el Benders no logra soltar con la caja de +-1. La
# solucion del caso 4 es factible (generacion y BESS opcionales): pulida sobre el
# caso 5 da UB 2.402.019 (vs 2.620.765), lo mismo que funciono en carga_ob_multiaño.
# Corre en cola: despues de la cola principal (PID 305807) y del caso 6 con tope 10
# (centro_tope10.sh -> tmux centro10). Salida en <caso 5>/inc_caso4/.
cd /home/avilches/Elmo_infrastructure_sizing_NP-
PY=~/miniconda3/envs/py310/bin/python
O=output/Resultados_finales_tesis/Mina_modelo/casos_finales
L=$O/cola.log
D=data/Tesis_final/Mina_modelo/Produccion_escalada20/zona_norte/05C/RED_GEN_BESS
C5=$O/Produccion_escalada20/zona_norte/05C/RED_GEN_BESS
INC=$O/Produccion_escalada20/zona_norte/05C/RED/mejorar_ub_2/solucion
LBOP=2208945.69   # cota operacional del caso 5 (ronda 1), para no recalcularla
R=$C5/inc_caso4; B=$R/benders
HORAS=${HORAS:-12}; SIN_MEJORA=4
msg() { echo "[cola caso5-inc] $(date '+%F %T') $1" | tee -a $L; }

# El orden (despues del caso 6 con tope 10) lo da reanudar_cola.sh.
msg "inicio: Benders $HORAS h desde la solucion del caso 4 ($INC)"

vigia() {  # STOP tras $SIN_MEJORA iteraciones cerradas sin mejora del UB
  while true; do
    if $PY -c "
import re,sys
ub=[float(u) for u in re.findall(r'k=\d+  UB=([\d.]+)\s+LB=',open('$R/benders.log',errors='replace').read())]
s=0
for i in range(len(ub)-1,0,-1):
    if ub[i] < ub[i-1]-1e-6: break
    s+=1
sys.exit(0 if s>=$SIN_MEJORA else 1)" 2>/dev/null; then
      touch $B/STOP; msg "$SIN_MEJORA iteraciones sin mejora del UB: STOP"; return
    fi
    sleep 30
  done
}
ub_resumen() {
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print(r.get('solucion') or '-', repr(r['ub_final']))" || echo "- -"
}

# --- 1. Benders ---------------------------------------------------------------
mkdir -p $B; rm -f $B/STOP
vigia & VP=$!
$PY -u setup.py --data_folder $D --solver gurobi --days_per_year 4 --free_charging --free_maintenance \
    --block_build_jobs 1 --output_folder $B --mode decomposed --n_years 10 --gap_tol 0.01 --max_iter 50 \
    --max_hours $HORAS --lb_inicial $LBOP --incumbente_inicial $INC \
    --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 \
    --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 \
    --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 \
    --polish_each_iter --skip_last_backward --corte_cargas_lhd $C5/corte_cargas_lhd.json 2>&1 | tee $R/benders.log
kill $VP 2>/dev/null; wait $VP 2>/dev/null
read UB0 LB < <($PY -c "
import re;t=open('$R/benders.log',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t) or re.findall(r'k=\d+  UB=([\d.]+)\s+LB=([\d.]+)',t)
print(*(m[-1] if m else ('','')))")
[ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ] && { msg "Benders sin resultado"; exit 1; }
msg "Benders: UB $UB0  LB $LB"

# --- 2. mejorar_ub ------------------------------------------------------------
$PY -u mejorar_ub.py --data_folder $D --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
    --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1 2>&1 | tee $R/mejorar_ub_1.log
read SOL UB < <(ub_resumen $R/mejorar_ub_1/resumen.json)
[ "$SOL" = "-" ] && { SOL=$B; UB=$UB0; }
msg "tras pasada 1: UB $UB ($SOL)"
$PY -u mejorar_ub.py --data_folder $D --solucion $SOL --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
    --timelimit 900 --jobs 4 --out $R/mejorar_ub_2 2>&1 | tee $R/mejorar_ub_2.log
read SOL2 UB2 < <(ub_resumen $R/mejorar_ub_2/resumen.json)
if [ "$SOL2" != "-" ]; then SOL=$SOL2; UB=$UB2; fi
msg "tras pasada 2: UB $UB ($SOL)"
$PY -u escribir_parameters.py $D $SOL 2>&1 | tee $R/consumer.log
$PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log

# --- 3. certificado -----------------------------------------------------------
GO=$($PY -c "g=($UB-$LB)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
msg "certificar (UB $UB, gap_objetivo $GO)"
$PY -u certificar_inversiones.py --data_folder $D --solucion $SOL --ub_esperado $UB --gap_objetivo $GO \
    --jobs 4 --out $R/certificado 2>&1 | tee $R/certificado.log
msg "FIN: $(grep -E 'LB CERTIFICADA' $R/certificado.log | tail -1 | sed 's/^\[cert\] //')"
