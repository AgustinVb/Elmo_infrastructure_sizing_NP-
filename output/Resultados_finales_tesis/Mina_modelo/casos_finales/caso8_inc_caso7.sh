#!/bin/bash
# Caso 8 (base/norte/2C/RED_GEN_BESS) desde la solucion del caso 7 (misma data con 1C).
# El incumbente del caso 8 (UB 3.263.252, gap 12,5 %) compra 2 modulos SSEE; el del
# caso 7 usa 1 nave, 1 cargador, 1 bateria y 1 SSEE por año, que es justo la
# candidata 1 del certificado del 8 (LB 2.855.203, VIVA, la que fija el gap) y nunca
# se evaluo con una solucion real. La data solo difiere en el cargador (2C: 964 kW,
# 964.000) y la degradacion (0,0451 vs 0,0273), asi que la factibilidad NO esta
# asegurada como en el caso 4 -> 5: Benders pule el incumbente al arrancar y lo dice
# en la linea "[ub] incumbente pulido ..." (si es infactible, abortar a mano).
# Correr DESPUES del caso 7 pasada 3 (usa su solucion si existe). Salida en <caso 8>/inc_caso7/.
cd /home/avilches/Elmo_infrastructure_sizing_NP-
PY=~/miniconda3/envs/py310/bin/python
O=output/Resultados_finales_tesis/Mina_modelo/casos_finales
L=$O/cola.log
D=data/Tesis_final/Mina_modelo/Produccion_base/zona_norte/2C/RED_GEN_BESS
C8=$O/Produccion_base/zona_norte/2C/RED_GEN_BESS
C7=$O/Produccion_base/zona_norte/1C/RED_GEN_BESS
INC=$C7/mejorar_ub_3/solucion; [ -d $INC ] || INC=$C7/mejorar_ub_2/solucion
LBOP=2836567.46   # cota operacional del caso 8 (ronda 1), para no recalcularla
R=$C8/inc_caso7; B=$R/benders
HORAS=${HORAS:-12}; SIN_MEJORA=4
msg() { echo "[cola caso8-inc] $(date '+%F %T') $1" | tee -a $L; }

msg "inicio: Benders $HORAS h desde la solucion del caso 7 ($INC)"

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
    --polish_each_iter --skip_last_backward --corte_cargas_lhd $C8/corte_cargas_lhd.json 2>&1 | tee $R/benders.log
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
