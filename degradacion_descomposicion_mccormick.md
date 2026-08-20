# Descomposición del bloque de degradación bilineal

Complemento de la guía `implementacion_descomposicion_carga_ob.md`. Cubre **solo** el
bloque de degradación de baterías en su nueva formulación (capacidad inicio/fin de año
con producto bilineal), y cómo descomponerlo por año generando cortes de Benders.

Dos rutas:

- **Camino A — McCormick** (para probar primero): convexifica el producto bilineal y
  recupera un subproblema MILP; los cortes de Benders estándar aplican sin cambios.
- **Camino B — Lagrangeano** (respaldo): mantiene el subproblema como MIQCP no convexo
  y genera cortes válidos vía relajación Lagrangeana.

El resto de la descomposición (estado de infraestructura, forward/backward pass,
criterio de parada, arquitectura Pyomo) no cambia respecto de la guía principal.

---

## 1. La formulación monolítica de partida

Dos variables de capacidad por año:

- `B_y` = capacidad al **inicio** del año `y`
- `D_y` = capacidad al **final** del año `y`

```
(1)  B_y ≤ b^max
(2)  B_y ≤ D_{y-1} + b^max · 0.3 · R_y            ∀ y > 2
(3)  N^ciclos_y · B_y = Σ_{k,i,d,t} P_{k,i,y,d,t} · Δt · 365 / (‖D‖ · n^lhd)
(4)  D_y = B_y − γ_m · N^ciclos_y
(5)  B_{y=1} = b^max
```

Notas de interpretación (confirmadas):

- **El `0.3` es un big-M de reemplazo.** La degradación máxima es 20 %, así que
  `b^max · 0.3 · R_y` con `R_y = 1` relaja por completo la cota heredada (2), y (1)
  `B_y ≤ b^max` pasa a mandar: la batería queda al 100 %.
- **Las desigualdades operan como igualdades en el óptimo.** Mayor `B_y` abarata la
  operación, así que el optimizador empuja `B_y` a la cota. No hace falta forzar
  igualdad explícita.
- **La cuadrática es (3):** producto `N^ciclos_y · B_y` de dos variables continuas del
  **mismo año**. Se resuelve hoy como producto bilineal directo en Gurobi (no convexo).

---

## 2. Qué cambia al descomponer: la variable de estado de degradación

La única restricción que cruza años es (2), que conecta `B_y` con `D_{y-1}`. Por tanto:

> **La variable de estado de degradación es `D_y`** (capacidad al final del año).
> Reemplaza a `AN^ciclos_y` de la formulación anterior.

Al fijar el año anterior en el forward pass, `D_{y-1}` se vuelve el **parámetro
heredado** `D̂_{y-1}`, y el acople (2) queda **lineal**:

```
B_y ≤ b^max
B_y ≤ D̂_{y-1} + b^max · 0.3 · R_y      (lineal: D̂_{y-1} constante, R_y binaria)
```

El estado completo de la descomposición pasa a ser:

| Estado `x_y`   | Dominio      | Rol                                   |
|----------------|--------------|---------------------------------------|
| `N^C_{k,y}`    | entero ≥ 0   | cargadores acumulados (infraestruct.) |
| `G_{g,y}`      | continuo ≥ 0 | capacidad de generación acumulada     |
| `H_y`          | continuo ≥ 0 | unidades de almacenamiento acumuladas |
| **`D_y`**      | continuo ≥ 0 | **capacidad de batería fin de año**   |

La restricción de acople de degradación es:

```
D^{prev}_y = D̂_{y-1}     ← μ^D_y        (dual para el corte)
```

Con condición de borde en `y_1`: `B_{y_1} = b^max` (batería nueva), lo que fija el
punto de partida sin necesidad de un `D̂_{y_0}` heredado. Para `y = 2` se usa
`D̂_{y_1}` del forward pass como cualquier otro año.

**El producto (3) NO se elimina solo** al descomponer, a diferencia de la formulación
anterior de ciclos acumulados: aquí `N^ciclos_y · B_y` es producto de dos variables
del mismo año, ninguna heredada. Ese producto es lo que las dos rutas siguientes tratan.

---

## 3. Camino A — McCormick (probar primero)

### 3.1 Idea

El producto problemático `N^ciclos_y · B_y` se reemplaza por una variable auxiliar
`w_y` acotada por su **envolvente convexa de McCormick**, construida a partir de las
cotas de ambos factores. El subproblema anual vuelve a ser **MILP** y los cortes de
Benders estándar (relajación LP + duales del acople) son válidos sin cuidado extra.

**Por qué es prometedor aquí:** el error de McCormick escala con el ancho de las cotas
de las variables. `B_y` vive en un rango estrecho (20 % de degradación máxima), así que
la envolvente es ajustada.

### 3.2 Cotas

```
B_y      ∈ [B^L, B^U]  = [0.8 · b^max,  b^max]      # degradación máxima 20 %
N^ciclos_y ∈ [N^L, N^U]                              # de la meta de producción y
                                                     # energía máxima cargable/año
```

> Confirmar `B^L = 0.8 · b^max`. La calidad de McCormick depende directamente de que
> este rango sea correcto y lo más ajustado posible.

### 3.3 Restricciones McCormick

Sustituir el producto `w_y = N^ciclos_y · B_y` por la variable `w_y` y las cuatro
desigualdades:

```
w_y ≥ N^L · B_y     + B^L · N^ciclos_y − N^L · B^L
w_y ≥ N^U · B_y     + B^U · N^ciclos_y − N^U · B^U
w_y ≤ N^U · B_y     + B^L · N^ciclos_y − N^U · B^L
w_y ≤ N^L · B_y     + B^U · N^ciclos_y − N^L · B^U
```

Y reescribir el bloque de degradación con `w_y` en lugar del producto:

```
B_y ≤ b^max
B_y ≤ D̂_{y-1} + b^max · 0.3 · R_y

w_y = S_y                     # donde S_y = Σ P·Δt·365/(‖D‖·n^lhd)  (energía cargada)
                              # con w_y aproximando N^ciclos_y · B_y por McCormick

D_y = B_y − γ_m · N^ciclos_y
```

El subproblema anual es ahora **MILP** (McCormick es lineal; `R_y` binaria; `N^C`
entero). Toda la maquinaria de Benders de la guía principal aplica: relajación LP,
duales del acople `μ^D_y`, corte para el año `y-1`.

### 3.4 Consecuencia: McCormick es una relajación

McCormick **relaja** el producto: `w_y` puede tomar valores que el producto exacto no
alcanzaría. Esto tiene dos efectos que hay que tener presentes:

- El subproblema anual McCormick es una **relajación** del subproblema bilineal exacto,
  así que su óptimo **subestima** el costo real del año. En el contexto de la
  descomposición esto empuja las cotas, pero **la solución del forward pass puede ser
  ligeramente infactible respecto de la física exacta de (3)**.
- **Validación obligatoria:** al terminar, verificar el gap de McCormick evaluando el
  producto exacto `N^ciclos_y · B_y` con los valores óptimos y comparándolo con `S_y`.
  Si el residuo es pequeño (rango estrecho de `B_y`), McCormick es adecuado. Si es
  grande, refinar (Sección 3.5) o pasar al Camino B.

### 3.5 Refinamiento si el gap es grande

Si la envolvente resulta floja:

- **Piecewise McCormick:** partir el rango de `B_y` en `m` sub-intervalos con binarias
  y aplicar McCormick en cada uno. Con `B_y` ya estrecho, 2–4 tramos suelen bastar.
  Cada tramo agrega una binaria local por año (no cambia el estado).
- **Estrechar `[B^L, B^U]`** con las cotas físicas más ajustadas disponibles por año
  (p. ej. si la degradación anual está acotada, `B_y` no baja de cierto nivel).

---

## 4. Camino B — Lagrangeano (respaldo, exacto)

Si McCormick deja un gap inaceptable, mantener el subproblema como **MIQCP no convexo**
(Gurobi con producto bilineal directo) y generar cortes **Lagrangeanos**, que sí son
válidos con subproblemas no convexos.

### 4.1 Por qué Benders-LP no sirve aquí

El corte de Benders clásico saca sus coeficientes de la relajación **convexa** del
subproblema. Con (3) bilineal no convexa, la relajación continua de Gurobi no
subestima limpiamente el costo futuro y el corte **puede cortar soluciones válidas**,
rompiendo la corrección del algoritmo. El corte Lagrangeano evita esto porque su
validez no depende de la convexidad del subproblema.

### 4.2 Relajación Lagrangeana del subproblema anual

Dualizar la igualdad de acople `z_y = x̂_{y-1,k}` (que ahora incluye
`D^{prev}_y = D̂_{y-1}`) y penalizar su violación en el objetivo:

```
L_y(μ) = min   f_y(x_y, y_y) + φ_y(x_y) − μ^T (z_y − x̂_{y-1,k})
        x,y,z
         s.a. (x_y, y_y, z_y) ∈ X_y     # X_y incluye el producto bilineal EXACTO (3)
```

Este `L_y(μ)` se resuelve como MIQCP no convexo en Gurobi. El dual Lagrangeano
`Φ^LD_y = max_μ L_y(μ)` da los multiplicadores óptimos `μ̄`, y el corte es:

```
α_{y-1} ≥ Φ^LD_y,k + μ̄_{y,k} · (x̂_{y-1,k} − x_{y-1})     ∀ k
```

### 4.3 Optimización de los multiplicadores (subgradiente)

```
PARA y = Y … y_1+1  EN LA ITERACIÓN k:
  1. Resolver el MIQCP no convexo original del año  →  Φ^OP_y,k
  2. Inicializar μ (p. ej. con los duales de una relajación McCormick del año)
  3. Resolver el Lagrangeano L_y(μ) (MIQCP no convexo)  →  Φ^LR_y,k
  4. Criterios de parada:
       (a) Φ^OP_y,k − Φ^LR_y,k ≤ ε₂       → brecha cerrada, almacenar y salir
       (b) |Φ^LR,old − Φ^LR| ≤ ε₃         → sin progreso, salir
  5. Actualizar por subgradiente:
       μ ← μ − step · (z_y − x̂_{y-1,k}),   step = (Φ^OP − Φ^LR)/‖z_y − x̂‖²
     volver a 3
  6. Añadir el corte Lagrangeano a la lista del año y-1
```

### 4.4 Costo y cuándo justifica

- **Ventaja:** no aproxima (3); el subproblema mantiene la física exacta del producto
  bilineal.
- **Costo:** cada iteración de subgradiente resuelve un MIQCP no convexo, y hay varias
  por año y por iteración del algoritmo. Es sustancialmente más caro que McCormick.
- **Cuándo:** solo si el residuo de McCormick (Sección 3.4) es demasiado grande y el
  refinamiento piecewise (3.5) no lo cierra a un costo razonable.

### 4.5 Híbrido recomendado

Un punto intermedio eficiente: usar **McCormick para el forward pass** (rápido, da la
cota superior y una solución factible aproximada) y **Lagrangeano solo en el backward
pass** para generar cortes exactos. Así se paga el MIQCP no convexo únicamente al
cortar, no al recorrer el horizonte hacia adelante.

---

## 5. Resumen de decisión

| Aspecto                     | Camino A (McCormick)          | Camino B (Lagrangeano)            |
|-----------------------------|-------------------------------|-----------------------------------|
| Subproblema anual           | MILP                          | MIQCP no convexo                  |
| Tratamiento de (3)          | Relajación convexa (aprox.)   | Exacto                            |
| Cortes de Benders           | Estándar (LP + duales)        | Lagrangeanos (subgradiente)       |
| Costo por iteración         | Bajo                          | Alto                              |
| Validez del corte           | Garantizada (MILP)            | Garantizada (no depende de convex.)|
| Cuándo usarlo               | **Primero**                   | Si McCormick deja gap grande      |

**Plan:** implementar Camino A, validar el residuo del producto exacto (Sección 3.4).
Si es pequeño, listo. Si no, refinar con piecewise (3.5); si aún no basta, pasar al
Camino B o al híbrido (4.5).

---

## 6. Cambios concretos en la guía principal

Respecto de `implementacion_descomposicion_carga_ob.md`, esta nueva formulación implica:

1. **Estado de degradación:** `AN^ciclos_y` → **`D_y`** (Sección 2 de este documento).
   Actualizar la tabla de variables de estado y la restricción de acople.
2. **Parámetro heredado:** `ÂN^ciclos_{y-1}` → **`D̂_{y-1}`** (`Param(mutable=True)`).
3. **Bloque de degradación del subproblema:** reemplazar la formulación lineal anterior
   por las restricciones McCormick (Sección 3.3) o el MIQCP exacto (Camino B).
4. **Validación adicional:** al chequeo de trayectoria de `B_y` agregar la verificación
   del residuo de McCormick (Sección 3.4). En el año con `R_y = 1`, confirmar que
   `B_y = b^max` (batería nueva ese mismo año).
5. **Sin cambios** en: estado de infraestructura (`N^C, G, H`), forward/backward pass,
   cost-to-go, criterio de parada, arquitectura Pyomo. La descomposición temporal es la
   misma; solo cambia el interior del bloque de degradación.
