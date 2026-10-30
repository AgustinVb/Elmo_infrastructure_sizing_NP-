# Vigia de la cola DCH v4: si la cola completa muere (terminal cerrada, bash caido) y no ha
# terminado, la relanza con cola_dch_v4_todo.cmd en una ventana nueva. La cola retoma sola lo
# que quedo a medias (Benders desde su respaldo, pasos terminados se saltan).
#   Lanzar:  Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','vigia_cola_dch.ps1' -WindowStyle Hidden
#   Detener: crear output\Resultados_finales_tesis\DCH\casos_finales_v4_Carga_libre\NO_RELANZAR
# Las caidas de Python dentro de la cola no pasan por aca: las reintenta la propia cola.
$repo = $PSScriptRoot
$V = Join-Path $repo 'output\Resultados_finales_tesis\DCH\casos_finales_v4_Carga_libre'
$log = Join-Path $V 'vigia.log'
$cola_log = Join-Path $V 'cola.log'
$CHEQUEOS_SIN_COLA = 3     # chequeos seguidos (1 por minuto) sin procesos antes de relanzar
$MAX_RELANZAMIENTOS = 20

function Anotar($texto) {
    "[vigia] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $texto" | Out-File -FilePath $log -Append -Encoding utf8
}

function Cola-Viva {
    $p = Get-CimInstance Win32_Process -Filter "Name='bash.exe'" |
        Where-Object { $_.CommandLine -match 'cola_dch_v4_todo\.sh|cola_dch_ob_v4\.sh' -and $_.CommandLine -notmatch 'shell-snapshots' }
    return [bool]$p
}

function Cola-Terminada {
    # cola_dch_v4_todo.sh corre la ronda 2 dos veces; cada pasada cierra con "[dch v4 r2 fin]".
    if (-not (Test-Path $cola_log)) { return $false }
    $n = @(Select-String -Path $cola_log -Pattern '\[dch v4 r2 fin\]').Count
    return ($n -ge 2)
}

Anotar "inicio (pid $PID)"
$sin_cola = 0
$relanzamientos = 0
while ($true) {
    if (Test-Path (Join-Path $V 'NO_RELANZAR')) { Anotar 'existe NO_RELANZAR: termina'; break }
    if (Cola-Terminada) { Anotar 'la cola termino (ronda 2, dos pasadas): termina'; break }
    if (Cola-Viva) {
        $sin_cola = 0
    } else {
        $sin_cola++
        if ($sin_cola -ge $CHEQUEOS_SIN_COLA) {
            if ($relanzamientos -ge $MAX_RELANZAMIENTOS) { Anotar "ya van $relanzamientos relanzamientos: termina sin relanzar"; break }
            $relanzamientos++
            Anotar "la cola no esta corriendo hace $sin_cola min: RELANZAMIENTO $relanzamientos"
            Start-Process -FilePath (Join-Path $repo 'cola_dch_v4_todo.cmd') -WorkingDirectory $repo
            $sin_cola = 0
            Start-Sleep -Seconds 120
        }
    }
    Start-Sleep -Seconds 60
}
