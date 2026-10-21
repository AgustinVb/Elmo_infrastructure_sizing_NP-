#!/bin/bash
# 1) Al terminar el caso 6 (centro, tope 2): guarda sus resultados en RED_GEN_BESS_tope2
#    y sube g_max/h_max a 10 en sus datos.  2) Al terminar la cola principal (PID 305807):
#    relanza solo el caso 6 con el tope en 10 (sesion tmux 'centro10').
cd /home/avilches/Elmo_infrastructure_sizing_NP-
L=output/Resultados_finales_tesis/Mina_modelo/casos_finales/cola.log
until grep -q "\[cola r1 6/8 Produccion_base/zona_centro/05C/RED_GEN_BESS\] .* FIN:" $L; do
  kill -0 305807 2>/dev/null || { echo "$(date '+%F %T') la cola termino sin cerrar el caso 6"; exit 1; }
  sleep 30
done
sleep 10
mv output/Resultados_finales_tesis/Mina_modelo/casos_finales/Produccion_base/zona_centro/05C/RED_GEN_BESS output/Resultados_finales_tesis/Mina_modelo/casos_finales/Produccion_base/zona_centro/05C/RED_GEN_BESS_tope2
~/miniconda3/envs/py310/bin/python - <<'PYEOF'
import openpyxl
f="data/Tesis_final/Mina_modelo/Produccion_base/zona_centro/05C/RED_GEN_BESS/elmo_data.xlsx"
wb=openpyxl.load_workbook(f)
for hoja,col in (("Generators","g_max"),("Storage","h_max")):
    ws=wb[hoja]; hdr=[c.value for c in ws[1]]; j=hdr.index(col)+1
    for r in range(3, ws.max_row+1):
        v=ws.cell(r,j).value
        if isinstance(v,(int,float)) and 0<v<10: ws.cell(r,j).value=10
wb.save(f); print("centro: g_max/h_max -> 10")
PYEOF
echo "[cola centro10] $(date '+%F %T') caso 6 con tope 2 guardado en RED_GEN_BESS_tope2; datos de centro con tope 10" | tee -a $L
while kill -0 305807 2>/dev/null; do sleep 60; done
echo "[cola centro10] $(date '+%F %T') cola principal terminada: se relanza el caso 6 con tope 10 (tmux centro10)" | tee -a $L
tmux new-session -d -s centro10 -c "/home/avilches/Elmo_infrastructure_sizing_NP-" 'TOPE_H=200 bash cola_tesis_swap.sh 6 2>&1 | tee cola_centro10.out; echo; echo "=== centro tope 10 terminado (Enter para cerrar) ==="; read'
