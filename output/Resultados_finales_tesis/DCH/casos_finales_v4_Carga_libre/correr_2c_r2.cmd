@echo off
rem Lanzador para PowerShell/cmd: corre correr_2c_r2.sh con el bash de Git, mostrando todo en la
rem terminal y guardandolo en 2C\P_red\r2\r2.out. Uso:  .\correr_2c_r2.cmd
cd /d "%~dp0"
if not exist "2C\P_red\r2" mkdir "2C\P_red\r2"
"C:\Program Files\Git\bin\bash.exe" -c "bash correr_2c_r2.sh 2>&1 | tee -a 2C/P_red/r2/r2.out"
