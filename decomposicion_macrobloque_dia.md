# Descomposición por macrobloque y por día

Contexto de la implementación que resuelve el problema completo (todas las
estaciones, todos los días significativos) como una colección de subproblemas
más chicos en vez de un único MIP monolítico. Pensado para retomar el tema en
una conversación futura sin tener que releer el código desde cero.

Esta es la implementación para el modelo de **carga on-board** (rama
`carga_on_board`), portada desde la implementación original en `battery_swapping`
(ver `git show battery_swapping:decomposicion_macrobloque_dia.md` para la
versión swap). Diferencia clave: como no hay intercambio de baterías, la
única infraestructura a fijar entre fases es `N_chargers[k]`/`X[k]` — no
existen `N_bays` ni `N_batteries`.

## Por qué existe

El modelo completo (`OptimizationModel` sobre todas las estaciones/LHD y
todos los días a la vez) escala mal: el tamaño del MIP crece con
`estaciones x días x intervalos`, y para escenarios de varios meses con 3
estaciones se vuelve intratable en tiempos razonables. La descomposición
parte el problema en piezas independientes que se resuelven en paralelo y
después se combinan, a costa de perder el acople exacto entre estaciones/días
que sí existe en el modelo completo (ver sección "Qué se pierde al
descomponer").

## Archivos involucrados

- [run_macrobloques_decomposicion.py](../run_macrobloques_decomposicion.py) — arma y resuelve los subproblemas.
- [combine_macrobloques_for_plot.py](../combine_macrobloques_for_plot.py) — combina los JSON de salida de varios subproblemas y genera gráficos.
- [consumer.py](../consumer.py) (función `analyze_macrobloques` y soporte alrededor de la línea 963) — calcula costos/métricas agregados a partir de carpetas desacopladas.
- [src/optimization/opt_model.py](../src/optimization/opt_model.py) — soporta `fixed_infra` (fija `N_chargers`/`X`) y recibe `daily_target_override`.
- [src/optimization/functions.py](../src/optimization/functions.py) (`daily_production`) — usa `daily_target_override` en vez de `sum(m_j)` cuando está presente.

Nota sobre `N_chargers`: antes de esta implementación existía una restricción
hardcodeada (`fixed_n_chargers`) que forzaba `N_chargers[k] == 1` para
`station_1/2/3` en **todas** las corridas, no solo en la descomposición. Se
eliminó al introducir `fixed_infra` para que `N_chargers` vuelva a ser una
variable de sizing real en corridas normales (decisión explícita del
usuario — cambia resultados respecto a corridas anteriores a esta
implementación). Ahora solo se fija cuando la Fase 2 de la descomposición lo
pide explícitamente.

## Dos niveles de descomposición

### 1) Por macrobloque (estación de carga / grupo de LHD)

Cada estación tiene su propio conjunto de LHD y nodos de extracción
(hoja `StationAssignment` / `NodeAssignment`). `run_macrobloques_decomposicion.py`
no toca los Excel originales: lee `elmo_data.xlsx` + `time_series.xlsx` **una
sola vez** y arma un `FilteredReader` en memoria por estación (filtra `LHD`,
`stations`, `extraction_nodes`; `chargers` y `discharge_nodes` se comparten
completos). Cada estación se resuelve como un MIP independiente.

### 2) Por día (`--parallel_days`, opcional, requiere `--days` con 2+ días)

Cada día significativo se modela como un ciclo cerrado de batería
(`B[i,d,0] == B[i,d,tf]`), sin continuidad de SOC entre días, así que los días
ya son independientes entre sí en el modelo completo, **salvo** por:

- La infraestructura (`N_chargers`, `X`): se decide una sola vez para todo
  el horizonte, no por día.
- `P_pot` / costo de potencia pico: acopla a través del máximo entre
  días/intervalos, no es per-día.

Por eso esta descomposición usa **dos sub-fases + agregación**:

1. **Fase 1 (infra libre):** cada par `(estación, día)` se resuelve por
   separado y en paralelo (`ProcessPoolExecutor`), con la infraestructura
   libre. Carpeta `<output>/<estacion>_d<dia>_stage1/`.
2. **Agregación:** para cada estación, se toma el **máximo** de
   `N_chargers/X` entre todos sus días (la infraestructura tiene que servir
   al día más exigente).
3. **Fase 2 (infra fija):** cada par `(estación, día)` se vuelve a resolver
   en paralelo, esta vez con `fixed_infra` aplicado (`_apply_fixed_infra` en
   `opt_model.py` hace `model.X[k].fix(...)`, etc.). Carpeta final
   `<output>/<estacion>_d<dia>/`.

`solve_macrobloque_day` es la función worker (debe ser top-level para ser
picklable con multiprocessing); cada worker vuelve a leer el Excel desde
disco porque `Reader`/`Series` no son picklables y releer es barato frente al
tiempo de Gurobi. Los hilos de Gurobi se reparten entre workers
(`threads_per_worker = cpus // n_workers`) para no sobre-suscribir la
máquina.

Cuando un día se resuelve solo, `Timeseries.scaling_factor_op_cost` (que por
defecto asume `365/len(days)`, es decir que ese único día representa todo el
año) se sobreescribe a `365 / total_n_days` para que siga representando
correctamente "1 de N días significativos del año".

## Fase 0 (problema maestro): reparto del target de producción

Sin acople, cada macrobloque sólo está obligado a producir la suma de los
`m_j` de sus **propios** nodos (`daily_production` por defecto). Eso no
reproduce la compensación que existe en el modelo completo, donde una
estación puede producir de más y otra de menos mientras se cumpla el target
**global** del día.

`compute_master_daily_targets` calcula, para cada estación y día, el rango
`[Cap_min, Cap_max]` de producción alcanzable por esa estación sola, usando
la misma fórmula floor/ceil que `ConstraintRules.production()` en
`functions.py` (exacta, no estimada, sale directo de los datos sin resolver
ningún MIP). Luego reparte el target global `D_d` con un water-filling de un
solo paso: cada estación arranca en su `Cap_min` y recibe, proporcional a su
holgura (`Cap_max - Cap_min`), lo que falte para alcanzar `D_d`. El resultado
(`daily_target_override`) se pasa a `ConstraintRules` y reemplaza la suma de
`m_j` en `daily_production` (functions.py:746-749). Si ni la suma de
`Cap_max` entre estaciones alcanza `D_d`, se imprime una advertencia: el caso
es infactible también en el modelo completo, no es un artefacto de la
descomposición.

## Combinación de resultados (`combine_macrobloques_for_plot.py`)

Combina los JSON de salida de varios macrobloques en una sola carpeta para
graficarlos juntos con `JSONPlotter`. Reglas:

- `parameters.json`: merge "primero gana" (los parámetros físicos
  compartidos como `delta_t`, `p_peak`, `costo_electricidad` no se suman; los
  indexados por estación no colisionan).
- Cualquier otra variable: merge con **suma** en colisiones de hoja
  (`P_red` es la que realmente colisiona entre estaciones del mismo
  día/intervalo, que es justo la demanda de red que se quiere sumar).

Si las subcarpetas siguen el patrón `<estación>_d<día>` (salida de
`--parallel_days`), primero se agrupan por día y se combina cada grupo por
separado, en `<output>/combined/day_<d>/` — sumar `P_red` entre carpetas de
**días distintos** no tiene sentido físico (no son simultáneos).

## Análisis de costos (`consumer.py`)

`consumer.py` detecta automáticamente una carpeta desacoplada
(`find_macrobloque_subfolders`: si `root` no tiene `parameters.json` directo
pero sus subcarpetas sí, excluyendo `combined` y `*_stage1`) y delega en
`analyze_macrobloques` en vez de `analyze_single_folder`.

Puntos no triviales de esa agregación:

- **Costo de potencia pico:** cada macrobloque por separado suele subestimar
  `power_cost` porque sólo ve su propia demanda aislada y puede evitar cargar
  en horario punta. `calculate_combined_peak_power_cost` recalcula el costo
  "ex post" sumando `P_red` de todos los macrobloques en cada
  `(día, intervalo)`, replicando la lógica de `power_cost_peak_limit` en
  `functions.py` (temporada de punta `91<=día<=244`, ventana horaria
  18:00-22:00 con horizonte operativo arrancando a las 09:00). El costo total
  corregido es `naive_total - naive_peak + combined_peak_cost`.
- **Deduplicación de inversión (CAPEX) cuando hay descomposición por día:**
  cada `(estación, día)` final re-incluye el costo de inversión completo (es
  CAPEX, no depende del día) porque la Fase 2 resuelve cada día con la misma
  infraestructura fija. Sumarlo por cada día lo infla N veces (N = días por
  estación). `analyze_macrobloques` agrupa por estación
  (`group_by_station`, detecta el patrón `<estación>_d<día>`) y cuenta el
  CAPEX **una sola vez por estación**, mientras que el OPEX
  (`grid_energy_cost`, `penalty_cost`, `gen_op_cost`, `bess_op_cost`) sí se
  suma por cada día.
- **Potencia pico de carga** (`calculate_peak_charging_power`, distinta del
  costo de potencia pico de red): combina `P.json` de todas las estaciones
  para el mismo `(d,t)` antes de buscar el máximo, porque las estaciones
  cargan en paralelo. `P[k,i,d,t]` ya está en kW (potencia de carga directa
  del LHD), a diferencia del swap donde había que multiplicar `Sv` (baterías
  cargando) por `p_charger`.

CLI: `python consumer.py <carpeta_raiz> [--summary_only]`. Detecta solo
automáticamente si está parado sobre una carpeta de macrobloques o una
corrida normal.

## Cómo correrlo

```bash
# Solo por estación (3 macrobloques, todos los días en un MIP cada uno)
python run_macrobloques_decomposicion.py \
  --data_folder data/Escenarios_DCH_costos_nuevos/Costo_fijo/Carga_on_board_fixed_3estaciones_P320kW/ \
  --series time_series.xlsx --days 1,32,60 --output_folder output/MB_test/

# Por estación Y por día, en paralelo (Fase1 -> agregación -> Fase2)
python run_macrobloques_decomposicion.py \
  --data_folder data/.../Carga_on_board_fixed_3estaciones_P320kW/ \
  --days 1,32,60,91,...  --parallel_days --n_workers 6 \
  --output_folder output/MB_test/

# Combinar y graficar
python combine_macrobloques_for_plot.py --root_dir output/MB_test/ --mode DCH

# Costos/métricas agregados
python consumer.py output/MB_test/ --summary_only
```

## Qué se pierde al descomponer (limitaciones conocidas)

- El acople de potencia pico entre estaciones/días sólo se recupera *ex
  post* en `consumer.py`, no dentro de la optimización: cada subproblema
  optimiza como si no compartiera demanda de red con los demás, así que el
  óptimo combinado no es necesariamente el óptimo global del problema
  completo (es una heurística de descomposición, no una relajación exacta).
- El reparto de target del problema maestro (Fase 0) es un water-filling de
  un solo paso sobre cotas exactas, no una iteración hasta convergencia; no
  reproduce exactamente la compensación que haría el solver del modelo
  completo, sólo se acerca a ella.
- `P_red` no se debe sumar entre carpetas de días distintos (no son
  simultáneos); sólo el costo total escalado es correcto de sumar entre
  días. `combine_macrobloques_for_plot.py` y `consumer.py` ya respetan esto,
  pero cualquier análisis manual sobre los JSON crudos debe tenerlo en
  cuenta.
- **Generación renovable / BESS (`Generators`/`Storage`) no se propagan a los
  macrobloques.** `build_macrobloque_mine` arma el `FilteredReader` solo con
  `LHD`/`stations`/`extraction_nodes`/`chargers`/`discharge_nodes`; como
  `mine.Mine.__init__` activa `self.generators`/`self.storage` solo si esas
  hojas están en el container, en cualquier macrobloque quedan
  silenciosamente desactivadas (`None`), aunque el escenario completo sí las
  tenga. Esto **no es trivial de resolver con `fixed_infra`**: a diferencia
  de `N_chargers`/`X` (que son por estación), `P_gen`/`P_bat`/`G_g`/`H_h`
  son recursos **compartidos por todo el sitio** — `power_balance` suma la
  demanda de TODAS las estaciones contra un único `P_red`/`P_gen`/`P_bat`
  (ver `power_balance` en `functions.py`, indexado solo por `(d,t)`, no por
  estación). Decomponer generación/BESS por estación requeriría un diseño
  aparte (ej. sizing compartido vía Fase 1/2 + dispatch corregido ex-post en
  `consumer.py`, similar a `calculate_combined_peak_power_cost`, pero sin una
  forma limpia de recalcular el dispatch óptimo sin volver a resolver). Por
  ahora, **usar este script solo con escenarios sin `Generators`/`Storage`**
  (los 3 escenarios `Carga_on_board_fixed/variable_3estaciones_*` no las
  tienen; `Escenarios_Gx` sí, pero es de una sola estación, no se ha probado
  la combinación).

## Estado de verificación

### Smoke test inicial (1 día, timelimit corto)

Sobre `data/Escenarios_DCH_costos_nuevos/Costo_fijo/Carga_on_board_fixed_3estaciones_P320kW/`:
- Fase 0 (`compute_master_daily_targets`) + filtrado por estación
  (`build_macrobloque_mine`) — OK.
- Resolución con infraestructura libre vía `OptimizationModel(...,
  daily_target_override=...)` — óptimo encontrado, `N_chargers=1, X=1`.
- Resolución con `fixed_infra={'N_chargers': 2, 'X': 1}` — confirma que
  `_apply_fixed_infra` fija correctamente las variables (costo subió como se
  esperaba al forzar más cargadores de los necesarios).
- `consumer.py` en modo carpeta plana (`station_1/2`) y modo por día
  (`station_1_d1/d2`, `station_2_d1`) — detección automática, dedup de CAPEX
  por estación, y costo de potencia pico ex-post, todos correctos.
- `combine_macrobloques_for_plot.py --mode DCH` sobre carpeta plana — combina
  JSONs y genera gráficos sin errores.

### Corrida real con `--parallel_days` (12 días, 3 estaciones) vs baseline monolítico

`output/DCH_taller_julio/`, comparando contra el equivalente sin descomponer
(mismo data folder, un solo `OptimizationModel` con los 12 días y las 3
estaciones juntos). `consumer.py --summary_only` sobre la carpeta
desacoplada vs `consumer.py` sobre la carpeta monolítica.

**Costo fijo, 320kW** (baseline monolítico: óptimo confirmado, gap 0%,
1514s):

| Métrica | Monolítico | Descomposición | Diferencia |
|---|---|---|---|
| N_chargers/X por estación | 1 / 1 (las 3) | 1 / 1 (las 3) | idéntico |
| Costo total | 726,008.80 | 728,530.14 | +0.35% |
| Extracción total | 483,900.48 | 484,354.08 | +0.09% |
| Energía cargada | 118,608.59 kWh | 118,759.63 kWh | +0.13% |
| Tiempo de pared | 1,514 s | ~191 s | **~7.9x** |

Diferencia explicada por las dos limitaciones ya documentadas arriba: el
reparto de target de Fase 0 (~0.1% en costo de operación) y el acople de
potencia punta recuperado solo ex-post (2,061.93 de los 2,521.34 de
diferencia total).

**Costo variable, 320kW** (baseline monolítico: gap 0.90%, 22,617s — casi
óptimo):

| Métrica | Monolítico | Descomposición | Diferencia |
|---|---|---|---|
| N_chargers/X por estación | 1 / 1 (las 3) | 1 / 1 (las 3) | idéntico |
| Costo total | 656,777.83 | 662,363.65 | +0.85% |
| Tiempo de pared | 22,617 s | ~277 s | **~81.6x** |

Acá el costo de operación se abre más (+4.37% vs +0.13% en costo fijo): con
precio de electricidad variable por intervalo, el reparto de Fase 0 no
considera *cuándo* es más barato cargar, solo *cuánto* le toca a cada
estación — el solver conjunto sí puede mover carga entre estaciones para
aprovechar intervalos baratos. El costo de potencia punta también se mueve
(ex-post 2,367 vs 9,134 reales) por la misma causa: el reparto distinto de
Fase 0 hizo que el día más exigente (día 91) terminara su carga fuerte antes
de la ventana punta en la descomposición, mientras que el reparto óptimo
conjunto le exige más a alguna estación ese día puntual y la obliga a cargar
parte dentro de la ventana. **La dirección del error no es predecible a
priori** (acá la descomposición subestimó el costo de potencia punta; en
costo fijo lo sobreestimó) — no asumir que la descomposición es siempre
conservadora.

**Costo variable, 640kW — caso especial, baseline monolítico NO confiable**:
el monolítico paró por timeout a las 48h (172,800s) con **gap 37.3%** (mejor
solución 1,018,535.21 con `N_chargers=2` por estación, mejor cota
638,366 — sin convergencia). La descomposición (~268s de pared) encontró
`N_chargers=1` por estación, costo total 685,250.80. El usuario confirmó por
una corrida anterior (no conservada) que **el óptimo real es 1 cargador por
estación** — es decir, la descomposición encontró la respuesta correcta de
dimensionamiento y el monolítico se quedó pegado en una solución peor por
falta de tiempo de cómputo. Este es el caso de uso más importante de la
descomposición: no es solo más rápida, en escenarios donde el problema
completo es intratable en tiempos razonables (horas/días), puede dar una
**mejor** respuesta que un monolítico con presupuesto de tiempo limitado,
porque resuelve subproblemas mucho más chicos que sí alcanzan a converger.
No se intentó re-verificar esto últomo computacionalmente (un chequeo de
600s con `fixed_infra={'N_chargers':1,'X':1}` sobre el modelo conjunto no
encontró ninguna solución factible en ese tiempo, pero tampoco lo descartó —
habría que correrlo varias horas para una verificación independiente).

### Pendiente

Escenarios con `Generators`/`Storage` (ver limitación arriba) — no probado.
