# Validación de la DP de la estación contra el MILP (Fase 2)

Script: `validar_dp_estacion.py`. DP: `dp_estacion.py` (Fase 1, tests en `tests/test_dp_estacion.py`).
Código: rama `battery_swapping_multiaño`, commit `0b911ba9c`, con la convención de `notas_convencion_estacion.md`.

## Método

Para cada (año, día), con un incumbente cargado y pulido:

- **MILP:** el día de `BendersDias` (partición year_day), con la inversión fija (copias del maestro: N_batteries, N_chargers, n_ssee_k, P_pot, G_g, H, b_bar…). Además se fijan `Y`, `Z` y `Z_swap` en el incumbente. Quedan libres solo la estación (`S`, `Sv`, `X_ini`, `X_dch`, `W`) y la potencia (`P_red`, `P_gen`, `Curt_g`, BESS). Se resuelve a optimalidad.
- **DP:** `dp_evaluar` con `W_t = Σ_i Z_swap` del incumbente, `N = N_batteries`, `N_C = N_chargers` y `L = t_charge`. El costo por arco es `coef(P_red)_t · max(0, p_charger·Q − A_t)`, con `A_t = Σ_g G_g·p_max_g·α_t`, y el coeficiente se toma del objetivo del propio modelo. Los topes de `Q` por intervalo son cargadores, subestación (`500·n_ssee/p_charger`), pico (`n_peak_max` y `P_red ≤ p_peak`) y, **solo en las horas de punta** (`time_intervals_peak_set`) de los días 91–244, `P_red ≤ P_pot`. Borde del día `ciclo="modelo"`.

## Caso 3 de swap v4 (RED_GEN, sin BESS): costo exacto

Incumbente: k=2 de la cola v4 (`casos_finales_v4/.../05C/RED_GEN/benders/incumbente_respaldo_k2.pkl`), pulido 3.289.542,58.

| Día | N_bat | N_C | L | Swaps | MILP | Energía MILP | DP | Energía DP | Diferencia rel. | Cargas MILP / DP | Q_t idéntico |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|---|---|
| (3,105) | 1 | 1 | 13 | 13 | factible | 20.405,20 | factible | 20.405,20 | 5e-16 | 13 / 13 | no (14 intervalos) |
| (4,196) | 2 | 2 | 13 | 19 | factible | 24.462,76 | factible | 24.462,76 | −4e-16 | 19 / 19 | no (48) |
| (5,288) | 2 | 2 | 13 | 20 | factible | 17.267,81 | factible | 17.267,81 | −8e-16 | 20 / 20 | no (50) |
| (6,196) | 2 | 2 | 13 | 20 | factible | 23.353,83 | factible | 23.353,83 | −3e-15 | 20 / 20 | no (29) |
| (7,105) | 2 | 2 | 13 | 21 | factible | 22.108,26 | factible | 22.108,26 | −5e-16 | 21 / 21 | no (40) |
| (8,15) | 2 | 2 | 13 | 16 | factible | 4.685,13 | factible | 4.685,13 | 4e-16 | 16 / 16 | sí |

**Conclusión:** la DP y el bloque de estación del MILP describen lo mismo. Coinciden la factibilidad, el costo de energía (hasta el redondeo de punto flotante) y el número de cargas, en días de punta y fuera de punta, con N = 1 y N = 2. Donde los perfiles `Q_t` difieren, se trata de óptimos alternativos del mismo costo: con precio plano y solar, hay muchos horarios de carga equivalentes. La DP tarda ~4 s por día (N = 2, N_C = 2, L = 13; 105 estados).

Primer intento: los días de punta salían infactibles en la DP porque el script aplicaba `P_red ≤ P_pot` a todo el día. En el modelo, `power_peak_limit` solo rige en `time_intervals_peak_set`. Era un error del script, no del MILP.

## Borde del día: costo de la restricción actual

Con `ciclo="completo"`, una carga puede cruzar la medianoche y el pipeline es cíclico. La misma DP da:

| Día | Energía, borde del modelo | Energía, ciclo completo | Diferencia |
|---|---:|---:|---:|
| (3,105) | 20.405,20 | 20.405,20 | 0,0 % |
| (4,196) | 24.462,76 | 24.150,81 | −1,3 % |
| (5,288) | 17.267,81 | 16.868,69 | −2,3 % |
| (6,196) | 23.353,83 | 23.086,39 | −1,1 % |
| (7,105) | 22.108,26 | 21.766,08 | −1,5 % |
| (8,15) | 4.685,13 | 4.368,30 | −6,8 % |

La restricción del borde (punto 3.3 de `notas_convencion_estacion.md`: nada cargando en `tf` y pipeline vacío al inicio) le cuesta a la estación entre 0 y 7 % de la energía del día. Propuesta: hacer cíclico el pipeline, igualando `Sv[t0..t0+L−2]` con las cargas que siguen en curso en `tf`. El inventario cíclico `S`/`X_dch` ya existe; faltaría el de las baterías en carga. **La usuaria la aprobó (2026-10-22)**; ver la sección siguiente.

## Pipeline cíclico en el MILP (`ELMO_PIPELINE_CICLICO=1`)

Implementado en `src/optimization/functions.py`, detrás de la variable de entorno (`setup.py --pipeline_ciclico`). **Está apagado por defecto.** Con la variable apagada, el modelo es idéntico al del commit `0b911ba9c`: el LP escrito con nombres simbólicos (1 año, 1 día) es igual byte a byte. Con la variable activa:

- Las ventanas de `Sv` son circulares (`ventana_carga`): en `t` cuentan los L inicios `t, t−1, …` módulo T. Por ejemplo, en t=1 la ventana es {1, 180, 179, …, 169}.
- `charging_duration`: `Sv[t,a] = Sv[anterior(t),a]`, con anterior(t0) = tf.
- `inventory_charged`: `S[sig(t)] = S[t] + Sv[t, inicio más antiguo de la ventana] − W[sig(t)]` en todo t, con sig(tf) = t0. Una carga que empieza en 168 ocupa 168–180 y queda disponible en t=1; una que empieza en 169 sigue en t=1 y queda disponible en t=2.
- `inventory_discharged`: `X_dch[t] = X_dch[anterior(t)] − X_ini[t] + W[t]` en todo t. Ahora `X_dch[t0]` es el inventario al **final** de t0.
- Se omiten `CI_S` y `CI_X_dch`, porque las ecuaciones de inventario ya cierran el ciclo. `CB_general` pasa a `S[t0] + X_dch[t0] + Σ_{a∈ventana(t0)} Sv[t0,a] = N_batteries`.
- El corte de cargas (`min_charges_cut`, `driver._agregar_corte_cargas_lhd` y `certificar_inversiones`) cuenta todos los t: el inventario cíclico da `Σ X_ini = Σ W`, así que el corte sigue siendo válido.
- `t0` pasa a ser un intervalo más. Un swap en t0 gasta una batería, pero sigue sin reponer energía (`battery_boundary_swap`); ninguna solución lo usa.

**Relación con el modelo anterior:** una solución sin ciclo se lleva exactamente al modelo cíclico con `X_dch[t0] −= X_ini[t0]` (`adaptar_incumbente_ciclico`), porque no queda nada cargando en tf y no hay swaps en t0. Por lo tanto, el modelo cíclico es una relajación del anterior: toda solución vieja sigue siendo factible y con el mismo costo. El incumbente k=2 adaptado se pule a 3.289.542,58, el mismo costo de antes; en 30 de los 40 días había cargas en t0.

**Validación (caso 3 v4, `validar_dp_estacion.py --pipeline_ciclico`, salida en `validacion_dp_estacion/caso3_v4_ciclico/`):**

| Día | MILP cíclico | DP `ciclo="completo"` | Diferencia rel. |
|---|---:|---:|---:|
| (3,105) | 20.405,20 | 20.405,20 | 5e-16 |
| (4,196) | 24.150,81 | 24.150,81 | −3e-16 |
| (5,288) | 16.868,69 | 16.868,69 | −1e-15 |
| (6,196) | 23.086,39 | 23.086,39 | −3e-15 |
| (7,105) | 21.766,08 | 21.766,08 | −5e-16 |
| (8,15) | 4.368,30 | 4.368,30 | −2e-16 |

El MILP con el pipeline cíclico reproduce exactamente la DP circular, que es la convención del flujo de la Fase 3. Las energías coinciden también con las que la DP completa había anticipado en la tabla anterior.

Nota: en un primer intento, el incumbente no se adaptó y el pulido falló. El script siguió con los valores sin pulir, y las continuas del maestro venían crudas del respaldo (P_pot = 86 en vez de 241, y otro G). El MILP y la DP coincidieron igual, porque reciben las mismas entradas, pero esas energías no se pueden comparar con la tabla anterior.

## Caso 7 (RED_GEN_BESS, 1C, L = 6)

**No se pudo validar con el incumbente disponible.** El de la pasada 3 de la v3 (`casos_finales/.../1C/RED_GEN_BESS/mejorar_ub_3/solucion`) **no es factible con el código actual**: el pulido falla con "el incumbente dado no es factible al pulirlo". Es lo esperable, porque se calculó antes de corregir la degradación (4c6491e50) y la duración de la carga (0b911ba9c). Con 1C, la carga ahora ocupa 6 intervalos y no puede adelantarse, así que el calendario de swaps de la v3 deja de ser servible. La prueba de 2 días de RED_GEN_BESS v4 corrió antes de 0b911ba9c y tiene el mismo problema.

Hace falta un incumbente del caso 7 (o de otro con BESS) calculado con el código actual. Por ejemplo, una iteración de la cola swap v4 sobre ese caso. Con él se repite este paso: solo factibilidad y perfiles, porque la DP no modela el BESS.
