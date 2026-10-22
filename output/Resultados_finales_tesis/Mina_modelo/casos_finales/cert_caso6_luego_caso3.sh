#!/usr/bin/bash
# Caso 6 (centro/05C/P_red_gen_bat) con topes en 10: su solucion v3 pulida con los datos nuevos cuesta
# lo mismo (2.726.476,27) y la cota operacional nueva es 2.703.388,23 (gap 0,85 %), asi que se corto
# el Benders y se certifica directo esa solucion. Despues se relanza la ronda 2 del caso 3
# (rerun_caso3.sh, que ya no tiene nada que esperar).
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
C=output/Resultados_finales_tesis/Mina_modelo/casos_finales
D=data/Resultados_finales_tesis/Mina_modelo/Produccion_base/zona_centro/05C/P_red_gen_bat
R=$C/Produccion_base/zona_centro/05C/P_red_gen_bat/topes10
SOL=$C/Produccion_base/zona_centro/05C/P_red_gen_bat/mejorar_ub_2/solucion
LOG=$C/cola.log
msg() { echo "[topes10 caso 6] $(date '+%F %T') $1" | tee -a $LOG; }
msg "Benders cortado a mano tras el pulido inicial (UB 2.726.476,27 = v3; cota 2.703.388,23; 0,85 %): certificado directo"
mkdir -p $R
$PY -u certificar_inversiones.py --data_folder $D --free_charging --free_maintenance --solucion $SOL \
    --ub_esperado 2726476.27 --gap_objetivo 0.005 --evaluar_vivas --jobs 4 --out $R/certificado 2>&1 \
    | tee $R/certificado.log
msg "FIN: $(grep -E 'LB CERTIFICADA' $R/certificado.log | tail -1 | sed 's/^\[cert\] //')"
bash $C/rerun_caso3.sh
