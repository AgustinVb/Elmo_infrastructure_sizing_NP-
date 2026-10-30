@echo off
rem Lanzador para PowerShell/cmd (sin bash en el PATH): corre cola_dch_v4_todo.sh con el bash
rem de Git, mostrando todo en la terminal y guardandolo en cola.out. Uso:  .\cola_dch_v4_todo.cmd
rem El resumen por hitos va a output\Resultados_finales_tesis\DCH\casos_finales_v4_Carga_libre\cola.log.
cd /d "%~dp0"
if not exist "output\Resultados_finales_tesis\DCH\casos_finales_v4_Carga_libre" mkdir "output\Resultados_finales_tesis\DCH\casos_finales_v4_Carga_libre"
"C:\Program Files\Git\bin\bash.exe" -c "bash cola_dch_v4_todo.sh 2>&1 | tee -a output/Resultados_finales_tesis/DCH/casos_finales_v4_Carga_libre/cola.out"
