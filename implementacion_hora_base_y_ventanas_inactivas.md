# Hora base del horizonte y ventanas inactivas DET

Este documento resume dos cambios relacionados, hechos sobre `carga_on_board`
(escenario `data/Taller_DET_Agosto/Carga_ob/`), para poder replicarlos de
forma análoga en `battery_swapping`.

## 1. Problema original

`t=1` no representaba una hora real fija en todo el código: distintas partes
asumían distintas anclas:

- `_get_time_intervals_for_pause_type` (pausas DET/DCH) y `time_intervals_peak_set`
  (`src/optimization/functions.py`), `consumer.py` (`_peak_clock_interval_set`) y
  varios ticks/ventanas de `json_plotter.py` hardcodeaban `base_minutes = 9 * 60`
  (asumían que el horizonte arranca a las 09:00).
- `get_shift_start_interval` / `get_shift_end_interval` (`src/time_series/timeseries.py`),
  que alimentan `NodeAssignment`/`BatteryAssignment` y `time_intervals_between_shifts_set`,
  usan directamente `hour_start / delta_t + 1` sin ningún ancla ajustable — es decir,
  asumen que `hour_start=0` en la hoja `Shifts` corresponde a `t=1`.

En los datos reales, `hour_start=0` en `Shifts` **no** es medianoche: para el
esquema DET corresponde a las **08:30** reales (el turno 1 arranca ahí). Como
el pause-parser usaba 09:00 en vez de 08:30, las ventanas de colación/
mantención/despeje de vías (meal/maintenance/road_clearing) quedaban ~30min
corridas respecto al resto del modelo (asignación de nodos, turnos), lo cual
podía generar conflictos/infactibilidad al alinearlas correctamente.

## 2. Fix A — Parametrizar la hora base (`base_hour`)

En vez de hardcodear el ancla, se agrega una columna `base_hour` a la hoja
`Shifts` de `time_series.xlsx`, con la hora real (decimal) en que arranca el
horizonte (ej. `8.5` para las 08:30). **Basta con que un solo valor esté
lleno** en toda la columna (el resto puede quedar vacío); el código toma el
primer valor no nulo.

### Dato (Excel)

- Hoja `Shifts`, agregar columna `base_hour` como **séptima columna** (justo
  después de `duration`), valor `8.5` en al menos una fila.
- Para DCH, la hora real históricamente asumida es 09:00 → usar `base_hour=9`
  en el `Shifts` correspondiente si se preserva ese esquema.

### Código

**`src/io/reader.py`**
```python
self.init = dict(MarginalCost=3, Emissions=3, ExtractionGoal=3, Shifts=7, NodeAssignment=5, StationAssignment=3, GenProfiles=3)
```
(`Shifts` pasó de 6 a 7, para que la columna nueva sobreviva el recorte que
separa la hoja "simple" cacheada del bloque `.npy`.)

**`src/time_series/timeseries.py`**
- `sample_shifts()`: incluir `base_hour` en las columnas seleccionadas si
  existe en la hoja.
- Nuevo método:
```python
def get_base_hour(self) -> float:
    """Hora real (decimal) en que arranca el horizonte (t=1), leida de la
    columna 'base_hour' de la hoja Shifts (basta con que un solo valor
    este lleno en toda la columna; el resto puede quedar vacio).
    Si la hoja no trae esa columna, o esta vacia (escenarios viejos),
    usa 8.5 por compatibilidad con el default DET actual."""
    shifts = self.mapper['Shifts']
    if 'base_hour' in shifts.columns:
        values = pd.to_numeric(shifts['base_hour'], errors='coerce').dropna()
        if not values.empty:
            return float(values.iloc[0])
    return 8.5
```
- En `build_mappers()`, después de poblar `self.mapper['Shifts']`, agregar:
```python
self.base_hour = self.get_base_hour()
```

**`src/optimization/functions.py`**
- `_get_time_intervals_for_pause_type`: `base_minutes = int(round(self.time_series.base_hour * 60))`
  en vez del hardcode.
- `build_sets()` → `time_intervals_peak_set`: mismo reemplazo (`base_minutes`
  calculado igual, usado en la ventana de hora punta 18:00-22:00).
- `build_parameters()`: agregar `model.base_hour = pyo.Param(initialize=self.time_series.base_hour, mutable=True)`
  junto a `model.delta_t`, para que quede escrito automáticamente en
  `parameters.json` (mismo mecanismo genérico que ya vuelca cualquier `pyo.Param`).

**`consumer.py`**
- `_peak_clock_interval_set(delta_t, max_t, base_hour=8.5)`: agregar el
  parámetro `base_hour` (default 8.5 para runs viejos sin el dato) y usarlo en
  vez del hardcode `9*60`.
- En el call site, leer `base_hour = _as_float(params_data.get("base_hour", 8.5))`
  del `parameters.json` de cada subcarpeta, igual que ya se hace con `delta_t`.

**`json_plotter.py`**
- Clase `Parameters`: agregar `self.base_hour = 8.5` (default) y
  `self.base_hour = float(data.get("base_hour", 8.5))` en `_load()`.
- `JSONPlotter.__init__`: `self.start_hour = self.params.base_hour if self.params.ok else 8.5`.
- `_get_hourly_time_ticks(self, start_hour=None)` y
  `_build_intervals_from_clock_windows(self, windows, start_hour=None)`:
  si `start_hour is None`, resolver a `self.start_hour`.
- `_get_fixed_time_ticks`: generar las etiquetas dinámicamente a partir de
  `self.start_hour` en vez de una lista fija ("09:00","13:00",...).
- Todos los call sites DET (ventanas meal/road_clearing/maintenance del
  fallback, `peak_windows`, los `_get_hourly_time_ticks(...)` de los distintos
  plots) pasan `start_hour=self.start_hour` en vez del `9` hardcodeado.
- **El bloque DCH** (fallback en `_load_special_intervals`, ventanas
  `between_shifts`/`meal`/`maintenance` con `start_hour=9`) se dejó **sin
  tocar** a propósito — evaluar si corresponde parametrizarlo igual en
  `battery_swapping` si ese esquema sigue vivo ahí.

## 3. Fix B — Regla robusta para asignar pausas a intervalos

`_get_time_intervals_for_pause_type` marcaba un intervalo `t` como bloqueado
si había **cualquier solape**, por mínimo que fuera, con la ventana de pausa:

```python
s, e = (t - 1) * dt_minutes, t * dt_minutes
if max(s, a) < min(e, b):
    indices.add(t)
```

Esto **redondea siempre hacia arriba** en ambos extremos de cada ventana,
inflando el tiempo inactivo total (en este escenario, de ~11h16min literales a
~12h56min efectivos — el modelo trababa producción por más tiempo del que la
lista de pausas realmente especifica).

### Fix aplicado

Reemplazar el criterio de solape por un test de **punto medio** del intervalo
(redondeo al más cercano, error máximo de `dt/2` por borde, en vez de sesgo
sistemático hacia arriba):

```python
for t in range(1, max_t + 1):
    mid = (t - 1) * dt_minutes + dt_minutes / 2
    if a <= mid < b:
        indices.add(t)
```

Esta regla es **independiente del valor de `base_hour`** — no requiere que las
horas de la lista de pausas estén alineadas a la grilla de `delta_t` minutos
para dar un resultado correcto (a diferencia de un ajuste manual de los
horarios de pausa, que sólo funciona para un `base_hour`/`delta_t` específicos).

### Nota sobre el ajuste manual de -2min

Antes de implementar la regla robusta, se probó alinear a mano los 24 bordes
(inicio/fin) de la lista de pausas DET restándoles 2 minutos a cada uno, para
que calzaran exactos con la grilla de intervalos (dado `base_hour=8.5` y
`delta_t=8min`, los bordes originales eran múltiplos de 8 pero la grilla real
cae en `≡6 mod 8`). Ese ajuste sigue siendo válido y no hace daño, pero **ya
no es necesario** con la regla de punto medio — se puede usar la lista de
pausas con sus horarios "naturales" (sin el -2min) y el resultado será
igualmente correcto.

## 4. Qué revisar en `battery_swapping`

1. Confirmar si esa rama tiene su propia versión de
   `_get_pause_definitions_det` / `_get_time_intervals_for_pause_type` (puede
   haber divergido de `carga_on_board`) y aplicar el mismo cambio de regla de
   punto medio ahí.
2. Confirmar el valor real de `hour_start=0` en la hoja `Shifts` de los
   escenarios de swap (`Swap_max_production`, etc.) — no asumir que es 8:30
   solo porque en DET carga on-board lo es; puede ser otro valor.
3. Agregar la columna `base_hour` a los `time_series.xlsx` de swap con el
   valor que corresponda a esos escenarios.
4. Replicar los 5 cambios de código (reader.py, timeseries.py, functions.py,
   consumer.py, json_plotter.py) verificando primero si ya existen o si esa
   rama tiene una implementación distinta de estas mismas piezas (mine/battery
   swapping puede tener funciones equivalentes con otros nombres, ej. sets de
   pausas propios del esquema de intercambio de baterías).
5. Invalidar/regenerar los caches (`simple_time_series.xlsx`, `Shifts.npy`,
   `series.ini`) de cada escenario de swap tocado — esto es automático en
   cuanto se modifica el `time_series.xlsx` original (el mtime cambia y
   `Series` detecta que el cache quedó desactualizado).
