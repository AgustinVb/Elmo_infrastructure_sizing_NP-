# Contexto para retomar: DP y flujo de la estación de swap

Fecha: 2026-10-23, alrededor de las 00:05. Repo: `C:\Users\TECNO-MASTER\Desktop\Elmo_infrastructure_sizing_NP-`, rama **`battery_swapping_multiaño`**, HEAD `0b911ba9c`. **Nada está commiteado** (ver §6).

Reglas de la usuaria que siguen vigentes:
- Responder en español.
- No hacer commit ni push sin que lo pida.
- Todo lo nuevo va detrás de flags, y con los flags apagados el comportamiento no cambia.
- No corregir el MILP sin avisar.
- Probar antes de concluir.

## 1. Tarea

Implementar `C:\Users\TECNO-MASTER\Desktop\implementacion_dp_estacion_swap.md`: la estación de swap como DP (Schneider-Thonemann-Klabjan) y como flujo en el grafo de la DP (Martin-Rardin-Campbell), para intentar mejorar la relajación del problema anual. Las fases son:

- 0: convención.
- 1: DP.
- 2: validación.
- 3: flujo integrado al constructor y al Benders.
- 4: experimento de cota.
- 5: lagrangiana, opcional.

**Decisiones de la usuaria (2026-10-22):**
- Acotar baterías y cargadores a 4, con combinaciones `1 ≤ N_C ≤ N_bat ≤ 4`.
- Aplicar la "corrección natural" del borde del día: pipeline de carga cíclico, porque el día va de 08:30 a 08:30.
- Orden: primero el experimento de la Fase 4, armando el flujo sobre el MILP del día con la inversión fija. La Fase 3 completa, solo si conviene.

## 2. Estado por fase

| Fase | Estado | Archivos |
|---|---|---|
| 0 | Lista | `notas_convencion_estacion.md` |
| 1 | Lista, 6 tests pasan (se corren llamando a las funciones; no hay pytest) | `dp_estacion.py`, `tests/test_dp_estacion.py` |
| 2 | Lista, también para el modo cíclico | `validar_dp_estacion.py` (flag `--pipeline_ciclico`), `validacion_dp_estacion.md` |
| Pipeline cíclico | Implementado y validado, **apagado por defecto** | `src/optimization/functions.py`, `setup.py`, `driver.py`, `certificar_inversiones.py` |
| Tope 4 | Implementado y verificado en el LP | `functions.py`, `setup.py` |
| 4 | **Terminada** | `experimento_estacion_flujo.py`, `resultados_estacion_flujo.md` |
| 3 | No se hace: la Fase 4 indica que no conviene (ver §4) | — |
| 5 | Solo si la usuaria la pide | — |

## 3. Qué se implementó

**Pipeline cíclico** (`ELMO_PIPELINE_CICLICO=1`, o `setup.py --pipeline_ciclico`):
- Funciones nuevas en `functions.py`: `pipeline_ciclico()`, `ventana_carga(t, intervals, L)`, `intervalo_anterior` e `intervalos_corte_cargas`.
- Las ventanas de `Sv` son circulares (`SV_INDEX` y `_sv_a_window`). `charging_duration` usa anterior(t0) = tf.
- Inventarios en todo t: `S[sig(t)] = S[t] + Sv[t, a_fin] − W[sig(t)]` y `X_dch[t] = X_dch[ant(t)] − X_ini[t] + W[t]`.
- Se omiten `CI_S` y `CI_X_dch`. `CB_general` pasa a `S[t0] + X_dch[t0] + Σ_{a∈ventana(t0)} Sv[t0,a] = N_batteries`.
- El corte de cargas (`min_charges_cut`, `driver._agregar_corte_cargas_lhd` y `certificar_inversiones`) suma todos los t cuando el flag está activo.
- **Con el flag apagado, el LP es idéntico byte a byte al del commit 0b911ba9c** (verificado con 1 año y 1 día).
- Una solución sin ciclo se lleva al modelo cíclico con `X_dch[t0] −= X_ini[t0]`, mediante `validar_dp_estacion.adaptar_incumbente_ciclico`. Por eso el modelo cíclico es una relajación exacta del anterior: el incumbente k=2 se pule al mismo costo, 3.289.542,58.
- **Decisión de la usuaria (2026-10-23): queda pendiente para la versión 5.** En la v4 sigue apagado (los resultados v4 se hicieron sin ciclo). Al armar la v5 hay que:
  - activarlo (`--pipeline_ciclico`, o hacerlo el valor por defecto);
  - recorrer todos los casos de swap con el ciclo;
  - no reutilizar los cortes de Benders de corridas sin ciclo, porque no son válidos para el modelo cíclico;
  - si se quieren los incumbentes viejos como punto de partida, traducirlos con `X_dch[t0] −= X_ini[t0]`.

**Tope** (`ELMO_TOPE_ESTACION=4`, o `setup.py --tope_estacion 4`): pone una cota superior a `N_batteries[k,y]` y `N_chargers[k,y]` y hace `N_max = min(N_max, 4·|K|)` en la degradación. `N_chargers ≤ N_batteries` ya existía en el modelo.

**Validación cíclica** (caso 3 v4, `output/.../validacion_dp_estacion/caso3_v4_ciclico/`): el MILP cíclico coincide con la DP `ciclo="completo"` en los 6 días, con diferencias ≤ 3e-15. Energías: (3,105) 20.405,20; (4,196) 24.150,81; (5,288) 16.868,69; (6,196) 23.086,39; (7,105) 21.766,08; (8,15) 4.368,30. El borde viejo costaba entre 0 y 7 % de la energía de la estación.

## 4. Fase 4: experimento del flujo (`experimento_estacion_flujo.py`)

**Montaje:**
- Caso 3 v4 (`data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte/05C/RED_GEN`, sin BESS), incumbente `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/05C/RED_GEN/benders/incumbente_respaldo_k2.pkl`.
- Día de `BendersDias` (year_day) armado en gurobipy, con las copias de la inversión fijas (N=2, N_C=2, n_ssee=1, P_pot=241), los presupuestos libres y **Y, Z y Z_swap libres**.
- El script fija `ELMO_PIPELINE_CICLICO=1`. 900 s por variante, 5 trabajos de 6 hilos.

**Variantes:**
- `actual`: el MILP de siempre, con pipeline cíclico.
- `power_link`: la estación se reemplaza por una circulación en el grafo de la DP (105 estados, unos 27.000 arcos). Q entra al balance de potencia.
- `arc_cost`: el costo de energía va en los arcos y P_red sale del objetivo. Es exacto aquí, porque el único costo del día es P_red y no hay BESS.
- `*_u`: el enlace se desagrega por número de swaps, `u[t,w] = Σ_{a: w_a=w} f_a`, con u binaria.

**Chequeo del grafo (con Y, Z y Z_swap fijos en el incumbente):**

| Día | Arcos con w_a = W_t | DP | Enlace agregado `Σ Z_swap = Σ w_a f_a` |
|---|---:|---:|---:|
| (4,196) | 24.150,81 (f entero) | 24.150,81 | 17.489 (1.649 arcos fraccionarios) |
| (5,288) | 16.868,69 (f entero) | 16.868,69 | 8.886 |
| (7,105) | 21.766,08 (f entero) | 21.766,08 | 11.596 |

**CORREGIDO (2026-10-23):** la columna del enlace agregado estaba MAL, por un error del script: faltaba `update()` antes de `relax()`, así que los swaps quedaban libres. Bien medida, da 24.150,81 / 16.868,69 / 21.766,08, igual que la DP. Con los swaps enteros la estación es exacta en cualquier formulación; no hay "mezcla de calendarios" con los swaps enteros.

**Resultados (cota y solución a 900 s):**

| Día | Variante | LP | Raíz | Cota final | Mejor solución |
|---|---|---:|---:|---:|---:|
| (4,196), incumbente 24.471 | actual | 17.489 | 18.041 | **18.470** | **20.949** |
| | arc_cost | 17.489 | 18.041 | 18.041 | 22.506 |
| | arc_cost_u | 17.489 | 18.041 | 18.041 | 22.653 |
| | power_link_u | 17.489 | 18.041 | 18.041 | 23.746 |
| | power_link | 17.489 | 18.041 | 18.041 | 24.096 |
| (5,288), incumbente 17.268 | actual | 8.886 | 10.910 | 10.910 | 16.716 |
| | arc_cost_u | 8.886 | 10.910 | 10.910 | **16.070** |
| | power_link_u | 8.886 | 10.910 | 10.910 | 16.669 |
| | power_link | 8.886 | 10.910 | 10.910 | 17.597 |
| | arc_cost | 8.886 | 10.910 | 10.910 | 18.152 |
| (7,105), incumbente 22.108 | actual | 11.596 | 16.368 | **16.435** | **20.493** |
| | arc_cost | 11.596 | 16.368 | 16.368 | 20.664 |
| | power_link_u | 11.596 | 16.368 | 16.368 | 20.835 |
| | power_link | 11.596 | 16.368 | 16.368 | 20.851 |
| | arc_cost_u | 11.596 | 16.368 | 16.368 | 23.059 |

**Experimento TERMINADO** (2.839 s). El entregable `resultados_estacion_flujo.md` ya está escrito. Suma de los 3 días: incumbente 63.847; actual 58.158 (el mejor); flujos entre 61.250 y 62.544.

Tamaños en (4,196): `actual` tiene 15.098 filas y 15.135 columnas; los flujos, entre 28.000 y 31.500 filas y entre 35.000 y 40.000 columnas.

**CORRECCIÓN (2026-10-23):** el LP del modelo **actual** con Y, Z y Z_swap fijos en el incumbente da 24.150,81 / 16.868,69 / 21.714,36. Es decir, la DP exacta, o un 0,24 % bajo. Con los swaps enteros, la formulación actual de la estación es casi exacta, y **el hueco del LP viene de los swaps fraccionarios (lado LHD)**. La afirmación de abajo, "el hueco no viene del lado LHD", valía solo para el flujo con enlace agregado y era **incorrecta** como diagnóstico del problema. Esto respalda una descomposición por vehículo (Fernando Soto) con pricing exacto: ver `resultados_estacion_flujo.md`.

**Conclusión preliminar (ya comunicada a la usuaria; el segundo punto quedó corregido arriba):**
- **El flujo no mejora la relajación.** El LP y la raíz son idénticos en las 5 formulaciones, y en los flujos la cota no se mueve de la raíz en 900 s.
- El hueco está en el acople entre la hora de los swaps (lado LHD) y la hora de carga, no en cómo se formula la estación.
- Como heurística de solución, el resultado está dividido: en (4,196) ganó `actual` y en (5,288) ganó `arc_cost_u`.
- Recomendación: no hacer la Fase 3. Conservar el pipeline cíclico.
- Dato útil: con la inversión del incumbente, el MILP del día encuentra operaciones más baratas que las del incumbente (−14 % en (4,196) y −3 a −7 % en (5,288)). Parte del gap del caso 3 es de solución.

## 5. Cómo retomar

1. **El experimento ya terminó** y el paso 2 está hecho (`resultados_estacion_flujo.md`). Los comandos siguientes quedan solo como referencia, para revisar o relanzar:
   ```bash
   O=output/Resultados_finales_tesis/Mina_modelo/experimento_estacion_flujo/caso3_v4
   grep -E "^\[flujo\] \(|listo en" $O/experimento.log | sed 's/vars.*| LP/| LP/'
   ```
   Hay resultados parciales en `$O/parcial.jsonl` (uno por variante), el final en `$O/experimento.json` y los logs de Gurobi en `$O/gurobi/`. Si el proceso murió antes de (7,105), se puede relanzar solo ese día:
   ```bash
   nohup .venv_elmo/Scripts/python.exe -u experimento_estacion_flujo.py --data_folder data/Tesis_final/Mina_modelo_v4/Produccion_base/zona_norte/05C/RED_GEN --respaldo output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/05C/RED_GEN/benders/incumbente_respaldo_k2.pkl --dias "7,105" --timelimit 900 --jobs 5 --threads 6 --out output/Resultados_finales_tesis/Mina_modelo/experimento_estacion_flujo/caso3_v4_dia7 > /dev/null 2>&1 &
   ```
2. **Escribir `resultados_estacion_flujo.md`** (entregable de la Fase 4): la tabla completa de los 3 días, el chequeo del grafo, los tamaños, la fraccionalidad del LP (Z_swap fraccionarias de 720 y Q_t fraccionarios, que están en el JSON) y una conclusión de 3–5 líneas.
3. **Preguntar a la usuaria:**
   - ~~¿El pipeline cíclico pasa a ser el comportamiento por defecto?~~ Respondida: se activa en la v5 (ver §3).
   - ¿Se hace la segunda parte de la Fase 4, la inversión libre con disyunción δ sobre las 10 combinaciones con tope 4? Ojo: (N=4, N_C=4, L=13) son 2.380 estados, unos 3 millones de arcos por día, muy pesado.
   - ¿Se prueba la Fase 5 (lagrangiana)?
   - ¿Se hace commit?
4. **Pendiente aparte:** validar el caso 7 (con BESS) cuando exista un incumbente calculado con el código actual. El de la v3 es infactible desde los commits 4c6491e50 y 0b911ba9c.

## 6. Cambios locales sin commitear (rama swap)

- **Modificados:**
  - `src/optimization/functions.py`: pipeline cíclico y tope.
  - `setup.py`: `--pipeline_ciclico` y `--tope_estacion`.
  - `src/optimization/decomposition/driver.py` y `certificar_inversiones.py`: corte de cargas con `intervalos_corte_cargas`.
  - `corte_cargas_op.py`: `--days_per_year`, de antes.
  - Dos archivos de `data/.../1C/RED_GEN_BESS` modificados antes de esta tarea; no son míos, no tocarlos.
- **Nuevos:** `dp_estacion.py`, `tests/test_dp_estacion.py`, `validar_dp_estacion.py`, `experimento_estacion_flujo.py`, `notas_convencion_estacion.md`, `validacion_dp_estacion.md` y este archivo.

## 7. Otro contexto de la sesión

- El caso 3 de swap v4 se corrió en este equipo y **se detuvo a pedido de la usuaria**. Los cortes y el incumbente quedaron en `output/.../casos_finales_v4/Produccion_base/zona_norte/05C/RED_GEN/benders/`: `incumbente_respaldo_k2.pkl` (UB 3.289.542,58, LB 2.424.993,82, 18 cortes) y `corte_cargas_lhd.json`.
- La prueba de 2 días de RED_GEN_BESS v4 está en `output/.../prueba_2dias_RED_GEN_BESS_v4/` (UB 3.056.765, LB 2.339.455). Corrió antes de 0b911ba9c.
- Rama OB (`carga_ob_multiaño`): los resultados v4 ya están pusheados (commits 5ceadaab5, af9acf700 y 806cc405b). El caso 3 de OB quedó con gap de 1,398 %, aceptado por la usuaria.
- Python del proyecto: `.venv_elmo/Scripts/python.exe`. En la consola de Windows no está el `python` del sistema.
