# Modelo algebraico — Esquema de costos de inversión en estaciones

Este documento describe el esquema de costos de inversión agregado al modelo, para los
dos casos soportados: **battery swap** y **carga on-board**.

Referencia de código: `src/optimization/functions.py`.

---

## Caso battery swap

### 1. Conjuntos

| Símbolo | Descripción | Set en Pyomo |
|---|---|---|
| k ∈ K | Estaciones de swap | `model.stations_set` |
| i ∈ Iₛ | LHD con batería swap | `model.slhd_set` |
| d ∈ D | Días | `model.days` |
| t ∈ T | Intervalos de tiempo | `model.time_intervals_set` |

### 2. Variables de decisión

| Símbolo | Dominio | Variable Pyomo | Descripción |
|---|---|---|---|
| X_k | {0,1} | `model.X[k]` | Estación k instalada |
| N_k^bays | ℤ≥0, 1 ≤ · ≤ 12 | `model.N_bays[k]` | Naves (bahías de swap simultáneo) en k |
| N_k^ch | ℤ≥0, 1 ≤ · ≤ 12 | `model.N_chargers[k]` | Cargadores instalados en k |
| N_k^bat | ℤ≥0, 1 ≤ · ≤ 12 | `model.N_batteries[k]` | Baterías estacionarias en k |
| Z^swap_{k,i,d,t} | {0,1} | `model.Z_swap[k,i,d,t]` | LHD i hace swap en estación k en (d,t) |

Ninguna de las variables de inversión (N^bays_k, N^ch_k, N^bat_k) tiene una expresión
cerrada que la calcule directamente: son variables enteras libres dentro de sus cotas,
cuyo valor óptimo queda determinado por el balance entre el costo de instalarlas y la
necesidad operacional de swaps/cargas que imponen las restricciones de la sección 4.

### 3. Parámetros de costo

| Símbolo | Parámetro Pyomo | Columna de origen | Multiplica a | Significado |
|---|---|---|---|---|
| c_k^fixed | `station_cost_k[k]` | `c_fixed` (hoja stations) | X_k | Costo fijo de instalar la estación |
| c_k^bays | `c_bays_k[k]` | `c_bays` | N^bays_k | Costo por nave |
| c_k^crane | `c_crane_k[k]` | `c_crane` | N^bays_k | Costo por puente grúa (uno por nave) |
| c^charger | `charger_cost` | `charger_cost` (hoja chargers) | N^ch_k | Costo del equipo cargador (igual en todo k) |
| c_k^ch.space | `c_charger_space_k[k]` | `c_charger_space` | N^ch_k | Costo de espacio/obra civil por cargador |
| c^battery | `battery_cost` | `battery_cost` (hoja chargers) | N^bat_k | Costo del equipo batería (igual en todo k) |
| c_k^bat.space | `c_battery_space_k[k]` | `c_battery_space` | N^bat_k | Costo de espacio por batería |

Parámetros de cotas usados en las restricciones:

| Símbolo | Parámetro Pyomo | Columna de origen |
|---|---|---|
| N_k^bays,max | `max_bays_k[k]` | `max_bays` |
| m_k^ch,max | `max_chargers_per_bay_k[k]` | `max_chargers_per_bay` |
| m_k^bat,max | `max_batteries_per_bay_k[k]` | `max_batteries_per_bay` |

### 4. Restricciones de dimensionamiento

- N^bays_k ≤ N^bays,max_k · X_k ∀k — Máx. naves, solo si la estación existe (`max_n_bays`).
- Σ_i Z^swap_{k,i,d,t} ≤ N^bays_k ∀k,d,t — Swaps simultáneos no superan las naves (`bays_limit_swap`).
- N^bays_k ≤ N^ch_k ∀k — Cada nave requiere al menos un cargador (`bays_le_chargers`).
- N^ch_k ≤ m^ch,max_k · N^bays_k ∀k — Máx. cargadores por nave (`max_chargers_per_bay_constr`).
- N^bat_k ≤ m^bat,max_k · N^bays_k ∀k — Máx. baterías por nave (`max_batteries_per_bay_constr`).
- N^ch_k ≤ N^bat_k ∀k — No más cargadores que baterías (`chargers_le_batteries`).

La restricción `bays_limit_swap` es la que efectivamente "dimensiona" N^bays_k desde el
lado operacional: el solver debe elegir un valor suficiente para cubrir el pico de swaps
simultáneos max_{d,t} Σ_i Z^swap_{k,i,d,t}, balanceándolo contra el costo c^bays_k + c^crane_k
por nave en la función objetivo.

### 5. Función de costo de inversión

```
C_inv = Σ_k [ c^fixed_k · X_k                                   (costo fijo)
            + c^bays_k · N^bays_k                                (costo por nave)
            + c^crane_k · N^bays_k                                (costo puente grúa)
            + (c^charger + c^ch.space_k) · N^ch_k                  (costo sistema de carga)
            + (c^battery + c^bat.space_k) · N^bat_k ]               (costo por batería)
```

Código: `ObjectiveRules.inversion_cost` (`functions.py:1328-1337`).

---

## Caso carga on-board

El esquema de carga on-board es estructuralmente más simple que el de swap: no existen
naves de swap (N^bays) ni baterías estacionarias (N^bat), porque cada LHD carga su propia
batería directamente en un cargador de la estación. La única variable de inversión por
estación es la cantidad de cargadores instalados, N^ch_k.

### 1. Conjuntos

| Símbolo | Descripción | Set en Pyomo |
|---|---|---|
| k ∈ K | Estaciones de carga | `model.stations_set` |
| i ∈ I_e | LHD eléctrico con carga on-board | `model.elhd_set` |
| d ∈ D | Días | `model.days` |
| t ∈ T | Intervalos de tiempo | `model.time_intervals_set` |
| (k,i) ∈ ZCHARGE_INDEX | Pares estación-LHD habilitados para cargar | `model.ZCHARGE_INDEX` |

### 2. Variables de decisión

| Símbolo | Dominio | Variable Pyomo | Descripción |
|---|---|---|---|
| X_k | {0,1} | `model.X[k]` | Estación k instalada |
| N_k^ch | ℤ≥0, 1 ≤ · ≤ 12 | `model.N_chargers[k]` | Cargadores instalados en k |
| Z_charge_{k,i,d,t} | {0,1} | `model.Z_charge[k,i,d,t]` | LHD i está cargando en estación k en (d,t) |
| StartCharge_{k,i,d,t} | {0,1} | `model.StartCharge[k,i,d,t]` | LHD i inicia una carga en k en (d,t) |
| EndCharge_{k,i,d,t} | {0,1} | `model.EndCharge[k,i,d,t]` | LHD i termina una carga en k en (d,t) |
| P_{k,i,d,t} | ℝ≥0 | `model.P[k,i,d,t]` | Potencia de carga de i en k en (d,t) |

A diferencia del caso swap, N^ch_k es la única variable de inversión de la estación: no
hay N^bays (la "nave" la ocupa directamente el cargador) ni N^bat (no hay baterías
estacionarias que swappear).

### 3. Parámetros de costo

| Símbolo | Parámetro Pyomo | Columna de origen | Multiplica a | Significado |
|---|---|---|---|---|
| c_k^fixed | `station_cost_k[k]` | `c_fixed` (hoja stations) | X_k | Costo fijo de instalar la estación |
| c_k^bays | `c_bays_k[k]` | `c_bays` | N^ch_k | Costo de obra civil/espacio asociado a cada cargador (reutiliza el parámetro "bays" del caso swap, ya que aquí un cargador ocupa el rol de una nave) |
| c^charger | `charger_cost` | `charger_cost` (hoja chargers) | N^ch_k | Costo del equipo cargador (igual en todo k) |
| c_k^ch.space | `c_charger_space_k[k]` | `c_charger_space` | N^ch_k | Costo de espacio/obra civil por cargador |

No se utilizan `c_crane_k`, `battery_cost` ni `c_battery_space_k`: no hay puente grúa
(no hay swap de baterías) ni baterías estacionarias en el caso on-board.

Parámetros adicionales relevantes para el dimensionamiento eléctrico:

| Símbolo | Parámetro Pyomo | Columna de origen | Significado |
|---|---|---|---|
| p^charger | `p_charger` | — (hoja chargers) | Potencia nominal de un cargador |
| p_k^max | `p_max_k[k]` | — (hoja stations) | Capacidad máxima del sistema de distribución eléctrica de la estación k |

Parámetros de cotas:

| Símbolo | Parámetro Pyomo | Columna de origen |
|---|---|---|
| N_k^bays,max | `max_bays_k[k]` | `max_bays` |

### 4. Restricciones de dimensionamiento

- N^ch_k ≤ N^bays,max_k · X_k ∀k — Máx. cargadores, solo si la estación existe (`max_n_chargers`).
- Z_charge_{k,i,d,t} ≤ X_k ∀(k,i,d,t) — Solo se puede cargar en una estación instalada (`station_existence_constraint`).
- Σ_i Z_charge_{k,i,d,t} ≤ N^ch_k ∀k,d,t — Cargas simultáneas no superan los cargadores instalados (`charger_limit`).
- Z_charge_{k,i,d,t} − Z_charge_{k,i,d,t−1} = StartCharge_{k,i,d,t} − EndCharge_{k,i,d,t} ∀(k,i,d,t) — Lógica de inicio/término de carga (`charge_state`).
- Z_charge_{k,i,d,t} + Z_charge_{k,i,d,t+1} ≥ 2 · StartCharge_{k,i,d,t} ∀(k,i,d,t) — Duración mínima de carga de 2 intervalos una vez iniciada (`min_charge_duration`).
- P_{k,i,d,t} ≤ Z_charge_{k,i,d,t} · p^charger ∀(k,i,d,t) — Potencia de carga acotada al cargador solo si está conectado (`max_power`).
- Σ_i P_{k,i,d,t} ≤ p_k^max ∀k,d,t — Potencia total demandada en la estación acotada por su capacidad de distribución (`max_installed_capacity`).

A diferencia de `bays_limit_swap` en el caso swap (que dimensiona naves por número de
swaps simultáneos), aquí es `charger_limit` la restricción que dimensiona N^ch_k desde el
lado operacional: el solver debe elegir un valor suficiente para cubrir el pico de cargas
simultáneas max_{d,t} Σ_i Z_charge_{k,i,d,t}, balanceándolo contra el costo
(c^charger + c^ch.space_k + c^bays_k) por cargador en la función objetivo.

### 5. Función de costo de inversión

```
C_inv = Σ_k [ c^fixed_k · X_k                                          (costo fijo)
            + (c_k^bays + c^charger + c_k^ch.space) · N^ch_k ]           (costo sistema de carga)
```

Código: `ObjectiveRules.inversion_cost` (`functions.py:903-909`).

### 6. Función objetivo total

```
min Z = C_red^el + C_inv + C_inv^gen + C_op^gen + C_inv^bess + C_op^bess + C^peak
```

donde:

- **C_red^el** — costo de electricidad comprada a la red (`lhd_charge_cost`).
- **C_inv** — costo de inversión en estaciones (sección 5, este documento).
- **C_inv^gen, C_op^gen** — inversión y operación de generación renovable (`gen_investment_cost`, `gen_op_cost`).
- **C_inv^bess, C_op^bess** — inversión y operación de almacenamiento BESS (`bess_investment_cost`, `bess_op_cost`).
- **C^peak** — cargo por potencia punta / demand charge (`power_cost`).

Código: `ObjectiveRules.total_cost` (`functions.py:936-942`).

Esta función objetivo total es la misma estructura usada en el caso swap; la única
diferencia entre ambos casos es la composición de C_inv (sección 5 de cada caso) y de las
variables/restricciones de dimensionamiento de estaciones (sección 4 de cada caso).
