# Plan: llevar battery_swapping a paridad con carga_on_board

Objetivo: portar a `battery_swapping` las implementaciones hechas en
`carga_on_board` para correr los escenarios "Taller DET Agosto", evitando un
merge directo (las ramas divergen desde 2025-12-09 y `functions.py`/
`opt_model.py` tienen lógica de swap entrelazada con varios de estos mismos
bloques, por lo que un merge produciría conflictos reales de contenido, no
solo de texto).

## Alcance revisado

- Merge-base: `8862b759` (2025-12-09, "casos de estudi V2").
- Último commit de `battery_swapping`: `fb300291` (2026-06-18).
- Commits de `carga_on_board` posteriores a esa fecha (el tramo que falta
  portar): `e3e628a0`..`5aeee384`, 15 commits, 23-jun a 28-jul 2026.
- Antes del 2026-06-18 ambas ramas evolucionaron en paralelo (curtailment,
  ciclicidad BESS, Gx aparecen en ambas ~12-jun), así que no se revisan aquí.

## Principio de orden

Portar de menor a mayor riesgo de conflicto con la lógica de swap: primero lo
aditivo/independiente (nuevos parámetros opcionales, archivos nuevos), al
final lo que toca directamente `build_all_constraints` y las detenciones,
que es donde OB y BS tienen implementaciones distintas del mismo concepto.

## Pasos

### 1. Fix aislado: `json_plotter.py`
Corregir el cálculo de día-del-año para escenarios multi-año
(`day_of_year = ((di - 1) % 365) + 1` en vez de relativo a `min(self.days)`).
Sin dependencias, aplica igual en BS. Hacerlo primero como calentamiento y
para no arrastrarlo mezclado con cambios de mayor riesgo.

### 2. Modelo de consumo WP2 (`timeseries.py`, `setup.py`)
- Portar `_load_wp2_consumption_by_node` y el nuevo `get_trips(consumption_model=, wp2_consumption_json=)`.
- Portar `resolve_wp2_json_path` y los flags `--consumption_model`/`--wp2_consumption_json` en `setup.py`.
- Los getters de datos (`elhd.get_fuel_consumption`, etc.) **ya existen** en
  `elhd.py` de BS — no hay que tocar esa capa.
- Riesgo bajo: es una rama de código nueva (`if consumption_model == 'wp2'`),
  wp1 queda intacto como default.
- Validar: correr un caso BS chico con `wp1` antes/después (no debe cambiar
  nada) y luego con `wp2`.

### 3. Eficiencia de batería (`functions.py`: `eta_charge_i`/`eta_discharge_i`)
- Los getters (`get_charge_efficiency`/`get_discharge_efficiency`) ya existen
  en `elhd.py` de BS. Falta el parámetro Pyomo y aplicarlo en la restricción
  de SOC.
- **Punto de decisión**: en OB, `charge`/`discharge` son tasas continuas de
  carga en estación. En BS el "swap" probablemente reemplaza la batería de
  golpe (no es una tasa) — antes de replicar
  `charge_eff = charge * eta_charge_i` / `discharge_eff = discharge / eta_discharge_i`
  hay que revisar cómo está formulado el `battery_soc` de BS y confirmar que
  el mismo patrón aplica, o si la eficiencia debe entrar de otra forma (p.ej.
  al calcular cuánta energía trae la batería de repuesto).

### 4. Plumbing de descomposición: `daily_target_override` / `fixed_infra` / `threads`
- Portar en `OptRules.__init__` (functions.py), `OptModel.__init__` +
  `_apply_fixed_infra` (opt_model.py), y la firma de `OptimizationModel`
  (`src/optimization/__init__.py`).
- Todos son parámetros opcionales con default `None`/`32` — no cambian el
  comportamiento existente si no se usan. Riesgo bajo.
- También portar el fix del warm-start (solo carga variables discretas y
  fija explícitamente a 0 los índices binarios no exportados) — es una
  corrección de correctitud independiente del resto.

### 5. Asignación LHD→nodo (`StartAssign`/`EndAssign`, `assign_state`, `min_assign_duration`)
- Aditivo (variables y restricciones nuevas, no reemplazan nada existente).
- Antes de portar: revisar si BS ya tiene una restricción equivalente de
  duración mínima de asignación a nodo con otro nombre, para no duplicar.

### 6. `run_descomposicion.py` (generalización completa)
- Requiere diseño propio para BS, no es copy-paste: `_apply_fixed_infra` solo
  conoce `N_chargers`/`X` (infraestructura on-board). BS necesita fijar el
  equivalente de bahías/baterías de swap en la Fase 2, y la etapa de
  agregación (`max` de `N_chargers`/`X` entre días) necesita las variables
  de infraestructura de swap correspondientes.
- La limitación conocida de Generators/Storage (Gx/BESS) no propagados a
  macrobloques (documentada en el docstring del script) aplica igual para BS.
- Este es el paso de mayor esfuerzo de diseño del plan; conviene hacerlo
  después de tener 2-5 andando en BS, no antes.

### 7. Intervalos DET nuevos + switch de restricciones DCH/DET
- Mayor riesgo: `build_all_constraints` de BS entrelaza detenciones con
  lógica de swap propia. No es un copy-paste del bloque de OB.
- Aplicar directamente el esquema correcto (`meal`/`maintenance`/
  `road_clearing`, con `maintenance` = detenido sin cargar/swap y
  `road_clearing` = igual que `meal`) en vez de replicar el estado
  intermedio que tuvo OB (donde `road_clearing` se trataba como
  `maintenance` hasta que se corrigió en esta sesión).
- **Punto de decisión**: ¿puede un LHD hacer swap durante `road_clearing` en
  BS, igual que puede cargar en OB? Asumimos que sí por analogía, pero hay
  que confirmarlo antes de portar la restricción `charge_only_meal_or_between_shifts_det`
  equivalente.
- Verificar que no quede ninguna referencia residual al pause_type viejo
  (`"stop"`/`"stops"`, `shift_change`) que haga que un set quede vacío en
  silencio — es exactamente el bug que se corrigió hoy en `functions.py` de
  `carga_on_board`.

### 8. Herramientas de reporte (`combine_macrobloques_for_plot.py`, agregaciones de `consumer.py`, modo DET/DCH en `json_plotter.py`)
- Riesgo bajo, mayormente independientes del modelo de optimización.
- Portar cuando ya haya resultados de BS por macrobloque/día que agregar
  (después del paso 6).

### 9. Scripts de un solo uso (`batch_plot_taller.py`, `export_material_extraction_totals.py`)
- Ambos hardcodean `ROOT = Path("output/DCH_taller_julio")` y `MODE = "DCH"`.
- Prioridad baja / opcional: adaptar solo si se necesita un reporte análogo
  para un estudio específico de BS.

## Validación sugerida por etapa

- Antes/después de cada paso: correr un caso BS chico (1 día) ya existente y
  comparar costo total y SOC de borde — deben quedar iguales si el paso es
  puramente aditivo (2, 4, 5, 8).
- Para el paso 7 (DET): confirmar explícitamente, imprimiendo o inspeccionando
  los sets construidos, que `time_intervals_forced_detention_set` y
  `time_intervals_road_clearing_det_set` no quedan vacíos por un nombre de
  pause_type que no matchea.

## Puntos que requieren decisión del usuario antes de portar

1. Semántica de swap durante `road_clearing` (paso 7).
2. Cómo extender `fixed_infra`/agregación de infraestructura a las variables
   propias de swap (paso 6).
3. Si BS necesita correr con `wp2` para el mismo dataset de Taller DET
   Agosto, o si por ahora WP2 es exclusivo de OB.
4. Si la fórmula de eficiencia de batería de OB aplica tal cual al
   `battery_soc` de BS (paso 3).

## Nota sobre documentación existente

`implementacion_modelo_consumo_wp2.md` (raíz del repo) menciona variables
`s_eta_charge_b`/`s_eta_discharge_b` para swap y referencia un
`CAMBIOS_WP2_PAUSAS_Y_RUNNER.md` que no existe en el repo. No se encontró
ninguna de esas variables en el código real de ninguna rama al momento de
este plan — tratar ese documento como aspiracional/desactualizado, no como
fuente de verdad, y confirmar contra el código antes de asumir que algo ya
está implementado.
