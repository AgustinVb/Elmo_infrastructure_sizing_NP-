# Escenario DET de vehículos autónomos — cambios implementados

Este documento resume los cambios hechos para soportar un escenario DET de
LHD autónomos, en el que durante la colación el equipo puede además operar
(viajar/extraer), no solo cargar o estar detenido.

## 1. Contexto / motivación

El esquema DET define, por turno de 8h, una ventana combinada de
"cambio de turno + colación" que antes se trataba como un solo bloque de
pausa. Se dividió en dos sub-ventanas con semántica distinta:

- **Cambio de turno (`between_shifts`)**: 16 min al inicio de cada turno.
  El LHD **solo puede cargar o estar detenido**, nunca operar. Esto aplica
  igual en ambos modos (normal y autónomo).
- **Colación (`meal`)**: 56 min restantes.
  - **Modo normal**: igual que antes, solo cargar o estar detenido.
  - **Modo autónomo**: además de cargar o estar detenido, el LHD puede
    **operar** (viajar/extraer), porque no depende de un operador humano
    que deba parar a comer.

Adicionalmente, `between_shifts` dejó de inferirse de los huecos entre
turnos de la hoja Excel `Shifts` (`Timeseries.get_intervals_between_shifts`)
y ahora se extrae directamente de la lista de pausas DET
(`_get_pause_definitions_det`), igual que `meal`, `maintenance` y
`road_clearing`.

## 2. Cambios en `src/optimization/functions.py`

### 2.1 Flag de modo (`OptRules.__init__`)

Se agregó el parámetro `autonomous_mode: bool = False` al constructor base,
guardado en `self.autonomous_mode`. Por defecto el comportamiento no cambia
(modo normal).

### 2.2 Nuevo set `time_intervals_between_shifts_det_set`

En `OptSets.build_sets`, se extrae `between_shifts` de la lista de pausas
DET con el mismo helper que ya se usaba para `meal`/`maintenance`/
`road_clearing`:

```python
det_between_shifts_intervals = self._get_time_intervals_for_pause_type(
    "between_shifts", pauses=det_pauses
)
...
model.time_intervals_between_shifts_det_set = pyo.Set(
    initialize=sorted(det_between_shifts_intervals)
)
```

El set legado `time_intervals_between_shifts_set` (basado en Excel) se dejó
intacto para no romper el esquema DCH (actualmente sin uso activo), pero
las restricciones DET activas ya no lo referencian.

### 2.3 `time_intervals_det_set` depende del modo

Este es el set que fuerza "sin viajar" (solo cargar o detenido) vía
`det_stop_all`. Antes incluía siempre `meal_det ∪ maintenance_det ∪
road_clearing_det`. Ahora:

```python
if self.autonomous_mode:
    model.time_intervals_det_set = pyo.Set(initialize=sorted(set(det_stop)))
    # det_stop = maintenance_det ∪ road_clearing_det (sin meal)
else:
    model.time_intervals_det_set = pyo.Set(
        initialize=sorted(set(det_meal_intervals) | set(det_stop))
    )
```

En modo autónomo, `meal_det` queda **fuera** de este set, así que durante
la colación se aplica la restricción general `state_unique_elhd` (permite
Z, Z_charge o Y libremente) en vez de la restricción `det_stop_all`
(que fuerza Z + Z_charge = 1, sin viajar).

`between_shifts` nunca estuvo en este set — tiene su propia restricción
dedicada (`between_shifts_elhd`), que no cambia con el modo.

### 2.4 Restricciones actualizadas para usar el set DET nuevo

- `between_shifts_elhd`: ahora se registra sobre
  `model.time_intervals_between_shifts_det_set` (antes usaba el set
  legado basado en Excel).
- `charge_only_meal_or_between_shifts_det`: la ventana de carga permitida
  (`meal_det ∪ road_clearing_det ∪ between_shifts_det`) ahora usa el nuevo
  set DET en vez del legado.

## 3. Hilo del flag `autonomous_mode` (para poder elegir el modo en runtime)

Se propagó el parámetro `autonomous_mode=False` a través de toda la cadena
de construcción del modelo:

- `OptModel.__init__` (`src/optimization/opt_model.py`): nuevo parámetro,
  pasado a `OptSets(mine_system, time_series, autonomous_mode=autonomous_mode)`.
- `OptimizationModel.__init__` (`src/optimization/__init__.py`): nuevo
  parámetro, pasado a `OptModel(...)`.
- `setup.py`: nuevo flag CLI `--autonomous_mode` (`store_true`), pasado a
  `OptimizationModel(...)`.
- `run_descomposicion.py`: mismo flag CLI, propagado a:
  - `run_macrobloque(...)` (modo no paralelo por día)
  - el *job tuple* de `solve_macrobloque_day` (workers de
    `--parallel_days`, vía `multiprocessing`)
  - `run_parallel_by_station_and_day` (a través de `make_job`, que ya
    cierra sobre `args`)

## 4. Cómo correrlo

```powershell
# Con run_descomposicion.py (descomposición por estación/día)
python run_descomposicion.py --data_folder data/.../mi_escenario/ --output_folder output/.../mi_escenario/ --days 1,32,60,91,121,152,182,213,244,274,305,335 --parallel_days --solver gurobi --consumption_model wp2 --autonomous_mode

# Con setup.py (modelo completo, sin descomponer)
python setup.py --data_folder data/.../mi_escenario/ --output_folder output/.../mi_escenario/ --solver gurobi --consumption_model wp2 --autonomous_mode
```

Omitir `--autonomous_mode` reproduce exactamente el comportamiento previo
(modo normal).

> Nota aparte (no relacionada a la autonomía): si el escenario trae
> `electric_routes_within_time.json`, hay que pasar también
> `--consumption_model wp2` — de lo contrario se usa el cálculo físico
> interno (wp1), que puede exigir bastante más energía de tracción que el
> valor precalculado y volver el modelo infactible por falta de capacidad
> de carga.

## 5. Resumen de comportamiento por ventana y modo

| Ventana | Duración/turno | Modo normal | Modo autónomo |
|---|---|---|---|
| `between_shifts` (cambio de turno) | 16 min | Cargar o detenido | Cargar o detenido (igual) |
| `meal` (colación) | 56 min | Cargar o detenido | Cargar, detenido u **operar** |
| `road_clearing` | — | Cargar o detenido | Cargar o detenido (igual) |
| `maintenance` | — | Detenido, sin cargar | Detenido, sin cargar (igual) |

Con los datos de referencia (`Carga_ob_8lhds_autonomo`, delta_t=8min,
180 intervalos/día): el cambio libera **21 intervalos/día** (168 min,
~2.8 h) que antes estaban forzados a "solo cargar o detenido" y ahora
quedan disponibles para operar — subiendo el total de intervalos
elegibles para operar de 97 (53.9%) a 118 (65.6%) sobre 180.

## 6. Extra: sombreado de "Between Shifts" en `json_plotter.py`

Se agregó (no estrictamente parte de la lógica de optimización, pero para
que los gráficos en modo DET reflejen la nueva ventana explícita):

- `Parameters.between_shifts_det`, leído desde
  `time_intervals_between_shifts_det_set` en `parameters.json`.
- Color propio en `DET_SHADE_COLORS["between_shifts"]` (azul, alpha 0.20).
- Ventana de fallback (`_load_special_intervals`, solo si el
  `parameters.json` no trae el set) con los horarios actuales de
  `between_shifts` (00:30-00:46, 08:30-08:46, 16:30-16:46).
- Sombreado y leyenda ("Between Shifts") agregados al gráfico
  `ChargePower_vs_price` en modo DET.

## 7. Archivos modificados

- `src/optimization/functions.py`
- `src/optimization/opt_model.py`
- `src/optimization/__init__.py`
- `setup.py`
- `run_descomposicion.py`
- `json_plotter.py`
