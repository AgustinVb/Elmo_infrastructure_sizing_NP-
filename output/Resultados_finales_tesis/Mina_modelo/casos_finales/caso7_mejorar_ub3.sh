#!/bin/bash
# Caso 7 (base/norte/1C/RED_GEN_BESS): tercera pasada de mejorar_ub con mas tiempo.
# El certificado dejo VIVA solo la inversion del incumbente (1 nave, 1 cargador,
# 1 bateria, 1 modulo SSEE): el gap de 7,79 % es todo de operacion. La pasada 2
# (900 s por dia) termino con MILP diarios de gap 25-99 % en los años 3-8; aca se
# repiten esos años con 3600 s por dia, desde la solucion de la pasada 2.
# Corre en cola: despues del caso 5 desde la solucion del caso 4 (caso5_inc_caso4.sh).
cd /home/avilches/Elmo_infrastructure_sizing_NP-
PY=~/miniconda3/envs/py310/bin/python
O=output/Resultados_finales_tesis/Mina_modelo/casos_finales
L=$O/cola.log
D=data/Tesis_final/Mina_modelo/Produccion_base/zona_norte/1C/RED_GEN_BESS
R=$O/Produccion_base/zona_norte/1C/RED_GEN_BESS
SOL=$R/mejorar_ub_2/solucion; UB=1810947.1036117477
LB=1669905.71   # LB certificada (la unica inversion VIVA es la del incumbente)
msg() { echo "[cola caso7-ub3] $(date '+%F %T') $1" | tee -a $L; }

msg "inicio: mejorar_ub pasada 3 (años 3-8, 3600 s) desde $SOL"

$PY -u mejorar_ub.py --data_folder $D --solucion $SOL --ub_esperado $UB --anios 3,4,5,6,7,8 \
    --timelimit 3600 --jobs 4 --out $R/mejorar_ub_3 2>&1 | tee $R/mejorar_ub_3.log
read SOL3 UB3 < <([ -f $R/mejorar_ub_3/resumen.json ] && $PY -c "
import json;r=json.load(open('$R/mejorar_ub_3/resumen.json')); print(r.get('solucion') or '-', repr(r['ub_final']))" || echo "- -")
if [ "$SOL3" = "-" ]; then
  msg "FIN: sin mejora, UB sigue en $UB"; exit 0
fi
msg "tras pasada 3: UB $UB3 ($SOL3)"
$PY -u escribir_parameters.py $D $SOL3 2>&1 | tee $R/consumer_ub3.log
$PY consumer.py $SOL3 2>&1 | tee -a $R/consumer_ub3.log
msg "FIN: UB $UB3, LB certificada $LB -> gap $($PY -c "print(f'{($UB3-$LB)/$UB3:.3%}')")"
