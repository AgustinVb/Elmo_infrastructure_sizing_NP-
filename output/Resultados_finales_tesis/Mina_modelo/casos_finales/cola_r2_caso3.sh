#!/usr/bin/bash
# Ronda 2 del caso 3 (base/norte/05C/P_red_gen): su primer intento se cayo a los 7 min por
# una instruccion ilegal del CPU (0xc000001d) y la cola lo salto. Se relanza cuando termina
# la ronda 2 en curso (caso 5).
cd /c/Users/TECNO-MASTER/Desktop/Elmo_infrastructure_sizing_NP-
corriendo() {
  powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"name='bash.exe'\" | Where-Object { \$_.CommandLine -like '*cola_tesis_ob_r2.out*' }) { exit 0 } else { exit 1 }"
}
while corriendo; do sleep 60; done
R=output/Resultados_finales_tesis/Mina_modelo/casos_finales/Produccion_base/zona_norte/05C/P_red_gen/r2
[ -f $R/benders.log ] && mv $R/benders.log $R/benders_caida_0xc000001d.log
RONDA=2 PY=.venv_elmo/Scripts/python.exe bash cola_tesis_ob.sh 3 >> cola_tesis_ob_r2_caso3.out 2>&1
