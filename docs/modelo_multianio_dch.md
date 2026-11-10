# Modelo multiaño de DCH: datos, modelo, algoritmos y resultados

Este documento describe el modelo multiaño de inversión y operación usado para DCH
(plan minero 2035–2045):
- los datos de entrada;
- el modelo monolítico, con la formulación completa en los anexos A y B;
- los algoritmos (heurísticas, Nested Benders y L-shaped);
- el postprocesamiento: validación, informes, ajustes de la comparación, gráficos y tablas;
- las pruebas realizadas y el esquema que dio mejores resultados.

El documento se lee sin el código: todo se describe a nivel de formulación y de método.
Si se recibe con los zips del repositorio, la **guía de revisión** (después del resumen)
indica qué revisar en cada uno. La
sección 7 resume qué se probó y con qué resultado, pensando en llevar el multiaño al
nivel de transporte, que hoy solo tiene el modelo anual.

Estado al 2026-11-10.

---

## 0. Resumen

- **Qué resuelve.** Decide cuánta infraestructura de carga instalar y cuándo, en 11
  años: estaciones, cargadores, subestación, reemplazos de batería y potencia
  contratada. Lo hace junto con la operación de 12 LHD eléctricos:
  - 4 días representativos por año;
  - intervalos de 8 min;
  - degradación de batería.

  La instancia tiene unos 4,3 millones de variables y 2,5 millones de restricciones.
- **El monolítico no escala.** A 11 años no cierra en 10 h (gap 12,3 %). A 6 años no
  encontró solución factible en 38 min.
- **Esquema que funcionó en carga on-board (OB):**
  1. preprocesamiento de capacidad;
  2. cota operacional (inversión entera, operación relajada) como cota inferior;
  3. Nested Benders por año, con cortes fortalecidos, estabilización de caja y arranque
     por días;
  4. si no cierra bajo 1 %: mejora de la operación y certificado.

  Con eso, la tasa 1C cerró en 0,10 % en 3,3 h.
- **Cuando la cota superior queda mala (2C)**, la cota inferior no es el problema. El
  paso hacia adelante de Benders sobredimensionó el año pico porque no ve la
  degradación futura. El gap bajó de 12,3 % a 5,4 % con:
  - el L-shaped entero, para la cota inferior;
  - un diagnóstico del año crítico;
  - una búsqueda local sobre reemplazos y cargadores.
- **0,5C es infactible** en el año 7: un cargador de 241 kW por estación no alcanza a
  recargar 4 LHD.
- **En battery swapping (BS)** el mejor algoritmo fue el L-shaped entero, con gap
  promedio de 2,5 % en la mina modelo. En DCH se le agregaron macrobloques por estación,
  un recorte de la solución y cortes de capacidad (3.10):

  | Tasa | Costo BS | Gap | Frente a OB |
  |---|---|---|---|
  | 0,5C | 6,39 M | 15,5 % | OB infactible |
  | 1C | 6,29 M | 13,9 % | −26,0 % |
  | 2C | 9,80 M | 1,7 % | −11,7 % |

  Las diferencias entre las dos tecnologías están en la sección 2.7, y los resultados de
  DCH en la 5.5.

---

## Guía de revisión (para quien recibe los zips)

Este documento va con dos zips, uno por rama del repositorio. Son para leer y verificar
el código. No traen los datos (`data/`) ni las salidas de las corridas (`output/`), así
que el código no se puede ejecutar tal cual: la revisión es de lectura.

| Zip | Rama y commit | Qué contiene |
|---|---|---|
| `Elmo_carga_ob_multianio.zip` | `carga_ob_multiaño`, `fe58fefdc` | modelo on-board y todo lo usado en DCH: Nested Benders, cota operacional, mejora de la operación, certificado, L-shaped, búsqueda local y diagnóstico; script que arma los datos DCH; colas con los parámetros exactos de las corridas |
| `Elmo_battery_swapping_multianio.zip` | `battery_swapping_multiaño`, `d87b59852` | corridas DCH de battery swapping (macrobloques por estación, recorte, cortes de capacidad) y su comparación con OB; modelo battery swapping, diseño y resultados del L-shaped, DP de degradación, generación de columnas, postprocesamiento y comparación OB–BS, figuras y tablas resumen de la mina modelo |

Los dos zips incluyen este documento en `docs/modelo_multianio_dch.md`. En el de battery
swapping no van las figuras de detalle de cada caso (`cost_comparisons/resultados_finales/caso_*/`,
690 MB); sí van las figuras resumen, las tablas y la holgura operativa.

### Cómo abrir y leer los zips

1. **Descomprimir los dos zips en una misma carpeta.** Cada uno crea su propia
   carpeta, `Elmo_carga_ob_multianio/` y `Elmo_battery_swapping_multianio/`, así que no se
   pisan.
2. **Empezar por este documento** (`docs/modelo_multianio_dch.md`), que es el mismo en
   las dos carpetas. Todas las instrucciones están aquí.
3. **Las rutas** de esta guía son relativas a la raíz de cada carpeta. Por ejemplo,
   `src/optimization/functions.py` del zip OB está en
   `Elmo_carga_ob_multianio/src/optimization/functions.py`.
4. **Son copias del estado de cada rama, sin historial de git.** No se puede ver el
   historial ni cambiar de rama. Si hace falta una versión anterior de algo, hay que
   pedirla.
5. **Para leer:**
   - Cualquier editor de código sirve. En VS Code: *File → Open Folder* sobre la carpeta
     que contiene las dos.
   - Los `.md` se ven formateados con la vista previa de Markdown (Ctrl+Shift+V en VS
     Code), que también muestra las ecuaciones.
   - Los `.sh` son las colas de corrida, en bash. Se leen como texto para ver las opciones
     exactas de cada corrida.
6. **No hace falta ejecutar nada.** El código necesita Python 3.10, Pyomo y Gurobi
   (`requirements.txt`) y, además, los datos, que no van en los zips.
7. **Con Claude Code:** abrirlo en la carpeta que contiene las dos carpetas
   descomprimidas, para que vea ambas, y darle una instrucción como esta:

   ```
   Lee Elmo_carga_ob_multianio/docs/modelo_multianio_dch.md, en especial la sección
   "Guía de revisión". Sigue sus pasos 1 a 4 con el código de las dos carpetas
   (Elmo_carga_ob_multianio y Elmo_battery_swapping_multianio). Solo lectura: no
   ejecutes nada, porque no están los datos. Entrega:
   (1) las diferencias entre el documento y el código, con archivo y línea;
   (2) las respuestas a las preguntas guía;
   (3) una propuesta de plan para llevar a multiaño el modelo anual de transporte.
   ```

### Objetivo

Entender el modelo multiaño y los algoritmos que funcionaron, para decidir cómo extender
a multiaño el modelo anual de transporte. No hace falta revisarlo todo: los pasos 1 a 4
de abajo son lo esencial.

### Qué revisar, en orden

| Paso | Tema (sección del documento) | Zip | Archivos |
|---|---|---|---|
| 1 | Visión general (0, 2, 6 y 7) | cualquiera | este documento |
| 2 | Formulación (anexos A y B) | OB para el A, BS para el B | `src/optimization/functions.py`: clases `OptSets` (conjuntos), `OptParameters` (parámetros), `BoundRules` (variables y cotas), `ConstraintRules` (restricciones) y `ObjectiveRules` (objetivo). `ConstraintRules.build_all_constraints` muestra qué restricciones se activan y con qué opciones. La tabla de equivalencias está al final de esta guía |
| 3 | Descomposición por año (3.1–3.4) | OB | `src/optimization/decomposition/`: `driver.py` (bucle, preprocesamiento de capacidad, cota operacional, estabilización), `year_block.py` (bloque anual, copias del estado, decisiones únicas), `passes.py` (pasos hacia adelante y hacia atrás), `cuts.py` (cortes de Benders, fortalecidos y de factibilidad), `day_blocks.py` (arranque por días), `respaldo.py`. Diseño en `implementacion_descomposicion_carga_ob.md` y `degradacion_descomposicion_mccormick.md`; bitácora de pruebas en `CONTEXTO_pendientes_descomposicion.md` |
| 4 | Configuración exacta de las corridas DCH (3.1, 5) | OB y BS | OB: `cola_dch_ob_v4.sh` (todas las opciones de Benders, la mejora y el certificado). BS: `cola_dch_2c.sh`, `cola_dch_05c.sh`, `cola_dch_lshaped_macro.sh` y las demás `cola_dch_*.sh`. En los dos, `setup.py` es el punto de entrada y la ayuda de cada opción explica qué hace |
| 5 | Heurísticas y cierre del gap (3.5–3.8) | OB | `mejorar_ub.py`, `certificar_inversiones.py`, `candidatas.py`, `diagnostico_año.py`, `lshaped.py` y `src/optimization/decomposition/lshaped.py`, `benders_dias.py` |
| 6 | L-shaped y particularidades de battery swapping (2.7, 6) | BS | `lshaped_entero_swap.md` (diseño), `resultados_lshaped.md` (resultados), `cola_swap_v4_algoritmo.md` (esquema anterior y DP), `src/optimization/decomposition/degradacion_dp.py` y `dp_degradacion.py` (DP de degradación), cortes de cargas (3.9): `cota_cargas_lhd.py`, `corte_cargas_op.py`, `cortes_cargas.py`, `diagnostico_corte_swaps.py` y `agregar_corte_cargas` en `src/optimization/decomposition/lshaped.py`; cortes de Dantzig-Wolfe en `src/optimization/decomposition/cortes_dw.py` |
| 7 | Generación de columnas (7.2) | BS | `docs/generacion_columnas.md`, `cg_propia.py` y `src/optimization/decomposition/cg_propia.py` |
| 8 | Postprocesamiento (4) | ambos; comparación en BS | `consumer.py` (informe de la solución), `json_plotter.py` (gráficos estándar), `escribir_parameters.py`; en BS, `cost_comparisons/`: `comparar_swap_ob_v4.py`, `ajuste_energia_swap.py`, `redimensionar_gen_bess.py`, `resultados_finales.py`, `plot_despacho_swap_vs_ob_v4.py`; `scripts/indicador_holgura.py` y `scripts/figuras_holgura.py`; resultados en `cost_comparisons/resultados_finales/` (empezar por su `LEEME.md`) |
| 10 | Battery swapping en DCH (3.10, 5.5) | BS | `src/optimization/decomposition/benders_dias.py` (macrobloques por estación), `recortar_holgada.py` y `diagnostico_holgada.py` (recorte), `cortes_capacidad.py` y `agregar_corte_produccion` en `src/optimization/decomposition/lshaped.py` (cortes de capacidad y producción), `lshaped.py` (opciones `--por_estacion`, `--corte_produccion`, `--cortes_capacidad`), `cost_comparisons/resultados_finales_dch.py` (comparación DCH) |
| 9 | Datos de entrada (1) | OB y BS | `data/Resultados_finales_tesis/DCH/armar_datos_dch.py` (OB) y `data/Tesis_final/DCH/armar_datos_dch_swap.py` (BS). Los datos mismos no van en los zips |

### Preguntas guía

Lo que interesa que salga de la revisión:

1. ¿La formulación de los anexos A y B coincide con el código? Anotar cada diferencia
   con archivo y línea.
2. Para cada elemento del modelo minero, ¿cuál es su análogo en el modelo anual de
   transporte, o no existe? Los elementos son:
   - estado entre años (stock e incrementos);
   - decisiones únicas del año 1;
   - degradación de flota y reemplazos;
   - pausas;
   - cobro de potencia.
3. En transporte, ¿la operación diaria es casi exacta en su relajación (como OB) o
   tiene integralidad fuerte (como BS)? La respuesta decide el camino (7.4).
4. ¿Qué trampas de 7.3 aplican a transporte?
5. ¿Qué partes del código se podrían reutilizar y cuáles habría que reescribir?
6. Propuesta de plan de implementación para transporte, siguiendo 7.4.

### Si la revisión la hace Claude u otro asistente

- Tomar este documento como punto de partida. El código sirve para verificarlo.
- Solo leer: no ejecutar nada, porque no hay datos ni salidas.
- Entregar tres cosas:
  1. las diferencias entre el documento y el código, con archivo y línea;
  2. las respuestas a las preguntas guía;
  3. una propuesta de plan para transporte.
- El código tiene variantes apagadas por defecto que no forman parte de los resultados
  (pipeline cíclico, tope de SOC al intercambio, carga con la capacidad del año, entre
  otras). Las controlan variables de entorno `ELMO_*` y opciones de `setup.py`. Las
  corridas usaron los valores por defecto, salvo lo que fija cada cola.

### Equivalencias entre el documento y el código

| Documento | Código | Documento | Código |
|---|---|---|---|
| $X$, $\Delta X$ | `X`, `Delta_X` | $Y$, $Z$ | `Y`, `Z` |
| $N^C$, $\Delta N^C$ | `N_chargers`, `Delta_N_chargers` | $Z^{ch}$ | `Z_charge` |
| $n^{ssee}$, $P^{max}$ | `n_ssee_k`, `P_max_k` | $\sigma^+$, $\sigma^-$ | `StartCharge`, `EndCharge` |
| $P^{pot}$ | `P_pot` | $P$, $B$ | `P`, `B` |
| $R$ | `R` | $P^{red}$ | `P_red` |
| $\bar b$, $D$ | `b_bar`, `D` | $P^{gen}$, $C$ | `P_gen`, `Curt_g` |
| $S$ (energía anual, OB) | `S` | $P^{bat}$, $A$ | `P_bat`, `A_h` |
| $N$, $w$ | `N_ciclos`, `w_deg` | $G$, $H$ | `G_g`, `H` |
| $Z^{sw}$ | `Z_swap` | $V$ | `Sv` |
| $B^s$ | `B_s` | $S$ (cargadas, BS), $X^{dch}$ | `S`, `X_dch` |
| $\tilde P$ | `P_bbar_zagg` | $X^{ini}$, $W$ | `X_ini`, `W` |
| $N^{bay}$, $N^{bat}$ | `N_bays`, `N_batteries` | $N^{pool}$, $F$ | `N_batteries_total`, `n_battery_fleet` |
| $E$, $N^{tot}$ | `EnergyConsumed`, `N_total` | $Z^R$ | `Z_repl` |

| Restricción | Código |
|---|---|
| A1–A3 | `link_station_stock`, `apertura_solo_primer_anio`, `link_charger_stock` |
| A4, A5 | `max_n_chargers`, `ssee_discreta` |
| A7, A8 | `state_unique_elhd`, `between_shifts_elhd` |
| A9, A10 | `station_existence_constraint`, `charger_limit` |
| A11, A12 | `charge_state`, `min_charge_duration` |
| A13, A14 | `max_power`, `max_installed_capacity` |
| A15–A18 | `battery_soc`, `battery_lower`/`battery_upper`, `battery_boundary`, `battery_soc_break_symmetry` |
| A19, A20 | `daily_production`, `production` |
| A21, A22 | `maint_stop_all`, `maint_no_charge`, `meal_g1_no_travel_group1`, `meal_g2_no_travel_group2` |
| A23–A25 | `power_balance`, `grid_limit`, `power_cost_peak_limit` (en BS, `power_peak_limit`) |
| A26–A29 | `gen_limit`, `bess_power_upper`/`lower`, `bess_soc_balance`/`init`/`cyclic`, `bess_soc_upper`/`lower` |
| A30–A33 | `s_def`, `n_ciclos_link` o `build_mccormick_degradation_block`, `d_y_fade`, `b_y_link`, `b_y_link_lower`, `b_y_replace` |
| B1–B3 | `link_bays_stock`, `link_battery_stock`, `max_n_bays`, `bays_le_chargers`, `max_chargers_per_bay_constr`, `max_batteries_per_bay_constr`, `chargers_le_batteries` |
| B4–B7 | `state_unique_elhd_swap`, `between_shifts_elhd_swap`, `station_existence_constraint_swap`, `max_swaps`, `bays_limit_swap`, `total_swaps`, `maint_stop_all` |
| B8–B12 | `battery_soc_swap`, `battery_soc_swap_update_1`–`4`, `p_bbar_zagg_*`, `battery_boundary_swap`, `battery_energy_conservation` |
| B13–B19 | `charging_duration_rule`, `charger_limit_swap`, `max_installed_capacity_swap`, `peak_power_swap`, `inventory_discharged_batteries_rule`, `inventory_charged_batteries_rule`, `CI_S`, `CI_X_dch`, `CB_general` |
| B20 | `power_balance` |
| B21–B25 | `n_batteries_total_def`, `n_battery_fleet_def`, `energy_consumed_def`, `build_mccormick_degradation_block` (o `n_total_def` y `n_ciclos_link`), `d_y_fade`, `z_repl_upper1`/`2`, `z_repl_lower`/`2` |
| B26 | `min_charges_cut` (cota en `min_charges_n`) |

---

## 1. Datos de entrada

### 1.1 Fuentes

La instancia DCH se armó sobre la plantilla de la mina modelo (caso solo red). Sobre
ella se reemplazó:

| Bloque | Fuente |
|---|---|
| Plan minero: metas por punto y año, asignación punto–LHD, LHD–estación, turnos, flota activa por año | Plan DCH 2035–2045 |
| 288 puntos de extracción con distancias, parámetros del LH518B | Campaña DCH de septiembre (modelo anual) |
| Cargador y coeficiente de degradación por tasa de carga | Mina modelo |
| Límites físicos | DCH: 3 estaciones, 2 bahías por estación, 1 cargador por bahía, hasta 1.500 kW de subestación por estación, 5.000 kW de red |
| Costos unitarios | Mina modelo |
| Consumo energético | Modelo de consumo por ruta de la campaña de septiembre (energía por viaje entre cada punto y su punto de descarga) |

### 1.2 Plan minero

11 años y 3 macrobloques; cada macrobloque tiene su estación y 4 LHD.

| Año | 1 | 2 | 3 | 4 | 5 | 6 | **7** | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| LHD activos | 1 | 5 | 9 | 12 | 12 | 12 | **12** | 11 | 9 | 4 | 2 |
| Meta diaria (t/día) | 1.074 | 14.728 | 33.712 | 44.805 | 48.543 | 50.583 | **54.441** | 44.051 | 31.097 | 10.592 | 431 |

El año 7 es el de mayor producción y el que dimensiona todo: subestación, cargadores y
factibilidad.

**Extracción por calle.** En cada año y macrobloque, cada LHD activo extrae solo su
calle. La meta del macrobloque se reparte pareja entre las calles de sus LHD activos y,
dentro de cada calle, pareja entre sus puntos. En el plan original la meta se repartía
entre todos los puntos del macrobloque. Con menos LHD que calles, eso dejaba a un LHD
con dos calles.

**Cotas por punto.** El número de visitas a cada punto en el día va entre
`⌊meta/carga⌋ − 1` y `⌈meta/carga⌉ + 2`.

### 1.3 Equipos, infraestructura y costos

| Parámetro | Valor |
|---|---|
| LHD LH518B | 18 t por balde, factor de llenado 0,84, 54,8 t de peso, 16,2 km/h, batería de 482 kWh, SOC mínimo 20 %, eficiencias de carga y descarga 0,95 |
| Estación | apertura 219.495 USD, bahía 234.851, espacio de cargador 35.228 |
| Cargador por tasa | 0,5C: 241 kW / 241.000 USD; 1C: 482 kW / 482.000 USD; 2C: 964 kW / 964.000 USD |
| Subestación | módulos de 500 kW a 302 USD/kW, hasta 1.500 kW por estación |
| Red | tope de 5.000 kW, energía a precio fijo, potencia contratada a 10 USD/kW-mes, cobrada en horas de punta de los días 91 a 244 |
| Degradación | pérdida γ = 0,0188 (0,5C), 0,0273 (1C) o 0,0451 (2C) kWh por ciclo equivalente; piso del 80 % (385,6 kWh); reemplazo a 299.000 USD por batería |
| Descuento | 8 % anual |
| Generación y BESS | están en el modelo, pero con tope cero (caso solo red) |

### 1.4 Tiempo

- **Días representativos.** Días del año 15, 105, 196 y 288, con peso 365/4 = 91,25
  cada uno. El cobro de potencia aplica a los días 105 y 196.
- **Intervalos.** Δt = 8 min, 180 por día; el día parte a las 09:00.

### 1.5 Pausas (esquema DCH)

Son las mismas reglas del modelo anual de la campaña de septiembre:
- **Mantenciones:** 10:04–12:26, 16:04–17:34, 22:04–00:26 y 04:04–05:34. En ellas los
  LHD no operan ni cargan.
- **Colación:** 14:04–15:04 y 02:04–03:04, escalonada. Los LHD pares no extraen en la
  primera mitad y los impares en la segunda.
- **Entre turnos:** 2 turnos de 10 h.
- **Carga libre:** se puede cargar fuera de mantención, también en horas de operación.

Se dejó prevista la opción de operar en colación en años puntuales. No se usa: si el
caso es factible sin operar en colación, se usa el esquema que no lo permite.

### 1.6 Battery swapping

La instancia BS usa los mismos datos de producción, extracción por calle, turnos y
flota. Cambia la estación, con estos límites: 3 estaciones, 4 bahías, 2 baterías y 2
cargadores por bahía y 1.500 kW. Los cargadores y la degradación son los del swap de la
mina modelo. Se corrió con **2 días representativos por año** (15 y 196), no con 4 como
OB.

---

## 2. Modelo monolítico

Índices: estación k, LHD i, punto de extracción j, año y, día representativo d,
intervalo t.

Esta sección resume el modelo. La formulación completa (conjuntos, parámetros, variables,
objetivo y cada restricción numerada) está en el **anexo A** para carga on-board y en el
**anexo B** para battery swapping.

### 2.1 Decisiones

| Tipo | Variables | Nota |
|---|---|---|
| Inversión por año | X_{k,y} (estación abierta), N^C_{k,y} (cargadores), con sus incrementos ΔX, ΔN^C | stock = stock del año anterior + incremento; se paga el incremento |
| Inversión única | n^ssee_k (módulos de 500 kW, P^max_k = 500·n^ssee_k), generación G_g, almacenamiento H | se deciden una vez, en el año 1, para todo el horizonte |
| Potencia contratada | P^pot_y | P^red ≤ P^pot en horas de punta de los días 91–244 |
| Batería | b̄_y (capacidad al inicio del año), D_y (al final), N^ciclos_y, S_y (energía anual), R_y (reemplazo, binaria) | ver 2.4 |
| Operación | Y_{i,j,y,d,t} (extrae en j), Z_{i,y,d,t} (detenido), Z^ch_{k,i,y,d,t} (conectado al cargador), P_{k,i,y,d,t} (potencia de carga), B_{i,y,d,t} (energía en batería), inicio y fin de carga, P^red_{y,d,t} | binarias salvo P, B y P^red |

### 2.2 Inversión

- **Acumulación.** Stock = stock del año anterior + incremento, para estaciones y
  cargadores.
- **Apertura en el año 1 o nunca.** Una estación se abre al inicio del horizonte o no
  se abre. Así el monolítico y la descomposición, donde X queda fija, resuelven el mismo
  problema. En DCH las 3 estaciones quedan abiertas desde el año 1, aunque ese año opera
  un solo LHD.
- **Cargadores:** N^C_{k,y} ≤ bahías máximas · X_{k,y}.
- **Subestación:** se compra en módulos de 500 kW.

### 2.3 Operación diaria

- **Estado único.** En cada intervalo el LHD está en exactamente un estado:
  `Z + Σ_k Z^ch + Σ_j Y = 1`.
- **Energía en batería.**

  ```
  B_t = B_{t−1} + η_c·Σ_k P·Δt − (energía de los viajes)/η_d
  bmin·b̄_y ≤ B ≤ b̄_y
  ```

  La energía es cíclica en el día (la final es igual a la inicial). Dentro de cada
  estación se rompe la simetría de la energía inicial.
- **Carga.**
  - Cada sesión de carga dura al menos 2 intervalos.
  - `P ≤ Z^ch·p_cargador`
  - `Σ_i Z^ch ≤ N^C`
  - `Σ_i P ≤ P^max_k`
- **Balance de potencia y red.**
  - `P^red + generación + BESS = Σ P`
  - `P^red ≤ 5.000 kW`
- **Producción.** `Σ Y·(t por viaje) ≥ meta del día`, con las cotas por punto de 1.2.
- **Pausas** según 1.5.

### 2.4 Degradación

Se modela a nivel de flota, con recursión año a año:

```
S_y            = 91,25 · Σ_d Σ_t Σ_i P·Δt          (energía anual cargada)
N^ciclos_y · b̄_y = S_y / n_LHD,y                   (bilineal)
D_y            = b̄_y − γ · N^ciclos_y
b̄_y ≤ D_{y−1} + 0,3·b_max·R_y
b̄_y ≥ D_{y−1} − b_max·R_y
b̄_y ≥ b_max·R_y
b̄_1 = b_max;   D_y ≥ 0,8·b_max
```

- **Bilineal.** El producto N^ciclos·b̄ se reemplaza por su envolvente de McCormick,
  con cotas de N^ciclos calculadas por año. Así el modelo queda MILP puro. Es una
  relajación, y su residuo crece con los años (ver 4.2 y 8).
- **Capacidad físicamente consistente.** Las dos últimas desigualdades fuerzan
  `b̄_y = D_{y−1}` sin reemplazo y `b̄_y = b_max` con reemplazo. Al principio b̄ solo
  tenía cota superior, y el modelo "tiraba" capacidad sin costo. En la descomposición,
  el primer paso hacia adelante bajaba la batería ~90 kWh por año, cuando la degradación
  real es ~4–5 kWh, y forzaba reemplazos.

### 2.5 Objetivo

Todo se descuenta al 8 % al año en que ocurre:

```
min  energía de red (P^red · precio · Δt · 91,25)
   + estaciones y cargadores (sobre los incrementos)
   + subestación (una vez, año 1)
   + generación y BESS (inversión una vez, O&M anual; 0 en el caso red)
   + potencia contratada (12 · 10 · P^pot_y)
   + reemplazos (n_LHD,y · R_y · 299.000)
```

### 2.6 Comportamiento del monolítico

| Instancia | Configuración | Resultado |
|---|---|---|
| DCH, 11 años (versión anterior del modelo) | Gurobi, 10 h | Mejor solución 9.951.146, cota 8.730.375, gap 12,27 %. La generación estaba apagada por un error de datos, así que no vale como resultado |
| DCH, 6 años | 600 s, parámetros por defecto | 9.295.910 |
| DCH, 6 años | 2.260 s, enfoque en la cota (MIPFocus 3) | sin solución factible: 1 nodo, 1,55 millones de iteraciones simplex |
| Mina modelo BS, 2 años | 1 h | gap 1,04 %, no cierra |
| Mina modelo BS, 4 años | 2 h | sin solución factible |

Conclusión: a partir de unos 4 años el monolítico solo sirve para validar, es decir,
para reconstruir y pulir soluciones con las enteras fijas. No sirve como método de
solución.

### 2.7 Diferencias entre carga on-board y battery swapping

Los dos modelos comparten la estructura: inversión por año, días representativos,
pausas, potencia, producción, descuento y degradación de flota. Cambia cómo se carga la
energía, y con eso cambian la inversión, la degradación, la relajación y el algoritmo
que funciona.

**Modelo**

| Aspecto | Carga on-board | Battery swapping |
|---|---|---|
| Dónde se carga | El LHD se conecta a un cargador y queda detenido mientras carga | La batería se carga en el rack de la estación. El LHD intercambia batería en un intervalo (8 min) |
| Inversión | Estaciones, cargadores (1 por bahía), subestación | Estaciones, bahías, cargadores (bahías ≤ cargadores ≤ baterías), **pool de baterías en la estación**, subestación |
| Potencia de carga | Continua: `P ≤ Z^ch·p_cargador`, `Σ P ≤ P^max_k` | Bloque fijo: cada batería carga a la potencia del cargador durante un número fijo de intervalos, y Σ (baterías cargando)·p_cargador ≤ 500·n^ssee_k |
| Energía | Una batería por LHD, con carga y descarga | La batería del LHD solo se descarga y el intercambio la repone. La estación lleva inventarios de baterías cargadas y descargadas, cargas iniciadas e intercambios. Se permite intercambiar a cualquier SOC sobre el mínimo |
| Mantención | El LHD no opera ni carga | El LHD no opera ni intercambia, pero la estación sigue cargando baterías |
| Flota que se degrada | Una batería por LHD activo | Pool + una batería por LHD activo |
| Ciclos | `N^ciclos·b̄ = S/n_LHD`, con S la energía **cargada** | `N^ciclos·b̄ = E/(n° de baterías de la flota)`, con E la energía **consumida** repartida entre toda la flota. Cada batería del pool baja los ciclos por batería, así que comprar baterías retrasa o evita el reemplazo |
| Reemplazo | n_LHD·R·299.000 | (n° de baterías de la flota)·R·299.000. El producto R × flota se linealiza con McCormick (4 desigualdades) |
| Escala numérica | No hace falta | Ciclos en miles, energía en MWh y producción en kt, para evitar problemas numéricos en las descomposiciones |

**Consecuencias para los algoritmos**

| | Carga on-board | Battery swapping |
|---|---|---|
| Relajación de la operación | Casi exacta: LP = MILP salvo ~700 USD en 40 días, y los días MILP cierran en segundos | Floja: cada carga es una batería completa (el LP hace 16,35 cargas en un día y el entero al menos 17). Días con gaps de 13–39 % a 900 s |
| Cota inferior | La cota operacional basta | Cota operacional + **corte de cargas** por día: n° de cargas ≥ ⌈energía mínima del día / energía de una carga⌉. El resto lo suben los cortes enteros del L-shaped o el certificado |
| Cota superior | El paso hacia adelante de Benders da buenas soluciones; casi todos los casos cierran en la primera iteración | El paso hacia adelante es miope frente a la degradación: pospone baterías y cae bajo el piso. Hacen falta una DP de degradación, fijar los reemplazos y la mejora de la operación |
| Trampa del reemplazo | Aparece en DCH 2C: compra cargadores en vez de reemplazar antes (sección 5.2) | Fuerte: la relajación de R abarata reemplazar toda la flota. Las soluciones sin reemplazo resultaron 14–21 % más baratas |
| Mejor algoritmo | Nested Benders con cota operacional (sección 6) | L-shaped entero con solución inicial de la DP y mejora final |
| Gaps finales (mina modelo, 8 casos) | 0,43–1,40 % | 0,93–3,92 % (promedio 2,53 %) |

**Resultados de la comparación en la mina modelo**

Los costos de BS están ajustados a la energía real de los días y redimensionados
(generación, BESS y red). Es la comparación justa: el costo total del modelo directo no
se compara.

| Caso | BS ajustado (USD) | OB (USD) | BS vs OB |
|---|---|---|---|
| 1 base, norte, 0,5C, red | 3.101.299 | 3.083.536 | +0,58 % |
| 2 base, norte, 0,5C, red + gen + BESS | 2.419.370 | 2.383.673 | +1,50 % |
| 3 base, norte, 0,5C, red + gen | 2.486.503 | 2.487.888 | −0,06 % |
| 4 escalada, norte, red (OB a 1C) | 3.801.192 | 5.399.073 | −29,6 % |
| 5 escalada, norte, red + gen + BESS (OB a 1C) | 3.019.295 | 4.567.768 | −33,9 % |
| 6 base, centro, 0,5C, red + gen + BESS | 2.648.802 | 2.608.964 | +1,53 % |
| 7 base, norte, 1C, red + gen + BESS | 2.711.776 | 2.935.693 | −7,6 % |
| 8 base, norte, 2C, red + gen + BESS | 4.908.561 | 5.657.803 | −13,2 % |

- **Producción base a 0,5C: empate (±1,5 %).** OB paga el reemplazo de la flota (~754
  mil USD). BS no reemplaza, pero paga las baterías del pool (~800 mil USD en el caso
  1). Estaciones, bahías y cargadores cuestan casi lo mismo en las dos.
- **Producción escalada: BS gana 30–34 %.** OB a 0,5C es infactible, porque los LHD no
  alcanzan a cargar sin quitarle tiempo a la extracción. Tiene que pasar a 1C, con
  cargadores más caros y más degradación. BS carga en el rack y sigue a 0,5C. Es el
  mismo motivo por el que OB 0,5C es infactible en el año 7 de DCH.
- **2C: la ventaja de −13,2 % está inflada.** OB 2C opera a ~500 kW (un módulo de
  subestación, ≈1C), pero su degradación usa el coeficiente de 2C.
- **Holgura operativa (producción base, años de régimen).** BS deja ~19 % del tiempo
  disponible del LHD inactivo, porque el intercambio dura 8 min. OB queda en ~0 %:
  con 2 cargadores para 4 LHD, tiene que cargar fuera de las detenciones.
- **DCH:** BS gana 26,0 % a 1C y 11,7 % a 2C, y a 0,5C solo BS es factible (sección 5.5). Los gaps de BS en 0,5C y 1C siguen abiertos (15,5 % y 13,9 %).

---

## 3. Algoritmos

### 3.1 Esquema usado en DCH (OB)

```
preprocesamiento de capacidad ─► cota operacional (cota inferior)
        ─► Nested Benders por año (cota superior)
              ├─ gap < 1 %  ─► fin
              └─ gap ≥ 1 %  ─► mejora de la operación (2 pasadas) ─► certificado
                                    └─ si el certificado no cabe ─► L-shaped entero (cota inferior)
                                                                   + diagnóstico del año crítico
                                                                   + búsqueda local (cota superior)
```

Parámetros usados en Benders:
- tolerancia de gap de 1 %, hasta 6 h;
- años resueltos como MILP con 120 s y enfoque en soluciones factibles (MIPFocus 1);
- arranque por días con 120 s y gap de 5 % por día, 4 días en paralelo;
- cortes fortalecidos con 300 s;
- caja de estabilización de ±1, libre una iteración de cada 3;
- corte automático tras 4 iteraciones sin mejora de la cota superior.

### 3.2 Preprocesamiento de capacidad

Antes de iterar se resuelve el año de mayor meta con todo el estado heredado libre y
objetivo `min n^ssee_k`, por estación y en suma. La cota dual del MILP, redondeada hacia
arriba, da cotas inferiores válidas para la subestación. Se imponen en el año 1.

En DCH da al menos 2 módulos (1.000 kW) en cada estación, en 1,5 a 5,5 min.

Sin este paso, la necesidad del año pico viaja hacia atrás un año por iteración, con
cortes de factibilidad. Medido a 6 años:

| | Cortes de factibilidad | Tiempo de una iteración | Cota LP del monolítico |
|---|---|---|---|
| Sin preprocesamiento | 9 | 2.060 s | — |
| Con preprocesamiento, cota agregada y propagación de los cortes directo al año 1 | 0 | 942 s | +3,2 % |

### 3.3 Cota operacional

Se resuelve el monolítico con la inversión entera y las binarias de operación relajadas.
En DCH son 2,5 millones de binarias relajadas. Es la cota inferior del método.

- **Tiempo:** 64 min en 1C y 70 min en 2C.
- **Calidad.** En OB la operación diaria es casi exacta en su relajación, así que esta
  cota queda pegada al óptimo.
- **Por qué hace falta.** Benders con cortes LP no puede superar la relajación LP de sus
  subproblemas: en todas las pruebas la cota inferior de Benders quedó clavada en el LP
  del monolítico. En la mina modelo a 10 años la cota operacional dio 2,15 M contra 1,59
  M del LP, y el gap del mejor incumbente bajó de 26,6 % a 1,2 %.
- **Otro uso.** Su inversión sirve como centro de la estabilización (3.4).

### 3.4 Nested Benders por año

Se basa en Lara et al. (2018), en versión determinista y sin retiro de activos.

- **Bloques.** Un subproblema por año, construido una sola vez. Entre iteraciones solo
  cambian los valores heredados.
- **Estado entre años.**
  - Adyacente (año y−1 → y): cargadores acumulados y capacidad de batería.
  - De decisión única: subestación, generación y BESS. Se deciden en el año 1 y en los
    demás años se fijan por igualdad. Su costo se cobra solo en el bloque del año 1.
  - Las estaciones quedan fijas.
  - Cada valor heredado entra como copia continua fijada por igualdad. El dual de esa
    igualdad forma el corte.
- **Paso hacia adelante.** Cada año se resuelve como MILP con el estado del año
  anterior → cota superior.
  - **Arranque por días.** El punto de partida de cada año se arma en 4 pasos:
    1. cada día con la infraestructura libre;
    2. el máximo de la infraestructura entre días;
    3. cada día con esa infraestructura fija;
    4. pulido LP del año.
  - **Pulido.** La solución completa de los 11 años se pule con el monolítico (enteras
    fijas, LP de las continuas). Esto reconcilia energía y degradación entre años.
- **Paso hacia atrás.** Se resuelve el LP de cada año en el estado del paso hacia
  adelante → corte de Benders hacia el año anterior.
  - Opcionalmente el corte se **fortalece** con una evaluación lagrangiana (Lara
    ec. 63).
  - La cota que se usa es la dual del lagrangiano, con piso en el valor del LP, y no la
    mejor solución. Así el corte vale aunque el MILP termine por tiempo.
- **Factibilidad.** Si un año sale infactible, se genera un corte de factibilidad desde
  un LP con holguras. Si el corte toca solo variables de decisión única, se envía
  directo al año 1.
- **Estabilización de caja** (Box-step, Göke et al. 2024).
  - Las enteras de inversión quedan a ±1 del centro, que es la inversión de la cota
    operacional. Una iteración de cada 3 va sin caja.
  - Sin caja, el paso hacia adelante oscila o cae en cascadas de factibilidad (7.1).
- **Robustez.** En cada mejora se guarda un respaldo con la solución, los cortes y la
  cota, para retomar sin recalcular la cota operacional.

### 3.5 Heurísticas de cota superior

| Heurística | Qué hace | Resultado |
|---|---|---|
| Arranque por días | Ver 3.4 | imprescindible para tener soluciones de cada año |
| Pulido | LP del monolítico con todas las enteras fijas | reconcilia los años |
| Mejora de la operación | Con la inversión fija, reoptimiza los días como MILP más largos, partiendo de la operación actual, y pule. Pasada 1: años 1–11, 600 s por día. Pasada 2: años 3–9, 900 s | DCH 2C: −957 USD en la pasada 1, nada en la 2. BS: −0,7 a −1,7 % |
| Búsqueda local de candidatas | Combina calendarios de reemplazo con los cargadores del año crítico. Filtra por la trayectoria de capacidad con la energía de la solución actual, ordena por costo estimado y evalúa cada una con los días MILP y pulido | DCH 2C: −7,3 % |
| DP de degradación (BS) | Programación dinámica exacta sobre (año, baterías del pool, bahías, capacidad). Repara la trayectoria de inversión de la cota operacional para que no cruce el piso de degradación | BS caso 2: gap 10,4 % → 5,3 % en 33 min |
| Fijar reemplazos (BS) | Fija R en el valor de la cota operacional en todos los años | evita la trampa de reemplazar toda la flota |

### 3.6 Certificado por enumeración de inversiones

1. Se arma el pool: todas las inversiones enteras cuya cota operacional queda bajo
   `(1 − gap objetivo)·cota superior`, con un tope de 5.000.
2. Para cada candidata, con sus enteras fijas, se corre un Benders LP por (año, día)
   hasta converger y se obtiene su cota.
3. Cota inferior certificada = mínimo entre el umbral y la menor cota de las
   candidatas.
4. Opcionalmente, cada candidata que queda viva se evalúa como solución.

Funciona cuando el gap ya es de ~5 % o menos. Con más gap el pool se llena y no
certifica. En DCH 2C (gap de 12,3 %):
- 5.000 candidatas bajo el umbral, pool incompleto;
- 14 evaluadas, todas vivas con la operación relajada;
- ninguna con operación entera factible.

Se cortó a mano.

### 3.7 L-shaped entero multicorte

Se basa en Laporte y Louveaux (1993), con cortes alternados de Angulo et al. (2016) y
branch-and-cut con cortes perezosos (*lazy*).

- **Maestro.** Contiene:
  - toda la inversión de los 11 años;
  - presupuestos de potencia contratada por año y de energía por (año, día);
  - la degradación con McCormick;
  - una variable de costo θ por (año, día).

  Son 44 subproblemas independientes, resueltos en paralelo.
- **Fase 1.** Maestro continuo y luego entero, con cortes LP de los 44 días. En DCH
  2C: 2,1 h, cota 10.163.742 (todavía bajo la cota operacional), 1.584 cortes.
- **Fase 2.** Branch-and-cut entero. En cada solución entera del maestro:
  1. se agregan cortes LP;
  2. si no separan, se agregan cortes enteros locales por día. Usan la mejor cota del
     MILP del día, nunca la mejor solución (Riley et al. 2025).

  Además, en los días sin operación factible se agrega un "corte de piso".
- **Paso final.** Mejora de la operación con días a 600–900 s.
- **Validación.** La solución se reconstruye en el monolítico.

Resultado en DCH 2C:
- cota inferior 10.267.820 (+4.072 sobre la cota operacional), con 66 cortes LP y 29
  enteros;
- la cota superior no cambió: gap 12,30 %, en 3,6 h.

La cota inferior ya estaba bien; el gap era de cota superior (sección 5.2).

### 3.8 Diagnóstico de un año

Se toma la solución actual, se cambian los cargadores de un año y se resuelven sus días
MILP en tres escenarios:
- A: capacidad de batería y potencia contratada de la solución;
- B: potencia contratada libre;
- C: potencia libre y batería nueva.

Sirve para separar las causas de infactibilidad: degradación, potencia contratada o
falta de cargadores.

### 3.9 Cortes, cotas y desigualdades válidas

Además de los cortes propios de cada algoritmo, se probaron desigualdades que suben la
cota inferior sin cortar soluciones factibles. Casi todas atacan el mismo problema de
BS: la relajación de la operación hace cargas fraccionarias (16,35 en vez de 17 en un
día) y cada carga es una batería completa. En OB no hacen falta, porque su operación es
casi exacta en la relajación.

Notación: $U = (1-\beta)\,\bar b$ es la energía máxima que repone un intercambio, y
$\sum X^{ini}$ es el número de cargas del día ($t < T$).

| Corte | Forma | Por qué es válido | Uso y resultado | Código |
|---|---|---|---|---|
| Benders (optimalidad) | $\theta_{y-1} \ge \Phi^{LP} + \mu(\hat x - x)$ | dualidad LP del bloque del año | Nested Benders (3.4). La cota nunca supera el LP | `cuts.py`, `passes.py` |
| Benders fortalecido | igual, con $\Phi$ = cota dual del lagrangiano en el $\mu$ del LP, con piso $\Phi^{LP}$ | dualidad lagrangiana; vale aunque el MILP corte por tiempo | OB; ganancia chica | `cuts.py` |
| Factibilidad | desde un LP con holguras; va directo al año 1 si solo toca decisiones únicas | lema de Farkas | el preprocesamiento los eliminó (9 → 0) | `cuts.py`, `passes.py` |
| Preprocesamiento de capacidad | $n^{ssee}_k \ge \lceil$cota dual$\rceil$ por estación y en suma | es una relajación del año pico | 3.2 | `driver.py` |
| **Cargas por día** | (B26): $\sum X^{ini} \ge \lceil E^{min}_{yd} / ((1-\beta) b^{max}) \rceil$ | SOC cíclico; la energía solo se repone con intercambios | Activo por defecto en BS. Coincide con el techo del mínimo LP en los 16 días verificados. Cota operacional: +1,5 % a 4 años; 1,97 → 2,00 M a 10 años. Calcular el mínimo con la cota dual del MILP del día da lo mismo salvo en 2 años, y no mueve la cota | `functions.py` (`min_charges_n`, `min_charges_cut`); verificación en `cortes_cargas.py` |
| **Cargas por LHD** | $\sum X^{ini} \ge$ óptimo del MIP chico $\min \sum_i s_i$ s.a. $U s_i \ge \sum_{j \in i} e_{ij} v_j$, visitas $v_j$ enteras en sus cotas y producción ≥ meta | la batería de cada LHD es cíclica y solo la reponen sus intercambios | Más fuerte que el agregado cuando la carga se reparte entre LHD. En el certificado se usa con la capacidad de cada candidata: explica buena parte de lo que la cota del certificado le gana a la operacional (2,113 M contra 2,072 M en un caso de la mina modelo) | `cota_cargas_lhd.py` |
| Cargas por LHD, condicional al reemplazo | $\sum X^{ini} \ge n^{sinR}_{yd} - (n^{sinR}_{yd} - n^{nom}_{yd}) \sum_{s \le y} R_s$ | $n^{nom}$ (batería nueva) vale siempre; $n^{sinR}$ (capacidad máxima sin reemplazo hasta $y$) vale si no hubo reemplazo | se agrega a la cota operacional de BS, como paso 0 del esquema | `corte_cargas_op.py` |
| **Cargas en función de la capacidad** | $\sum X^{ini} \ge g_d(\bar b_y)$, con $g_d$ la envolvente convexa del escalón $n_d(B)$; una fila por arista. Versión por niveles: binarias $w_{y,l}$ que dan el escalón exacto en las soluciones enteras | $n_d(B)$ no crece con $B$ y es válido para cada $B$ | L-shaped de BS, en las fases 1 y 2. Con este corte la cota de la Fase 1 queda a 1–2,5 % de la final | `lshaped.py` (`agregar_corte_cargas`, `preparar_niveles`) |
| Pisos de intercambios por LHD | $\sum_t Z^{sw}_{it} \ge \lceil E^{min}_i / U \rceil$ | mismo argumento, LHD por LHD | no superan al corte agregado (probado a 10 años) | `diagnostico_corte_swaps.py` |
| Enteros locales (L-shaped) | 3.7 | mejor cota del MILP del día | L-shaped en OB y BS | `lshaped.py` |
| Piso de continuas (L-shaped) | con las enteras de $\hat x$, el día solo tiene operación factible si una continua $x_k$ (p. ej. la potencia contratada) es al menos $\phi_k$: $x_k \ge \phi_k - (\phi_k - lo_k)\,\text{dist}(x_I, \hat x_I)$ | mínimo de una copia continua con las enteras fijas | DCH 2C; sin ganancia visible | `lshaped.py`, `benders_dias.py` |
| Dantzig-Wolfe | $\theta_{y-1} \ge L(\pi; \hat x) + \sum_r a_r \pi_r (x - \hat x)$, con $L$ la cota lagrangiana de la generación de columnas del año | $L(\pi; x) \le$ DW $\le$ MILP para todo $\pi$ | solos no suben la cota | `cortes_dw.py` |
| Rotura de simetría | (A18): $B_{i'0} \le B_{i0}$ dentro de cada estación | en OB los LHD de una estación son intercambiables en el SOC inicial | Activa en OB. En BS, la precedencia de intercambios por índice **no es válida**, porque los LHD tienen frentes y metas distintas; está desactivada | `functions.py` |
| Variantes de la cota operacional | dejar enteras $Z^{sw}$ o la operación de la estación | — | +0,01 % de cota con más de 4 veces el tiempo; descartadas | `cota_operacional_variantes.py` |

**Lectura para transporte.** Lo que más rindió fue acotar el **número de recargas** de
cada día por debajo:
1. primero de forma agregada;
2. después por vehículo;
3. por último como función de la capacidad de batería, que es una decisión de primera
   etapa.

Si la operación de transporte tiene recargas discretas, el análogo directo es un mínimo
de recargas por día, calculado con la energía mínima para cumplir el servicio.

### 3.10 Battery swapping en DCH: macrobloques, recorte y cortes de capacidad

DCH tiene 3 estaciones y 12 LHD, contra 1 estación en la mina modelo. Para BS en DCH se
agregaron tres piezas al L-shaped (3.7):

- **Macrobloques por estación.**
  - Cada subproblema (año, día) se parte en un macrobloque por estación (la estación y
    sus 4 LHD) y un bloque de red.
  - La extracción del día, el balance de potencia y la energía consumida pasan al
    maestro como presupuestos.
  - Cambiar la inversión de una estación solo obliga a volver a resolver sus
    macrobloques.
- **Recorte de una solución holgada** (búsqueda local de cota superior). Parte de una
  solución factible con inversión de sobra y la baja paso a paso sin perder
  factibilidad. Los movimientos, en orden:
  1. *escalera*: diseños uniformes de nivel creciente; el primero factible es la
     solución inicial;
  2. *construir*: nada de equipo antes del primer año con LHD activos en la estación;
  3. *tope*: bajar de a uno el máximo de bahías, baterías o cargadores;
  4. *subestación*: un módulo menos;
  5. *punta*: potencia contratada desde 0, subiéndola solo si algún día la necesita;
  6. *retrasar*: una compra un año más tarde;
  7. *macro*: el algoritmo anual por macrobloques dentro del multiaño. Cada (año, día,
     estación) se resuelve con su infraestructura libre; luego se toma el máximo por
     estación y año, acumulado entre años, y se evalúa.

  Cada candidata se completa con el maestro, que elige los reemplazos. Se vuelven a
  resolver solo los días que cambian, y la candidata se acepta si baja la cota superior.
- **Cortes de capacidad y de producción por estación** en el maestro.
  - **Capacidad.** Para cada estación y año se prueba, macrobloque por macrobloque, que
    con n baterías (o bahías, o cargadores) no hay operación entera, con todo lo demás
    libre. Si no la hay, $N_{k,y} \ge n+1$ vale para todo diseño.
  - **Producción.** Acota lo que puede producir cada estación según sus baterías y la
    capacidad de batería; la suma tiene que cubrir la meta.
  - **Por qué hacen falta.** La relajación compraba muy pocas baterías y cargadores: en
    1C, el 80 % del gap eran baterías.

**Solución final por tasa:**
- 0,5C: la del recorte con macrobloques (el L-shaped no la mejoró);
- 1C: L-shaped con macrobloques y mejora final;
- 2C: L-shaped.

La cota inferior de cada tasa es la mayor de sus corridas L-shaped.

---

## 4. Postprocesamiento: de la solución a resultados, gráficos y tablas

El postprocesamiento convierte la solución del optimizador en cifras y figuras que se
pueden comparar. Se aplica igual a OB y a BS, y no reoptimiza nada salvo los ajustes de
la comparación (4.5), que son LP con la inversión fija.

### 4.1 Qué se guarda de cada corrida

- **Solución.** El valor de cada variable de inversión, degradación y operación, por
  LHD, punto, año, día e intervalo.
- **Parámetros del escenario.** Dependen solo de los datos, así que se regeneran cuando
  una solución mejorada trae solo las variables.
- **Registro de la corrida:**
  - cota superior e inferior por iteración;
  - tiempos de cada etapa;
  - un resumen final con la cota superior, la inferior, el gap y de dónde salió cada
    cota.
- **Respaldo** para retomar la corrida (3.4).

### 4.2 Validación de la solución

Antes de reportar una solución se hacen tres controles:

1. **Reconstrucción.** Se arma el monolítico completo y se fijan todas las enteras de
   la solución. Se resuelve el LP de las continuas y su costo tiene que coincidir con
   la cota superior informada (tolerancia relativa 1e-6). Se revisan los residuos fila
   por fila: el máximo medido fue ~1e-8.
2. **Degradación exacta.** Se recalcula la capacidad con la ecuación bilineal y se
   compara con la envolvente de McCormick (2.4). Si la diferencia es grande, la
   capacidad informada de los años tardíos no es confiable.
3. **Reconciliación del informe.** Los términos de costo tienen que sumar el costo total,
   y este tiene que ser la cota superior. Si algo no cuadra, el proceso se detiene.

### 4.3 Informe de cada solución

Por solución se genera un informe con estas tablas:

| Tabla | Contenido |
|---|---|
| Costos | energía de red, inversión en estaciones y cargadores, subestación, generación (inversión y O&M), BESS (inversión y O&M), potencia de punta, reemplazos y total, todo en valor presente; energía comprada a la red en el horizonte (MWh) |
| Balance energético | energía cargada, consumida y perdida en eficiencias |
| Extracción | toneladas por punto y día frente a la meta |
| Operación diaria | viajes, ciclos equivalentes y energía por LHD |
| Generación y BESS | capacidad instalada, energía generada, vertida, cargada y descargada |
| Degradación | por año: capacidad al inicio y al final, % de la nominal, ciclos equivalentes, reemplazo y su costo |

Se calculan también dos indicadores de control:
- **Energía de tracción por tonelada (kWh/t).** Se compara con el modelo anual de la
  campaña de septiembre (0,184 kWh/t). Así se detectó la corrida descartada de 5.4.
- **Potencia comprada en la ventana de punta.** Tiene que quedar bajo la potencia
  contratada de cada año.

**Criterio horario.** Un intervalo pertenece a la ventana de punta según su **punto
medio**. El modelo y todos los lectores de resultados tienen que usar el mismo criterio:
cuando no coincidían, la ventana de las figuras quedaba corrida respecto de la del
modelo.

### 4.4 Gráficos estándar de una solución

Para cada año y día representativo se generan:
- despacho de potencia: red, generación, carga y descarga del BESS frente a la demanda
  de carga;
- potencia de carga frente al precio;
- energía de cada LHD con su estado en cada intervalo (extrayendo, cargando, detenido),
  sobre las ventanas de pausa;
- extracción por punto frente a la meta;
- capacidad de generación y estado de carga del BESS.

Para el horizonte completo:
- capacidad de batería por año (inicio y fin), con los reemplazos marcados;
- ciclos equivalentes por año.

### 4.5 Ajustes para comparar OB con BS

El costo total que entrega el modelo no se compara directamente entre tecnologías. Se
aplican dos ajustes, en este orden. Ninguno cambia las cotas del modelo.

1. **Energía real de cada intercambio (solo BS).**
   - **El problema.** El modelo BS cobra cada recarga como un bloque completo, del SOC
     mínimo al 100 %. La batería que llega con un intercambio necesita en realidad
     (capacidad del año − energía de llegada)/η. El modelo despacha 16–20 % más energía
     que la real.
   - **Asignación.** Las baterías descargadas se asignan a las cargas en orden de
     llegada. La asignación es cíclica: las primeras cargas del día son de los últimos
     intercambios del día anterior. Para resolverlo se recorre el día dos veces y se usa
     la segunda vuelta.
   - **Sobrante.** En cada bloque solo cuentan los intervalos necesarios; el resto es
     sobrante. El sobrante se descuenta en orden de mérito: primero la red, luego la
     descarga del BESS (la energía queda guardada) y al final la generación (quedaría
     vertida).
   - **Déficit.** Si la energía real supera el bloque (pasa a 1C y 2C con baterías casi
     nuevas), la carga se alarga a potencia del cargador y esa energía se compra a la
     red, que es lo conservador.
   - **Control.** Sobrante − déficit tiene que ser igual a la energía de los bloques
     menos la real. Con solo red, el resultado reproduce exactamente la energía real.
2. **Redimensionamiento de generación, BESS y red (las dos tecnologías).**
   - **El LP.** Con la carga real fija (horario incluido) y la inversión en estaciones,
     subestación y reemplazos también fija, un LP vuelve a elegir solar, eólica, BESS,
     compra de red y potencia contratada. Usa las mismas ecuaciones y costos del
     modelo, incluido el valor residual lineal de generación y BESS.
   - **Por qué hace falta.** El modelo BS dimensiona la generación para la demanda
     inflada (caso 2: 1.004 kW de solar contra 876 kW de OB).
   - **Control obligatorio.** Con la carga de la propia solución, el LP tiene que
     reproducir lo que pagó el modelo. Puede costar menos si el dimensionamiento no era
     óptimo, nunca más. En los 8 casos de la mina modelo la diferencia fue 0, en OB y en
     BS.

El costo comparable de BS es el del ajuste 2. OB pasa por el mismo LP con su propia
carga.

### 4.6 Figuras y tablas de la comparación

| Producto | Qué muestra | Cómo se arma |
|---|---|---|
| Costo total desagregado | BS ajustado frente a OB, por término de costo; en los 8 casos y por grupo (base norte, tasa de carga, ubicación, producción) | Del informe de costos (4.3) después de los ajustes (4.5) |
| Despacho lado a lado | Red, generación y BESS frente a la carga, OB y BS, en un año de régimen (año 5) y días de invierno, verano y otoño | La atribución de la carga a cada fuente sigue un orden de mérito (BESS descargando → renovable → red), que es un supuesto de presentación, no un resultado del modelo. Se verifica que la red no supere la potencia contratada en punta |
| Estado de la flota | Cada LHD por intervalo: extrayendo, cargando o intercambiando, detenido, sobre las pausas | Directo de las variables de estado |
| Baterías en carga | Baterías cargando a la vez frente a los cargadores instalados y a las horas de sol | Directo de la operación de la estación |
| Circulación de baterías (BS) | Recorrido de cada batería de la estación: en LHD, descargada, cargando, cargada | El modelo no identifica baterías, solo inventarios. La identidad se reconstruye en orden de llegada desde los inventarios y las cargas iniciadas, y se contrasta intervalo a intervalo con los inventarios del modelo |
| Uso de cargadores (OB) | Una fila por cargador con el LHD conectado y su nivel de potencia (máxima, parcial, conectado sin potencia) | El modelo solo cuenta los LHD conectados (Σ Z^ch ≤ N^C), no qué cargador usan. Cada sesión se asigna al primer cargador libre por orden de inicio, lo que nunca usa más cargadores que los instalados |
| Degradación | Capacidad por batería año a año, OB y BS | Del informe de degradación (4.3) |
| Infraestructura | Estaciones, bahías, cargadores, baterías del pool, subestación, generación y BESS por año | De las variables de inversión |
| Tablas LaTeX | Costos e infraestructura por grupo de casos | Del mismo resumen, con coma decimal |
| Planilla resumen | Una hoja por bloque: resumen, costos (5 variantes por caso: modelo, ajuste de energía, ajustado, OB modelo, OB comparable), infraestructura, infraestructura anual, energía, degradación, informes completos y notas | Junta todo lo anterior, con las definiciones y los controles en la hoja de notas |

### 4.7 Indicador de holgura operativa

Mide cuánto tiempo disponible le queda a cada LHD: el que podría usar para producir más
o absorber contingencias.

- **Partición.** Cada intervalo de cada LHD cae en exactamente una categoría:
  - operación (asignado a extracción);
  - carga (OB) o intercambio (BS) fuera de las detenciones;
  - detención operacional cargando;
  - detención operacional sin cargar;
  - **holgura**: inactivo fuera de las detenciones.

  Se verifica que la partición sea exacta (residuo nulo) y que no haya operación dentro
  de las detenciones.
- **Agregación.**
  - Se calcula por LHD, día y año.
  - Se promedia en los años de régimen (los de meta máxima), con los pesos de los días.
  - Se expresa en % del tiempo disponible (24 h menos las detenciones). También se
    reporta el peor día.
- **Variante OB: holgura potencial.** Es la holgura que habría si toda la carga se
  hiciera a potencia máxima: energía cargada / potencia efectiva. Corrige que el modelo
  pueda cargar a potencia parcial.
- **Estación BS:**
  - utilización de los cargadores = Σ baterías cargando / (cargadores × intervalos);
  - baterías cargadas en inventario (media y mínimo).
- **Productos:**
  - barras apiladas del uso del tiempo por LHD, con el peor día marcado;
  - cargadores en uso por hora, OB y BS;
  - mapa de calor de la holgura por LHD y día;
  - tabla LaTeX.

### 4.8 Estado en DCH

DCH usa el mismo proceso de la mina modelo (4.2–4.6), adaptado a sus diferencias:
- el año de régimen es el 7;
- BS tiene 2 días por año y OB 4;
- no hay generación ni BESS.

Produce una planilla resumen, el costo total desagregado y, para cada tasa, la
comparación, el despacho y los gráficos estándar de las dos tecnologías. El indicador de
holgura (4.7) no se calculó para DCH.

---

## 5. Corridas DCH y resultados (caso red, batería 482 kWh, costo fijo, carga libre)

| Tasa | Cota superior (USD) | Cota inferior (USD) | Gap | Origen de la cota inferior | Tiempo | Estado |
|---|---|---|---|---|---|---|
| 0,5C | — | — | — | — | — | **Infactible en el año 7** |
| **1C** | **8.217.027** | 8.208.719 | **0,10 %** | cota operacional | 3,3 h | Benders en 1 iteración |
| 2C (Benders) | 11.708.379 | 10.263.748 | 12,34 % | cota operacional | 5,3 h + 1,3 h de mejora | certificado cortado (pool incompleto) |
| **2C (final)** | **10.853.115** | 10.267.820 | **5,39 %** | L-shaped | +3,6 h L-shaped, +3,75 h búsqueda local | solución de la búsqueda local |

### 5.1 1C

- Preprocesamiento: 1,5 min.
- Cota operacional: 64 min.
- Iteración 1: 2,2 h, incluidos 37 min de pulido.

La primera iteración ya queda a 0,10 % de la cota. La solución tiene:
- **Cargadores:** 1 por estación (estación 1 desde el año 1, estación 2 desde el año 2,
  estación 3 desde el año 3) y un 2º cargador en las tres estaciones en el año 7.
- **Subestación:** 1.000 kW por estación.
- **Reemplazo:** en el año 7.
- **Potencia contratada:** solo en los años 4 (3 kW) y 7 (197 kW).

Desglose del costo (valor presente):

| Término | USD | % |
|---|---|---|
| Estaciones, bahías y cargadores | 3.864.381 | 47,0 % |
| Reemplazo de baterías | 2.093.564 | 25,5 % |
| Energía de red (21.917 MWh) | 1.406.119 | 17,1 % |
| Subestación | 838.889 | 10,2 % |
| Potencia de punta | 14.075 | 0,2 % |
| **Total** | **8.217.027** | |

### 5.2 2C: dónde estaba el gap

La solución de Benders compraba el 2º cargador de las estaciones 1 y 2 en el año 7, por
1,555 M USD descontados (casi todo el gap). Ese año la batería llegaba degradada (430 de
482 kWh).

Diagnóstico del año 7 (3.8) con 1 cargador por estación:

| Escenario | Resultado |
|---|---|
| A: batería y potencia de la solución | infactible |
| B: potencia contratada libre | infactible |
| C: batería nueva | factible en menos de 40 s, 54.271 USD por día |

La causa es la degradación, no la falta de cargadores.

La búsqueda local probó 19 combinaciones: 6 calendarios de reemplazo ({5,7}, {5,7,11},
{5,7,10}, {4,7}, {6,8}, {5,8}) × 3 configuraciones de cargadores del año 7. Descartó 10
por la trayectoria de capacidad y evaluó 6. La mejor solución:
- **Reemplazos:** años 5, 7 y 11.
- **Cargadores:** 1 por estación en todo el horizonte.
- **Subestación:** 1.000 kW por estación.
- **Potencia contratada:** 371 kW en el año 7.
- **Costo:** 10.853.115 USD (−7,3 %).

**Lectura.** El paso hacia adelante de Benders es miope frente a la degradación.
Resuelve el año 7 con la batería que le dejaron los años anteriores y compra cargadores.
No ve que reemplazar antes es más barato. La misma miopía apareció en BS (7.3).

### 5.3 0,5C

Con las pausas DCH, 2 cargadores de 241 kW por estación no recargan 4 LHD a 54.441 t
por día, ni siquiera operando en colación. Se probó cada año del plan por separado como
un modelo de un año:
- 0,5C es factible en los años 2–6 y 8, e infactible en el 7;
- 1C y 2C son factibles en el año 7 sin operar en colación.

### 5.4 Corrida descartada: pausas de la mina modelo

La primera corrida usó las pausas de la mina modelo en vez de las de DCH. Dio 0,5C con
3.697.729 USD y gap 0,044 %.

Se descartó porque la energía de tracción quedaba en 0,160–0,168 kWh/t, contra 0,184
del modelo anual de septiembre. El modelo concentraba la producción en los puntos
baratos: la mitad más barata quedaba al 128–148 % de su meta. Se pasó a las pausas DCH
y a la extracción por calle.

### 5.5 Battery swapping en DCH

Mismos datos, pausas DCH y carga libre (1.6), con **2 días representativos por año**
(15 y 196) en vez de 4. El algoritmo está en 3.10.

| Tasa | Cota superior BS (USD) | Cota inferior BS (USD) | Gap BS | BS ajustado frente a OB |
|---|---|---|---|---|
| 0,5C | 6.392.693 | 5.402.526 | 15,5 % | OB infactible |
| 1C | 6.290.483 | 5.418.602 | 13,9 % | −26,0 % |
| 2C | 9.804.695 | 9.636.679 | 1,7 % | −11,7 % |

- **Cómo se comparan.** Igual que en la mina modelo (4.5): ajuste de energía real de
  cada intercambio y LP de red y punta (en DCH no hay generación ni BESS), con los
  mismos controles. Cada tecnología pasa por el LP con sus propios días: 4 en OB y 2 en
  BS.
- **0,5C.** Solo BS es factible: carga en el rack y no depende de que el LHD alcance a
  cargar.
- **Gaps de 0,5C y 1C.** Siguen abiertos, así que la ventaja de BS en 1C todavía no está
  certificada. El gap de 1C es casi todo de baterías del pool: la relajación compra muy
  pocas (3.10).
- **Corrección del reemplazo OB.** El informe de la solución OB calculaba el costo de
  reemplazo con la flota completa, no con los LHD activos del año como el modelo. La
  comparación lo corrige.

---

## 6. Esquema que dio mejores resultados

### Carga on-board (DCH y mina modelo)

1. **Preprocesamiento de capacidad** del año pico.
2. **Cota operacional**, como cota inferior y como centro de la caja.
3. **Nested Benders**, configurado así:
   - cortes fortalecidos;
   - caja de ±1, libre una iteración de cada 3;
   - arranque por días;
   - pulido en cada iteración;
   - corte tras 4 iteraciones sin mejora, hasta 6 h.
4. **Si el gap queda bajo 1 %:** fin.

   En la mina modelo, los 8 casos OB quedaron entre 0,43 % y 1,40 %, casi todos
   cerrando en la primera iteración. DCH 1C quedó en 0,10 %.
5. **Si no:** mejora de la operación en dos pasadas y certificado, con gap objetivo
   `min(0,5 %, 0,8·gap)`.
6. **Si el certificado no cabe (gap sobre ~5 %):**
   - L-shaped entero, para confirmar la cota inferior;
   - diagnóstico del año que concentra la inversión;
   - búsqueda local sobre reemplazos y cargadores.

### Battery swapping (mina modelo)

**L-shaped entero**, con la solución inicial de la DP de degradación y una mejora final
de la operación. Gap promedio de 2,53 % en 8 casos, contra 3,24 % del esquema anterior
(Nested Benders + DP + mejora + certificado).

En BS la cota inferior necesita el corte de cargas por día. Las iteraciones de Benders
no aportaron, porque el paso hacia adelante pospone baterías.

---

## 7. Qué se probó y qué resultó (para el nivel de transporte)

El nivel de transporte tiene hoy solo el modelo anual. Lo que sigue es lo probado en
minería (OB y BS), con su resultado y lo que sugiere para un multiaño de transporte.
Las recomendaciones son condicionales: nada de esto se probó sobre el modelo de
transporte.

### 7.1 Pruebas

| # | Técnica | Dónde se probó | Resultado | Para transporte |
|---|---|---|---|---|
| 1 | Monolítico multiaño | DCH 6 y 11 años; mina modelo 2 y 4 años | 11 años: 12,3 % en 10 h. 6 años: sin solución en 38 min. 4 años: sin solución en 2 h. 2 años: 1,04 % en 1 h | Útil para validar en 2–3 años y para pulir con las enteras fijas, no como método |
| 2 | Cota LP del monolítico | Todas | La cota inferior de Benders queda clavada en ella. En un año de BS: LP 270 mil contra MILP 1,01 M | No sirve como cota inferior si la operación tiene integralidad fuerte |
| 3 | **Cota operacional** (inversión entera, operación relajada) | OB y BS | OB: a 0,10 % del óptimo en DCH 1C. Mina modelo 10 años: 2,15 M contra 1,59 M del LP | **Lo primero a implementar.** Medir antes cuánto se aleja la operación diaria de su relajación (LP vs MILP en unos días). Si es casi exacta, como en OB, basta para cerrar |
| 4 | Preprocesamiento de capacidad del año pico | OB y BS, 2 y 6 años | Cortes de factibilidad 9 → 0, tiempo −54 %, cota LP del monolítico +3 % (OB) y +30 % (BS) | Implementar si hay activos que dimensiona el año pico (subestación, potencia) |
| 5 | Nested Benders con cortes LP | OB y BS | Solo baja la cota superior; la inferior no se mueve | Útil como generador de soluciones factibles, no de cotas |
| 6 | Cortes fortalecidos (lagrangianos) | OB | Ganancia chica (p. ej. 745,9 → 760,9 en el año 11). Hay que usar la cota dual, no la mejor solución | Barato de agregar |
| 7 | Estabilización de caja ±1 en torno a la cota operacional | Mina modelo BS, 4 años | Gap 6,11 % → 3,90 %, 5 pasos de mejora y 0 nulos. Una caja que crece tras pasos nulos oscila. Sin caja: paso hacia adelante miope y cascadas de factibilidad (4 reinicios en 3 h) | Usar caja fija, centrada en la inversión de la cota operacional, con una iteración libre cada 3 |
| 8 | Estabilización por nivel (*level-set*) | Mina modelo BS, 4 años | 2,75 % contra 2,42 % de la caja; la inversión no se movía | Descartada |
| 9 | Híbrido: 3 iteraciones de Benders y luego el monolítico con esa solución de partida | DCH, 6 años | Mejor resultado antes de la cota operacional: 7,04 M, gap 17 %, −24 % frente al monolítico solo. Enfocar el MILP en soluciones (MIPFocus 1) fue mejor que en la cota (MIPFocus 3) | Solo si no hay una buena cota inferior |
| 10 | Arranque del año armado por días | OB y BS | Imprescindible: el MILP anual casi nunca mejora el arranque por días (2 de 43 años). Con 4 días en paralelo: 177 → 79 s por año | Si el año de transporte tiene días separables, armar el arranque por días |
| 11 | Pulido LP con las enteras fijas | Todas | Reconcilia energía y degradación entre años. Sin redondear las enteras antes de entregar la solución de partida, el solver la rechazaba por residuos de ~5e-5 | Redondear las enteras y pulir antes de entregar una solución de partida |
| 12 | McCormick para ciclos × capacidad | OB y BS | MILP puro, pero el residuo crece con el año (de −180 a −6.500 en DCH) | Si se modela la degradación así, verificar el residuo y evaluar la solución con la ecuación exacta |
| 13 | Capacidad físicamente consistente (igualdad b̄_y = D_{y−1} sin reemplazo) | OB y BS | Eliminó reemplazos espurios. En una variante de BS con carga parcial hubo que relajarla, y la cota de la Fase 1 quedó a 6–9 % de la solución (antes 2–4 %) | Escribir igualdades físicas, no solo cotas, y medir su efecto en la cota |
| 14 | Mejora de la operación (días más largos con la inversión fija) | OB y BS | BS: −0,7 a −1,7 %. OB: casi nada | Barato; correrlo siempre al final |
| 15 | Certificado por enumeración de inversiones | OB y BS | Cierra si el gap es ~5 % o menos. Con más gap el pool se llena (5.000) | Útil si el espacio de inversión es chico |
| 16 | **L-shaped entero multicorte** | BS (8 casos), DCH 2C OB | BS: gap 3,24 % → 2,53 % en una corrida de 5–7 h. DCH 2C: cota +4 mil, sin solución nueva | **Mejor algoritmo cuando la operación tiene integralidad fuerte.** Hay que decidir qué copias del estado van enteras (cortes enteros) y cuáles continuas (gap de dualidad) |
| 17 | Generación de columnas propia (Dantzig-Wolfe + branch-and-price) | Mina modelo BS | 2 años: óptimo demostrado en 19 min, donde el monolítico y Benders quedaban en 1,04 %. Con generación renovable, la cota DW queda bajo la operacional | Prometedora si el maestro queda chico (7.2) |
| 18 | Dantzig-Wolfe automático (solver GCG) | Mina modelo | En pausa: el subproblema de precios no cierra, y la cota quedó bajo la operacional (1,77 M en 19 h contra 1,80 M) | No recomendado, salvo con un subproblema de precios a medida |
| 19 | DP de degradación para reparar la primera solución | BS | Caso 2: 2,66 M → 2,52 M en 33 min (gap 10,4 % → 5,3 %) | Si la degradación es de flota y de baja dimensión, una DP exacta es mucho mejor que dejarla a los cortes |
| 20 | Fijar los reemplazos | BS | Evitó soluciones que reemplazaban toda la flota (−14 a −21 %) | Ver 7.3 |
| 21 | Búsqueda local de candidatas | DCH 2C OB | −7,3 % de la cota superior en 3,75 h | Útil cuando un año concentra la inversión |
| 22 | Corte de cargas por día (detalle de todos los cortes en 3.9) | BS | Cota inferior +1,5 % a 4 años; 1,97 → 2,00 M a 10 años | El análogo en transporte serían cotas mínimas de recargas o de energía por día |

### 7.2 Por qué Dantzig-Wolfe funciona en transporte y aquí no

El Dantzig-Wolfe por vehículo del modelo de transporte funciona muy bien allá:
- el maestro es chico;
- la flota homogénea se agrega en un solo subproblema de precios.

En la minería caen al maestro el balance de potencia por intervalo, los cargadores, la
potencia contratada, las metas y la degradación. Con bloques por vehículo y día, el
maestro quedó en el 13,7 % de las filas en OB y en el 36,5 % en BS.

Para un transporte multiaño, una opción natural es combinar dos niveles:
- un bloque o una variable de costo por (año, día), como en el L-shaped;
- el Dantzig-Wolfe por vehículo dentro de cada día.

### 7.3 Trampas encontradas (válidas para cualquier multiaño)

1. **Miopía del paso hacia adelante frente a la degradación.** Cada año ve solo la
   batería que le dejaron.
   - BS: posponía baterías, caía bajo el piso en el año 10 y compraba 5 baterías de
     golpe.
   - DCH 2C: compraba cargadores en el año pico en vez de reemplazar antes.

   Soluciones probadas: DP de degradación, fijar reemplazos y búsqueda local. Queda
   pendiente meter la DP como parte exacta del costo futuro.
2. **La relajación LP del reemplazo lo abarata** en los cortes. Benders cae en
   soluciones que reemplazan toda la flota.
3. **Cotas sin igualdad física** (b̄_y ≤ D_{y−1}) dejan "tirar" capacidad sin costo.
4. **Inversiones de decisión única** (subestación, generación, BESS):
   - se cobran solo en el bloque del año 1, porque si no cada bloque las vuelve a pagar;
   - sus cortes de factibilidad van directo al año 1.
5. **Apertura diferida.** Si el monolítico puede abrir estaciones después y la
   descomposición no, resuelven problemas distintos y sus gaps no son comparables. Hay
   que fijar la misma regla ("año 1 o nunca") en los dos.
6. **Cortes enteros con la mejor cota del MILP**, nunca con la mejor solución si el
   MILP no cerró.
7. **Cortes perezosos con coeficientes grandes** (~1e6, duales de holguras
   penalizadas). El solver no hace cumplir violaciones chicas. Hay que contar como
   separador solo lo que viola más de una tolerancia relativa al objetivo (1e-5).
8. **Cambios pendientes en el solver.** En Gurobi, copiar o relajar un modelo no ve los
   cambios de cotas pendientes hasta llamar a `update()`. Una corrida completa de BS
   usó cotas corruptas por esto.
9. **Errores silenciosos en los datos.**
   - Los perfiles de generación devolvían 0 cuando el día representativo no estaba en
     la tabla: la generación quedó apagada en corridas enteras.
   - Había metas en puntos que ningún equipo visita ese año: el modelo era infactible
     sin aviso.

   Conviene validar los datos año por año antes de correr.
10. **Escala numérica.** Las filas que suman un día entero dan coeficientes de ~1e5 en
    el maestro de Dantzig-Wolfe. Escalar ciclos a miles y energía a MWh lo resolvió.

### 7.4 Orden sugerido para transporte

1. **Extender el modelo anual a un monolítico multiaño:**
   - stock e incrementos de inversión;
   - inversiones de decisión única;
   - degradación con igualdades físicas;
   - descuento.

   Validarlo en 2–3 años contra el anual: un año del multiaño debe dar lo mismo que el
   anual.
2. **Medir LP vs MILP de la operación** en algunos días con la inversión fija. Eso
   decide el camino:
   - **operación casi exacta en su relajación** (como OB): cota operacional, Nested
     Benders y mejora de la operación;
   - **integralidad fuerte** (como BS): L-shaped entero con presupuestos por (año, día),
     cortes mínimos de recargas o energía por día y una heurística dedicada a la
     degradación (DP).
3. **Antes de iterar:** preprocesamiento de capacidad del año pico y cota operacional.
4. **Heurísticas de cota superior,** en este orden:
   - arranque por días;
   - pulido con las enteras fijas;
   - mejora de la operación;
   - búsqueda local de reemplazos.
5. **Certificado o L-shaped** solo cuando el gap ya sea de ~5 % o menos.
6. **Postprocesamiento desde la primera corrida:** validación por reconstrucción y
   reconciliación de costos (4.2). Si se va a comparar contra otra tecnología o contra
   el modelo anual, conviene definir desde el inicio los ajustes de energía y el
   redimensionamiento (4.5), con su control de reproducción.

---

## 8. Limitaciones y pendientes

- **DCH 2C queda en 5,39 %.**
  - La cota inferior (L-shaped) es casi la cota operacional.
  - Falta cota superior: la búsqueda local exploró solo reemplazos y cargadores del año
    7.
  - El corte de piso del L-shaped no mostró ganancia en lo que se corrió.
- **BS en DCH:** los gaps de 0,5C (15,5 %) y 1C (13,9 %) siguen abiertos; en 1C son casi todo baterías del pool. BS usó 2 días por año y OB 4.
- **Residuo de McCormick.** Crece con el año: en los años tardíos la solución satisface
  la envolvente, no el bilineal exacto.
- **Idea abierta:** un corte "al menos un cargador en cada estación con LHD activos ese
  año". Varias candidatas del certificado de 2C eran infactibles por eso.
- **Integrar la DP de degradación al costo futuro de Benders:** costo futuro = valor
  exacto de la degradación + resto aproximado con cortes. Así el paso hacia adelante
  vería el costo de dejar la batería corta, y no habría que fijar los reemplazos.

---

## Anexo A. Formulación completa: carga on-board

Es el modelo con el que se corrió DCH: pausas DCH, carga libre, caso solo red. Las
restricciones de generación y BESS se incluyen porque forman parte del modelo, aunque en
el caso red sus topes son cero. Las unidades son físicas: kW, kWh, t, USD y h.

### A.1 Conjuntos

| Símbolo | Descripción |
|---|---|
| $\mathcal{K}$ | estaciones (DCH: 3) |
| $\mathcal{I}$ | LHD (DCH: 12) |
| $\mathcal{J}$ | puntos de extracción (DCH: 288) |
| $\mathcal{Y}$ | años del horizonte, con $y_1$ el primero, $y^-$ el año anterior a $y$ y $\mathcal{Y}^+ = \mathcal{Y}\setminus\{y_1\}$ |
| $\mathcal{D}$ | días representativos $\{15, 105, 196, 288\}$; $\mathcal{D}^{pot} = \{d \in \mathcal{D} : 91 \le d \le 244\}$, los días con cobro de potencia |
| $\mathcal{T} = \{1,\dots,T\}$ | intervalos del día ($T = 180$); $\mathcal{T}_0 = \{0\} \cup \mathcal{T}$ incluye el estado inicial |
| $\mathcal{T}^{HP}$ | intervalos de punta: el punto medio del intervalo cae entre las 18:00 y las 22:00 |
| $\mathcal{T}^{mant}$ | intervalos de mantención |
| $\mathcal{T}^{col,1}, \mathcal{T}^{col,2}$ | primera y segunda mitad de cada ventana de colación |
| $\mathcal{T}^{et}$ | intervalos entre turnos |
| $\mathcal{I}^{1}, \mathcal{I}^{2}$ | grupos de colación: LHD pares e impares |
| $\mathcal{J}_{iydt}$ | puntos que el LHD $i$ puede visitar en $(y,d,t)$, según la asignación del plan minero |
| $\mathcal{K}_i$ | estaciones donde puede cargar el LHD $i$ (la de su macrobloque) |
| $\mathcal{P}$ | pares $(i, i')$ de LHD consecutivos de una misma estación, con $i < i'$ (rotura de simetría) |
| $\mathcal{G}$, $\mathcal{H}$ | tecnologías de generación (solar, eólica) y almacenamiento (un BESS) |

### A.2 Parámetros

| Símbolo | Descripción | DCH |
|---|---|---|
| $\Delta t$ | duración del intervalo (h) | 8/60 |
| $\omega$ | peso de cada día representativo, $365/\lvert\mathcal{D}\rvert$ | 91,25 |
| $r$, $\delta_y$ | tasa de descuento; factor $\delta_y = (1+r)^{-\text{pos}(y)}$, con $\text{pos}(y_1) = 1$ | 8 % |
| $m_{jy}$ | meta diaria del punto $j$ en el año $y$ (t/día) | plan |
| $q_{ij}$ | toneladas por intervalo asignado: carga del balde × factor de llenado × viajes por intervalo | 18 × 0,84 × $n_{ij}$ |
| $e_{ij}$ | energía de tracción por intervalo asignado (kWh): consumo del viaje × duración × viajes por intervalo | modelo de consumo |
| $\underline{v}_{jy}, \overline{v}_{jy}$ | visitas mínimas y máximas al punto: $\lfloor m_{jy}/q_j \rfloor - 1$ y $\lceil m_{jy}/q_j \rceil + 2$ | |
| $b^{max}$ | capacidad nominal de la batería (kWh) | 482 |
| $\beta$ | SOC mínimo | 0,2 |
| $\eta^c, \eta^d$ | eficiencias de carga y descarga | 0,95 |
| $\gamma$ | pérdida de capacidad por ciclo equivalente (kWh/ciclo) | 0,0188 / 0,0273 / 0,0451 |
| $B_L, B_U$ | piso de capacidad ($0{,}8\,b^{max}$) y capacidad nominal | 385,6 / 482 |
| $n_y$ | LHD activos en el año $y$ | 1–12 |
| $c^{R}$ | costo de reemplazo por batería (USD) | 299.000 |
| $\rho$ | capacidad que puede recuperar un reemplazo en la cota superior | $0{,}3\,b^{max}$ |
| $c^X_k, c^{bay}_k, c^{esp}_k$ | apertura de estación, bahía y espacio de cargador (USD) | 219.495 / 234.851 / 35.228 |
| $c^C, p^C$ | costo (USD) y potencia (kW) del cargador | según la tasa |
| $\overline{N}_k$ | bahías máximas de la estación (1 cargador por bahía) | 2 |
| $c^{ssee}_k$, $\pi$ | costo de subestación (USD/kW) y tamaño del módulo (kW) | 302 / 500 |
| $\overline{P}^{red}$ | potencia máxima de la red (kW) | 5.000 |
| $c_{ydt}$ | precio de la energía (USD/kWh) | perfil fijo |
| $c^{pot}$ | cargo por potencia contratada (USD/kW-mes) | 10 |
| $c^{inv}_g, c^{op}_g, p^{max}_g, \overline{G}_g, \alpha_{gdt}, L_g$ | generación: inversión (USD/kW), O&M (USD/kW-año), potencia por unidad, unidades máximas, factor de planta y vida útil | $\overline{G}_g = 0$ |
| $c^{inv}_h, c^{op}_h, p^{max}_h, \eta_h, a^{min}_h, a^{max}_h, \overline{H}, L_h$ | BESS: inversión y O&M por unidad, potencia y energía por unidad, eficiencia, unidades máximas y vida útil | $\overline{H} = 0$ |
| $\phi_g, \phi_h$ | factor neto de inversión con valor residual lineal: $\delta_{y_1} - (1 - \lvert\mathcal{Y}\rvert/L)\,\delta_{y_{\lvert\mathcal{Y}\rvert}}$ | |

### A.3 Variables

| Variable | Dominio | Descripción |
|---|---|---|
| $X_{ky}$, $\Delta X_{ky}$ | $\{0,1\}$ | estación abierta y su apertura en el año $y$ |
| $N^C_{ky}$, $\Delta N^C_{ky}$ | $\mathbb{Z}_+$ | cargadores instalados y comprados en el año $y$ |
| $n^{ssee}_k$; $P^{max}_k$ | $\mathbb{Z}_+$; $\ge 0$ | módulos de subestación y su potencia (decisión única) |
| $G_g$, $H$ | $\ge 0$ | unidades de generación y de BESS (decisión única) |
| $P^{pot}_y$ | $\ge 0$ | potencia contratada (kW) |
| $R_y$ | $\{0,1\}$, $R_{y_1} = 0$ | reemplazo de la flota de baterías al inicio del año |
| $\bar b_y$ | $[B_L, B_U]$, $\bar b_{y_1} = b^{max}$ | capacidad por batería al inicio del año |
| $D_y$ | $[B_L, B_U]$ | capacidad por batería al final del año |
| $S_y$ | $\ge 0$ | energía cargada por la flota en el año (kWh) |
| $N_y$ | $[N^L_y, N^U_y]$ | ciclos equivalentes por batería en el año |
| $w_y$ | $\ge 0$ | variable auxiliar de McCormick ($\approx N_y \bar b_y$) |
| $Y_{ijydt}$ | $\{0,1\}$, $j \in \mathcal{J}_{iydt}$ | LHD $i$ asignado a extraer en $j$ |
| $Z_{iydt}$ | $\{0,1\}$ | LHD detenido |
| $Z^{ch}_{kiydt}$ | $\{0,1\}$, $k \in \mathcal{K}_i$ | LHD conectado a un cargador de la estación $k$ |
| $\sigma^+_{kiydt}, \sigma^-_{kiydt}$ | $\{0,1\}$ | inicio y fin de una sesión de carga |
| $P_{kiydt}$ | $\ge 0$ | potencia de carga (kW) |
| $B_{iydt}$ | $[0, b^{max}]$, $t \in \mathcal{T}_0$ | energía en la batería (kWh) |
| $P^{red}_{ydt}$ | $\ge 0$ | potencia comprada a la red (kW) |
| $P^{gen}_{gydt}$, $C_{gydt}$ | $\ge 0$ | generación aprovechada y vertida |
| $P^{bat}_{hydt}$ | libre | potencia del BESS (> 0 descargando) |
| $A_{hydt}$ | $\ge 0$, $t \in \mathcal{T}_0$ | energía en el BESS |

### A.4 Función objetivo

$$
\min\; C^{E} + C^{inv} + C^{ssee} + C^{gen} + C^{BESS} + C^{pot} + C^{R}
$$

$$
\begin{aligned}
C^{E} &= \omega \sum_{y} \delta_y \sum_{d,t} c_{ydt}\, P^{red}_{ydt}\, \Delta t \\
C^{inv} &= \sum_{y} \delta_y \sum_{k} \Big[ c^X_k\, \Delta X_{ky} + \big(c^{bay}_k + c^C + c^{esp}_k\big)\, \Delta N^C_{ky} \Big] \\
C^{ssee} &= \delta_{y_1} \sum_k c^{ssee}_k\, P^{max}_k \\
C^{gen} &= \sum_g c^{inv}_g\, p^{max}_g\, \phi_g\, G_g + \sum_y \delta_y \sum_g c^{op}_g\, p^{max}_g\, G_g \\
C^{BESS} &= \sum_h c^{inv}_h\, \phi_h\, H + \sum_y \delta_y \sum_h c^{op}_h\, H \\
C^{pot} &= \sum_y \delta_y \cdot 12\, c^{pot}\, P^{pot}_y \\
C^{R} &= \sum_y \delta_y\, n_y\, c^{R}\, R_y
\end{aligned}
$$

Cada cargador ocupa una bahía, por eso el costo de la bahía va con $\Delta N^C$. La
subestación, la generación y el BESS se pagan una vez, en el año 1.

### A.5 Restricciones

**Inversión**

$$
\begin{aligned}
&X_{ky} = X_{ky^-} + \Delta X_{ky}, \qquad X_{ky_1} = \Delta X_{ky_1} && \text{(A1)}\\
&\Delta X_{ky} = 0 \quad \forall y \in \mathcal{Y}^+ \quad \text{(apertura solo en el año 1)} && \text{(A2)}\\
&N^C_{ky} = N^C_{ky^-} + \Delta N^C_{ky}, \qquad N^C_{ky_1} = \Delta N^C_{ky_1} && \text{(A3)}\\
&N^C_{ky} \le \overline{N}_k\, X_{ky} && \text{(A4)}\\
&P^{max}_k = \pi\, n^{ssee}_k && \text{(A5)}\\
&G_g \le \overline{G}_g, \qquad H \le \overline{H} && \text{(A6)}
\end{aligned}
$$

**Estado del LHD y carga** (para todo $i, y, d, t$ y $k \in \mathcal{K}_i$)

$$
\begin{aligned}
&Z_{iydt} + \sum_{k \in \mathcal{K}_i} Z^{ch}_{kiydt} + \sum_{j \in \mathcal{J}_{iydt}} Y_{ijydt} = 1 \quad \text{si } \mathcal{J}_{iydt} \neq \emptyset && \text{(A7)}\\
&Z_{iydt} + \sum_{k \in \mathcal{K}_i} Z^{ch}_{kiydt} = 1 \quad t \in \mathcal{T}^{et} && \text{(A8)}\\
&Z^{ch}_{kiydt} \le X_{ky} && \text{(A9)}\\
&\textstyle\sum_{i:\, k \in \mathcal{K}_i} Z^{ch}_{kiydt} \le N^C_{ky} && \text{(A10)}\\
&Z^{ch}_{kiydt} - Z^{ch}_{kiyd,t-1} = \sigma^+_{kiydt} - \sigma^-_{kiydt}, \qquad Z^{ch}_{kiyd1} = \sigma^+_{kiyd1} - \sigma^-_{kiyd1} && \text{(A11)}\\
&Z^{ch}_{kiydt} + Z^{ch}_{kiyd,t+1} \ge 2\,\sigma^+_{kiydt} \quad t < T, \qquad Z^{ch}_{kiydT} = 0 && \text{(A12)}\\
&P_{kiydt} \le p^C\, Z^{ch}_{kiydt} && \text{(A13)}\\
&\textstyle\sum_{i:\, k \in \mathcal{K}_i} P_{kiydt} \le P^{max}_k && \text{(A14)}
\end{aligned}
$$

(A12) exige que cada sesión de carga dure al menos 2 intervalos y que no quede ninguna
abierta en el último intervalo del día.

**Energía en la batería**

$$
\begin{aligned}
&B_{iydt} = B_{iyd,t-1} + \eta^c \sum_{k \in \mathcal{K}_i} P_{kiydt}\, \Delta t - \frac{1}{\eta^d} \sum_{j \in \mathcal{J}_{iydt}} e_{ij}\, Y_{ijydt} && \text{(A15)}\\
&\beta\, \bar b_y \le B_{iydt} \le \bar b_y && \text{(A16)}\\
&B_{iyd0} = B_{iydT} && \text{(A17)}\\
&B_{i'yd0} \le B_{iyd0} \quad \forall (i, i') \in \mathcal{P} && \text{(A18)}
\end{aligned}
$$

**Producción**

$$
\begin{aligned}
&\sum_{i,j,t} q_{ij}\, Y_{ijydt} \ge \sum_{j} m_{jy} && \text{(A19)}\\
&\underline{v}_{jy} \le \sum_{i,t} Y_{ijydt} \le \overline{v}_{jy} && \text{(A20)}
\end{aligned}
$$

**Pausas DCH**

$$
\begin{aligned}
&Z_{iydt} = 1, \quad Z^{ch}_{kiydt} = 0 \qquad t \in \mathcal{T}^{mant} && \text{(A21)}\\
&\textstyle\sum_{j} Y_{ijydt} = 0 \qquad i \in \mathcal{I}^{1},\ t \in \mathcal{T}^{col,1}; \quad i \in \mathcal{I}^{2},\ t \in \mathcal{T}^{col,2} && \text{(A22)}
\end{aligned}
$$

(A22) no aplica en los años marcados para operar en colación; en DCH no hay ninguno.
Con carga libre no hay más restricciones sobre cuándo cargar.

**Potencia, red, generación y BESS**

$$
\begin{aligned}
&P^{red}_{ydt} + \sum_g P^{gen}_{gydt} + \sum_h P^{bat}_{hydt} = \sum_{k,i} P_{kiydt} && \text{(A23)}\\
&P^{red}_{ydt} \le \overline{P}^{red} && \text{(A24)}\\
&P^{red}_{ydt} \le P^{pot}_y \qquad d \in \mathcal{D}^{pot},\ t \in \mathcal{T}^{HP} && \text{(A25)}\\
&P^{gen}_{gydt} + C_{gydt} = G_g\, p^{max}_g\, \alpha_{gdt} && \text{(A26)}\\
&-p^{max}_h H \le P^{bat}_{hydt} \le p^{max}_h H && \text{(A27)}\\
&A_{hydt} = A_{hyd,t-1} - \frac{P^{bat}_{hydt}}{\eta_h}\, \Delta t, \qquad A_{hyd0} = 0, \qquad A_{hyd1} = A_{hydT} && \text{(A28)}\\
&a^{min}_h H \le A_{hydt} \le a^{max}_h H && \text{(A29)}
\end{aligned}
$$

**Degradación**

$$
\begin{aligned}
&S_y = \omega \sum_{d,t} \sum_{k,i} P_{kiydt}\, \Delta t && \text{(A30)}\\
&N_y\, \bar b_y = S_y / n_y && \text{(A31)}\\
&D_y = \bar b_y - \gamma\, N_y && \text{(A32)}\\
&\bar b_y \le D_{y^-} + \rho\, R_y, \qquad \bar b_y \ge D_{y^-} - b^{max} R_y, \qquad \bar b_y \ge b^{max} R_y \qquad y \in \mathcal{Y}^+ && \text{(A33)}
\end{aligned}
$$

(A33) deja $\bar b_y = D_{y^-}$ sin reemplazo y $\bar b_y = b^{max}$ con reemplazo. Junto
con las cotas de $D_y$, garantiza que la capacidad nunca baje del piso.

**Linealización de (A31).** En la versión exacta, (A31) es bilineal y se resuelve con el
solver en modo no convexo. En la descomposición, y opcionalmente en el monolítico, se
reemplaza por su envolvente de McCormick:

$$
\begin{aligned}
&n_y\, w_y = S_y\\
&w_y \ge N^L_y \bar b_y + B_L N_y - N^L_y B_L, \qquad w_y \ge N^U_y \bar b_y + B_U N_y - N^U_y B_U\\
&w_y \le N^U_y \bar b_y + B_L N_y - N^U_y B_L, \qquad w_y \le N^L_y \bar b_y + B_U N_y - N^L_y B_U
\end{aligned}
$$

**Cotas de $N_y$.** Se calculan por año. Una cota global deja la envolvente floja.
- $N^L_y = \lfloor E^{low}_y / (n_y B_U) \rfloor$. $E^{low}_y$ es la energía mínima
  para cumplir la meta: una mochila fraccionaria que toma primero los puntos de menor
  energía por tonelada.
- $N^U_y = \max\{N^L_y, \min(\lceil E^{cap}_y/(n_y B_L) \rceil, \lceil 1{,}2\, E^{high}_y/(n_y B_L) \rceil)\}$.
  - $E^{high}_y$ es la misma mochila con visitas enteras.
  - $E^{cap}_y$ es la energía máxima que pueden entregar los cargadores en el año.

**Auxiliar de reporte.** Las toneladas por intervalo, $M_{ijydt} = q_{ij} Y_{ijydt}$, se
guardan para los informes y no afectan la optimización.

---

## Anexo B. Formulación completa: battery swapping

Comparte con el anexo A:
- la inversión en estaciones y subestación (A1, A2, A5, A6);
- la producción (A19, A20);
- la colación por grupos (A22);
- la red, la generación y el BESS (A24–A29);
- el cobro de potencia (A25);
- la estructura de la degradación.

Cambian el estado del LHD, la energía de la batería, la estación (con su inventario de
baterías) y la flota que se degrada. No existen $Z^{ch}$, $\sigma^\pm$ ni $P$.

### B.1 Conjuntos y parámetros adicionales

| Símbolo | Descripción |
|---|---|
| $L$ | intervalos que dura una carga completa: $\text{round}\big(b^{max}(1-\beta) / (p^C \eta^c \Delta t)\big)$ (0,5C: 13) |
| $\mathcal{A}_t$ | inicios de carga todavía activos en $t$: $a \in [\max(1, t-L+1),\, t]$ |
| $\mathcal{K}_i$ | estaciones donde puede intercambiar el LHD $i$ |
| $c^{bay}_k, c^{gr}_k$ | costo de bahía y de su grúa (USD) |
| $c^{bat}, c^{espb}_k$ | costo de una batería del pool y de su espacio (USD) |
| $\overline{N}^{bay}_k, \overline{N}^{C/bay}_k, \overline{N}^{bat/bay}_k$ | bahías máximas, cargadores y baterías máximas por bahía |
| $n^{peak}$ | baterías que pueden cargar a la vez con la red: $\lfloor \overline{P}^{red} / p^C \rfloor$ |
| $n^S_y$ | LHD de swap activos en el año $y$ |
| $F^{min}_y, F^{max}$ | cotas de la flota de baterías (pool + una por LHD activo) |
| $\mathcal{V}_y$ | valores enteros posibles de la flota en el año $y$ |
| $\mathcal{S}$, $[L_s, U_s]$ | 8 tramos iguales de $[B_L, B_U]$ para la McCormick por tramos |

### B.2 Variables adicionales

| Variable | Dominio | Descripción |
|---|---|---|
| $N^{bay}_{ky}, \Delta N^{bay}_{ky}$ | $\mathbb{Z}_+$ | bahías instaladas y compradas |
| $N^{bat}_{ky}, \Delta N^{bat}_{ky}$ | $\mathbb{Z}_+$ | baterías del pool instaladas y compradas |
| $Z^{sw}_{kiydt}$ | $\{0,1\}$ | el LHD $i$ intercambia batería en la estación $k$ |
| $B^{s}_{iydt}$ | $\ge 0$, $t \in \mathcal{T}_0$ | energía del LHD después del intercambio |
| $\tilde P_{iydt}$ | $\ge 0$ | linealización de $\bar b_y \cdot \sum_k Z^{sw}_{kiydt}$ |
| $X^{ini}_{kydt}$ | $\mathbb{Z}_+$ | cargas que empiezan en $t$ |
| $V_{kydta}$ | $\mathbb{Z}_+$, $a \in \mathcal{A}_t$ | baterías cargando en $t$ que empezaron en $a$ |
| $S_{kydt}$ | $\mathbb{Z}_+$ | baterías cargadas disponibles |
| $X^{dch}_{kydt}$ | $\mathbb{Z}_+$ | baterías descargadas en espera |
| $W_{kydt}$ | $\mathbb{Z}_+$ | intercambios en $t$ |
| $N^{pool}_y$, $F_y$ | $\mathbb{Z}_+$ | baterías del pool y flota total |
| $E_y$ | $\ge 0$ | energía de tracción de los días representativos (kWh) |
| $N^{tot}_y$ | $\ge 0$ | ciclos totales de la flota ($N_y F_y$) |
| $Z^{R}_y$ | $[0, F^{max}]$ | baterías reemplazadas ($R_y F_y$) |
| $u_{yv}, \nu_{yv}$ | $\{0,1\}$, $\ge 0$ | desagregación exacta de $N_y F_y$ por el valor $v$ de la flota |
| $z_{ys}, b_{ys}, \tau_{ys}, w_{ys}$ | $\{0,1\}$, $\ge 0$ | McCormick por tramos de $N^{tot}_y \bar b_y$ |

### B.3 Función objetivo

Igual que en A.4, salvo dos términos:

$$
\begin{aligned}
C^{inv} &= \sum_{y} \delta_y \sum_{k} \Big[ c^X_k \Delta X_{ky} + (c^{bay}_k + c^{gr}_k)\, \Delta N^{bay}_{ky} + (c^C + c^{esp}_k)\, \Delta N^C_{ky} + (c^{bat} + c^{espb}_k)\, \Delta N^{bat}_{ky} \Big] \\
C^{R} &= \sum_y \delta_y\, c^{R}\, Z^{R}_y
\end{aligned}
$$

### B.4 Restricciones

**Inversión de la estación** (además de A1, A2, A5 y A6)

$$
\begin{aligned}
&N^{bay}_{ky} = N^{bay}_{ky^-} + \Delta N^{bay}_{ky}, \quad N^{C}_{ky} = N^{C}_{ky^-} + \Delta N^{C}_{ky}, \quad N^{bat}_{ky} = N^{bat}_{ky^-} + \Delta N^{bat}_{ky} && \text{(B1)}\\
&N^{bay}_{ky} \le \overline{N}^{bay}_k X_{ky} && \text{(B2)}\\
&N^{bay}_{ky} \le N^C_{ky} \le \overline{N}^{C/bay}_k N^{bay}_{ky}, \qquad N^{bat}_{ky} \le \overline{N}^{bat/bay}_k N^{bay}_{ky}, \qquad N^C_{ky} \le N^{bat}_{ky} && \text{(B3)}
\end{aligned}
$$

**Estado del LHD e intercambio**

$$
\begin{aligned}
&Z_{iydt} + \sum_{j} Y_{ijydt} + \sum_{k \in \mathcal{K}_i} Z^{sw}_{kiydt} = 1 \quad \text{si } \mathcal{J}_{iydt} \neq \emptyset; \qquad Z_{iydt} + \sum_k Z^{sw}_{kiydt} = 1 \quad t \in \mathcal{T}^{et} && \text{(B4)}\\
&Z^{sw}_{kiydt} \le X_{ky}, \qquad \textstyle\sum_{k} Z^{sw}_{kiydt} \le 1 && \text{(B5)}\\
&\textstyle\sum_{i} Z^{sw}_{kiydt} \le N^{bay}_{ky}, \qquad W_{kydt} = \sum_{i} Z^{sw}_{kiydt} && \text{(B6)}\\
&Z_{iydt} = 1 \qquad t \in \mathcal{T}^{mant} \quad \text{(sin operar ni intercambiar; la estación sigue cargando)} && \text{(B7)}
\end{aligned}
$$

**Energía en la batería del LHD.** Sea $\zeta_{iydt} = \sum_{k} Z^{sw}_{kiydt}$.

$$
\begin{aligned}
&B_{iydt} = B^{s}_{iyd,t-1} - \frac{1}{\eta^d} \sum_{j} e_{ij} Y_{ijydt} && \text{(B8)}\\
&B^{s}_{iyd,t-1} \le B_{iyd,t-1} + (1-\beta)\, \tilde P_{iydt}, \qquad B^{s}_{iyd,t-1} \ge B_{iyd,t-1} && \text{(B9)}\\
&B^{s}_{iyd,t-1} \le \bar b_y, \qquad B^{s}_{iyd,t-1} \ge \beta\, \bar b_y + (1-\beta)\, \tilde P_{iydt} && \text{(B10)}\\
&\tilde P_{iydt} \le \bar b_y, \qquad \tilde P_{iydt} \le b^{max}\zeta_{iydt}, \qquad \tilde P_{iydt} \ge \bar b_y - b^{max}(1 - \zeta_{iydt}) && \text{(B11)}\\
&\beta\, \bar b_y \le B_{iydt} \le \bar b_y, \qquad B^{s}_{iyd0} = B_{iyd0}, \qquad B_{iyd0} = B_{iydT} && \text{(B12)}
\end{aligned}
$$

Con intercambio en $t$, (B9)–(B11) dejan $B^{s}_{t-1} = \bar b_y$: el LHD sale con una
batería llena de la capacidad del año. Sin intercambio, $B^{s}_{t-1} = B_{t-1}$. Se
permite intercambiar a cualquier SOC sobre el mínimo.

**Estación: carga en bloques e inventarios**

$$
\begin{aligned}
&V_{kydaa} = X^{ini}_{kyda}, \qquad V_{kydta} = V_{kyd,t-1,a} \quad a < t \le a + L - 1 && \text{(B13)}\\
&\textstyle\sum_{a \in \mathcal{A}_t} V_{kydta} \le N^C_{ky} && \text{(B14)}\\
&p^C \textstyle\sum_{a \in \mathcal{A}_t} V_{kydta} \le \pi\, n^{ssee}_k, \qquad \sum_k \sum_{a \in \mathcal{A}_t} V_{kydta} \le n^{peak} && \text{(B15)}\\
&X^{dch}_{kydt} = X^{dch}_{kyd,t-1} - X^{ini}_{kydt} + W_{kydt} \quad (t \ge 2), \qquad X^{ini}_{kyd1} \le X^{dch}_{kyd1} && \text{(B16)}\\
&S_{kyd,t+1} = S_{kydt} + V_{kydt,\,t-L+1} - W_{kyd,t+1} \quad (t < T;\ \text{sin el término } V \text{ si } t - L + 1 < 1) && \text{(B17)}\\
&S_{kyd1} = S_{kydT}, \qquad X^{dch}_{kyd1} = X^{dch}_{kydT} && \text{(B18)}\\
&S_{kyd1} + X^{dch}_{kyd1} = N^{bat}_{ky} && \text{(B19)}
\end{aligned}
$$

En (B16), para $t = 2$ se descuentan también las cargas iniciadas en $t = 1$:
$X^{dch}_{2} = X^{dch}_{1} - X^{ini}_{1} - X^{ini}_{2} + W_{2}$. Una batería que llega con
un intercambio en $t$ puede empezar a cargar en ese mismo $t$, no antes.

**Balance de potencia**

$$
P^{red}_{ydt} + \sum_g P^{gen}_{gydt} + \sum_h P^{bat}_{hydt} = p^C \sum_k \sum_{a \in \mathcal{A}_t} V_{kydta} \qquad \text{(B20)}
$$

**Degradación del pool**

$$
\begin{aligned}
&N^{pool}_y = \textstyle\sum_k N^{bat}_{ky}, \qquad F_y = N^{pool}_y + n^S_y && \text{(B21)}\\
&E_y = \textstyle\sum_{i,d,t,j} e_{ij}\, Y_{ijydt} && \text{(B22)}\\
&N^{tot}_y = N_y\, F_y, \qquad N^{tot}_y\, \bar b_y = \omega\, E_y && \text{(B23)}\\
&D_y = \bar b_y - \gamma\, N_y, \qquad \text{(A33) igual} && \text{(B24)}\\
&Z^R_y \le F_y, \quad Z^R_y \le F^{max} R_y, \quad Z^R_y \ge F_y - F^{max}(1 - R_y), \quad Z^R_y \ge F^{min}_y R_y && \text{(B25)}
\end{aligned}
$$

- **(B23) es trilineal** ($N_y \bar b_y F_y$) y se reduce a dos productos.
  - **$N_y F_y$, exacto.** La flota es entera y toma pocos valores, así que se desagrega
    por valor:
    $\sum_v u_{yv} = 1$, $F_y = \sum_v v\,u_{yv}$, $N_y = \sum_v \nu_{yv}$,
    $N^L_y u_{yv} \le \nu_{yv} \le N^U_y u_{yv}$, $N^{tot}_y = \sum_v v\,\nu_{yv}$.
  - **$N^{tot}_y \bar b_y$, por tramos.** Se usa una McCormick por tramos de $\bar b_y$
    (8 tramos):
    $\sum_s z_{ys} = 1$, $\bar b_y = \sum_s b_{ys}$, $N^{tot}_y = \sum_s \tau_{ys}$,
    $w_y = \sum_s w_{ys} = \omega E_y$, $L_s z_{ys} \le b_{ys} \le U_s z_{ys}$, y las
    cuatro desigualdades de McCormick de cada tramo, con sus constantes multiplicadas
    por $z_{ys}$.
  - **Por qué así.** Con una sola envolvente en todo $[B_L, B_U]$, el modelo contaba solo
    el 93–96 % de los ciclos físicos.
- **(B25) es la envolvente exacta de $R_y F_y$.** Sin la cuarta desigualdad, la
  relajación deja $Z^R = 0$ con $R$ fraccional y abarata el reemplazo.
- **(B22) usa la energía consumida**, no la cargada en la estación. Una batería
  intercambiada tarde puede no terminar de cargar ese mismo día, y la energía cargada
  subestimaría el desgaste.

**Desigualdad válida: corte de cargas** (activa por defecto)

$$
\sum_k \sum_{t < T} X^{ini}_{kydt} \ge \left\lceil \frac{E^{min}_{yd}}{(1-\beta)\, b^{max}} \right\rceil \qquad \text{(B26)}
$$

- $E^{min}_{yd}$ es la energía de tracción mínima para cumplir la meta del día. Se
  calcula con la misma mochila fraccionaria de A.5, sobre las cotas de visitas de (A20).
- Es válida porque el SOC del LHD es cíclico, la energía solo se repone con
  intercambios y cada intercambio repone a lo sumo $(1-\beta)b^{max}$.
- No cambia el óptimo entero. Sube la relajación, que sin ella es floja (sección 2.7).

**Escala numérica.** En la implementación, la producción se escribe en kt, los ciclos en
miles y la energía de la degradación en MWh, para evitar coeficientes de ~1e5 en las
descomposiciones. Las ecuaciones de arriba están en unidades físicas.

---

## Referencias

- Lara, C. L., Mallapragada, D. S., Papageorgiou, D. J., Venkatesh, A. y Grossmann, I. E. (2018). Deterministic electric power infrastructure planning: Mixed-integer programming model and nested decomposition algorithm. *European Journal of Operational Research*, 271(3), 1037–1054.
- Laporte, G. y Louveaux, F. V. (1993). The integer L-shaped method for stochastic integer programs with complete recourse. *Operations Research Letters*, 13(3), 133–142.
- Angulo, G., Ahmed, S. y Dey, S. S. (2016). Improving the integer L-shaped method. *INFORMS Journal on Computing*, 28(3), 483–499.
- Riley, B. P., Daoutidis, P. y Zhang, Q. (2025). Integer L-shaped method with non-supporting no-good optimality cuts. arXiv:2511.06340.
- Göke, L., Schmidt, F. y Kendziorski, M. (2024). Estabilización Box-step de Benders para planificación energética (Algoritmos 2 y 6). *European Journal of Operational Research*, 316.
- Jacobson, A., Pecci, F., Sepulveda, N., Xu, Q. y Jenkins, J. (2023). A computationally efficient Benders decomposition for energy systems planning problems with detailed operations and time-coupling constraints. *INFORMS Journal on Optimization*.
- Pecci, F. y Jenkins, J. D. (2025). Regularized Benders decomposition for high performance capacity expansion models. *IEEE Transactions on Power Systems*, 40(4), 3105–3116.
- Zou, J., Ahmed, S. y Sun, X. A. (2019). Stochastic dual dynamic integer programming. *Mathematical Programming*, 175, 461–502.
