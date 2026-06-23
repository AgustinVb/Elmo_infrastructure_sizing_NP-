# Modelo algebraico — Esquema de costos de inversión en estaciones de swap

Este documento describe el esquema de costos de inversión agregado al modelo
(`costo fijo`, `costo por nave`, `costo por puente grúa`, `costo del sistema
de carga` y `costo por cantidad de baterías`), junto con las variables y
restricciones que lo determinan.

Referencia de código: `src/optimization/functions.py`.

## 1. Conjuntos

| Símbolo | Descripción | Set en Pyomo |
|---|---|---|
| $k \in K$ | Estaciones de swap | `model.stations_set` |
| $i \in I_s$ | LHD con batería swap | `model.slhd_set` |
| $d \in D$ | Días | `model.days` |
| $t \in T$ | Intervalos de tiempo | `model.time_intervals_set` |

## 2. Variables de decisión

| Símbolo | Dominio | Variable Pyomo | Descripción |
|---|---|---|---|
| $X_k$ | $\{0,1\}$ | `model.X[k]` | Estación $k$ instalada |
| $N^{bays}_k$ | $\mathbb{Z}_{\ge 0}$, $1 \le \cdot \le 12$ | `model.N_bays[k]` | Naves (bahías de swap simultáneo) en $k$ |
| $N^{ch}_k$ | $\mathbb{Z}_{\ge 0}$, $1 \le \cdot \le 12$ | `model.N_chargers[k]` | Cargadores instalados en $k$ |
| $N^{bat}_k$ | $\mathbb{Z}_{\ge 0}$, $1 \le \cdot \le 12$ | `model.N_batteries[k]` | Baterías estacionarias en $k$ |
| $Z^{swap}_{k,i,d,t}$ | $\{0,1\}$ | `model.Z_swap[k,i,d,t]` | LHD $i$ hace swap en estación $k$ en $(d,t)$ |

Ninguna de las variables de inversión ($N^{bays}_k$, $N^{ch}_k$, $N^{bat}_k$)
tiene una expresión cerrada que la calcule directamente: son variables
enteras libres dentro de sus cotas, cuyo valor óptimo queda determinado por
el balance entre el costo de instalarlas y la necesidad operacional de
swaps/cargas que imponen las restricciones de la sección 4.

## 3. Parámetros de costo

| Símbolo | Parámetro Pyomo | Columna de origen | Multiplica a | Significado |
|---|---|---|---|---|
| $c^{fixed}_k$ | `station_cost_k[k]` | `c_fixed` (hoja stations) | $X_k$ | Costo fijo de instalar la estación |
| $c^{bays}_k$ | `c_bays_k[k]` | `c_bays` | $N^{bays}_k$ | Costo por nave |
| $c^{crane}_k$ | `c_crane_k[k]` | `c_crane` | $N^{bays}_k$ | Costo por puente grúa (uno por nave) |
| $c^{charger}$ | `charger_cost` | `charger_cost` (hoja chargers) | $N^{ch}_k$ | Costo del equipo cargador (igual en todo $k$) |
| $c^{ch.space}_k$ | `c_charger_space_k[k]` | `c_charger_space` | $N^{ch}_k$ | Costo de espacio/obra civil por cargador |
| $c^{battery}$ | `battery_cost` | `battery_cost` (hoja chargers) | $N^{bat}_k$ | Costo del equipo batería (igual en todo $k$) |
| $c^{bat.space}_k$ | `c_battery_space_k[k]` | `c_battery_space` | $N^{bat}_k$ | Costo de espacio por batería |

Parámetros de cotas usados en las restricciones:

| Símbolo | Parámetro Pyomo | Columna de origen |
|---|---|---|
| $\overline{N^{bays}_k}$ | `max_bays_k[k]` | `max_bays` |
| $\overline{m}^{ch}_k$ | `max_chargers_per_bay_k[k]` | `max_chargers_per_bay` |
| $\overline{m}^{bat}_k$ | `max_batteries_per_bay_k[k]` | `max_batteries_per_bay` |

## 4. Restricciones de dimensionamiento

$$
\begin{aligned}
N^{bays}_k &\le \overline{N^{bays}_k} \, X_k
  & \forall k
  &\quad\text{(máx. naves, solo si la estación existe — \texttt{max\_n\_bays})}\\[6pt]
\sum_{i \,:\, (k,i) \in \text{ZSWAP\_INDEX}} Z^{swap}_{k,i,d,t} &\le N^{bays}_k
  & \forall k, d, t
  &\quad\text{(swaps simultáneos} \le \text{naves — \texttt{bays\_limit\_swap})}\\[6pt]
N^{bays}_k &\le N^{ch}_k
  & \forall k
  &\quad\text{(cada nave requiere $\ge 1$ cargador — \texttt{bays\_le\_chargers})}\\[6pt]
N^{ch}_k &\le \overline{m}^{ch}_k \, N^{bays}_k
  & \forall k
  &\quad\text{(máx. cargadores por nave — \texttt{max\_chargers\_per\_bay\_constr})}\\[6pt]
N^{bat}_k &\le \overline{m}^{bat}_k \, N^{bays}_k
  & \forall k
  &\quad\text{(máx. baterías por nave — \texttt{max\_batteries\_per\_bay\_constr})}\\[6pt]
N^{ch}_k &\le N^{bat}_k
  & \forall k
  &\quad\text{(no más cargadores que baterías — \texttt{chargers\_le\_batteries})}
\end{aligned}
$$

La restricción `bays_limit_swap` es la que efectivamente "dimensiona"
$N^{bays}_k$ desde el lado operacional: el solver debe elegir un valor
suficiente para cubrir el pico de swaps simultáneos
$\max_{d,t}\sum_i Z^{swap}_{k,i,d,t}$, balanceándolo contra el costo
$c^{bays}_k + c^{crane}_k$ por nave en la función objetivo.

## 5. Función de costo de inversión

$$
C^{inv} = \sum_{k \in K} \Big[\;
\underbrace{c^{fixed}_k \, X_k}_{\text{costo fijo}}
\;+\;
\underbrace{c^{bays}_k \, N^{bays}_k}_{\text{costo por nave}}
\;+\;
\underbrace{c^{crane}_k \, N^{bays}_k}_{\text{costo puente grúa}}
\;+\;
\underbrace{\big(c^{charger} + c^{ch.space}_k\big) N^{ch}_k}_{\text{costo sistema de carga}}
\;+\;
\underbrace{\big(c^{battery} + c^{bat.space}_k\big) N^{bat}_k}_{\text{costo por batería}}
\;\Big]
$$

Código: `ObjectiveRules.inversion_cost` (`functions.py:1328-1337`).

## 6. Función objetivo total

$$
\min \; Z = C^{el}_{red} + C^{inv} + C^{gen}_{inv} + C^{gen}_{op} + C^{bess}_{inv} + C^{bess}_{op} + C^{peak}
$$

donde:

- $C^{el}_{red}$ — costo de electricidad comprada a la red (`lhd_charge_cost_bs`)
- $C^{inv}$ — costo de inversión en estaciones (sección 5, este documento)
- $C^{gen}_{inv}$, $C^{gen}_{op}$ — inversión y operación de generación renovable (`gen_investment_cost`, `gen_op_cost`)
- $C^{bess}_{inv}$, $C^{bess}_{op}$ — inversión y operación de almacenamiento BESS (`bess_investment_cost`, `bess_op_cost`)
- $C^{peak}$ — cargo por potencia punta / demand charge (`peak_power_cost`)

Código: `ObjectiveRules.total_cost` (`functions.py:1362-1369`).
