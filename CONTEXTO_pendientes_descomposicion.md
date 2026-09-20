# Descomposición Nested Benders — estado y pendientes

Documento de contexto para retomar el trabajo sobre las dos ramas de interés.
Escrito el 2026-09-17. Cubre `battery_swapping_multiaño` y `carga_ob_multiaño`.

---

## 1. Estado de las ramas

| rama | último commit | estado |
|---|---|---|
| `battery_swapping_multiaño` | ver `git log` | presolve, propagación directa, cota agregada, apertura, flags `--mono_timelimit`/`--mip_focus`, pulido y escaneo del MIP start, benchmark 6 años (§4.1bis) |
| `carga_ob_multiaño` | ver `git log` | ídem + test sin degradación (validación 3) + benchmark completo 6 años (§4.1) y decisión de método (§2.5) |

Las dos tienen ahora lo mismo: descomposición por año con cortes de optimalidad y
de factibilidad, cota del LP monolítico, modo híbrido, subestación en módulos de
500 kW, y una batería de tests que lo verifica.

**Sin commitear en `carga_ob_multiaño`**: nada. `apertura_solo_primer_anio`
quedó validada y commiteada en OB (`b6764a311`, ver §7) y portada a swap
(`037141c90`, validada en `160kW_2dias` a 2 años: con X libre el monolítico
deja `station_3` cerrada). Los logs de todas las corridas están en
`output/_presolve/`; el reporte del híbrido a 6 años en `output/_hibrido_6y/`. La rama swap se trabajó en un worktree aparte, ya borrado.

### Instancias de prueba

| instancia | naves | LHDs | generación | uso |
|---|---|---|---|---|
| `data/DCH/160kW_2dias` (swap) | 3 | 12 | `g_max=0` | regresión de cortes de factibilidad |
| `data/DCH/160kW_2dias_1MB` (swap) | 1 | 4 | habilitada | iteración rápida |
| `data/DCH/640kW_2dias` (OB) | 3 | 12 | habilitada | validación principal |
| `data/DCH/640kW_2dias_1MB` (OB) | 1 | 4 | habilitada | su `ExtractionGoal` fue editada (ver §6) |

Todo se corre con `--consumption_model wp2`; con `wp1` los escenarios no cargan
(`KeyError: 'distance_to_d_node_outbound'`).

---

## 2. Presolve de capacidad — IMPLEMENTADO (2026-09-17)

**Estado**: hecho, medido, commiteado y pusheado en las dos ramas (OB
`97d2a2373` + `38447aee8`; swap `b7f07eb9b`).

### 2.1 El problema (como estaba)

A 6 años en `carga_ob_multiaño` el forward necesitó **9 cortes de factibilidad**
y **2.060 s**. El año 1 dimensiona `n_ssee_k` mirando sólo su propia demanda
(2% del pico) y el requerimiento del futuro viaja hacia atrás un año por vez,
con un barrido completo por escalón.

### 2.2 Lo implementado

**a) Presolve** (`NestedBendersSolver._capacity_presolve`,
`YearBlockBuilder.capacity_presolve_mode`, flag `--capacity_presolve
{peak,all,off}`, default `peak`). Antes de iterar, por nave:

```
n*_y(k) = min { n_ssee_k[k] : año y factible, TODO el estado heredado libre }
n_ssee_k[k] >= max_y n*_y(k)
```

El auxiliar desacopla las igualdades `link_*` (N_chargers, n_ssee_k, G, H, D)
del bloque y cambia el objetivo a `min n_ssee_k[k]`; es una relajación del año
en contexto, así que la desigualdad es válida (no invalida UB ni LB). Se usa
`ceil(ObjBound)` del MILP (válido aunque corte por tiempo; `_solve` ganó
`load_solutions=False`). La cota se impone como `lb` de `n_ssee_k` en el bloque
del año 1 **y en el LP monolítico**. `peak` resuelve sólo el año de mayor meta
(`sum_j m_j`), `all` todos. Al final de `solve()` se imprime la fuerza de la
cota contra lo que el forward eligió.

**b) Propagación directa** (`ForwardPass._add_feasibility_cut`): si todas las
familias con `mu ≠ 0` del corte de factibilidad son `global_once` (n_ssee_k,
G, H), el corte va **directo al bloque del año 1** en vez de al padre
inmediato. Riguroso: el corte restringe sólo ese vector, que es literalmente la
variable del año 1 y cuya ancla es la misma en todos los años del barrido. El
log ahora imprime además los `mu` por nave de las familias que atan.

**c) Cota agregada** (mismo auxiliar con objetivo `min Σ_k n_ssee_k[k]`, 1
MILP más por año): `Σ_k n_ssee_k[k] ≥ max_y min Σ`. Es el complemento de las
cotas por nave: como las naves se acoplan por la meta diaria total, "una u
otra nave necesita un módulo más" da mínimo 0 en cada una por separado y +1 en
la suma. Se impone como restricción `presolve_capacity_sum` en el bloque del
año 1 y en el LP monolítico, sólo si supera la suma de las cotas por nave.

**d) Test** `tests/test_presolve_capacidad_ob.py [escenario] [años] [--modo
peak|all] [--sin_referencia] [--comparar]`: (1) el modo presolve restaura el
bloque, (2) cotas e imposición, (3) validez contra una solución factible del
monolítico (caché del gate o incumbente con timelimit), (4) una iteración con
presolve, (5) opcional la misma sin presolve.

### 2.3 Medido

**`640kW_2dias_1MB`, 2 años** (`--comparar`): TODO OK. Cota = 1, ajustada
contra el óptimo monolítico. Sin cortes en ninguno de los dos. **La cota del LP
monolítico subió de 560.938 a 612.915 (+9,3%)** y el gap de la primera
iteración bajó de 46,1% a 41,1% — es la primera cosa que mueve el LB clavado de
§5.1.

**`640kW_2dias`, 3 naves, 6 años, presolve `all`** (log en
`output/_presolve/test_3naves_6y.log`):

```
meta   1:1,074  2:14,728  3:33,712  4:44,805  5:48,543  6:50,583
n*_y   año 1: (1,0,0)   año 2: (1,0,0)   años 3-6: (1,1,1)
```

18 MILP del presolve, 6–20 s cada uno (~270 s; en `peak` serían 3, ~55 s).
Cota impuesta (1,1,1). Incumbente monolítico (600 s, 9.295.910) usa **(3,2,2)**:
cota válida pero floja. LP monolítico con la cota: 3.736.166 (360 s).

| | sin presolve | por nave (`all`) | por nave + prop. directa | + cota agregada |
|---|---|---|---|---|
| cotas | — | (1,1,1) | (1,1,1) | (1,1,1), Σ ≥ 4 |
| cortes de factibilidad | 9 | 4 | 1 | **0** |
| MILP del forward, 1 iter. | — | 20 + 4 LP elást. | 11 + 1 LP elást. | **6** |
| tiempo forward+backward | 2.060 s | 1.415 s | 1.402 s (*) | **942 s** |
| LB del LP monolítico | — | 3.736.166 | 3.736.166 | **3.856.476** |
| `n_ssee_k` / UB | — | (2,1,1) / 7.682.499 | igual | igual |

(*) Esa corrida (`test_3naves_6y_directo.log`) se hizo con la máquina ~2× más
lenta (el mismo LP monolítico tardó 734 s contra 360-370 s en las otras); su
tiempo no es comparable. Los conteos de resoluciones sí: 24 → 12 → 6. Logs:
`test_3naves_6y.log`, `_directo.log`, `_suma.log`.

Los 4 cortes del presolve solo eran **la misma información** (`station_1`:
1→2, `v=0,0095`, familia `n_ssee_k`) bajando 5→4→3→2→1; la propagación
directa la lleva en un solo corte al año 1 y el segundo barrido completa los 6
años. La misma solución final en los dos casos.

**Por qué ese corte no se redondea a entero**: los multiplicadores son
distintos por nave, `mu = {station_1: 0.949, station_2: 1.0, station_3:
0.925}`, y `_redondeo_entero` exige coeficiente común. El corte queda en su
forma LP (`Σ mu_k Δn_k ≥ 0.0095`); con `n_ssee_k` entera eso igualmente obliga
a subir al menos un módulo en alguna nave, y cuál la decide el costo del año 1
(acertó a la primera aquí). Con `mu` desiguales y `Δn` que pueden ser
negativos no hay un redondeo Chvátal-Gomory limpio; no vale la pena
perseguirlo mientras el corte cueste un solo reinicio.

**Swap, `160kW_2dias`, 3 naves, 2 años** (`test_swap_3naves_2y*.log`):

| | sin presolve | cotas por nave | + cota agregada |
|---|---|---|---|
| cotas | — | (1,0,0) | (1,0,0) y Σ ≥ 2 |
| cortes de factibilidad | 1 | 1 | **0** |
| `n_ssee_k` elegido | (1,0,1) | (1,0,1) | (1,1,0) = óptimo monolítico |
| LB del LP monolítico | 824.782 | 938.962 (+14%) | **1.070.460 (+30%)** |
| UB | 2.004.192 | 2.004.192 | 2.004.192 |

El único corte sin presolve era exactamente `n_2 + n_3 ≥ 1` (entero,
`mu = {2: 1.0, 3: 1.0}`): invisible para las cotas por nave, capturado por la
agregada. A 2 años no hay cascada, así que la propagación directa no
interviene. Los tiempos de esta sesión no son comparables entre sí (máquina
cargada, los mismos MILP tardan el doble de una corrida a la otra).

### 2.4 Pendiente inmediato

1. `apertura_solo_primer_anio`: validada y commiteada en las dos ramas (§7).
2. **Por qué la cota por nave es floja**: la asignación LHD–nave es 1:1, pero
   las naves se acoplan por la meta diaria total (`daily_production`). Al
   minimizar `n_k` con las otras libres, la nave k produce su piso y las otras
   absorben el resto con capacidad ilimitada. La cota agregada cubre el caso
   "una u otra"; lo que sigue sin cubrir es la combinación exacta (qué nave).
   Opción no implementada: pre-generar en el presolve los cortes de
   factibilidad del año relajado con un loop Benders chico sobre `n` (cada
   corte cuesta un LP elástico en vez de un barrido). En swap además el
   heredado libre incluye `N_batteries` (repuestos ilimitados y gratis en el
   auxiliar), que afloja más.
3. Push: hecho en las dos ramas.
4. Híbrido a 6 años: benchmark completo hecho (§4.1). Configuración
   recomendada: `--mode hybrid --max_iter 3 --mip_focus 1`.
5. Portar `apertura_solo_primer_anio` a swap: hecho (`037141c90`).

**Alternativa descartada por falta de rigor**: un modelo grueso de un día por
año sobre el horizonte completo; sólo serviría en híbrido o como MIP start.

### 2.5 Decisión de método (2026-09-19)

Con §4.1 y §4.1bis medidos, la descomposición sola no cierra gap: el LB queda
en el LP monolítico en todas las corridas (§5.1) y el gap se reduce sólo por
el UB. Pero **es la única vía que produce incumbentes a 6 años de forma
fiable**: el monolítico solo no encontró ninguno en 2.260 s (OB) ni en 600 s
(swap). El método para las corridas finales es el **híbrido con 3 iteraciones
y `MIPFocus=1`** (`--mode hybrid --max_iter 3 --mip_focus 1
--block_build_jobs 1`): en OB da 7.040.022 con gap 17 %, un 24 % mejor que el
mejor monolítico. El gap que queda lo cierra el B&B del monolítico, no los
cortes de Benders; mejorar el LB de la descomposición (§5.1) sigue abierto
pero ya no está en el camino crítico.

---

## 3. El MIP start rechazado por ruido numérico

**Medido de nuevo el 2026-09-18** con el presolve (0 cortes de factibilidad,
solución (2,1,1)): `build_report_model` ahora escanea todas las filas del
monolítico con los valores cargados (`_mip_start_residuals`, ~20 s a 6 años)
y la violación máxima es **1,16e-8** en `s_def[3]`; ninguna fila supera
1e-6. Los 5,7e-5 de abajo eran de otra solución (la de los 9 cortes), así que
el ruido no es estructural del ensamblado sino de aquella trayectoria.
**Confirmado en el híbrido a 6 años** (§4): `User MIP start produced solution
with objective 8.29221e+06 (0.87s)`. Script:
`tests/medir_residuo_mip_start_ob.py [escenario] [años]`. Commiteado en
`8bb9bda5a`.

**En swap el problema seguía vivo y ya se resolvió (2026-09-19).** El híbrido
de swap a 6 años rechazó el arranque (`violates constraint x937763 by
0.000048569`). Causa, vista con el escaneo a 2 años: swap cargaba las enteras
de los bloques **sin redondear** (`Z = 7,3e-7`, `Z_swap = 0,99999927`, avisos
`W1001` de Pyomo) y las continuas venían calculadas con esos valores; al fijar
el arranque Gurobi redondea las enteras y en las filas con big-M una
diferencia de 7e-7 por un coeficiente de ~100 supera `FeasibilityTol`.
Arreglo (en las dos ramas, `e61529c53` OB / swap): redondeo al cargar y
**pulido del MIP start** — se fijan las enteras en sus valores redondeados y
se resuelve el LP del modelo de reporte (opción 3 de este apartado), 43 s a 6
años en swap, costo idéntico. Resultado: `User MIP start produced solution
with objective 5.27977e+06 (0.59s)` (§4.1bis). Es el default de
`build_report_model` (`polish_mip_start=True`).

El escaneo también informa filas sin evaluar por variables sin valor: 0. Hay
variables sin valor (`Z`, `StartAssign`, `EndAssign`) pero no aparecen en
ninguna restricción activa.

--- lo que sigue es el diagnóstico original ---

En `carga_ob_multiaño` a 6 años, Gurobi descartó el arranque:

```
User MIP start did not produce a new incumbent solution
User MIP start violates constraint x1661848 by 0.000057418
```

No es un error conceptual (el de McCormick, ya corregido, violaba por 3.317): son
**5,7e-5 contra la tolerancia de factibilidad de Gurobi, 1e-6**. Ruido numérico
acumulado: cada bloque se resuelve por separado con su propio `MIPGap` y
tolerancias relativas; al ensamblar las seis soluciones en un modelo único, el
residuo se mide contra un umbral absoluto.

**Medido**: a 2 años hay 1.625 restricciones con residuo > 0, pero el mayor es
`s_def[2]` con **1,05e-9** — ninguna supera 1e-6. El problema **crece con el
horizonte**. La familia candidata es `s_def` (energía de la degradación).

**Arreglos posibles**, de menor a mayor invasividad:
1. `StartNodeLimit` — dejar que Gurobi trabaje más en reparar el arranque. En la
   corrida fallida lo intentó (`Another try with MIP start`) pero sólo tenía 60 s.
2. Subir `FeasibilityTol` para ese solve (p. ej. 1e-4). Legítimo pero afloja todo.
3. Sanear el punto antes de entregarlo: identificar la familia con mayor residuo
   y recalcular esas variables en vez de copiarlas.

---

## 4. Modo híbrido: benchmark de tres vías

### 4.1 Carga on board, `640kW_2dias`, 6 años — benchmark completo (2026-09-18)

Todo con el código actual (presolve, propagación directa, cota agregada,
`apertura_solo_primer_anio`), `--block_build_jobs 1`, `--consumption_model
wp2`. Logs `output/_presolve/bench_ob_*.log`, reportes en `output/_bench_ob/`.
Costos en la misma base (el monolítico; para el descompuesto solo se suma la
inversión en naves, 609.708, que su UB excluye). "tiempo" es pared, incluye
los ~13 min de construir bloques donde aplica.

| fase | configuración | mejor objetivo | mejor cota | gap | tiempo |
|---|---|---|---|---|---|
| A (original §4) | monolítico solo, 946 s | 9.299.056 | 5.207.678 | 44,0 % | 946 s |
| A' | monolítico solo, 600 s, opciones Gurobi por defecto | 9.295.910 | — | — | 600 s |
| **A2** | monolítico solo, **2.260 s** (presupuesto del híbrido), opciones de `setup.py` | **sin solución** | 4.427.925 | — | 2.260 s |
| **B** | descompuesto solo, 3 iteraciones | 7.746.932 (UB 7.137.224) | 3.856.476 | 50,2 % | 2.920 s + bloques ≈ 3.700 s |
| C0 | híbrido, 1 iter., `MIPFocus=3`, 600 s | 8.292.087 | 4.481.128 | 46,0 % | ≈ 2.260 s |
| C1 | híbrido, 1 iter., **`MIPFocus=1`**, 600 s | 8.291.929 | 5.791.564 | 30,2 % | ≈ 2.950 s |
| **C3** | híbrido, **3 iter.**, `MIPFocus=1`, 600 s | **7.040.022** | **5.834.467** | **17,1 %** | ≈ 4.380 s |

Lecturas:
- **El monolítico solo no es fiable a 6 años para encontrar incumbentes**: con
  600 s y opciones por defecto encontró uno; con 2.260 s y las opciones de
  `setup.py` (`MIPFocus=3`, `Heuristics=0.5`, `Presolve=2`) no encontró
  ninguno (1 nodo, 1,55 M iteraciones símplex). Eso, y no la calidad de la
  cota, es lo que justifica el híbrido.
- **La descomposición sola mejora el UB con las iteraciones** (7.682.499 →
  7.137.224 en la 3ª), aunque el LB no se mueva (§5.1). El tiempo por
  iteración NO se duplicó aquí (953, 870, 575 s): la duplicación de §5.2 se
  midió en swap a 2 años y no es general.
- **`MIPFocus=1` en la fase monolítica del híbrido** es mejor en todo: cota
  (5,79 M contra 4,48 M) e incumbente; con `MIPFocus=3` Gurobi se queda en la
  raíz. Es el default recomendable para `--mode hybrid` (flag `--mip_focus`).
- **Tres iteraciones antes del monolítico** dan el mejor resultado: arranque
  7.746.932 → 7.040.022 (−9,1 % en 600 s) y gap 17 %. Es la configuración a
  usar en la corrida de 11 años: `--mode hybrid --max_iter 3 --mip_focus 1`.
- Comparación a presupuesto igual: el híbrido C3 usa ~4.400 s; el monolítico
  solo con 2.260 s no tiene solución. No hace falta más afinado para decidir.

### 4.1bis Battery swapping, `160kW_2dias`, 6 años — benchmark rehecho (2026-09-18/19)

Código actual (presolve, propagación directa, apertura, pulido del MIP start),
`--block_build_jobs 1`, wp2. Logs `output/_presolve/bench_swap_*.log`,
reportes en `output/_bench_swap/`. Costos en la base del monolítico (al UB
del descompuesto se le suman 609.708 de inversión en naves).

| fase | configuración | mejor objetivo | mejor cota | gap | tiempo |
|---|---|---|---|---|---|
| A | monolítico solo, 600 s | **sin solución** | 4.294.643 | — | 600 s |
| B | descompuesto solo, 3 iter. | 4.858.328 (UB 4.248.620) | 3.718.318 | 12,5 % | 8.617 s + bloques ≈ 9.400 s |
| C | híbrido 1 iter., `MIPFocus=3`, **sin pulido** | arranque **rechazado** (4,9e-5), sin solución | 4.240.300 | — | ≈ 4.100 s |
| **C2** | híbrido 1 iter., `MIPFocus=1`, **con pulido** | 5.279.774 (= arranque) | 4.458.461 | 15,6 % | ≈ 4.200 s |

Por iteración de B: UB 4.670.066 → 4.670.066 → **4.248.620** (misma forma
que OB: la 2ª repite, la 3ª mejora), LB fijo en el LP monolítico; tiempos
3.189, 2.849, 2.269 s (bajan, no suben). El presolve dio (1,1,1) y cota
agregada redundante; 0 cortes de factibilidad.

Lecturas:
- Sin el pulido del MIP start el híbrido de swap **no funciona** a 6 años
  (§3): el arranque viola una fila por 4,9e-5 y Gurobi lo descarta. Con el
  pulido entra en 0,6 s y el monolítico explora 71 nodos, pero en 600 s no
  mejora el arranque.
- El descompuesto solo con 3 iteraciones (4.858.328, gap 12,5 %) le gana al
  híbrido de 1 iteración (5.279.774). La configuración que en OB fue la mejor
  —3 iteraciones + `MIPFocus=1`— **no se corrió en swap** (≈ 2,5 h más);
  es la primera corrida a hacer si se quiere el mejor número de swap.
- Cada iteración de swap cuesta ~3× la de OB (los MILP anuales de swap son más
  duros), pero el gap arranca en 20 % contra 50 %: el LP monolítico de swap
  está mucho más cerca del entero.

### 4.2 Corrida anterior del híbrido a 1 iteración (2026-09-18, antes del benchmark)

(`output/_presolve/hibrido_3naves_6y.log`, reporte en `output/_hibrido_6y/`;
`--mode hybrid --max_iter 1 --solve_timelimit 600 --block_build_jobs 1`):

| | monolítico solo (946 s) | monolítico solo (600 s) | híbrido con presolve |
|---|---|---|---|
| MIP start | — | — | **aceptado**, 8.292.208 en 0,87 s |
| mejor objetivo | 9.299.056 | 9.295.910 | **8.292.087** (−10,8 %) |
| mejor cota | 5.207.678 | — | 4.481.128 |
| gap | 44,0 % | — | 46,0 % |
| tiempo | 946 s | 600 s | 1.494 s desc. (incl. 13 min de bloques) + ~60 s puente + 705 s mono ≈ 2.260 s |

Lo que cambió respecto de la tabla original de abajo: el arranque entra (§3) y
el híbrido termina con un incumbente 10,8 % mejor que cualquiera del
monolítico solo. Lo que no cambió: en 600 s el monolítico **no mejora el
arranque** (8.292.208 → 8.292.087) y explora **1 solo nodo** (655 k
iteraciones símplex): el LP de raíz de 6 años se come el presupuesto, y su
cota queda por debajo de la del monolítico solo a 946 s. El costo total del
híbrido sigue siendo ~2,4× el del monolítico solo; la comparación a
presupuesto igual sigue pendiente (ver "defecto del protocolo" abajo). Para
horizontes largos la ventaja esperable del híbrido es la misma que en swap:
tener un incumbente cuando el monolítico solo no lo consigue.

--- lo que sigue es el benchmark original ---

Benchmark de tres vías, `X` fijada al mismo vector exógeno en las tres
configuraciones, 600 s de presupuesto por fase monolítica.

**swap, `160kW_2dias_1MB`, 6 años** — el híbrido gana claramente:

| método | tiempo | mejor objetivo | mejor cota | gap |
|---|---|---|---|---|
| monolítico solo | 643 s | **sin solución factible** | 1.444.293,10 | — |
| descompuesto solo (3 iter) | 736 s | 1.538.011,00 | 1.277.015,93 | 16,97% |
| híbrido (1 iter + B&B) | 637 s | 1.493.141,80 | 1.433.746,56 | **3,98%** |

**carga on board, `640kW_2dias`, 6 años** — el híbrido **pierde**:

| método | tiempo | mejor objetivo | mejor cota | gap |
|---|---|---|---|---|
| monolítico solo | 946 s | 9.299.056,41 | 5.207.678,10 | 44,00% |
| híbrido (1 iter) | 2.467 s | **sin solución** | 4.505.764,31 | — |

Desglose del híbrido: descomposición 2.060 s, puente 276 s, monolítico 131 s.

**Las dos causas**: los 9 cortes de factibilidad (§2) se comieron el presupuesto,
y el MIP start fue rechazado (§3). Cuando le tocó al monolítico le quedaban 60 s,
y su primer incumbente en la fase A había tardado 356 s.

**Defecto del protocolo**: el benchmark le da al monolítico
`presupuesto − tiempo_descomposición`, así que cuando la descomposición se pasa el
monolítico se queda sin nada. Conviene darle un presupuesto fijo. Aun así, 2.467 s
contra 946 s ya es desfavorable.

**La fase B (descompuesto solo) en carga on board nunca terminó** — se cortó a los
29 minutos para llegar antes a la fase C. Falta ese número.

---

## 5. Problemas de fondo, sin resolver

### 5.1 La cota inferior no mejora

En todas las corridas, de las dos ramas, **el LB queda clavado en el valor de la
relajación lineal del monolítico y los cortes del backward nunca lo superan**.

Medido en swap (`160kW_2dias_1MB`, 2 años, 10 iteraciones): LB = 810.135,52 desde
la primera iteración hasta la décima, sin moverse.

La causa está medida: en el punto de anclaje, la relajación LP del bloque del año
2 vale 270.275 contra 1.011.774 del MILP. **Ningún corte de Benders puede ser más
ajustado que la relajación de la que sale.** La vía de mejora no es afinar los
cortes sino atacar el gap de integralidad del subproblema anual.

### 5.2 El tiempo por iteración se duplica

Medido en swap, `160kW_2dias`, 2 años:

```
k=1  167.9s     k=2  435.2s     k=3  951.1s
```

Mientras tanto las cotas no se mueven. Los cortes acumulados llevan coeficientes
de hasta **9,18e7** y vuelven progresivamente más duro el MILP del primer año. Es
un límite real para horizontes largos y conviene mirarlo antes de correr 11 años.

### 5.3 El residuo de McCormick crece con el año

Medido en carga on board, 6 años:

```
anio 2:   -301.35      anio 3: -1,407.67      anio 4: -3,418.00
```

La solución descompuesta satisface la envolvente de McCormick, **no el bilineal
exacto**, y el error se acumula a medida que la degradación avanza. El modelo de
reporte se arma con McCormick para ser consistente (ver `build_report_model`),
pero eso no hace desaparecer el error: significa que la solución se aleja
progresivamente de la física real en los años tardíos. Con 11 años podría ser
considerable y conviene cuantificarlo antes de sacar conclusiones de los últimos
años del horizonte.

### 5.4 El caso de infactibilidad por integralidad

Cuando el año siguiente es infactible como MILP pero su relajación elástica da
`v = 0`, el corte de factibilidad clásico es **vacío** y el código aborta con un
error explícito. Ese es el caso donde haría falta un **corte lógico** derivado de
qué restricción provoca la infactibilidad (combinatorial Benders / IIS), no del
dual del LP.

Ocurrió realmente en carga on board antes de la discretización. Con `n_ssee_k`
entera el caso se volvió raro, pero el hueco sigue ahí.

Relacionado: el umbral `v <= 1e-7` de esa salvaguarda puede diagnosticar mal la
**cola de una convergencia lenta** — reportaría "infactible por integralidad"
cuando en realidad falta un residuo pequeño pero real. Conviene relativizarlo a la
escala del estado en vez de usar un absoluto.

---

## 6. Datos de entrada: lo que cambió y lo que quedó pendiente

### 6.1 Los perfiles de generación estaban en cero

`Timeseries.get_alpha_g` devuelve `0.0` sin avisar cuando no encuentra el día. La
lista de días representativos pedía los días-del-año `{1, 91}` y la hoja
`GenProfiles` sólo trae `{15, 105, 196, 288}`. **Ningún día coincidía**, así que la
generación y el almacenamiento estaban silenciosamente desactivados en todas las
corridas: el modelo podía invertir en solar/eólica, pagaba inversión y operación,
y recibía cero energía.

Corregido en las dos ramas: la lista pasa a `{15, 196}` (verano sin cobro de
potencia, invierno con cobro). Verificado: `alpha_g` pasa de 0 a medias de 0,29
(solar) y 0,35/0,82 (eólica, más en invierno).

**Consecuencia**: cualquier resultado anterior a este cambio tiene la generación
desactivada. La corrida guardada en `output/DCH_640kW_11anios/` (10 horas, gap
12,27%) es uno de ellos: su `G = 0` y `H = 0` son un artefacto, no un hallazgo.

### 6.2 La instancia de 1 macrobloque estaba infactible

`data/DCH/640kW_2dias_1MB` era infactible con cualquier lista de días y también
para el monolítico. Causa: `NodeAssignment` se recortó a los 4 LHDs del
macrobloque pero `ExtractionGoal` conservó las metas de la mina completa, dejando
nodos con meta que ningún equipo visita **ese año**. Falla en los años 1, 2, 3, 8
y 9; los años 4 a 7 eran consistentes.

Corregido poniendo en cero, **año por año**, la meta de los nodos no alcanzables
ese año (211 celdas). Respaldo en `time_series.xlsx.bak`.

**Pendiente en esa planilla**, sin tocar: la columna `macrobloque` tiene fórmulas
`XLOOKUP` rotas que apuntan a `NodeAssignment!$C$2:$C$289` (el rango de la mina
completa) y a filas más allá de las 95 que tiene la hoja. El código no lee esa
columna, así que no afecta al modelo, pero muestra error al abrir el archivo. Y
hay datos sueltos en las columnas ~1117 a 1278.

### 6.3 Otros

- **`stash@{0}`** contiene un `elmo_data.xlsx` de `640kW_2dias` modificado por el
  usuario, guardado durante un cambio de rama. Recuperable con `git stash apply`.
- **13 archivos `~$*.xlsx`** (locks de Excel) están versionados en el repo. Son
  basura de sesión que no debería estar ahí. Conviene borrarlos y agregar `~$*` al
  `.gitignore`, pero es decisión del usuario.
- **`consumer.py` duplica el valor 500.0** de `P_SSEE_STEP` porque lee resultados
  ya escritos y no importa el modelo. Si el paso cambia, hay que tocar los dos.

---

## 7. Pendientes menores

- **`--mode hybrid` deja `X` libre.** `build_report_model` arma el `OptModel` sin
  `exogenous_stations`, así que el monolítico del híbrido puede re-optimizar qué
  naves abrir, algo que la descomposición mantiene fijo. Los UB de
  `--mode decomposed` y `--mode hybrid` **no son comparables** tal cual. Decidir si
  el híbrido debe fijar `X`. **Medido (2026-09-18,
  `tests/test_apertura_primer_anio_ob.py`)**: a 2 años en `640kW_2dias` el
  monolítico con X libre deja `station_2` cerrada los dos años (2.393.464)
  contra 2.597.067 con las tres abiertas desde el año 1; una nave con equipo
  asignado NO está obligada a abrir. A 6 años coinciden porque desde el año 3
  las tres necesitan capacidad (presolve). `apertura_solo_primer_anio`
  (commiteada, `b6764a311`) elimina el *cuándo* pero no el *qué*; sin ella el
  monolítico abría `station_2` en el año 2 (2.390.855, dentro del MIPGap).
- **Cuántas iteraciones darle al híbrido.** Medido en swap a 6 años: 1 iteración da
  un arranque de 1.623.541 y 3 dan 1.538.011, un 5,3% mejor. Falta medir si un
  arranque mejor hace que cierre antes bajo presupuesto fijo.
- **El generador de perturbaciones** de `test_corte_fuera_del_ancla_ob.py` no
  consulta las cotas de las variables antes de perturbar, así que produce puntos
  infactibles que no informan (la mitad, cuando la generación estaba capada en
  cero). Debería consultar la cota superior y avisar si una familia queda sin
  ningún punto informativo.
- **Cuatro `clone()` sin portar** en `carga_ob_multiaño`, en los caminos
  lagrangeano, disyuntivo y fortalecido. No son el default
  (`strengthen=False`, `degradation_cut_mode="mccormick"`) y su corrección es
  delicada: además de clonar, desactivan restricciones, agregan otras y cambian el
  objetivo.
- **`apertura_solo_primer_anio`** validada en OB y portada a swap (ver arriba).

---

## 8. Cosas que conviene no volver a hacer

Aprendidas a los golpes en esta sesión:

- **`--block_build_jobs` por defecto (`min(años, cpus)`) puede colgar la corrida
  sin error.** Con 6 años lanza 5-6 workers de ~700 MB; con ~4 GB libres los
  mató la memoria, el `Pool` de `multiprocess` respawnea workers vacíos y
  `pool.map` espera para siempre (visto el 2026-09-18 en el híbrido: log vacío,
  proceso principal en 111 MB sin consumir CPU). Usar `--block_build_jobs 1`
  o `2` en esta máquina, y `python -u` para que el log salga en tiempo real.

- **No silenciar la salida del solver ni pasar `verbose=False`** en corridas
  largas. Pasó tres veces (el clonado, `OutputFlag=0`, la fase B del benchmark) y
  las tres terminaron en adivinar en vez de medir.
- **`TaskStop` mata el shell que se lanza, no sus hijos.** Un script que lanza
  subprocesos sigue vivo y avanzando. Llegó a haber **cuatro suites corriendo a la
  vez** escribiendo sobre los mismos logs, lo que invalidó una tanda entera de
  resultados. Verificar explícitamente que no quede nada vivo antes de relanzar.
- **Un chequeo en un solo punto no valida un corte.** El gate evalúa el corte en el
  óptimo del monolítico, y si el forward acierta ese óptimo, el punto coincide con
  el ancla: ahí todos los términos `mu*(x_hat - x)` se anulan y un corte con el
  signo invertido pasa igual. Por eso existe el test separado que perturba el
  estado a propósito.
- **Cambiar el modelo invalida las referencias cacheadas.** La clave de caché del
  gate incluye los hashes de `functions.py`, `setup.py` y los Excel de la
  instancia, justamente para que un cambio no deje en pie una referencia calculada
  sobre otro problema.
- **Verificar antes de afirmar.** Se afirmó que eliminar el clonado daba 100x; el
  A/B limpio dio 1,5x. Aquella medición se tomó con la máquina paginando: el
  número era real pero no representativo, y quedó escrito en comentarios de código
  y mensajes de commit hasta que se corrigió.
