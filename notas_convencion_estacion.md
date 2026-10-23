# Convención temporal de la estación de swap en el código (Fase 0)

Rama `battery_swapping_multiaño`, commit `0b911ba9c` (2026-10-22). Código: `src/optimization/functions.py`.
Todo lo de este documento se verificó leyendo el código; no se modificó nada.

## 1. Dónde está cada bloque

| Elemento (tesis) | Función en `ConstraintRules` | Variables |
|---|---|---|
| Inventario de descargadas (3.60) | `inventory_discharged_batteries_rule` | `X_dch[k,y,d,t]`, `X_ini`, `W` (enteras ≥ 0) |
| Swaps de la estación = Σ swaps de LHD (3.61) | `total_swaps` | `W[k,y,d,t] = Σ_i Z_swap[k,i,y,d,t]` |
| Inventario de cargadas (3.62) | `inventory_charged_batteries_rule` | `S[k,y,d,t]` (entera ≥ 0) |
| Inicio y duración de la carga (3.64)–(3.65) | `charging_duration_rule` | `Sv[k,y,d,t,a]` sobre `SV_INDEX` (`a ∈ [max(t0, t−t_charge+1), t]`) |
| Ciclicidad (3.66)–(3.67) | `CI_S`, `CI_X_dch` | `S[t0] = S[tf]`, `X_dch[t0] = X_dch[tf]` |
| Inventario inicial (3.68) | `CB_general` | `S[t0] + X_dch[t0] = N_batteries[k,y]` |
| Cargadores (3.13) | `charger_limit_swap` | `Σ_a Sv[t,a] ≤ N_chargers[k,y]` |
| Subestación (3.71) | `max_installed_capacity_swap` | `p_charger · Σ_a Sv[t,a] ≤ 500 · n_ssee_k[k]` |
| Pico de distribución (3.73) | `peak_power_swap` | `Σ_k Σ_a Sv[t,a] ≤ n_peak_max = ⌊p_peak/p_charger⌋` |
| A lo sumo un swap por LHD (3.69) | `max_swaps` | `Σ_k Z_swap[k,i,t] ≤ 1` |
| Ventanas de swap (3.85), (3.87), (3.91) | `swap_only_meal_or_between_shifts(_det)`, `no_swap_maintenance_det` (con `--free_charging`) | `Z_swap = 0` fuera de las ventanas |
| Balance de potencia (3.96) | `power_balance` | `P_red + Σ_g P_gen + Σ_h P_bat = p_charger · Σ_k Σ_a Sv[t,a]` |
| Cargo por potencia (3.74) | `power_peak_limit` | `P_red[y,d,t] ≤ P_pot[y]` en los días 91–244 |
| Costo de energía (3.2) | `ObjectiveRules.lhd_charge_cost_bs` | `Σ P_red · c_elec · Δt · DF(y) · 365/|D|` |
| Corte de cargas (no es de la tesis) | `min_charges_n` (cabecera de functions.py) | `Σ_{k,t<tf} X_ini ≥ n_min[y,d]` |

Las baterías cargando en `t` son `Q[k,y,d,t] = Σ_{a ∈ ventana(t)} Sv[k,y,d,t,a]`. Como `Sv` copia a `X_ini`, el objeto fraccional real del LP es `X_ini` (más `Z_swap` del lado de los LHD).

## 2. Convención temporal exacta

Parámetros: `Δt = 8 min` (0,1333 h); `t_charge = round(b_max·(1−b_min) / (p_charger·η_charge) / Δt)`, con `b_max = 482 kWh`, `b_min = 0,2` y `η_charge = 0,95`:

| Tasa | p_charger | t_charge (L) | Energía que entrega la carga / energía necesaria |
|---|---:|---:|---:|
| 0,5C | 241 kW | **13** | 103 % |
| 1C | 482 kW | **6** | 95 % |
| 2C | 964 kW | **3** | 95 % |

`t_charge` usa la capacidad nominal (482 kWh), no la degradada `b_bar[y]`. Ojo: el documento de implementación dice L = 13, pero eso vale solo para 0,5C. El caso 7 (1C) tiene **L = 6**.

Orden de eventos dentro del intervalo `t` (con `t ≥ t0+1`):

1. **Swaps:** los `W[t]` swaps de `t` sacan baterías cargadas del inventario: `S[t] = S[t−1] + (carga que terminó en t−1) − W[t]`. Las baterías descargadas que llegan entran a `X_dch` en el mismo `t`.
2. **Inicio de cargas:** `X_ini[t] ≤ X_dch[t−1] + W[t]`, porque `X_dch[t] = X_dch[t−1] − X_ini[t] + W[t] ≥ 0`. **Una batería que llega con el swap de `t` puede empezar a cargarse en `t`.**
3. **Cargando en `t`:** `Q_t = Σ_{a=t−L+1}^{t} X_ini[a]` (con `Sv[t,t] = X_ini[t]` y `Sv[t,a] = Sv[t−1,a]`). Una carga que empieza en `a` ocupa cargador y consume `p_charger` en `a, a+1, …, a+L−1` (**L intervalos**).
4. **Fin de carga:** la carga iniciada en `a` termina al cierre de `a+L−1` y queda disponible en `S` desde `a+L` (`S[t+1] = S[t] + Sv[t, t−L+1] − W[t+1]`).

Esto coincide exactamente con la convención propuesta para la DP (estado `(S, r_1..r_{L−1})`; `Q_t = Σ r_n + x_t`; `S_{t+1} = S − w_t + r_1`; `r'_{L−1} = x_t`).

**Intervalo inicial `t0`:** `X_dch[t0]` es el inventario de descargadas al **inicio** del día, y las cargas de `t0` salen de él (`X_ini[t0] ≤ X_dch[t0]`). `X_dch[t0+1] = X_dch[t0] − X_ini[t0] − X_ini[t0+1] + W[t0+1]`. Los swaps de `t0` (`W[t0]`) **no** entran a ningún inventario. Ver la sección 3.4.

## 3. Estado de las tres verificaciones del documento

### 3.1 Anticipación de baterías en (3.60) — **CORREGIDA** en `0b911ba9c`
Antes era `X_dch[t] = X_dch[t−1] − X_ini[t−1] + W[t]`, que dejaba empezar a cargar en `t−1` una batería que llega en `t`. Ahora es `X_dch[t] = X_dch[t−1] − X_ini[t] + W[t]`: la batería que llega en `t` puede empezar en `t`, nunca antes.

### 3.2 Duración de la carga — **CORREGIDA** en `0b911ba9c`
Antes la ventana de `Sv` terminaba en `t−1` y la carga contaba `L−1` intervalos de potencia y cargador. Ahora `SV_INDEX` incluye `a = t` y `Sv[t,t] = X_ini[t]`, así que son L intervalos.

### 3.3 Borde del día — **VIGENTE por defecto; corregido detrás de `ELMO_PIPELINE_CICLICO=1`** (2026-10-22, aprobado por la usuaria; ver `validacion_dp_estacion.md`, sección "Pipeline cíclico")
La conservación implícita `S[t] + X_dch[t] + Σ_{a∈ventana(t)} Sv[t,a] = N_batteries` vale en todo `t`. Al combinarla con `CI_S`, `CI_X_dch` y `CB_general` (`S[t0] + X_dch[t0] = N_batteries`), resulta que **`Σ_a Sv[tf,a] = 0`**:

- **No puede haber ninguna batería cargando en el último intervalo `tf`.** Las cargas del final del día no "se pierden": están **prohibidas**. Toda carga tiene que terminar a más tardar en `tf−1` (iniciada en `a ≤ tf−L`).
- Al inicio del día el pipeline está vacío (3.63): las primeras completaciones posibles son en `t0+L−1` (`inventory_charged_batteries_rule` usa `S[t+1] = S[t] − W[t+1]` mientras `t−L+1 < t0`).

Es una restricción de modelado: el día no puede terminar con una carga en curso. Con L = 13 (0,5C) restringe el final del día. No se corrige: hay que **replicarla en la DP** para que las dos describan lo mismo (estado inicial y final con pipeline vacío, más `Q_tf = 0`).

### 3.4 Observación adicional: swaps en `t0`
`t0` (08:30) está dentro de una ventana `between_shifts` (08:30–08:46), así que `Z_swap[t0]` está permitido, pero `W[t0]` no descuenta de `S` ni suma a `X_dch`. Es **inocuo**: `battery_boundary_swap` fija `B_s[0] = B[0]`, de modo que un swap en `t0` no repone energía al LHD. Ninguna solución lo usa (revisadas la prueba de 2 días, el caso 3 v4 y el caso 7 v3: 0 swaps en `t=1`). La DP prohíbe swaps en `t0`.

## 4. Implicancias para la DP y el flujo

- **Estado:** `(S, r_1, …, r_{L−1})` con `S + Σr ≤ N` y `Σr ≤ N_C` (porque `Q = Σr + x ≤ N_C`).
- **Inicio y fin:** pipeline vacío (`r = 0`) y `S_{inicio} = S_{fin}` (cíclico en `S`; `X_dch` cíclico queda implícito por conservación). Además `Q_tf = 0`.
- **Swaps permitidos:** según la ventana del régimen; con `--free_charging --free_maintenance` casi todo el día. Nunca en `t0`.
- **Cargas:** sin restricción horaria propia (solo cargadores, subestación y pico).
- **Costo de energía** (modo `arc_cost`): `ω_{y,d} · c_{y,d,t} · Δt · max(0, p_charger·Q − A_t)`, con `ω = (365/|D|)·DF(y)` y `DF(y) = 1/(1+r)^y` (year_position 1-indexada).
