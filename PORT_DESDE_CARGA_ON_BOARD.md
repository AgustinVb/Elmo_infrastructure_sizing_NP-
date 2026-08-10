# Cambios portados desde `carga_on_board` — resumen e impacto en la data de entrada

Este documento resume los cambios portados/adaptados desde la rama `carga_on_board`
hacia `carga_ob_multiaño` (índice `y` de año en todas las variables/parámetros).
Dos de los cuatro cambios pedidos (eficiencia de carga/descarga y el modelo de
consumo WP2) ya habían sido portados en una sesión anterior (commits
`3efa12f7f` y `165633fea`, 2026-07-23) — acá se completan los detalles que
faltaban y se documentan junto con el resto.

## 1. Cambios requeridos en la data de entrada

Nada de lo portado rompe escenarios existentes: todo lo nuevo es opcional y cae
a un default que reproduce el comportamiento actual si no se agrega.

- **Hoja `Shifts` de `time_series.xlsx`** — columna opcional **`base_hour`**
  (decimal, ej. `8.5` = 08:30, `9` = 09:00). Basta con que **una sola celda**
  de esa columna tenga un valor numérico; el resto puede quedar vacío. Si la
  columna no existe (todos los escenarios actuales), se usa el default `8.5`
  — mismo comportamiento que antes de este port. **Importante para quien
  agregue esta columna**: si el escenario ya tiene `simple_time_series.xlsx` /
  `Shifts.npy` cacheados (`series.ini`), hay que borrar esos 3 archivos (o
  tocar el `.xlsx` original para invalidar el cache por mtime) después de
  agregar la columna en el Excel fuente, para que `Series` reconstruya el
  cache incluyendo `base_hour` — si no, se sigue leyendo la versión cacheada
  vieja sin esa columna.
- **Hoja `LHD` de `elmo_data.xlsx`** — columnas `charge_efficiency` /
  `discharge_efficiency` (p.u., ej. `0.95`). Ya son obligatorias y están
  pobladas en todos los escenarios actuales (confirmado en
  `Escenarios_Gx`, `Escenarios_DCH_taller`, etc.); si algún escenario nuevo
  las deja vacías, `OptParameters.build_parameters` falla al construir
  `eta_charge_i`/`eta_discharge_i`.
- **`--consumption_model wp2`** — requiere un JSON de consumos precalculados
  por nodo (`electric_routes_within_time.json` o `diesel_routes_within_time.json`,
  ruta relativa a `--data_folder` salvo que sea absoluta), con una entrada
  por **cada** nodo del escenario:
  ```json
  {
    "<node_name>": {
      "time_per_cycle_s": 149.52,
      "energy_per_cycle_kwh": 2.774172082398328
    }
  }
  ```
  Hay ejemplos reales en `data/Taller_DET_Agosto/.../electric_routes_within_time.json`
  (rama `carga_on_board`) pero **no existe ninguno para los escenarios
  multi-año de esta rama** — hay que generarlo si se quiere usar wp2 acá. Si
  se usa con LHD diesel, además hay que poblar `fuel_consumption` (BSFC,
  g/kWh) en la hoja `LHD`; si se deja en `-1` (o inválido), cae a 230 g/kWh.
- **`--autonomous_mode`** — no requiere datos nuevos, es un flag de CLI. Ver
  nota de la sección 3: hoy no tiene efecto observable en esta rama porque el
  esquema DET que usa está definido pero no activo.

## 2. Eficiencia de carga/descarga de batería on-board

Ya portado (commit `3efa12f7f`, 2026-07-23). `OptParameters.build_parameters`
agrega `eta_charge_i`/`eta_discharge_i` (hoja `LHD`, por ELHD) y
`ConstraintRules.battery_soc` los aplica: la carga que entra a la batería se
atenúa por `eta_charge_i`, y la energía de tracción que sale se amplifica por
`1/eta_discharge_i`. Ver `src/optimization/functions.py`.

## 3. Modelo de consumo WP2

Portado (commit `165633fea`, 2026-07-23) y completado en esta sesión. Agrega
`Timeseries._load_wp2_consumption_by_node` y el parámetro
`consumption_model`/`wp2_consumption_json` en `Timeseries.get_trips`, más
`resolve_wp2_json_path` y los flags `--consumption_model`/`--wp2_consumption_json`
en `setup.py`. Completado en esta sesión: en modo wp2 con LHD diesel, el BSFC
ahora se lee desde `elhd.get_fuel_consumption(elhd)` (con fallback a 230 g/kWh
si es inválido) en vez de usar siempre el valor fijo — igual que en
`carga_on_board`. Ver `src/time_series/timeseries.py`.

Cómo correrlo:
```powershell
python setup.py --data_folder data/mi_escenario/ --solver gurobi --consumption_model wp2
```

## 4. Redefinición de intervalos DET/DCH + `base_hour`

Portado en esta sesión. Cambios en `src/optimization/functions.py` (`OptSets`):

- `_get_pause_definitions_det()` reemplazado por el esquema nuevo: cada turno
  de 8h se subdivide en `between_shifts` (16 min, cambio de turno) + `meal`
  (56 min, colación) + `maintenance`/`road_clearing` (paradas forzadas), en
  vez del esquema viejo `shift_change`/`stop`.
- `_get_time_intervals_for_pause_type` y `time_intervals_peak_set` (ventana
  punta 18-22h) ahora anclan el horizonte a `self.time_series.base_hour` en
  vez del literal `9*60` hardcodeado.
- `build_sets` expone los sets nuevos `time_intervals_meal_det_set`,
  `time_intervals_maintenance_det_set`, `time_intervals_road_clearing_det_set`,
  `time_intervals_between_shifts_det_set` (se serializan automáticamente en
  `parameters.json` vía `Printer`, igual que cualquier otro `pyo.Set`).
- Se agregó el método `charge_only_meal_or_between_shifts_det` (versión DET de
  `charge_only_meal_or_shift_change`).

**Importante — esquema activo**: esta rama sigue corriendo con el esquema
**DCH** activo (`meal_g1_no_travel_group1/2`, `maintenance_stop_all`,
`maint_no_charge`, `charge_only_meal_or_shift_change`, todos registrados en
`ConstraintRules.build_all_constraints`). El esquema DET queda con el código
actualizado pero **sin activar** — `det_stop_all` y
`charge_only_meal_or_between_shifts_det` están definidos pero comentados en
`build_all_constraints`, igual que ya estaba `det_stop_all` antes de este
port. Para activar DET hay que, a mano, en `build_all_constraints`:
descomentar esas dos líneas y comentar el bloque DCH (las 5 líneas
mencionadas arriba). Decisión explícita del usuario: no cambiar el
comportamiento de las corridas actuales al portar este código.

También se corrigió `src/io/reader.py` (`Series.init['Shifts']` de 6 a 7
columnas identificadoras) para que el cacheo a `.npy`/`simple_time_series.xlsx`
no descarte la columna `base_hour` cuando se agregue.

## 5. Modo autónomo (`--autonomous_mode`)

Portado en esta sesión. `OptRules.__init__` acepta `autonomous_mode: bool = False`;
`OptSets.build_sets` decide `time_intervals_det_set` en función de ese flag
(en modo autónomo, `meal_det` queda fuera del set que fuerza "sin viajar",
permitiendo operar durante la colación). Propagado end-to-end:
`setup.py --autonomous_mode` → `OptimizationModel` → `OptModel` → `OptSets`.

**Nota**: como el esquema activo hoy es DCH (sección 4), este flag no tiene
efecto observable en las corridas actuales — solo importa el día que se
active el esquema DET a mano. No se portó la propagación a
`run_descomposicion.py`/`run_fase2_only.py` porque esos scripts no existen en
esta rama (descomposición por macrobloque está fuera del alcance de este port).

Cómo correrlo (una vez activado el esquema DET):
```powershell
python setup.py --data_folder data/mi_escenario/ --solver gurobi --autonomous_mode
```

## 6. `consumer.py` — energía neta en batería (post-eficiencia)

Agregado `_get_eta_charge_by_lhd(params_data)` (lee `eta_charge_i` desde
`parameters.json`) y un parámetro opcional `eta_charge_by_lhd` en
`calculate_daily_charged_energy`: sin ese argumento devuelve la energía
**bruta** (lado cargador, comportamiento original); con él, la energía
**neta** que efectivamente queda almacenada en la batería (bruta × eta_charge
por LHD). La tabla "OPERACIÓN DIARIA" ahora muestra ambas columnas más las
pérdidas de carga (bruta − neta) cuando el escenario trae `eta_charge_i`.

De paso se corrigió un bug preexistente en `calculate_daily_charged_energy`:
no recorría el nivel `y` (año) de `P.json`, así que en esta rama devolvía
siempre un diccionario vacío (energía diaria en 0) para cualquier corrida
multi-año. Ahora recorre `k → i → y → d → t`, con compatibilidad hacia atrás
para el formato legado sin eje de año.

**`calculate_grid_energy_cost`/`calculate_lhd_charge_cost` no se tocaron a
propósito**: `P_red` ya es la energía bruta comprada a la red (`power_balance`
define `P_red = sum(P)` sin pérdidas), y como `battery_boundary` cierra el SOC
de cada día, en el agregado ya se cumple `P_red = (demanda neta de tracción /
eta_discharge) / eta_charge` por construcción del modelo — dividir de nuevo
por la eficiencia ahí duplicaría la pérdida. Ver el docstring de
`calculate_grid_energy_cost` para el detalle.

**Limitación heredada, no introducida por este port**: al igual que
`calculate_daily_trips`/`calculate_cycles_from_y_ntrips` (con las que
comparte la tabla "OPERACIÓN DIARIA"), la energía diaria se sigue agrupando
solo por número de día, no por (año, día) — si dos años representativos
distintos caen en el mismo día-del-año (caso común: `Escenarios_Gx` usa un
solo día representativo por año), sus valores se suman en la misma fila.
Arreglar esto de raíz implicaría reescribir las tres funciones y quedó fuera
del alcance pedido.

## 7. `json_plotter.py` — sombreado DET nuevo (opcional, graficación)

Portado en esta sesión, con compatibilidad hacia atrás para carpetas de
resultados ya generadas:

- `Parameters` lee `base_hour` y los 4 sets DET nuevos
  (`meal_det`/`maintenance_det`/`road_clearing`/`between_shifts_det`) desde
  `parameters.json`, sin eliminar los atributos viejos (`shift_change`/
  `forced_detention`) que ya leía.
- `JSONPlotter.DET_SHADE_COLORS` (mismos colores que `carga_on_board`) y
  `self.start_hour = self.params.base_hour` (antes hardcodeado a `9.0`,
  solo dentro de `plot_charge_power_vs_price`).
- `plot_charge_power_vs_price`, modo `"DET"`: si el `parameters.json` trae los
  4 sets nuevos, sombrea con 4 colores (between_shifts/meal/road_clearing/
  maintenance) y actualiza la leyenda; si vienen vacíos (carpeta generada
  antes de este port), cae automáticamente al sombreado 2-color viejo — no se
  rompe la lectura de resultados históricos.
- Solo se tocó `start_hour` dentro de esta función (la relacionada al
  sombreado DET); el resto de los gráficos del archivo (~2000 líneas, fuera
  de alcance) sigue asumiendo 09:00 como antes.

## 8. Archivos modificados

- `src/optimization/functions.py`
- `src/optimization/opt_model.py`
- `src/optimization/__init__.py`
- `setup.py`
- `src/time_series/timeseries.py`
- `src/io/reader.py`
- `consumer.py`
- `json_plotter.py`
- `PORT_DESDE_CARGA_ON_BOARD.md` (este archivo)
