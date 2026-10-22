@echo off
rem Lanzador para PowerShell/cmd (sin bash en el PATH): corre correr_mono_caso3.sh con el
rem bash de Git. Uso:  .\correr_mono_caso3.cmd      (MIPFocus 1)
rem                    .\correr_mono_caso3.cmd 3    (MIPFocus 3)
cd /d "%~dp0"
"C:\Program Files\Git\bin\bash.exe" correr_mono_caso3.sh %*
