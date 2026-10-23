#!/bin/bash
# Caso 6 v4 (centro 05C RED_GEN_BESS) desde la solucion reparada guardada en
# <caso>/solucion_reparada (+1 bateria/bahia/cargador en el año 3, sin reemplazo):
# mejorar_ub pasadas 1 y 2, consumer y certificado.
cd "$(dirname "$0")"
PY=~/miniconda3/envs/py310/bin/python

D=data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_centro/05C/RED_GEN_BESS
R=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_centro/05C/RED_GEN_BESS
LBOP=2639629.4495
LOG=$R/../../../../cola.log
msg() { echo "[caso6 reparado] $(date '+%F %T') $*" | tee -a $LOG; }
ub_resumen() {
  [ -f $1 ] && $PY -c "
import json;r=json.load(open('$1')); print(r.get('solucion') or '-', repr(r['ub_final']))" || echo "- -"
}

rm -rf $R/mejorar_ub_1 $R/mejorar_ub_2
SOL=$R/solucion_reparada
UB=2821624.7132   # pulido de la solucion reparada
msg "solucion reparada (+1 bateria/bahia/cargador año 3, sin reemplazo): UB $UB"

msg "mejorar_ub pasada 1 (años 1-10, 600 s)"
$PY -u mejorar_ub.py --data_folder $D --solucion $SOL --ub_esperado $UB \
    --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out $R/mejorar_ub_1 2>&1 | tee $R/mejorar_ub_1.log
read S1 U1 < <(ub_resumen $R/mejorar_ub_1/resumen.json)
[ "$S1" != "-" ] && { SOL=$S1; UB=$U1; }
msg "tras pasada 1: UB $UB ($SOL)"

msg "mejorar_ub pasada 2 (años 3-9, 900 s)"
$PY -u mejorar_ub.py --data_folder $D --solucion $SOL --ub_esperado $UB --anios 3,4,5,6,7,8,9 \
    --timelimit 900 --jobs 4 --out $R/mejorar_ub_2 2>&1 | tee $R/mejorar_ub_2.log
read S2 U2 < <(ub_resumen $R/mejorar_ub_2/resumen.json)
[ "$S2" != "-" ] && { SOL=$S2; UB=$U2; }
msg "tras pasada 2: UB $UB ($SOL)"

$PY -u escribir_parameters.py $D $SOL 2>&1 | tee $R/consumer.log
$PY consumer.py $SOL 2>&1 | tee -a $R/consumer.log

GO=$($PY -c "g=($UB-$LBOP)/$UB; print(round(max(1e-5, min(0.005, 0.8*g)), 6))")
msg "certificar (UB $UB, gap_objetivo $GO, capacidad station_1>=1)"
$PY -u certificar_inversiones.py --data_folder $D --solucion $SOL --ub_esperado $UB --gap_objetivo $GO \
    --capacidad station_1=1 --jobs 4 --out $R/certificado 2>&1 | tee $R/certificado.log
msg "FIN: $(grep -E 'LB CERTIFICADA' $R/certificado.log | tail -1 | sed 's/^\[cert\] //')"
