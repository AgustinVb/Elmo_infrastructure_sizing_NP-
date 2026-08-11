# Cambios portados desde `battery_swapping` — resumen e impacto en la data de entrada

Este documento resume los cambios portados/adaptados desde la rama `battery_swapping`
(HEAD `da6409d3d`, 2026-08-10) hacia `battery_swapping_multiaño` (HEAD `f495080c0`,
2026-07-23 antes de este port), análogo a `PORT_DESDE_CARGA_ON_BOARD.md` (port
`carga_on_board` → `carga_ob_multiaño`) pero usando `battery_swapping` como rama base,
tal como se pidió — **no** son los mismos 4 cambios: swapping tiene su propio historial
(descomposición por macrobloque, resultados de autonomía, esquema DET ya activado) que no
existe en `carga_on_board`, así que el alcance real se determinó comparando ambas ramas
directamente en vez de asumir paralelismo con el port anterior.

Dos de los cambios pedidos (eficiencia de carga/descarga y modelo de consumo WP2) ya
estaban portados desde el 2026-07-23 (commits `12b62a84e`/`f495080c0` en esta rama) y se
confirmó que siguen alineados con `battery_swapping` sin drift — no requirieron cambios.

## 1. Cambios requeridos en la data de entrada

Nada de lo portado rompe escenarios existentes: todo lo nuevo es opcional y cae a un
default que reproduce el comportamiento actual si no se agrega.

- **Hoja `Shifts` de `time_series.xlsx`** — columna opcional **`base_hour`** (decimal,
  ej. `8.5` = 08:30). Basta con que una sola celda tenga un valor numérico. Si la columna
  no existe (todos los escenarios de swap actuales, confirmado por muestreo), se usa el
  default `8.5`. **Igual que en el port anterior**: si el escenario ya tiene
  `simple_time_series.xlsx`/`Shifts.npy`/`series.ini` cacheados, hay que borrar esos 3
  archivos (o tocar el `.xlsx` fuente para invalidar el cache por mtime) después de agregar
  la columna, si no se sigue leyendo la versión cacheada vieja.
- **Hoja `LHD` de `elmo_data.xlsx`** — `charge_efficiency`/`discharge_efficiency`: ya
  obligatorias y pobladas en todos los escenarios de swap muestreados (`0.95` en los 6
  `elmo_data.xlsx` revisados). Sin cambios necesarios.
- **`--autonomous_mode`** — flag de CLI, no requiere datos nuevos. Hoy **no tiene efecto
  observable** porque el esquema activo sigue siendo DCH (ver sección 3) — igual que en el
  port anterior, solo importa el día que se active el esquema DET a mano.

## 2. Decisiones de diseño (confirmadas con el usuario antes de implementar)

1. **Esquema DCH/DET**: `battery_swapping` ya activó DET (DCH comentado) en algún punto
   después del commit que originó esta rama multi-año — un cambio de comportamiento real,
   no solo el port de mecánica `base_hour`. Se decidió **mantener DCH activo** en
   `battery_swapping_multiaño` (sin cambios de comportamiento en escenarios existentes) y
   portar el esquema DET corregido como **definido pero no registrado** en
   `build_all_constraints`, igual que hizo el port desde `carga_on_board`.
2. **Calibración de degradación** (`gamma_coef` para 142/320/530 kW): **fuera de alcance**.
   Ningún escenario real de swap tiene hoja `BatteryDegradation`; el único dato existente
   (`data/Escenarios_Gx/`, 160/320/640 kW) es la carpeta de prueba de generación renovable,
   no un escenario de costos de swap, y sus valores son una copia literal de la tabla de
   on-board. No se tocó `src/mine/degradation.py` ni se inventó una calibración para
   142/320/530 kW.
3. **`json_plotter.py`**: se portó **solo el sombreado DET**, no la reestructuración de
   funciones de gráficos de `battery_swapping` (`plot_battery_charging_power`,
   `plot_swaps_vs_price`, `plot_charging_batteries_vs_price`).

## 3. Intervalos DET/DCH con `base_hour`

- `Timeseries.get_base_hour()` (nuevo) + `self.base_hour` — lee la columna `base_hour` de
  `Shifts`, default `8.5`. `src/time_series/timeseries.py`.
- `OptSets._get_time_intervals_for_pause_type`: `base_minutes` ahora usa
  `self.time_series.base_hour` (antes hardcodeado a `9*60`), y el test de asignación de
  intervalo pasó de "solapa con la ventana de pausa" a "el punto medio del intervalo cae
  dentro de la ventana" — corrige un sesgo sistemático que inflaba el tiempo inactivo
  (bugfix real, documentado en `implementacion_hora_base_y_ventanas_inactivas.md` en
  `battery_swapping`). `src/optimization/functions.py`.
- `OptSets._get_pause_definitions_det`: reemplazado el esquema viejo
  (`shift_change`/`"stops"`) — que dejaba `time_intervals_det_set` prácticamente vacío por
  un `pause_type` mal escrito — por el esquema correcto
  (`between_shifts`/`meal`/`maintenance`/`road_clearing`).
- `OptSets.build_sets`: nuevos sets `time_intervals_meal_det_set`,
  `time_intervals_maintenance_det_set`, `time_intervals_road_clearing_det_set`,
  `time_intervals_between_shifts_det_set`, y `time_intervals_det_set` (con el condicional de
  `autonomous_mode`: excluye `meal_det` si está activo). Los sets viejos
  `time_intervals_shift_change_det_set`/`time_intervals_stops_set` (atados al esquema
  buggy) se eliminaron — no los usa nada más que el propio esquema viejo, que dejó de
  existir con este cambio.
- `ConstraintRules.det_stop_all`: cuerpo corregido (usa `time_intervals_det_set`, incluye el
  eje `y` que faltaba — bug preexistente inofensivo porque la restricción está inactiva) y
  `ConstraintRules.swap_only_meal_or_between_shifts_det` (nueva): permite swap durante
  `meal_det`/`road_clearing_det`/`between_shifts_det`, lo prohíbe en el resto (incluido
  `maintenance_det`, donde el LHD debe permanecer detenido sin swap). Ambas quedan
  **definidas pero comentadas** en `build_all_constraints` — DCH sigue siendo el esquema
  activo (decisión 1).
- `OptParameters.build_parameters`: nuevo `model.base_hour` (Param mutable). Se exporta
  automáticamente a `parameters.json` vía el loop genérico de `Param` que ya tenía
  `printer.py` — **no hizo falta tocar `printer.py`** (su exportador genérico de `Set`
  también recoge los 4 sets DET nuevos sin código adicional; se confirmó con un test que
  arma el modelo completo y exporta).

## 4. Modo autónomo

`OptRules.__init__` (clase base de la que heredan `OptSets`/`ConstraintRules`/etc.) agrega
`autonomous_mode=False`. En modo autónomo, `time_intervals_det_set` excluye `meal_det`, así
que el LHD puede operar (viajar/extraer) durante la colación DET; `between_shifts` sigue
restringido a swap-o-detenido en ambos modos. Propagado end-to-end: `setup.py
--autonomous_mode` → `OptimizationModel` → `OptModel` → `OptSets` (es el único de los seis
constructores que lo necesita, porque es el único que usa `self.autonomous_mode`).

**Nota**: como el esquema activo sigue siendo DCH (decisión 1), este flag no tiene efecto
observable hoy — solo importa el día que se active el esquema DET a mano. No se portó
`daily_target_override` (el otro parámetro nuevo de `battery_swapping`): su único consumidor
es el problema maestro de `run_macrobloques_decomposicion.py`, que no existe en esta rama y
está fuera del alcance de este port.

## 5. `consumer.py` — energía neta a la red (post-eficiencia)

A diferencia del port desde `carga_on_board` (que agregó un parámetro opcional a
`calculate_daily_charged_energy`), `battery_swapping` ya tenía su propia noción de "energía
neta" para swapping en `calculate_real_charged_energy_from_swaps` (energía real por evento,
a partir del nivel de batería `B` al llegar a la estación) — pero le faltaba la capa de
eficiencia. Se agregó:

- `eta_charge_map` (lee `eta_charge_i` de `parameters.json`, mismo patrón que `bmax_map`).
- `total_real_grid`/`event_real_grid = event_real / eta_charge_i`: estima la energía que
  habría que comprarle a la red para explicar la energía real que quedó en la batería
  (`event_real`, que ya es post-eficiencia).
- Nuevos campos en el `meta` devuelto: `real_grid_energy_kwh`, `gap_vs_sv_grid_kwh`; en cada
  evento de `event_details`: `real_event_grid_kwh`, `eta_charge`.
- Solo agrega claves nuevas al diccionario — los dos consumidores existentes en esta rama
  (`main()` y `calculate_total_costs`) solo leían claves preexistentes, así que no
  necesitaron cambios.
- `main()` ahora también **muestra** los dos campos nuevos en la tabla de reporte (filas
  "Energía real cargada a la red estimada" y "Brecha vs energía Sv (grid)"), para que sea un
  reporte visible y no solo un valor calculado y descartado.
- **`calculate_grid_energy_cost`/`calculate_lhd_charge_cost` no se tocaron a propósito**,
  igual que en el port anterior: siguen usando `P_red`/`Sv` brutos (sin dividir por
  eficiencia) para no duplicar la pérdida ya reflejada en el modelo.

Verificado con datos sintéticos (80 kWh reales a `eta_charge=0.8` → 100 kWh de red) y contra
`calculate_total_costs` de una carpeta de resultados real (no se rompe).

## 6. `json_plotter.py` — sombreado DET (decisión 3, alcance mínimo)

- `Parameters` lee `base_hour` y los 4 sets DET nuevos
  (`meal_det`/`maintenance_det`/`road_clearing`/`between_shifts_det`), sin tocar los
  atributos viejos que ya leía (`shift_change`/`forced_detention`, atados al esquema
  `mode="DET"` preexistente — ver más abajo).
- `JSONPlotter.DET_SHADE_COLORS` (mismos colores que `carga_on_board`/`battery_swapping`) y
  `self.start_hour = self.params.base_hour` (antes hardcodeado a `9.0` dentro de
  `plot_charge_power_vs_price`, que es el que usa esta rama — no existe
  `plot_battery_charging_power` acá, esa es la reestructuración de `battery_swapping` que
  quedó fuera de alcance).
- `plot_charge_power_vs_price`: si `parameters.json` trae al menos uno de los 4 sets DET no
  vacío, sombrea con los 4 colores nuevos; si no (carpeta generada antes de este port, o
  escenario sin `base_hour`), cae al comportamiento viejo exactamente como estaba (el propio
  `if self.mode == "DET": ... else: ...` preexistente, sin modificar).
- **No se tocó** `plot_lhd_soc_vs_price_and_states` (existe en ambas ramas; `battery_swapping`
  le agregó sombreado propio, pero mezclarlo acá implicaba fusionar más lógica divergente de
  la que justifica "solo sombreado DET" — queda como seguimiento). Tampoco se tocaron
  `plot_battery_degradation` (exclusivo de esta rama) ni `plot_swaps_vs_price`/
  `plot_charging_batteries_vs_price` (exclusivos de `battery_swapping`).

Verificado end-to-end: modelo completo → `Printer.write_parameters_json()` → `Parameters`
lee el JSON exportado → el sombreado DET nuevo efectivamente se activa; y por separado, un
`parameters.json` sin las claves nuevas cae al comportamiento viejo sin romperse.

## 7. Explícitamente fuera de alcance

- **Descomposición por macrobloque** (`run_descomposicion.py`,
  `run_macrobloques_decomposicion.py`, `batch_plot_taller.py`, `calcular_metricas.py`,
  `combine_macrobloques_for_plot.py`, `export_material_extraction_totals.py`,
  `opt_model.py`'s `_apply_fixed_infra`/`var_axis_order`, funciones de reporte de
  `consumer.py` como `analyze_macrobloques`/`analyze_single_folder`): no pedido, y
  `battery_swapping_multiaño` no tiene el concepto de "resolver por estación por separado".
- **Calibración de degradación para 142/320/530 kW**: decisión 2, sección 2.
- **Esquema `NodeAssignment`**: las dos ramas usan esquemas distintos para esa hoja
  (`battery_swapping`: referencia por nombre de turno, 5 columnas; esta rama: una columna
  por año, 3 columnas) — diferencia estructural no relacionada con este port, no se tocó.
- **`threads`/`warmstart_hard_only`/`fixed_infra`** (parámetros nuevos de `OptModel` en
  `battery_swapping`): ligados a descomposición por macrobloque o afinamiento de solver, no
  pedidos.

## 8. Verificación

- Los 8 archivos modificados parsean sin errores de sintaxis.
- Modelo Pyomo completo (sets/params/bounds/constraints/objective) construido sin resolver,
  sobre `data/Escenarios_DCH_costos_nuevos/Costo_fijo/Swap_fixed_3estaciones_320kW/`, en
  modo normal y con `autonomous_mode=True`: sin excepciones, `time_intervals_det_set` pasa
  de 77 a 56 intervalos al activar `autonomous_mode` (exactamente los 21 de `meal_det`
  excluidos), DCH sigue siendo el único esquema con restricciones activas registradas.
- 4 casos de `get_base_hour()` (columna poblada, ausente, presente-pero-vacía, valor
  distinto de 8.5) verificados de forma aislada.
- Export completo (`Printer.write_parameters_json`) → lectura (`json_plotter.Parameters`) →
  activación del sombreado DET, y el camino de compatibilidad hacia atrás (JSON sin las
  claves nuevas), verificados end-to-end.
- `calculate_real_charged_energy_from_swaps` verificado con datos sintéticos (aritmética
  exacta) y contra una carpeta de resultados real ya existente en el repo (sin romper
  `calculate_total_costs`).

## 9. Archivos modificados

- `src/optimization/functions.py`
- `src/optimization/opt_model.py`
- `src/optimization/__init__.py`
- `setup.py`
- `src/time_series/timeseries.py`
- `src/io/reader.py`
- `consumer.py`
- `json_plotter.py`
- `PORT_DESDE_BATTERY_SWAPPING.md` (este archivo)
