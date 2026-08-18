# Descomposición por año (Nested Benders) para el modelo de carga on-board multi-año

Guía de implementación de la descomposición temporal del modelo de planificación de
infraestructura de carga on-board con inversión multi-año (rama `carga_ob_multiaño`).
Adapta el algoritmo de Lara, Mallapragada, Papageorgiou, Venkatesh y Grossmann (2018)
al caso sin retiro de activos, con cortes de **Benders** como punto de partida.

Este documento asume que ya se corrigió el índice de degradación en el monolítico
(reemplazo al inicio del año, capacidad operable calculada con los ciclos heredados)
según lo acordado.

---

## 1. Resumen ejecutivo

El modelo monolítico resuelve simultáneamente todos los años del horizonte. La
descomposición lo reemplaza por una secuencia de subproblemas de **un año**, que
intercambian entre sí únicamente:

- **hacia adelante**, el estado del sistema (infraestructura acumulada y degradación
  heredada), y
- **hacia atrás**, cortes que aproximan por debajo el costo de los años futuros.

Iterando ambos intercambios (forward pass y backward pass), las cotas superior e
inferior del costo total se cierran hasta una tolerancia prefijada.

Tres hechos hacen que este problema sea un caso favorable:

1. **Todos los acoplamientos temporales son adyacentes** (`y-1 → y`). Al no haber
   retiro de activos (vida útil > horizonte), no existen enlaces `y-LT → y`; cada
   corte se agrega al año inmediatamente anterior, sin desfases.
2. **El estado es compacto.** Cuatro familias de variables de estado, de las cuales
   solo una es entera.
3. **Recurso completo garantizado sin holguras.** La red eléctrica abastece el balance
   de potencia (54), de modo que cualquier estado heredado admite una operación
   factible: no hace falta agregar variables de holgura penalizadas.

---

## 2. Variables de estado y variables locales

### 2.1 Variables de estado (linking)

Son las que aparecen en restricciones que cruzan períodos, y por tanto viajan entre
subproblemas anuales. En este modelo:

| Estado `x_y`        | Dominio            | Reemplaza a  | Balance origen | Decisión local asociada     |
|---------------------|--------------------|--------------|----------------|-----------------------------|
| `N^C_{k,y}`         | entero ≥ 0         | `N^C_k`      | (3)            | `ΔN^C_{k,y}`                |
| `G_{g,y}`           | continuo ≥ 0       | `G_g`        | (4)            | `ΔG_{g,y}`                 |
| `H_y`               | **continuo ≥ 0**   | `H_h`        | (5)            | `ΔH_y`                     |
| `AN^ciclos_y`       | continuo ≥ 0       | —            | (21)–(22)      | `R_y`, `N^ciclos_y`         |

> **Único estado entero: `N^C_{k,y}`** (cargadores acumulados). `H_y` es continuo,
> `G_y` y `AN^ciclos_y` también. Esto es relevante para la elección de corte
> (Sección 6): la relajación lineal solo afloja una familia de variables de estado,
> lo que favorece al corte de Benders.

**Fuera del estado:**

- Las estaciones `X_{k,y}` están **fijas** en esta configuración: entran como
  parámetro exógeno en (7)–(8), no como variable de estado. La restricción de acople
  (2) y su incremento `ΔX_{k,y}` no se generan.
- Las variables de incremento `Δ(·)_y` son **locales**, no de estado. A diferencia
  de la referencia (donde `ngb` era estado por el retiro a `t+LT`), aquí ningún año
  futuro observa los incrementos: solo afectan el balance de acumulación del propio
  año y su costo de inversión (1).

### 2.2 Variables locales `y_y`

Todo lo demás intra-anual:

- Incrementos de inversión `ΔN^C_{k,y}`, `ΔG_{g,y}`, `ΔH_y`.
- Operación horaria completa: `P_{k,i,y,d,t}`, `Z^charge`, `Y_{i,j,y,d,t}`,
  `Z_{i,y,d,t}`, `Start/EndCharge`, `Start/EndAssign`, `M_{i,j,y,d,t}`.
- Estado de energía de baterías `B_{i,y,d,t}`.
- Potencias de red / generación / batería: `P^red`, `P^gen`, `P^bat`, `Curt`, `A`.
- Decisiones de degradación del año: `R_y` (binaria), `N^ciclos_y`, `B_y`, `S_y`.

### 2.3 Copias del estado previo `z_y`

Variables continuas duplicadas que representan el estado del año anterior dentro del
subproblema del año `y`:

`z_y = (N^{C,prev}_{k,y}, G^{prev}_{g,y}, H^{prev}_y, AN^{ciclos,prev}_y)`

**Se declaran continuas aunque el estado original sea entero.** Esto es lo que
garantiza que la restricción de enlace tenga un dual bien definido al relajar el
subproblema.

---

## 3. Restricciones de acople

Cada estado del año anterior se duplica en una copia continua, fijada por igualdad a
un parámetro que contiene el valor heredado del forward pass. El dual de esa igualdad
es el que forma el corte.

```
# Infraestructura (adyacente, y-1 → y)
N^{C,prev}_{k,y} = N̂^C_{k,y-1}     ← μ^{NC}_{k,y}
N^C_{k,y}        = N^{C,prev}_{k,y} + ΔN^C_{k,y}

G^{prev}_{g,y}   = Ĝ_{g,y-1}        ← μ^{G}_{g,y}
G_{g,y}          = G^{prev}_{g,y}   + ΔG_{g,y}

H^{prev}_y       = Ĥ_{y-1}          ← μ^{H}_{y}
H_y              = H^{prev}_y       + ΔH_y
H_y              ≤ n^{H,max}

# Degradación (adyacente, y-1 → y)
AN^{ciclos,prev}_y = ÂN^ciclos_{y-1}   ← μ^{AN}_{y}
AN^ciclos_y        = (1 - R_y)·AN^{ciclos,prev}_y + N^ciclos_y
```

**Condición de borde (`y = y_1`).** Las copias se fijan a los stocks iniciales:

```
N̂^C_{k,y_0} = n^{C,0}_k      (0 en greenfield)
Ĝ_{g,y_0}   = g^0_g          (0 en greenfield)
Ĥ_{y_0}     = h^0            (0 en greenfield)
ÂN^ciclos_{y_0} = 0          (batería nueva → B_{y_1} = b^max)
R_{y_1}     = 0  (fijado; no tiene sentido reemplazar batería nueva)
```

Los cuatro parámetros `(N̂^C, Ĝ, Ĥ, ÂN^ciclos)` son los que se actualizan entre
iteraciones con el valor del forward pass. **En el subproblema son parámetros
mutables, no variables.**

---

## 4. Bloque de degradación linealizado (Opción A)

Con la convención de **reemplazo al inicio del año** (si `R_y = 1`, la flota opera ese
mismo año con batería nueva) y con `ÂN^ciclos_{y-1}` como parámetro heredado, el bloque
de degradación queda lineal salvo un único big-M, sin la expansión binaria (25)–(31)
ni la linealización (32)–(34) del monolítico.

```
# Capacidad operable del año — lineal en R_y (el coeficiente es constante)
B_y = b^max - γ_m·ÂN^ciclos_{y-1} + γ_m·ÂN^ciclos_{y-1}·R_y
      #   R_y = 0  ⇒  B_y = b^max - γ_m·ÂN^ciclos_{y-1}   (opera degradada)
      #   R_y = 1  ⇒  B_y = b^max                          (opera nueva)

# Energía total cargada por la flota en el año (24)
S_y = Δt · (365/‖D‖) · Σ_{(k,i)∈KI} Σ_{d,t} P_{k,i,y,d,t}

# Ciclos equivalentes — forma producto para evitar la división (20 reescrita)
n^elhd · B_y · N^ciclos_y = S_y        # un único big-M sobre N^ciclos_y ∈ [N^L, N^U]

# Arrastre hacia el futuro (21) — lineal (binaria × parámetro)
AN^ciclos_y = (1 - R_y)·ÂN^ciclos_{y-1} + N^ciclos_y
```

> **Big-M:** como `B_y` toma solo dos valores conocidos (según `R_y`), el producto
> `B_y · N^ciclos_y` se linealiza exacto con un solo big-M. **Recalcular la cota
> `N^ciclos_y ∈ [N^L, N^U]` con el `B_y` del año, no con una constante global**, o el
> big-M queda flojo y debilita los cortes.

> **`N^ciclos_y` continua:** en el monolítico era entera solo para habilitar la
> expansión binaria; al descomponer, esa expansión desaparece y puede volver a ser
> continua (un EFC es un cociente de energías, inherentemente real).

**Implicancia de la convención elegida:** el año con `R_y = 1` estrena capacidad ese
mismo año. Este esquema es el fiel a la física de reemplazo-al-inicio; a cambio
mantiene un big-M en el subproblema (frente al esquema reemplazo-al-final, que sería
100 % lineal pero introduce un año de "capacidad hundida").

---

## 5. Subproblema del año `y`

Forma concisa del subproblema `(P_{y,k})` para el año `y` en la iteración `k`:

```
Φ_{y,k}(x̂_{y-1,k}) =  min    f_y(x_y, y_y) + φ_{y,k}(x_y)
                    x_y,y_y,z_y

           s.a.    z_y = x̂_{y-1,k}          ← μ_{y,k}
                   (x_y, y_y, z_y) ∈ X_y
```

**Región factible `X_y`** = todas las restricciones del año, ya desacopladas:

- Acoples y balances de acumulación (Sección 3) + cota `H_y ≤ n^{H,max}`.
- Estaciones / cargadores (7)–(14) sobre el stock `N^C_{k,y}` (`X_{k,y}` fijo).
- Operación de baterías (15)–(18) con `B_y` la capacidad del año.
- Degradación linealizada (Sección 4).
- Distribución eléctrica (35)–(37).
- Operación de vehículos (38)–(42).
- Producción (43)–(45).
- Detenciones operacionales, esquema DCH (46)–(51).
- Generación y almacenamiento (52)–(59), si el escenario las define.

**Costo del año `f_y(x_y, y_y)`** = el término correspondiente de la suma sobre `y`
en (1):

- inversión anualizada del año: `AF_y(r,Y) · [c^s·ΔX + (...)·ΔN^C + (c^inv+c^op)·ΔG + (...)·ΔH]`
  (los `Δ(·)` locales; `ΔX = 0` porque las estaciones están fijas),
- operación descontada por `1/(1+r)^{pos(y)}`: energía `c^elec·P^red`, potencia
  contratada `c^Pelec·P^pot·12`,
- reemplazo puntual descontado: `n^elhd · c^bat · R_y`.

> El costo de inversión se devenga sobre los **incrementos** `Δ(·)_y` (locales), no
> sobre el stock acumulado. Por eso el costo total es separable por año sin doble
> conteo: cada activo paga una sola vez, el año en que se construye.

**Rol dual de cada año** (no hay maestro/subproblema separados):

- Es *maestro* respecto de `y+1`: le envía el estado y recibe cortes que acotan `α_y`.
- Es *subproblema* respecto de `y-1`: recibe el estado fijado y devuelve
  `(Φ_{y,k}, μ_{y,k})`.

---

## 6. Función cost-to-go y cortes de Benders

### 6.1 Cost-to-go

Una variable escalar `α_y` por año, acotada por debajo por los cortes acumulados:

```
φ_{y,k}(x̂_{y,k}) := min { α_y : α_y ≥ Φ_{y+1,k} + μ_{y+1,k}·(x̂_{y,k} − x_y), ∀k }
```

- El objetivo de cada subproblema es `min f_y + α_y`.
- `α_Y = 0` fijado en el último año (no tiene futuro).
- Los cortes son **acumulativos** (nunca se eliminan) y **específicos de cada año**.
- Como todos los enlaces son adyacentes, el corte generado para `y+1` se agrega a la
  lista del año `y`, sin desfases.

### 6.2 Corte de Benders (punto de partida)

Se convexifica el subproblema `y+1` por su **relajación lineal**. El corte usa el valor
óptimo del LP y sus duales:

```
α_y ≥ Φ^LP_{y+1,k} + μ_{y+1,k}·(x̂_{y,k} − x_y)     ∀k
```

donde `Φ^LP` y `μ` provienen de resolver la relajación lineal del subproblema `y+1`,
leyendo los duales de las cuatro igualdades de acople.

**Por qué empezar con Benders en este modelo:**

- Es el corte más barato: una sola resolución de LP por año e iteración, con duales
  directos del solver.
- Con **un solo estado entero** (`N^C`), la relajación lineal afloja poco: se espera
  que quede cerca del MILP y que el corte sea casi tan fuerte como los alternativos.
- Es el corte que mejor rindió en el caso ERCOT del paper, por la misma razón
  (formulación apretada, relajación LP cercana).

**Limitación a tener presente:** con estados enteros puede existir brecha de dualidad,
así que **no hay convergencia finita garantizada**. Se detiene por tolerancia o por
número máximo de iteraciones. Si el gap se estanca, la ruta de mejora es:

1. Benders (este documento).
2. Strengthened Benders — una evaluación Lagrangeana con los duales del LP, sin
   subgradiente; casi el mismo costo, cortes más fuertes.
3. Lagrangeano con subgradiente — el más fuerte y el más caro; red de seguridad.

---

## 7. El algoritmo

### 7.1 Forward pass (`y = y_1 → Y`)

Genera una solución factible y la cota superior.

1. Para cada año en orden, fijar los parámetros de estado heredado
   `(N̂^C, Ĝ, Ĥ, ÂN^ciclos)` con el óptimo del año anterior de esta iteración
   (en `y_1`, con los stocks iniciales).
2. Resolver el subproblema como **MILP completo** (sin relajar).
3. Guardar el estado óptimo `x̂_{y,k}` (incluye `ÂN^ciclos_y`, que además fijará `B_{y+1}`).
4. Conservar los cortes de iteraciones anteriores (ya están en la lista de cada año).

```
UB_k = Σ_y ( Φ_{y,k} − α_{y,k} )      ;   UB ← min(UB, UB_k)
```

Se resta `α_{y,k}` para no contar dos veces el costo futuro.

### 7.2 Backward pass (`y = Y → y_1`)

Genera los cortes y la cota inferior.

1. Los estados `x̂_{y,k}` quedaron fijados en el forward de esta iteración.
2. Resolver la **relajación lineal** del subproblema de cada año.
3. Leer los duales `μ_{y,k}` de las cuatro igualdades de acople.
4. Construir el corte de Benders y agregarlo a la lista del año `y-1`.

```
LB_k = Φ_1,k      ;   LB ← max(LB, LB_k)
```

### 7.3 Criterio de parada

```
mientras  (UB − LB)/UB > ε₁   y   k < MaxIter:
    k ← k+1
    forward pass   → UB_k
    backward pass  → LB_k
devolver la MEJOR solución factible encontrada y el gap
```

> **La UB no es monótona:** cada forward produce una solución factible distinta.
> Guardar la mejor encontrada, no la última.

---

## 8. Pseudocódigo

```
ENTRADA: datos del escenario, años Y, ε₁, MaxIter

# --- Construcción (una sola vez) ---
por cada año y ∈ Y:
    construir subproblema P_y con:
        - variables locales y_y (operación, incrementos, degradación)
        - variables de estado x_y = (N^C, G, H, AN^ciclos)
        - copias continuas z_y y restricciones de enlace z_y = (param heredado)
        - variable de costo futuro α_y   (fijar α_Y = 0)
        - lista de cortes vacía
        - parámetros mutables de estado heredado (N̂^C, Ĝ, Ĥ, ÂN^ciclos)
fijar en y_1 los parámetros heredados a los stocks iniciales y R_{y_1}=0

LB ← −∞ ;  UB ← +∞ ;  k ← 0

MIENTRAS (UB − LB)/UB > ε₁  Y  k < MaxIter:
    k ← k+1

    # ===== FORWARD PASS  (y = y_1 → Y) =====
    PARA y = y_1 … Y:
        actualizar parámetros heredados de P_y con x̂_{y-1,k}
        resolver P_y como MILP completo   →  Φ_{y,k} , α_{y,k}
        almacenar el estado óptimo  x̂_{y,k}
    UB_k ← Σ_y (Φ_{y,k} − α_{y,k}) ;  UB ← min(UB, UB_k)

    # ===== BACKWARD PASS  (y = Y → y_1+1) =====
    PARA y = Y … y_1+1:
        resolver la relajación LP de P_y   →  Φ^LP_{y,k}
        leer duales μ_{y,k} de las igualdades de enlace
        añadir a la lista de cortes del año y-1:
            α_{y-1} ≥ Φ^LP_{y,k} + μ_{y,k}·(x̂_{y-1,k} − x_{y-1})
    resolver P_{y_1} relajado  →  LB_k ← Φ_{1,k} ;  LB ← max(LB, LB_k)

DEVOLVER la mejor solución factible y el gap (UB − LB)/UB
```

---

## 9. Arquitectura de implementación (Pyomo)

Basada en la estructura del repositorio de referencia (`cristianallara/SDDiP`),
adaptada al caso determinista (sin árbol de escenarios).

### 9.1 Módulos sugeridos

| Módulo               | Responsabilidad                                                        |
|----------------------|------------------------------------------------------------------------|
| `read_data`          | Ingesta del escenario; cada parámetro como diccionario para Pyomo.     |
| `opt_blocks`         | Un `Block` de Pyomo por año, con variables locales, de estado, copias, y dos `ConstraintList` vacías (enlaces y cortes). |
| `forward`            | Resuelve un bloque como MILP; devuelve estado óptimo y costo neto de α. |
| `backward`           | Resuelve el bloque relajado; lee duales de los enlaces; devuelve μ y costo. |
| `driver`             | Parámetros del usuario, bucle de iteraciones, actualización de estado, construcción de cortes, cotas y gap. |

> No se requiere `scenario_tree`: el modelo es determinista multi-período, el "árbol"
> degenera en una única rama y el forward recorre los años en secuencia.

### 9.2 Las claves de diseño (aplicadas a este modelo)

1. **Un bloque por año, construido una sola vez.** El modelo se instancia al inicio
   y nunca se reconstruye; entre iteraciones solo cambian valores de parámetros.
2. **Estado heredado como parámetro mutable.** Los cuatro `(N̂^C, Ĝ, Ĥ, ÂN^ciclos)`
   son `Param(mutable=True)` dentro de la igualdad de enlace. Actualizarlos es una
   asignación; la estructura no se toca.
3. **Listas de restricciones vacías al construir.** Enlaces y cortes viven en
   `ConstraintList` que empiezan vacías y se llenan sobre la marcha.
4. **Duales vía `Suffix` de importación.** Declararlo en cada bloque antes de
   resolver. Atención al signo al leer `μ` (suele invertirse respecto del solver).
5. **Una `α_y` por bloque, fijada a cero en el último año.** Fijar en `y_Y`, liberar
   en el resto. Es la línea que más se olvida y produce cotas incorrectas.
6. **El mismo bloque, dos modos de resolución.** Forward: MILP con tolerancia de gap.
   Backward: opción de relajación de integralidad del solver sobre el mismo objeto.
7. **Estados almacenados por año E iteración.** Indexar los diccionarios por
   `(variable, año, iteración)`: los cortes de iteraciones previas referencian el
   estado de aquella iteración.

### 9.3 Flujo de ejecución

1. Configuración: años, MaxIter, tolerancia.
2. Datos y estructura: leer escenario, construir todos los bloques una vez.
3. `Suffix` de duales en cada bloque.
4. Poblar las igualdades de enlace (copia = parámetro mutable).
5. Forward: actualizar estado heredado, resolver MILP, guardar por `(var, y, k)`.
6. Cota superior: acumular costos, tomar el mínimo histórico compatible con LB.
7. Backward: fijar estados del forward, resolver relajado, leer duales, añadir corte.
8. Cotas y parada: `LB = Φ_{1}`, calcular gap, decidir si continuar.

---

## 10. Trampas específicas de este modelo

- **Big-M de degradación flojo.** Recalcular `[N^L, N^U]` con el `B_y` del año. Un
  big-M sobredimensionado debilita silenciosamente los cortes.
- **Signo de μ.** Verificar sobre una instancia de 2–3 años que el corte generado
  efectivamente subestima el costo futuro conocido.
- **UB no monótona.** Guardar la mejor solución, no la última.
- **`α_Y` sin fijar a cero.** Rompe las cotas sin dar error visible.
- **Recurso completo.** Confirmado sin holguras porque la red abastece (54); si en
  el futuro se limita la potencia de red (`p^peak` muy bajo), reevaluar y agregar
  slacks penalizados en (54).
- **Cota de estado.** Aunque `H_y` sea continuo, mantener `H_y ≤ n^{H,max}` como
  restricción local: acota el subproblema y evita duales degenerados.

---

## 11. Validación recomendada

1. **Contra el monolítico en pequeño.** Instancia de 2–3 años: comprobar que UB y LB
   encierran el óptimo conocido y que los cortes nunca lo cortan.
2. **Trayectoria de `B_y`.** En el año con `R_y = 1`, `B_y = b^max` ese mismo año
   (reemplazo al inicio); entre reemplazos, `B_y` decrece monótonamente.
3. **Régimen sin degradación** (`r = 0`, sin hoja `BatteryDegradation`): el estado de
   degradación colapsa y el algoritmo debe comportarse como el multi-año puro de
   infraestructura.
4. **Perfil del gap.** Registrar `(UB − LB)/UB` por iteración. Si se estanca por
   encima de la tolerancia, pasar a Strengthened Benders antes que al Lagrangeano.
