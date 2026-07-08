# Contexto: cambios en `carga_ob_multiaño` para portar a `battery_swapping_multiaño`

Rama origen: `carga_ob_multiaño` (carga on-board, multi-año).
Rama destino sugerida: `battery_swapping_multiaño`.

**Aviso**: las dos ramas ya divergieron bastante (`functions.py` difiere en ~1300
líneas entre ambas). Esto NO es un patch aplicable directamente — es la
descripción conceptual de cada cambio (qué problema resuelve, la fórmula, y el
código de referencia) para reimplementar la lógica análoga sobre la estructura
de variables/sets que ya tenga la rama de battery swapping.

---

## 1. Fix: día representativo inconsistente entre años (`setup.py`)

**Problema**: en la configuración "1 día representativo/año" (5 años, 5 días
totales), el año 4 usaba el día absoluto `1200` en vez de `1110`. Esto hacía
que el año 4 representara un día de abril (día-del-año 105) mientras años
1, 2, 3 y 5 representaban enero (día-del-año 15) — rompía la comparabilidad
estacional entre años (precios de energía / perfiles solares distintos).

```python
# antes (bug): year4 = día 1200 (día-del-año 105, "abril")
time_series = timeseries.Timeseries(series, [15, 380, 745, 1200, 1475], 8/60)
# después (fix): year4 = día 1110 (día-del-año 15, "enero", consistente)
time_series = timeseries.Timeseries(series, [15, 380, 745, 1110, 1475], 8/60)
```

Verificación: `get_year_of_day` en `src/time_series/timeseries.py` usa
`((day-1)//365)+1`, y el día-del-año real es `((day-1)%365)+1`. Al portar a
battery swapping, si esa rama también arma listas de días representativos a
mano, vale la pena aplicar el mismo chequeo (`día_del_año` debe ser igual para
todos los años si se quiere comparabilidad estacional).

---

## 2. Restricción: mínimo 2 intervalos consecutivos de asignación a extracción

**Contexto previo (ya existía, no es de esta sesión)**: el modelo ya tenía
`min_charge_duration` para forzar que, al iniciar una sesión de carga
(`StartCharge=1`), el LHD cargue al menos 2 intervalos seguidos en la misma
estación `k` ([functions.py:644-648](src/optimization/functions.py#L644-L648)):

```python
def charge_state(self, model, k, i, y, d, t):
    ...
    return model.Z_charge[k,i,y,d,t] - model.Z_charge[k,i,y,d,t-1] == model.StartCharge[...] - model.EndCharge[...]

def min_charge_duration(self, model, k, i, y, d, t):
    t_fin = self.time_series.get_time_intervals()[-1]
    if t == t_fin:
        return model.Z_charge[k,i,y,d,t] == 0
    return model.Z_charge[k,i,y,d,t] + model.Z_charge[k,i,y,d,t+1] >= 2 * model.StartCharge[k,i,y,d,t]
```

**Lo agregado en esta sesión**: la misma idea pero para el estado "asignado a
extracción" (`Y[i,j,y,d,t]`), agregado sobre **cualquier** nodo `j` (no exige
mantenerse en el mismo nodo, solo en el estado "extrayendo" vs. "no
extrayendo"). Decisión de diseño explícita del usuario: estado agregado, no
por nodo específico.

Nuevas variables ([functions.py:497-498](src/optimization/functions.py#L497-L498)):
```python
model.StartAssign = pyo.Var(model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
model.EndAssign   = pyo.Var(model.elhd_set, model.years, model.days, model.time_intervals_set, domain=pyo.Binary)
```

Nuevas restricciones ([functions.py:650-678](src/optimization/functions.py#L650-L678)):
```python
def assign_state(self, model, i, y, d, t):
    nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
    if not nodes:
        return pyo.Constraint.Skip
    assign_sum = sum(model.Y[i,j,y,d,t] for j in nodes)
    t0 = self.time_series.get_time_intervals()[0]
    if t > t0:
        nodes_prev = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t-1, i), [])
        assign_sum_prev = sum(model.Y[i,j,y,d,t-1] for j in nodes_prev)
        return assign_sum - assign_sum_prev == model.StartAssign[i,y,d,t] - model.EndAssign[i,y,d,t]
    else:
        return assign_sum == model.StartAssign[i,y,d,t] - model.EndAssign[i,y,d,t]

def min_assign_duration(self, model, i, y, d, t):
    nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])
    if not nodes:
        return pyo.Constraint.Skip
    t_fin = self.time_series.get_time_intervals()[-1]
    assign_sum_t = sum(model.Y[i,j,y,d,t] for j in nodes)
    if t == t_fin:
        return assign_sum_t == 0
    nodes_next = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t+1, i), [])
    assign_sum_next = sum(model.Y[i,j,y,d,t+1] for j in nodes_next)
    return assign_sum_t + assign_sum_next >= 2 * model.StartAssign[i,y,d,t]
```
Registro en `build_all_constraints` ([functions.py:843-844](src/optimization/functions.py#L843-L844)).

**Notas de diseño importantes para portar**:
- En el último intervalo del día (`t_fin`) se fuerza el estado a 0 (igual que
  `min_charge_duration`), para no dejar sesiones "colgando" al borde del día
  representativo.
- No fue necesario excluir explícitamente los intervalos de pausa (colación,
  mantención, cambio de turno): como esos intervalos ya fuerzan `Y=0` por
  otras restricciones existentes, la restricción nueva simplemente le impide
  al optimizador *iniciar* una asignación justo antes de una pausa (no puede
  completar los 2 intervalos) — no genera infactibilidad global, el
  optimizador usa otro intervalo disponible.
- **Para battery swapping**: si ahí "asignación a extracción" usa la misma
  variable `Y[i,j,y,d,t]` (o equivalente), el patrón se traslada igual. Si en
  cambio el estado de "en ruta/extrayendo" está modelado distinto (por
  ejemplo con una variable de estado explícita en vez de derivarla de `Y`),
  hay que adaptar `assign_state`/`min_assign_duration` a esa variable.

---

## 3. Restricción: la carga on-board solo puede ocurrir en colación o cambio de turno

**Pedido**: que `Z_charge` (carga on-board) solo pueda activarse durante
ventanas de colación o cambio de turno — no en cualquier momento del día.

```python
def charge_only_meal_or_shift_change(self, model, k, i, y, d, t):
    if t in model.time_intervals_meal_set or t in model.time_intervals_between_shifts_set:
        return pyo.Constraint.Skip
    return model.Z_charge[k,i,y,d,t] == 0
```
Registrada sobre `model.ZCHARGE_DAYS_TIME_INDEX`
([functions.py:770-773](src/optimization/functions.py#L770-L773),
registro en [functions.py:881](src/optimization/functions.py#L881)).

**Detalle importante encontrado durante la implementación** (relevante si
battery swapping tiene una lógica de pausas parecida): existen DOS sets que
suenan a "cambio de turno" pero **no son intercambiables**:
- `model.time_intervals_shift_change_set`: se arma con
  `_get_time_intervals_for_pause_type("shift_change")` usando por defecto la
  lista de pausas **DCH** (`_get_pause_definitions()`), que **no define**
  ningún tramo tipo `"shift_change"` (esa etiqueta solo existe en
  `_get_pause_definitions_det()`, modo DET, actualmente deshabilitado). Este
  set queda **vacío** en la configuración activa.
- `model.time_intervals_between_shifts_set`: se calcula a partir de los
  turnos reales (`get_intervals_between_shifts()` en
  `src/time_series/timeseries.py:295-302` — intervalos donde ningún turno
  está activo). Este es el que efectivamente representa "cambio de turno" en
  DCH, y ya se usaba en `between_shifts_elhd` para permitir carga (no
  extracción) en esos huecos.

Si battery swapping reutiliza estos mismos sets, ojo con no repetir el error
de usar el set vacío.

Si se porta esta restricción a swapping, probablemente no aplique igual (el
swap de batería no es un evento de "carga" con ventana de tiempo — es más
bien un evento puntual de intercambio en una estación). Revisar si la
restricción análoga ahí sería "el swap solo puede ocurrir en colación/cambio
de turno" en vez de "la carga".

---

## 4. Fix: costo de reemplazo de batería no escalaba por tamaño de flota

**Problema**: `c_bat_replace` es el costo (total, ver punto 5) de reemplazar
**una** batería (un LHD), pero `battery_replace_cost` lo aplicaba una sola vez
sin multiplicar por la cantidad de LHD eléctricos — subestimaba el costo real
de reemplazar la flota completa.

```python
# src/optimization/functions.py:950-961
def battery_replace_cost(self, model):
    if self.mine_system.battery_degradation is None:
        return 0
    n_elhd = len(model.elhd_set)
    return sum(
        n_elhd * model.R[y] * model.c_bat_replace * self._discount_factor(model, y)
        for y in model.years
    )
```

Mismo fix aplicado en el reporte post-solve `consumer.py` (función
`calculate_battery_degradation_metrics`, ~línea 1463-1483), que leía
`c_bat_replace` desde `parameters.json` y tenía el mismo problema:
```python
n_elhd = len(params_data.get("elhd_set", []) or [])
...
cost_y = n_elhd * c_bat_replace * disc if replaced else 0.0
```
`elhd_set` está disponible en `parameters.json` porque `Printer.write_parameters_json`
(`src/io/printer.py`) serializa **todos** los `Set` activos del modelo, no
solo los `Param`.

**Para battery swapping**: si ahí la "unidad de reemplazo" no es 1 batería
por LHD sino, por ejemplo, un pool compartido de baterías en estaciones de
swap, el factor de escala correcto NO sería `n_elhd` — habría que usar la
cantidad de baterías del pool (que podría ser distinta al número de LHD).
Vale la pena pensar esto desde el diseño, no asumir la misma relación 1:1.

---

## 5. Aclaración de dato: `c_bat_replace` es costo TOTAL, no anualizado

Se corrigieron docstrings inconsistentes
(`src/mine/degradation.py:26-29`, `functions.py:950-955`, `consumer.py:1465-1468`)
para reflejar que `c_bat_replace` debe ser el **costo total** de reemplazar
una batería (USD), no un valor ya anualizado.

**Por qué (razonamiento a preservar al portar)**: las demás inversiones
(estaciones, cargadores, generadores, BESS) se deciden **una sola vez al
inicio** y valen para todo el horizonte, por eso su costo SÍ se anualiza y se
multiplica por `annuity_factor_expr` (NPV de una anualidad recurrente). El
reemplazo de batería en cambio es un evento puntual cuyo **momento es una
decisión del optimizador** (`R[y]`), así que debe cobrarse como costo total
descontado solo al año en que ocurre — anualizarlo mezclaría dos lógicas de
financiamiento distintas (compra al contado vs. arriendo) y subestimaría el
costo si se aplicara una sola cuota anual en vez del total.

Esta misma distinción aplica a **cualquier** costo de reemplazo/reposición
puntual que se agregue en battery swapping (ej. reemplazo de baterías del
pool): usar costo total × variable binaria de reemplazo × factor de
descuento del año, no anualidad.

---

## 6. Modelo de degradación de batería (fade lineal) — repaso para portar

Cadena de variables (fleet-wide, una sola "batería representativa" para toda
la flota; exige que todos los ELHD compartan `e_max`):

```python
# EFC (equivalent full cycles) del año y, normalizado por batería:
N_ciclos[y] == energy_repr_day * scaling_factor_op_cost / (n_elhd * b_max_fleet)

# Acumulado desde el último reemplazo (reset si R[y]=1, vía linealización con W[y]):
CumEFC[y] == N_ciclos[y]                      # primer año
CumEFC[y] == W[y] + N_ciclos[y]               # años siguientes
# W[y] = (1-R[y]) * CumEFC[y-1], linealizado con big-M (cum_efc_max):
W[y] <= cum_efc_max * (1 - R[y])
W[y] <= CumEFC[y-1]
W[y] >= CumEFC[y-1] - cum_efc_max * R[y]

# Fade lineal de capacidad:
b_bar[y] == b_max_fleet - gamma_coef * CumEFC[y]
```
Código: `src/optimization/functions.py:795-826` (constraints),
`:497-543` (variables), `:422-441` (parámetros desde hoja `BatteryDegradation`).

`b_bar[y]` reemplaza a la capacidad fija en `battery_lower`/`battery_upper`
— la ventana de SoC operable se encoge con el desgaste, generando el
trade-off económico real entre seguir operando degradado vs. pagar
`c_bat_replace` y resetear.

### Calibración de `gamma_coef`

Fórmula (para que la batería llegue justo al piso `min_capacity_fraction` a
los `N_ciclos` deseados):
```
gamma_coef = b_max_fleet × (1 − min_capacity_fraction) / N_ciclos
```

Con `b_max_fleet = 353 kWh`, `min_capacity_fraction = 0.8` (valores del
escenario `Escenarios_Gx` / `GX_pruebas`), y asumiendo que la vida útil en
ciclos es **inversamente proporcional a la potencia del cargador**
(`N_ciclos(P) = N_ciclos_base × P_base / P`, con base 320 kW → 3000 ciclos,
valor final acordado con el usuario tras iterar desde un valor inicial de
2000):

| Potencia cargador | N_ciclos asumidos | gamma_coef |
|---|---|---|
| 160 kW | 6000 | 0.011767 |
| 200 kW | 4800 | 0.014708 |
| 320 kW | 3000 | 0.023533 |
| 640 kW | 1500 | 0.047067 |
| 1000 kW | 960 | 0.073542 |

**Importante**: esta relación inversa potencia↔ciclos es un supuesto de
calibración manual (no viene de un modelo físico de degradación por C-rate
dentro del código) — se definió conversando con el usuario, no está
hardcodeada en ningún archivo. Si se porta a battery swapping y ahí también
hay baterías con fade por ciclos, esta misma fórmula y tabla de referencia
aplican (la potencia del cargador seguiría sin entrar directamente en
`gamma_coef` — solo la energía ciclada importa para el conteo de EFC; la
potencia solo se usó acá como criterio externo de calibración, asumiendo que
cargar más rápido acelera el desgaste).

---

## 7. Pendiente: generar los documentos Excel análogos para la data de entrada

Comparé qué hojas lee cada rama (`model[...]` desde `elmo_data.xlsx` vía
`Reader`, `series[...]` desde `time_series.xlsx` vía `Series`) usando
`git show battery_swapping_multiaño:...`. Resultado:

**Hojas de `elmo_data.xlsx` — iguales en ambas ramas** (`src/mine/__init__.py`):
`Batteries`, `LHD`, `extraction_nodes`, `stations`, `Generators`, `Storage`,
`chargers`.

**Hojas de `time_series.xlsx` — iguales en ambas ramas** (`src/time_series/timeseries.py`):
`BatteryAssignment`, `Emissions`, `ExtractionGoal`, `GenProfiles`,
`MarginalCost`, `Misc`, `NodeAssignment`, `Shifts`, `StationAssignment`.

**La única que falta por completo en `battery_swapping_multiaño`: `BatteryDegradation`.**
Esa rama no tiene `src/mine/degradation.py`, no lee la hoja `'BatteryDegradation'`
en `src/mine/__init__.py`, y ningún `elmo_data.xlsx` de sus escenarios trae esa
hoja. Todo el subsistema de degradación (secciones 4-6 de este documento) hay
que construirlo ahí desde cero: el módulo `degradation.py`, el wrapper en
`mine/__init__.py`, y **agregar la hoja `BatteryDegradation` a cada
`elmo_data.xlsx`** de los escenarios de swapping que la necesiten, con
columnas `id, gamma_coef, c_bat_replace, min_capacity_fraction, discount_rate`
(mismo esquema que `src/mine/degradation.py:4-9` en esta rama).

**Escenarios de swapping ya existentes que necesitarían esta hoja** (`git ls-tree
battery_swapping_multiaño -- data`), y que usan potencias de estación
**distintas** a las calibradas en la sección 6 (142/320/530 kW, no
160/200/320/640/1000 kW — son estaciones de swap, no cargadores on-board, así
que probablemente cargan la batería fuera del vehículo a otro C-rate):
- `Escenarios_DCH_taller/Costo_fijo/Swap_fixed_3estaciones_{142,320,530}kW`
- `Escenarios_DCH_taller/Costo_variable/Swap_variable_3estaciones_{142,320,530}kW`
- `Escenarios_DCH_costos_nuevos/Costo_fijo/Swap_fixed_3estaciones_{142,320,530}kW`
- `Escenarios_DCH_costos_nuevos/Costo_variable/Swap_variable_3estaciones_{142,320,530}kW`
- `Escenarios_DET_taller/Swap_costo_fijo_max_mineral` (y variantes)

**Ojo antes de reusar la tabla de la sección 6 tal cual**: esa calibración
(320 kW → 3000 ciclos, proporción inversa a la potencia) se pensó para carga
on-board. En swapping la batería se carga fuera del vehículo, normalmente a
mayor C-rate y con mejor gestión térmica (o peor, según el diseño de la
estación) — la relación potencia↔ciclos de vida podría no ser la misma. Antes
de generar los Excel, conviene:
1. Definir `b_max` (capacidad nominal) de la batería de swap para cada tier
   de potencia (142/320/530 kW) — no asumir que es la misma de 353 kWh usada
   en `Escenarios_Gx`.
2. Decidir de nuevo la vida útil en ciclos para cada tier (con el usuario o
   ficha técnica del fabricante), en vez de extrapolar directamente la
   relación `N_ciclos = 3000×320/P` usada para on-board.
3. Recalcular `gamma_coef` con la misma fórmula de la sección 6:
   `gamma_coef = b_max × (1 − min_capacity_fraction) / N_ciclos`.

---

## Resumen de archivos tocados esta sesión
- `setup.py` — fix día representativo año 4.
- `src/optimization/functions.py` — `StartAssign`/`EndAssign`, `assign_state`,
  `min_assign_duration`, `charge_only_meal_or_shift_change`,
  `battery_replace_cost` (fix `n_elhd`), docstring `c_bat_replace`.
- `src/mine/degradation.py` — docstring `get_c_bat_replace`.
- `consumer.py` — `calculate_battery_degradation_metrics` (fix `n_elhd`).
- Pendiente por parte del usuario: actualizar columna `c_bat_replace` en el
  Excel (`BatteryDegradation`) de cada escenario a valor **total**, y
  `gamma_coef` según la tabla de calibración de la sección 6.
- Pendiente (sección 7): generar la hoja `BatteryDegradation` en los
  `elmo_data.xlsx` de los escenarios de `battery_swapping_multiaño` (no
  existe ninguna todavía en esa rama), más el módulo `src/mine/degradation.py`
  y su wrapper en `src/mine/__init__.py` — recalibrando `gamma_coef` para las
  potencias propias de swapping (142/320/530 kW), no reusando directamente la
  tabla de on-board (160/200/320/640/1000 kW).
