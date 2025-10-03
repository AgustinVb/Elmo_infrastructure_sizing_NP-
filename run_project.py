# run_project.py
import os, sys, subprocess, venv, platform

VENV_DIR   = ".venv_elmo"
REQ_FILE   = "requirements.txt"
ENTRYPOINT = "setup.py"  # tu archivo principal

def venv_python(venv_dir: str) -> str:
    if platform.system().lower().startswith("win"):
        return os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        return os.path.join(venv_dir, "bin", "python")

def ensure_venv(venv_dir: str):
    if not os.path.isdir(venv_dir):
        print(f"[bootstrap] Creando entorno virtual en {venv_dir} ...")
        venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(venv_dir)

def pip_install(python_exe: str, req_file: str):
        print(f"[bootstrap] Actualizando pip/setuptools/wheel ...")
        subprocess.check_call([python_exe, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
        print(f"[bootstrap] Instalando dependencias desde {req_file} ...")
        subprocess.check_call([python_exe, "-m", "pip", "install", "-r", req_file])

def main():
    repo_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(repo_root)

    if not os.path.isfile(ENTRYPOINT):
        sys.exit(f"[bootstrap] No se encontró '{ENTRYPOINT}' en {repo_root}")
    if not os.path.isfile(REQ_FILE):
        sys.exit(f"[bootstrap] No se encontró '{REQ_FILE}'. Debe estar en la raíz del proyecto.")

    # 1) crear venv si no existe
    ensure_venv(VENV_DIR)
    py_venv = venv_python(VENV_DIR)

    # 2) verificar (o instalar) dependencias exactas
    try:
        # Ajusta este import-check a tus librerías clave si quieres
        subprocess.check_call(
            [py_venv, "-c", "import pandas, numpy, pyomo.environ, xlrd, openpyxl, gurobipy, matplotlib"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print("[bootstrap] Entorno OK. (Dependencias ya presentes)")
    except subprocess.CalledProcessError:
        pip_install(py_venv, REQ_FILE)

    # 3) relanzar setup.py dentro del venv con los mismos argumentos
    cmd = [py_venv, ENTRYPOINT] + sys.argv[1:]
    print("[bootstrap] Ejecutando:", " ".join(cmd))
    # Usamos run en lugar de execve para que funcione en Windows sin sorpresas
    raise SystemExit(subprocess.call(cmd, env=os.environ))

if __name__ == "__main__":
    main()
