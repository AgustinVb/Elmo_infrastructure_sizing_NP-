#!/usr/bin/bash
# Cierre del caso 5 (escalada20/1C/P_red_gen_bat), ronda 2 desde el caso 4: corta el Benders al
# terminar la iteracion 4 (archivo STOP) y sigue con mejorar_ub (pasadas 1 y 2) y el certificado,
# con los mismos pasos que cola_tesis_ob.sh. Salida en r2/ (la usa el resumen de la cola).
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
D=data/Resultados_finales_tesis/Mina_modelo/Produccion_escalada20/zona_norte/1C/P_red_gen_bat
C=output/Resultados_finales_tesis/Mina_modelo/casos_finales
R=$C/Produccion_escalada20/zona_norte/1C/P_red_gen_bat/r2
B=$R/benders
FLAGS="--free_charging --free_maintenance"
LOG=$C/cola.log
msg() { echo "[caso5 r2b] $(date '+%F %T') $1" | tee -a $LOG; }
corriendo() {
  powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='bash.exe'\" | Where-Object { \$_.CommandLine -like '*r2_desde_caso4*' }) { exit 0 } else { exit 1 }"
}
ub_resumen() {
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print((r.get('solucion') or '-').replace(chr(92), '/'), repr(r['ub_final']))" | tr -d '\r' || echo "- -"
}

# 1. STOP al cerrar la iteracion 4
until grep -qE 'k=4  UB=' $R/benders.log 2>/dev/null || ! corriendo; do sleep 30; done
if corriendo; then touch $B/STOP; msg "iteracion 4 cerrada: STOP al Benders"; fi
while corriendo; do sleep 30; done

read -r UB0 LBOP < <($PY -c "
import re;t=open('$R/benders.log',encoding='utf-8',errors='replace').read()
m=re.findall(r'NestedBenders[^\n]*: UB=([\d.]+)\s+LB=([\d.]+)',t) or re.findall(r'k=\d+  UB=([\d.]+)\s+LB=([\d.]+)',t)
print(*(m[-1] if m else ('','')))" | tr -d '\r')
if [ -z "$UB0" ] || [ ! -f $B/incumbente_respaldo.pkl ]; then msg "Benders sin resultado"; exit 1; fi
msg "Benders: UB $UB0  LB $LBOP"

# 2. mejorar_ub
msg "mejorar_ub pasada 1 (años 1-10, 600 s)"
$PY -u mejorar_ub.py --data_folder $D $FLAGS --respaldo $B/incumbente_respaldo.pkl --ub_esperado $UB0 \
    --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1 2>&1 | tee $R/mejorar_ub_1.log
read -r SOL UB < <(ub_resumen $R/mejorar_ub_1/resumen.json)
if [ "$SOL" = "-" ] || [ -z "$SOL" ]; then FUENTE="--respaldo $B/incumbente_respaldo.pkl"; UB=$UB0; SOL=""
else FUENTE="--solucion $SOL"; fi
msg "tras pasada 1: UB $UB (${SOL:-respaldo de Benders})"
msg "mejorar_ub pasada 2 (años 3-9, 900 s)"
$PY -u mejorar_ub.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
    --timelimit 900 --jobs 4 --out $R/mejorar_ub_2 2>&1 | tee $R/mejorar_ub_2.log
read -r SOL2 UB2 < <(ub_resumen $R/mejorar_ub_2/resumen.json)
if [ "$SOL2" != "-" ] && [ -n "$SOL2" ]; then SOL=$SOL2; UB=$UB2; FUENTE="--solucion $SOL"; fi
msg "tras pasada 2: UB $UB (${SOL:-respaldo de Benders})"
if [ -n "$SOL" ]; then
  $PY -u escribir_parameters.py --data_folder $D $FLAGS $SOL 2>&1 | tee $R/consumer.log
  $PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log
fi

# 3. certificado
GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))" | tr -d '\r')
msg "certificar (UB $UB, gap_objetivo $GO)"
$PY -u certificar_inversiones.py --data_folder $D $FLAGS $FUENTE --ub_esperado $UB --gap_objetivo $GO \
    --evaluar_vivas --jobs 4 --out $R/certificado 2>&1 | tee $R/certificado.log
msg "FIN: $(grep -E 'LB CERTIFICADA' $R/certificado.log | tail -1 | sed 's/^\[cert\] //')"
