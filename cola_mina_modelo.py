"""Cola de corridas pendientes de Mina_modelo (swap y mantenimiento liberados).

Corre las tareas en orden, una detras de otra, registrando inicio y fin en
output/Resultados_finales_tesis/Mina_modelo/cola.log, y (en Windows) le pide al
sistema que NO se suspenda mientras la cola este viva: una notebook que se
suspende congela la corrida (paso: 7 h perdidas). No evita la suspension por
cerrar la tapa si esa accion esta configurada.

Uso (desde la raiz del repo, con .venv_elmo activo):

    python -u cola_mina_modelo.py                       # las tres, en orden
    python -u cola_mina_modelo.py gcg_largo v3          # solo esas, en ese orden

Para que sobreviva al cierre de la terminal, lanzarla desacoplada:

    Start-Process -FilePath .venv_elmo\\Scripts\\python.exe `
        -ArgumentList '-u','cola_mina_modelo.py' -WindowStyle Hidden `
        -RedirectStandardOutput cola.stdout -RedirectStandardError cola.stderr

Ver PENDIENTES_mina_modelo.md para que mira cada tarea y como leer el resultado.
"""
import datetime as dt
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(REPO, "output", "Resultados_finales_tesis", "Mina_modelo")
COLA_LOG = os.path.join(OUT, "cola.log")
MINA = "output/Resultados_finales_tesis/Mina_modelo"

COMUNES_SETUP = ["--data_folder", "data/Tesis_final/Mina_modelo/", "--solver", "gurobi",
                 "--days_per_year", "4", "--free_charging", "--free_maintenance",
                 "--block_build_jobs", "1"]

TAREAS = {
    # 1. GCG (Dantzig-Wolfe, bloques por dia) con los subproblemas resueltos por
    #    Gurobi, sobre el bloque anual del año 4. En 20 min la raiz iba en
    #    296.162; Gurobi solo demuestra 571.205 en ese tiempo.
    "gcg_largo": ("gcg_bloque_anual.py",
                  ["--out", f"{MINA}/gcg_bloque_y4_pricing_gurobi_largo",
                   "--year", "4", "--n_years", "10", "--timelimit", "14400",
                   "--modos", "day_gurobi"]),
    # 2. Certificado de optimalidad: raiz de DW sobre el monolitico de 4 años
    #    (bloques año-dia) con la solucion de Benders de incumbente.
    "certificado": ("gcg_certificado.py",
                    ["--n_years", "4",
                     "--solucion", f"{MINA}/solucion_benders_4anios",
                     "--ub_esperado", "1916141.92", "--cota_operacional", "1797006.58",
                     "--mode", "year_day", "--nodes", "1", "--pricing", "gurobi",
                     "--timelimit", "21600",
                     "--out", f"{MINA}/gcg_certificado_4anios_pricing_gurobi"]),
    # 3. Corrida v3 de 10 años: cortes fortalecidos + arreglos contra caidas.
    "v3": ("setup.py",
           COMUNES_SETUP +
           ["--output_folder", f"{MINA}/P_red_gen_bat_swap_libre_v3/",
            "--mode", "hybrid", "--n_years", "10",
            "--lb_inicial", "1968564.61",
            "--cut_type", "strengthened", "--strengthened_timelimit", "300",
            "--max_iter", "5",
            "--mip_focus", "1", "--block_mip_focus", "1",
            "--solve_timelimit", "300",
            "--day_warm_start", "always", "--day_timelimit", "60", "--day_gap", "0.05",
            "--polish_each_iter", "--skip_last_backward",
            "--mono_improve_start_time", "0", "--mono_timelimit", "21600"]),
}
ORDEN = ["gcg_largo", "certificado", "v3"]


def log(msg):
    linea = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linea, flush=True)
    with open(COLA_LOG, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def bloquear_suspension():
    if os.name != "nt":
        return False
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED))


def main():
    pedidas = sys.argv[1:] or ORDEN
    desconocidas = [t for t in pedidas if t not in TAREAS]
    if desconocidas:
        print(f"tareas desconocidas: {desconocidas}. Validas: {ORDEN}")
        return 2
    os.makedirs(OUT, exist_ok=True)
    log(f"cola iniciada en {os.environ.get('COMPUTERNAME', '?')} (PID {os.getpid()}), "
        f"tareas {pedidas}, suspension bloqueada: {bloquear_suspension()}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    for nombre in pedidas:
        script, args = TAREAS[nombre]
        carpeta = os.path.join(OUT, f"_logs_cola")
        os.makedirs(carpeta, exist_ok=True)
        salida = os.path.join(carpeta, f"{nombre}.log")
        cmd = [sys.executable, "-u", script] + args
        log(f"[{nombre}] inicio: {script} " + " ".join(args))
        with open(salida, "w", encoding="utf-8") as f:
            rc = subprocess.run(cmd, cwd=REPO, stdout=f, stderr=subprocess.STDOUT,
                                env=env).returncode
        log(f"[{nombre}] fin, codigo de salida {rc} (log: {salida})")
    log("cola terminada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
