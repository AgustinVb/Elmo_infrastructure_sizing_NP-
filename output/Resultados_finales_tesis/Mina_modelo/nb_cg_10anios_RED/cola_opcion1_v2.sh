#!/usr/bin/bash
# Opcion 1, segunda version: CG a 10 años desde el incumbente de Nested Benders (nb_cont),
# con las tres heuristicas (del maestro, por columnas e IP con b_bar comun, esta ultima
# con columnas austeras y respaldo para dias dificiles). Corre cuando termina
# Nested Benders con --cg_lb (nb_cglb), para no competir por CPU.
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
export PYTHONIOENCODING=utf-8
PY=.venv_elmo/Scripts/python.exe
O10=output/Resultados_finales_tesis/Mina_modelo/nb_cg_10anios_RED
D=data/Tesis_final/Mina_modelo/RED
corriendo() {
    powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { \$_.CommandLine -like '*nb_cglb*' -or \$_.CommandLine -like '*prueba_ip_bbar*' }) { exit 0 } else { exit 1 }"
}
until [ -f $O10/nb_cglb.log ]; do sleep 60; done
sleep 120
while corriendo; do sleep 60; done
echo "cg_desde_nb_v2 $(date '+%F %T')" >> $O10/cola.estado
$PY -u cg_propia.py --data_folder $D --n_years 10 --solucion $O10/nb_cont --variantes_inicio ninguna \
    --ub_modo ambos --out $O10/cg_desde_nb_v2 --tiempo_max 21600 --ub_cada 15 \
    > $O10/cg_desde_nb_v2.log 2>&1
echo "fin_v2 $(date '+%F %T')" >> $O10/cola.estado
