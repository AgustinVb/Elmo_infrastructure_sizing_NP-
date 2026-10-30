# Implementación: L-shaped entero multicorte para battery swapping

Instrucciones para Claude Code. Proyecto: modelo de inversión–operación de LHD eléctricos (`battery_swapping_multiaño`). Notación del Capítulo 3 de la tesis.

**Algoritmo:** L-shaped entero de Laporte y Louveaux (1993), en versión multicorte con un θ por (año, día), cortes alternados LP/MILP (Angulo et al., 2016) e implementado como *branch-and-cut* con cortes *lazy*. El esqueleto (maestro con variables de presupuesto, subproblemas en paralelo, esquema en dos fases) sigue a Jacobson et al. (2023) y a Pecci y Jenkins (2025). La DP de inversión y el pulido actuales se usan como heurísticas primales.

---

## 0. Reglas generales

- Leer primero el código existente: modelo diario, Nested Benders (copias de estado y duales), `cota_cargas_lhd.py` / `corte_cargas_op.py`, `mejorar_ub.py` / `bd.pulir`, la DP de inversión y el driver. Usar el mismo framework y las mismas estructuras.
- Todo lo nuevo va detrás del flag `--esquema lshaped`. Con el flag apagado, el comportamiento actual no cambia.
- Usar el mismo modelo en todo (por ejemplo, `ELMO_PIPELINE_CICLICO`, `ELMO_TOPE_ESTACION`). Si cambia el modelo, se invalidan los cortes guardados.
- Registrar en el log: LB, UB y gap por iteración; número de cortes de cada tipo; llamadas y tiempos de LP y MILP de los subproblemas; soluciones aportadas por cada heurística.
- No modificar ecuaciones del modelo sin avisar al usuario.

---

## 1. Maestro (MILP chico)

### Variables
| Grupo | Variables | Restricciones que se mantienen |
|---|---|---|
| Inversión por nave y año | X_{k,y}, ΔX_{k,y}, N^bays, N^C, N^bat (y sus incrementos) | (3.3)–(3.9), (3.19)–(3.22), tope de estación |
| Inversión del año 1 | q = (G_g, H, P^ssee_k) | (3.7), (3.92)–(3.93) |
| Batería y reemplazos | B_y, D_y, R_y, Z^repl_y | (3.36)–(3.38), (3.41), (3.42), (3.48)–(3.50) |
| **Presupuesto de potencia** | P^pot_y | pasa al maestro (antes en (3.74)) |
| **Presupuesto de energía diaria** | e_{y,d} ≥ 0 | ver abajo |
| Costo de operación | θ_{y,d} ≥ L_{y,d} | cortes |

### Degradación en el maestro
Los ciclos anuales se calculan con los presupuestos de energía:
```
energía anual de la flota = Σ_d (365/|D|) · e_{y,d}
```
y reemplazan el lado derecho de (3.40)/(3.59). Mantener la linealización McCormick (3.51)–(3.58) en el maestro.
**Advertencia:** McCormick es una relajación. La LB sigue siendo válida, pero al evaluar la UB la degradación debe verificarse con las ecuaciones exactas.

### Objetivo
```
min  costos de inversión de (3.2) (naves, estaciones, cargadores, baterías, subestación, G, H)
   + Σ_y descuento_y · [ 12 · c^Pelec · P^pot_y + Z^repl_y · c^bat + O&M de G y H ]
   + Σ_{y,d} θ_{y,d}
```
θ_{y,d} ya incluye el factor (365/|D|) y el descuento del año.

### Fase 2: discretización (solo para los cortes enteros)
El corte entero requiere que toda la primera etapa que afecta a un día sea binaria. En la Fase 2:
- Las enteras chicas (N^C, N^bat, N^bays ≤ 4, X, R) se codifican en binario.
- q, B_y, P^pot_y y e_{y,d} se discretizan en pocos niveles binarios (`--niveles_q`, `--niveles_B`, `--niveles_pot`, `--niveles_e`).
- El pulido final libera de nuevo estas variables como continuas.

---

## 2. Subproblema SP_{y,d}(x̂)

Operación de un día (año y, día representativo d) con la primera etapa fija:
- estación del año y en cada nave (X, N^bays, N^C, N^bat),
- q = (G, H, P^ssee),
- B_y (capacidad de batería del año),
- tope P^red ≤ P^pot_y si d ∈ D^peak (en intervalos de T^HP),
- presupuesto: **Σ_{i,j,t} Y_{i,j,y,d,t} · e_{i,j} · n_{i,j} ≤ e_{y,d}**.

Restricciones: las del día que ya existen en el modelo (swap (3.26)–(3.35), inventario (3.60)–(3.69) con el pipeline cíclico si está activo, potencia (3.71)–(3.73), operación (3.75)–(3.91), generación y BESS (3.94), (3.96)–(3.101), producción (3.77)–(3.78)). Con B_y fijo, (3.33)–(3.35) dejan de ser bilineales.

Objetivo: costo de energía del día, con el factor (365/|D|) y el descuento.

**Copias de estado:** cada parámetro fijado por el maestro entra al subproblema como variable copia continua, igualada al valor del maestro (igual que en el NB actual), para obtener los duales π.

**Factibilidad:** agregar holguras con penalización alta en el presupuesto y en la producción (como Pecci y Jenkins), para que el subproblema siempre sea factible. Si la holgura queda activa en la solución final, reportarlo.

Hay 10 años × 4 días = 40 subproblemas por evaluación, todos independientes → ejecución en paralelo.

---

## 3. Cortes

### 3.1 Corte de Benders (LP)
Resolver la relajación LP de SP_{y,d}(x̂) **más el corte de cargas** Σ X_ini ≥ n_corte[y,d]:
```
θ_{y,d} ≥ v̂_{y,d} + π_{y,d} · (x − x̂)
```
- π_{y,d}: duales de las restricciones de copia.
- **Validez:** n_corte debe calcularse con **b^max** (la capacidad más favorable), no con el B̂_y actual. Así el corte vale para cualquier B_y que elija el maestro.

### 3.2 Corte entero (Laporte–Louveaux)
Resolver el MILP de SP_{y,d}(x̂), con límite de tiempo:
```
θ_{y,d} ≥ (Q̂_{y,d} − L_{y,d}) · ( Σ_{i∈S} x_i − Σ_{i∉S} x_i − |S| + 1 ) + L_{y,d}
```
- x_i: las binarias (de la Fase 2) que afectan al día (y, d).
- S: las binarias iguales a 1 en x̂.
- **Q̂_{y,d}: la mejor cota inferior del MILP** (no la mejor solución), para que el corte sea válido aunque el MILP no cierre (Riley et al., 2025).
- L_{y,d}: cota inferior global del día, calculada **una vez al inicio** con el LP del día con todas las capacidades en su máximo, P^pot y e_{y,d} sin restringir, y n_corte con b^max.

### 3.3 Regla de alternancia (Angulo et al., 2016)
En cada solución entera x̂ del maestro:
1. Resolver los 40 LP en paralelo y agregar los cortes de Benders que corten la solución (θ̂_{y,d} < v̂_{y,d} − tol).
2. Si **ninguno** corta, resolver el MILP de los días cuyo θ̂_{y,d} todavía está bajo su valor entero conocido o desconocido, y agregar los cortes enteros.
3. Guardar en caché por (y, d, x̂ restringido a sus binarias relevantes) los valores LP y MILP, para no resolver dos veces.

### 3.4 Refuerzo opcional por monotonía
Si la Fase 0 confirma que el costo de un día no crece cuando aumentan las capacidades, el corte entero de x̂ también vale para toda inversión **menor o igual** a x̂ en esas componentes. Implementarlo solo después de verificar la monotonía (flag `--corte_monotono`).

---

## 4. Algoritmo

### Fase 0: Verificaciones (obligatoria)
1. **Reproducción del incumbente:** con la solución actual, fijar el maestro en sus valores (incluidos P^pot_y y e_{y,d} = energía real de cada día) y resolver los 40 MILP. La suma de inversión más Σ Q_{y,d} debe reproducir su costo (tolerancia 1e-6 relativa). Si no coincide, encontrar el término faltante (P^pot, reemplazos, descuentos, 365/|D|).
2. **Validez de las cotas:** para el incumbente, el LP + corte de cargas de cada día ≤ su costo MILP, y L_{y,d} ≤ ambos.
3. **Monotonía:** variar una componente a la vez (N^bat, N^C, N^bays, G, H, P^ssee, B_y, P^pot, e_{y,d}) y confirmar que el costo del día no aumenta. **H es el componente de riesgo** por (3.99) y (3.101): si a^min > 0, la monotonía puede fallar. Reportar en una tabla.

Entregable: `verificacion_lshaped.md`.

### Fase 1: Maestro continuo (arranque)
- Relajar la integralidad de toda la primera etapa.
- Solo cortes de Benders (LP + corte de cargas), con la estabilización ya existente (`--estabilizacion`).
- Iterar hasta que la LB converja o por límite de tiempo → LB₁ y conjunto de cortes.

### Fase 2: *Branch-and-cut* entero
- Maestro con integralidad y discretización (sección 1).
- Cargar los cortes de la Fase 1 (siguen siendo válidos).
- **Solución inicial (MIP start):** la DP de inversión actual + forward con infraestructura fija + pulido (ver sección 5).
- **Callback en Gurobi** (`MIPSOL`):
  1. Leer x̂ y θ̂.
  2. Regla de alternancia (3.3) → agregar cortes con `cbLazy`.
  3. Si x̂ es prometedor (costo de inversión + Σ θ̂ < UB), armar la solución completa con los MILP del día, pulir y, si mejora, entregarla con `cbSetSolution` (en el siguiente `MIPNODE`).
- **Paralelismo:** el pool de procesos para los 40 subproblemas se crea una sola vez fuera del callback. Cada proceso tiene su propio entorno de Gurobi y sus modelos diarios ya construidos, y solo se le actualizan los parámetros fijados.
- **Parada:** (UB − LB)/UB ≤ ε o límite de tiempo.

### Resultado
LB = cota del árbol del maestro. UB = mejor solución pulida. Reportar ambos y el gap.

---

## 5. Uso de las DP (heurísticas primales)

### 5.1 DP de inversión
1. **Al inicio:** con el consumo estimado por año (de la Fase 1), la DP arma el plan de inversión y reemplazos → evaluar con los MILP diarios y el pulido → MIP start.
2. **Durante la Fase 2** (cada `--frec_dp` soluciones enteras del maestro o cuando la LB mejora): correr la DP usando como costo de cada (año, configuración):
   - el costo MILP real si ya se evaluó (caché),
   - si no, la estimación del maestro: el máximo de los cortes de θ en esa configuración.
   Evaluar la trayectoria devuelta y, si mejora la UB, entregarla con `cbSetSolution`.
3. **Al final:** correr la DP con los consumos reales de la mejor solución para reajustar los reemplazos (la segunda pasada actual).

### 5.2 DP de la estación
Dentro del pulido: con el perfil de swaps de cada día, recalcular el calendario óptimo de carga de la estación (DP ya validada con el pipeline cíclico) para mejorar el costo de energía sin resolver el MILP de nuevo.

---

## 6. Flags propuestos
```
--esquema lshaped
--fase1_tiempo S              (tiempo máximo de la Fase 1)
--estabilizacion {ninguna, caja, regularizacion}
--niveles_q N --niveles_B N --niveles_pot N --niveles_e N
--milp_tiempo S               (límite por MILP diario; Q̂ = mejor cota)
--corte_monotono              (solo tras verificar la monotonía)
--frec_dp K                   (cada cuántas soluciones enteras corre la DP)
--hilos_sp N                  (procesos paralelos para los subproblemas)
--tol_corte E
```

---

## 7. Experimentos

Casos: los 8 de la cola actual. Comparar con el esquema actual (NB + certificado).

| Caso | Gap actual | LB (L-shaped) | UB (L-shaped) | Gap | Tiempo | Cortes LP | Cortes enteros | Soluciones de la DP |
|---|---|---|---|---|---|---|---|---|
| 1 Base/norte/0,5C/RED | 1,06 % | | | | | | | |
| … | | | | | | | | |

Reportar también:
- LB al final de la Fase 1 frente a la relajación operacional actual (LB₀).
- Cuánta LB aporta el corte de cargas y cuánta los cortes enteros.
- Gap promedio de los MILP diarios (limita el gap final).
- Sensibilidad a la discretización de la Fase 2 (niveles gruesos frente a finos) en un caso.

Entregable: `resultados_lshaped.md` con la tabla y una conclusión de 5 líneas.

---

## 8. Advertencias de validez (resumen)
- Corte de cargas en los cortes de LP: siempre con b^max.
- Corte entero: con la **mejor cota** del MILP, nunca con la mejor solución si el MILP no cerró.
- L_{y,d}: cota global calculada con capacidades máximas.
- Corte entero solo con primera etapa binaria (Fase 2 discretizada).
- Monotonía: verificar antes de usarla; H es el caso dudoso.
- McCormick de la degradación: relajación en el maestro, verificación exacta en la UB.
- El gap final no puede ser menor que lo que permiten las cotas de los MILP diarios.

---

## Referencias
- Laporte, G. y Louveaux, F. V. (1993). The integer L-shaped method for stochastic integer programs with complete recourse. *Operations Research Letters*, 13(3), 133–142.
- Angulo, G., Ahmed, S. y Dey, S. S. (2016). Improving the integer L-shaped method. *INFORMS Journal on Computing*, 28(3), 483–499. https://doi.org/10.1287/ijoc.2016.0695 · https://optimization-online.org/?p=12847
- Riley, B. P., Daoutidis, P. y Zhang, Q. (2025). Integer L-shaped method with non-supporting no-good optimality cuts. *Preprint*. https://arxiv.org/abs/2511.06340
- Jacobson, A., Pecci, F., Sepulveda, N., Xu, Q. y Jenkins, J. (2023). A computationally efficient Benders decomposition for energy systems planning problems with detailed operations and time-coupling constraints. *INFORMS Journal on Optimization*. https://doi.org/10.1287/ijoo.2023.0005 · https://arxiv.org/abs/2302.10037
- Pecci, F. y Jenkins, J. D. (2025). Regularized Benders decomposition for high performance capacity expansion models. *IEEE Transactions on Power Systems*, 40(4), 3105–3116. https://arxiv.org/abs/2403.02559
- Birge, J. R. y Louveaux, F. V. (1988). A multicut algorithm for two-stage stochastic linear programs. *European Journal of Operational Research*, 34(3), 384–392.
- Fischetti, M., Ljubić, I. y Sinnl, M. (2017). Redesigning Benders decomposition for large-scale facility location. *Management Science*, 63(7), 2146–2162.
- Schneider, F., Thonemann, U. W. y Klabjan, D. (2017). Optimization of battery charging and purchasing at electric vehicle battery swap stations. *Transportation Science*. https://doi.org/10.1287/trsc.2017.0781
