# Pendientes — Mina_modelo (swap y mantenimiento liberados)

Rama `battery_swapping_multiaño`. Secciones 1–5: estado al 2026-09-28.
Sección 0: comparación de escenarios, al 2026-10-07.

Instancia: `data/Tesis_final/Mina_modelo/`, 10 años, 4 días representativos
(15, 105, 196, 288), `--free_charging --free_maintenance`.

---

## 0. Comparación RED / RED_GEN / RED_GEN_BESS (2026-10-07)

Los tres escenarios (`data/Tesis_final/Mina_modelo/<ESC>`) tienen datos
idénticos (mismos hashes de series y `.npy`); `elmo_data.xlsx` solo cambia
`g_max` (0 → 2) y `h_max` (0 → 2). Los modelos son **anidados**:
z\*(RED) ≥ z\*(RED_GEN) ≥ z\*(RED_GEN_BESS) sin resolver nada.

Metodología de cada uno: Nested Benders → `mejorar_ub.py` → `certificar_inversiones.py`.

| Escenario | Mejor UB | LB certificada | Gap | Solución | Certificado |
|---|---:|---:|---:|---|---|
| RED | 2.569.771,29 | 2.555.303,48 | 0,56 % | `mejorar_ub_r4/solucion` | `certificado_r4_0563` |
| RED_GEN | 2.143.677,60 | 2.047.595,74 | 4,48 % | `mejorar_ub_red_gen/solucion` | `certificado_red_gen_3pc` |
| RED_GEN_BESS | 2.085.557,66 | 2.035.547,42 | 2,40 % | `mejorar_ub_gen_bat_v6b/solucion` | `certificado_gen_bat_v6b_2pc` |

RED_GEN: Benders `P_red_gen` (7 iteraciones, UB 2.167.384,29, se cayó en k=8 por
el PC) → `mejorar_ub_red_gen` (−1,09 %, 40 días a 600 s) → certificado con
`--gap_objetivo 0.03` (7 candidatas, pool completo; manda la inversión del
propio incumbente, VIVA en 2.047.595,74).

Métricas: `python consumer.py <carpeta solucion>` (las tres carpetas ya tienen su
`parameters.json`; para soluciones nuevas, `escribir_parameters.py`). El COSTO
TOTAL de consumer reproduce el UB al centavo en los tres.

**Qué comparaciones son válidas con estos gaps** (intervalo [LB; UB] de cada z\*):

| Comparación | Ahorro garantizado | ¿Válida? |
|---|---|---|
| Generación vs solo red | 411.626 – 522.176 (16,0 – 20,4 %) | Sí: intervalos disjuntos. Certifica además que el óptimo de RED_GEN instala generación. |
| Gen + BESS vs solo red | 469.746 – 534.224 (18,3 – 20,9 %) | Sí |
| Valor del BESS (RED_GEN → RED_GEN_BESS) | 0 – 108.130 (0 – ~5 %) | **No**: intervalos superpuestos en [2.047.596; 2.085.558]. Los 58.120 (2,7 %) entre incumbentes no están certificados. |

Las métricas de operación/inversión de cada escenario describen la **mejor
solución encontrada**, no el óptimo.

### Pendientes

1. **Certificar (o descartar) el valor del BESS.** Hace falta LB(RED_GEN) >
   UB(RED_GEN_BESS) = 2.085.557,66: faltan **+37.962 (+1,9 %)**. El gap de
   RED_GEN está en la OPERACIÓN de los años 3–8 con la inversión del incumbente
   (sus días quedaron con gaps de 13–39 % a 600 s), y es esa inversión la que fija
   la LB del certificado. Bajar el UB de RED_GEN solo acota el BESS por arriba.
   Idea: una LB con la inversión fija más fuerte que Benders LP + corte de cargas
   (p. ej. con cotas MILP por día). Si no se cierra, reportar el BESS como no
   concluyente.
2. **Repetir el certificado de RED_GEN en el otro PC.** Se calculó en el PC con
   la CPU inestable (i9-14900K, microcode 0x123: ver violaciones de acceso). ~10 min:
   ```
   python -u certificar_inversiones.py --data_folder data/Tesis_final/Mina_modelo/RED_GEN --solucion output/Resultados_finales_tesis/Mina_modelo/mejorar_ub_red_gen/solucion --ub_esperado 2143677.604934195 --gap_objetivo 0.03 --jobs 4 --out output/Resultados_finales_tesis/Mina_modelo/certificado_red_gen_3pc_pc2
   ```
   Tiene que volver a dar LB 2.047.595,74 (gap 4,482 %).
3. **Segunda pasada de `mejorar_ub` sobre RED_GEN** (años 3–9, 900 s por día,
   partiendo de `mejorar_ub_red_gen/solucion`). Quedó corriendo en el PC
   inestable al hacer este push; si no llega su resultado, repetirla:
   ```
   python -u mejorar_ub.py --data_folder data/Tesis_final/Mina_modelo/RED_GEN --solucion output/Resultados_finales_tesis/Mina_modelo/mejorar_ub_red_gen/solucion --ub_esperado 2143677.604934195 --anios 3,4,5,6,7,8,9 --timelimit 900 --jobs 4 --out output/Resultados_finales_tesis/Mina_modelo/mejorar_ub_red_gen_2
   ```
   Si mejora: `escribir_parameters.py`, `consumer.py` y re-certificar con el UB nuevo.
4. **RED_GEN compra el 2º cargador y la 2ª batería en el año 3** (los otros dos
   escenarios, en el año 4): +41.131 de inversión. Pulir en RED_GEN las soluciones
   de RED y de RED_GEN_BESS (calendario del año 4) dio PEOR: 2.311.398 y 2.231.626
   (`prueba_pulido_*_en_red_gen`). No es concluyente: el pulido fija la operación
   entera, que viene armada para el otro escenario. En el certificado, la
   inversión con calendario del año 4 sigue VIVA (LB 2.057.583,86).
5. **PC TECNO-MASTER (i9-14900K):** no usarlo para corridas largas hasta
   actualizar la BIOS de la MSI PRO H610M-G WIFI (microcode ≥ 0x12B, perfil Intel
   Default). Limitar el turbo (`PROCTHROTTLEMAX 99`) NO evitó los crashes y quedó
   activo: revertir con `powercfg /setacvalueindex scheme_current sub_processor
   PROCTHROTTLEMAX 100; powercfg /setactive scheme_current`.

---

## 1. Dónde estamos (2026-09-28)

| | Costo (UB) | Cota (LB) | Gap | Origen |
|---|---|---|---|---|
| **10 años, mejor guardada** | **2.260.472,52** | 1.968.564,61 | **12,9 %** | v1 (`P_red_gen_bat_swap_libre/`) |
| 10 años, mejor vista (**no guardada**) | 2.118.775,38 | 1.968.564,61 | 7,09 % | v2, iteración 3 (la corrida murió en la 4) |
| 4 años | 1.916.141,92 | 1.797.006,58 | 6,22 % | Benders + pulido / cota operacional |

- La **cota operacional** (binarias operacionales relajadas, inversión entera)
  es la LB de trabajo. A 4 años es prácticamente igual al *best bound* de 1 h de
  B&B del monolítico (1.797.386), pero se calcula en 5 min. **No se sabe** si el
  hueco de 6–13 % es cota floja o solución mala: para certificar 1 % hace falta
  una cota más fuerte o que el UB baje.
- La cota del backward de Benders **nunca** superó a la operacional.

---

## 2. Preparar el equipo nuevo

### 2.1 `.venv_elmo` (modelo, descomposición)
Python 3.10. Versiones con las que corrió: pyomo 6.8.2, pandas 2.0.3,
**numpy 1.24.4**, gurobipy 10.0.0.

```powershell
py -3.10 -m venv .venv_elmo
.venv_elmo\Scripts\python.exe -m pip install -r requirements.txt
```

### 2.2 `.venv_gcg` (GCG) — solo para las tareas 1 y 2
Aislado porque PySCIPOpt 6 trae numpy 2, que rompe el pandas 2.0.3 del otro
entorno. Tiene que llamarse exactamente `.venv_gcg` y estar en la raíz del repo
(lo busca `src/optimization/decomposition/gcg_block.py`).

**Tiene que ser Python 3.10, de 64 bits, en Windows.** Los tres wheels están
compilados para esa combinación exacta (`cp310-win_amd64` en el nombre del
archivo): con otra versión de Python pip los rechaza ("is not a supported wheel
on this platform"), y como se instala con `--no-index` no puede buscar otros.
Verificar antes con `py -3.10 --version`; si no está, instalar Python 3.10.

```powershell
py -3.10 -m venv .venv_gcg
# Los wheels NO están en git (~105 MB): vienen en wheels_gcg.zip, que ya trae la
# carpeta wheels_gcg/ adentro. Descomprimirlo en la raíz del repo:
Expand-Archive wheels_gcg.zip -DestinationPath .
.venv_gcg\Scripts\python.exe -m pip install --no-index --find-links wheels_gcg pygcgopt pyscipopt numpy
.venv_gcg\Scripts\python.exe -m pip install "gurobipy>=13,<14"
```

### 2.3 Licencia de Gurobi
La académica es **por equipo**: sacar una nueva en el equipo nuevo
(`grbgetkey`, desde la red de la universidad). `grbgetkey` **no** viene con
`pip install gurobipy`: bajarlo aparte desde la página de Gurobi o instalar
Gurobi completo. Tiene que ser **v13 o mayor**:
`.venv_gcg` usa gurobipy 13 (una licencia v13 sirve también para el gurobipy 10
de `.venv_elmo`).

### 2.4 Antes de dejarlo corriendo de noche
- Enchufado y **tapa abierta** (o "cerrar la tapa: no hacer nada").
- Pausar Windows Update: fuera de las horas activas puede reiniciar solo.
- La cola bloquea la suspensión por inactividad, no la de la tapa.
- La cola completa dura **~24–30 h** (4 + 6 + 14–20): más de una noche.

---

## 3. Qué correr (en este orden)

Todo desde la **raíz del repo** (las rutas son relativas). La tarea 2 usa
`output/.../Mina_modelo/solucion_benders_4anios/`, que está en git.

Todo junto, desacoplado de la terminal:

```powershell
Start-Process -FilePath .venv_elmo\Scripts\python.exe `
    -ArgumentList '-u','cola_mina_modelo.py' -WindowStyle Hidden `
    -RedirectStandardOutput cola.stdout -RedirectStandardError cola.stderr
```

Solo algunas tareas, en el orden dado (p. ej. si `.venv_gcg` no está listo, las
tareas 1 y 2 fallan y la cola sigue, así que conviene lanzar solo la v3):

```powershell
Start-Process -FilePath .venv_elmo\Scripts\python.exe `
    -ArgumentList '-u','cola_mina_modelo.py','v3' -WindowStyle Hidden `
    -RedirectStandardOutput cola.stdout -RedirectStandardError cola.stderr
```

Seguimiento: `cola.log` solo anota inicio y fin de cada tarea; el detalle
(lo que se describe abajo en "qué mirar") está en `_logs_cola\<tarea>.log`.

```powershell
Get-Content output\Resultados_finales_tesis\Mina_modelo\cola.log -Tail 5
Get-Content output\Resultados_finales_tesis\Mina_modelo\_logs_cola\v3.log -Tail 20 -Wait
```

Para cortar una corrida de `setup.py` **conservando la solución**: crear un
archivo `STOP` en su carpeta de salida. Ctrl+C no sirve si la salida va a un
archivo o por `Tee-Object`.

### Tarea 1 — `gcg_largo` (4 h)
GCG con los subproblemas diarios resueltos por **Gurobi**, sobre el bloque anual
del año 4. Resultado: `.../gcg_bloque_y4_pricing_gurobi_largo/comparacion.json`.

**Pregunta:** ¿a qué valor converge la cota de la raíz del Dantzig-Wolfe?

| Referencia (mismo bloque) | Incumbente | Cota |
|---|---|---|
| Gurobi solo, 20 min | 589.150,60 | **571.205,19** |
| GCG + pricing Gurobi, 20 min | ninguno | 296.162 (396 columnas) |
| GCG + pricing SCIP, 20 min | ninguno | 249.386 (no se movió) |

- Si la cota de la raíz **supera 571.205**: la convexificación por día aporta
  algo que el B&B de Gurobi no ve → vale la pena usar GCG para cortes o
  certificado.
- Si queda por debajo: GCG descartado.

### Tarea 2 — `certificado` (6 h)
Raíz de Dantzig-Wolfe sobre el **monolítico de 4 años** (16 bloques año-día,
maestro de 109 filas = 0,04 %), subproblemas con Gurobi, solución de Benders como
incumbente. Resultado: `.../gcg_certificado_4anios_pricing_gurobi/certificado.json`
y el resumen al final de `_logs_cola/certificado.log`.

**Pregunta:** ¿su cota supera la operacional (**1.797.006,58**)?

- Primero verificar en el log `solucion reconstruida ... desvio relativo ~1e-9`
  (si no coincide con 1.916.141,92 aborta solo) y `trySol (incumbente factible?) -> True`.
- Con pricing SCIP: 69 min solo de preparación y cota 1.112.173 (< LP) en 2 h.
- **Nunca se probó con pricing Gurobi a esta escala** (sí en el bloque anual y en
  un juguete). Si se cae, la cola sigue con la v3.
- Ojo: la cota de DW y la operacional **no están ordenadas** en teoría (una
  relaja la operación, la otra la inversión). Es empírico.

### Tarea 3 — `v3` (~14–20 h)
10 años, híbrido. Carpeta `.../P_red_gen_bat_swap_libre_v3/`. Igual que la v2 pero
con **cortes fortalecidos** y los arreglos contra caídas (sección 5).

**Meta:** superar **2.118.775 (7,09 %)**, lo mejor que vio la v2.

Qué mirar en `_logs_cola/v3.log`:
- `k=N pulido de la solucion: A -> B` y `k=N UB=... gap=...` por iteración.
- `cota del backward = ...`: si alguna vez dice `manda`, los cortes superaron a
  la cota operacional (nunca pasó).
- `ERROR en la iteracion`: ya no mata la corrida, conserva la mejor y sigue.
- `[Hibrido] cota final = ... gap final = ...` al terminar.

---

## 4. Pendientes de código y análisis

1. **Commit del arreglo del test M0** (`tests/test_m0_regresion.py`, falla
   "28 vs 25"): es un falso positivo, la fórmula no cuenta
   `apertura_solo_primer_anio`. Arreglo propuesto, sin aplicar:
   `esperadas += n_stations * max(0, len(years) - 1)`.
2. **Separar la causa del forward de 6,7 M** de la iteración 2 de la v2
   (cortes de Benders vs. tope de 300 s vs. start por días que ignora `alpha`).
   La v3 con cortes fortalecidos da un primer dato.
3. **Cuello de botella del UB**: los bloques anuales con `alpha` no cierran
   (gaps de bloque 16–98 %). Idea de fondo: maestro con la trayectoria de
   inversión de los 10 años + 40 subproblemas diarios independientes (los días
   se separan fijadas ~23 variables anuales; ver `day_blocks.py`).
4. **La descomposición anual es ciega a `G_g` y `H`** (generación y BESS): el
   año 1 los decide sin ver el resto. En la v1 el forward los dejó en 0 y recién
   el pulido final los instaló. `--polish_each_iter` lo corrige por iteración.
5. **Ajustar `--day_timelimit`**: en años con infraestructura heredada los 8
   solves diarios agotan el tope demostrando un 5 % relativo sobre un objetivo
   chico; el primer incumbente aparece en segundos.
6. Si GCG sirve (tarea 1 o 2): **cortes desde el maestro de DW** (duales de las
   filas `link_*`, que caen en el maestro) — la API lo permite
   (`getMasterProb` + `getDualsolLinear`), falta mapear filas.

---

## 5. Qué se implementó en esta tanda (sin probar en el equipo nuevo)

| Flag / archivo | Qué hace |
|---|---|
| `--free_charging`, `--free_maintenance` | Regímenes DET liberados (portados de OB) |
| `--operational_bound`, `--lb_inicial` | Cota por relajación operacional / cota ya conocida |
| `--day_warm_start {off,first,always,fallback}` + `day_blocks.py` | MIP start del bloque anual por descomposición en días |
| `--block_mip_focus`, `--polish_each_iter`, `--skip_last_backward`, `--mono_improve_start_time` | Ajustes de la descomposición y la fase híbrida |
| `--block_solver gcg`, `--gcg_blocks` + `gcg_block.py`, `gcg_run.py` | MILP anual con GCG (descartado: pierde contra Gurobi) |
| `gcg_pricing_gurobi.py` | Subproblemas de GCG con Gurobi (esquiva un bug de PyGCGOpt con ctypes) |
| `gcg_bloque_anual.py`, `gcg_certificado.py`, `cola_mina_modelo.py` | Pruebas y cola |

Robustez: un error en una iteración ya no pierde la mejor solución; la fase 2
del start por días arranca desde la fase 1; reintento largo antes de rendirse;
`SubproblemNoIncumbent` en vez de un traceback críptico de Pyomo.
