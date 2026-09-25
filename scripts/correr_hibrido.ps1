<#
.SYNOPSIS
    Lanza una instancia en --mode hybrid con la configuracion de solver calibrada
    sobre la corrida DET/Gen_Bat/241kW (22-sep-2026).

.DESCRIPTION
    Los defaults de este script NO son los de setup.py: salen de medir donde
    rinde cada hora de computo en esa corrida.

    --mono_timelimit 2700 (contra las 12 h que se venian usando)
        La cota dual de Gurobi en la fase monolitica llego a 2,149,198 a los
        245 s y solo a 2,195,084 a los 8,834 s: el 97,9% de la cota esta en los
        primeros 4 minutos. Las 2,4 h siguientes movieron el incumbente 1,364
        (0,045%). 45 minutos capturan la cota entera y liberan 11 h.

    --max_iter 8 (contra 3)
        Una iteracion de Benders mejoro el incumbente a 86,714 USD/h; el
        monolitico del hibrido, a 558 USD/h. Unas 150 veces peor. Ademas k=3
        rindio mas que k=2 (-5,79% contra -4,71%), asi que a las 3 iteraciones
        todavia no habia rendimientos decrecientes: se corto antes de tiempo.

    --solve_timelimit 900 (sin cambio)
        Es la causa raiz del gap -- 8 a 10 de los 14 bloques llegan al limite
        cada iteracion, con gaps por anio de hasta 89% -- pero subirlo duplica
        el costo por iteracion, y por lo de arriba conviene gastar esas horas
        en mas iteraciones. El ataque barato al mismo sintoma es MIPFocus=1 en
        los bloques (hoy corren con Gurobi en default, ver passes.py:86).

    Presupuesto esperado: ~18,4 h de descomposicion + 45 min de monolitico.

.EXAMPLE
    .\scripts\correr_hibrido.ps1 `
        -DataFolder data/Tesis_final/DET/241kW_2dias_Pred_Gen_Bat `
        -OutputFolder output/Resultados_finales_tesis/DET/Gen_Bat/241kW_v2

.EXAMPLE
    # Ver el comando sin ejecutarlo
    .\scripts\correr_hibrido.ps1 -DataFolder data/X -OutputFolder output/Y -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DataFolder,

    [Parameter(Mandatory = $true)]
    [string]$OutputFolder,

    [ValidateSet(2, 4)]
    [int]$DaysPerYear = 4,

    [int]$MaxIter = 8,

    [int]$SolveTimelimit = 900,

    [int]$MonoTimelimit = 2700,

    # Flags extra que van tal cual a setup.py (--autonomous_mode,
    # --consumption_model wp2, --n_years 4, etc.).
    [string[]]$Extra = @(),

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $DataFolder)) {
    throw "No existe la carpeta de datos: $DataFolder"
}
if (-not (Test-Path $OutputFolder)) {
    New-Item -ItemType Directory -Path $OutputFolder -Force | Out-Null
}

$runLog = Join-Path $OutputFolder 'run.log'

# Preflight. Antes de encolar ~19 h de computo conviene confirmar que el
# interprete es el correcto: en una consola sin el entorno de conda activado
# `python` resuelve al stub de la Microsoft Store y la corrida muere de entrada.
if (-not $DryRun) {
    $probe = python -c "import pyomo, gurobipy" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "El 'python' de esta consola no sirve para la corrida (falta pyomo/gurobipy, o es el stub de la Microsoft Store). Active el entorno de conda primero. Detalle: $probe"
    }
}

$argumentos = @(
    'setup.py'
    '--mode', 'hybrid'
    '--solver', 'gurobi'
    '--data_folder', $DataFolder
    '--output_folder', $OutputFolder
    '--days_per_year', $DaysPerYear
    '--max_iter', $MaxIter
    '--solve_timelimit', $SolveTimelimit
    '--mono_timelimit', $MonoTimelimit
) + $Extra

Write-Host "python $($argumentos -join ' ')" -ForegroundColor Cyan
Write-Host "log -> $runLog" -ForegroundColor Cyan
Write-Host ""
Write-Host "Para cortar a mano CONSERVANDO la solucion, cree el archivo:" -ForegroundColor Yellow
Write-Host "  $(Join-Path $OutputFolder 'STOP')" -ForegroundColor Yellow
Write-Host "Ctrl+C NO sirve: la salida esta pipeada a Tee-Object." -ForegroundColor Yellow
Write-Host ""

if ($DryRun) {
    Write-Host "[DryRun] no se ejecuta nada." -ForegroundColor DarkGray
    return
}

$t0 = Get-Date
python @argumentos 2>&1 | Tee-Object -FilePath $runLog
$codigo = $LASTEXITCODE
$duracion = (Get-Date) - $t0

Write-Host ""
Write-Host ("Termino en {0:hh\:mm\:ss} (exit {1})" -f $duracion, $codigo)
# En --mode hybrid la cota del LP monolitico se omite sola (ver setup.py), asi
# que no hace falta pasar --no_monolithic_lp_bound.
exit $codigo
