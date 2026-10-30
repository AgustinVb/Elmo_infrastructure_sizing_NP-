# Generacion de columnas en Elmo

Este documento explica:

1. los fundamentos de Dantzig-Wolfe y la generacion de columnas;
2. el algoritmo que implementa este repositorio;
3. como se descompone el modelo de `Mina_modelo`;
4. el flujo de la corrida actual de `RED_GEN_BESS`;
5. las diferencias entre la implementacion propia y la prueba con GCG;
6. literatura recomendada.

## 1. Idea general

Un MILP grande puede ser dificil no solo por el numero de variables, sino
porque muchas decisiones operativas se repiten por dia, vehiculo o año y se
acoplan mediante pocas decisiones globales: inversiones, capacidades, estados
degradacion y restricciones de enlace.

La generacion de columnas explota esa estructura. En vez de incluir
explicitamente todas las configuraciones operativas posibles, comienza con un
subconjunto de ellas y agrega nuevas configuraciones solo cuando pueden mejorar
la relajacion lineal actual.

Una configuracion operativa completa de un bloque se llama **columna**. En este
proyecto una columna representa, esencialmente, una solucion factible de un
bloque `(año, dia)` y sus valores en las variables que enlazan ese bloque con
el maestro.

La generacion de columnas no es una heuristica por si sola:

- el **maestro restringido** produce una cota inferior de la relajacion
  considerada;
- el **pricing** busca columnas con costo reducido negativo;
- cuando se demuestra que ningun bloque puede producir una columna mejorante,
  la relajacion lineal queda resuelta, salvo las tolerancias numericas y los
  limites de tiempo usados por el pricing.

Para obtener una solucion entera se necesita ademas una fase de
**branch-and-price**, o una heuristica que produzca una solucion incumbente
factible. La corrida actual usa ambas ideas: conserva el incumbente transferido,
genera columnas y puede explorar ramas sobre las inversiones enteras.

## 2. Formulacion Dantzig-Wolfe

Supongamos que el problema se separa en bloques `b` y que cada bloque tiene un
conjunto potencial de columnas `K_b`. Una columna `k` tiene:

- costo `c_bk`;
- contribucion `a_bk` a las restricciones de enlace;
- vector operativo interno que satisface las restricciones propias del bloque.

El maestro de Dantzig-Wolfe usa una variable `lambda_bk` para seleccionar o
combinar columnas:

```text
min  sum_b sum_k c_bk lambda_bk

sujeto a
     sum_b sum_k A_bk lambda_bk =/<=/>= b        (filas de enlace)
     sum_k lambda_bk = 1                         (convexidad de cada bloque)
     lambda_bk >= 0
```

En la relajacion lineal, `lambda` puede ser fraccionaria. La restriccion de
convexidad indica que cada bloque debe elegir una combinacion convexa de sus
columnas. En el problema entero, la seleccion de columnas y las inversiones
deben respetar la integralidad correspondiente.

El maestro restringido (RMP) contiene solo las columnas conocidas hasta el
momento. Su dual entrega multiplicadores `pi` para las filas de enlace y
`sigma_b` para las filas de convexidad.

Para una columna candidata del bloque `b`, el costo reducido tiene la forma:

```text
rc_bk = c_bk - pi' A_bk - sigma_b
```

Para un problema de minimizacion, una columna con `rc_bk < 0` puede mejorar el
RMP. El **pricing** de cada bloque resuelve:

```text
min_x  (c_b - pi' A_b) x
sujeto a x factible para el bloque b
```

El problema de pricing suele ser un MILP mas pequeño que el monolitico, pero se
resuelve muchas veces con distintos duales.

## 3. Cota dual y criterio de convergencia

Si el pricing de cada bloque se resuelve exactamente, el valor del RMP mas los
valores optimos de pricing permite obtener la cota de la relajacion completa.
En forma conceptual:

```text
LB = pi' b + sum_b min_x { (c_b - pi' A_b) x }
```

En el código, cada pricing puede terminar por tiempo. En ese caso se usa la
cota dual de Gurobi del pricing, que sigue siendo valida como cota inferior.
La implementacion solo actualiza la cota lagrangiana en rondas en las que se
han procesado todos los bloques necesarios.

La brecha global que se reporta para una solucion de minimizacion es:

```text
gap = (UB - LB) / abs(UB)
```

Aquí:

- `UB` es el costo de la mejor solucion entera factible encontrada;
- `LB` es una cota inferior valida;
- un gap pequeño acota la distancia entre la solucion encontrada y el optimo.

El valor del RMP por sí solo no debe confundirse con `LB`: mientras el pricing
no esté suficientemente cerrado, el RMP puede ser solo una relajacion
restringida y las columnas artificiales pueden seguir presentes.

## 4. Descomposicion usada en este repositorio

La estructura se construye desde el MPS y `decomp.json` exportados por
[`gcg_block.py`](../src/optimization/decomposition/gcg_block.py). La
clasificacion se hace observando en qué filas aparecen las variables:

- una variable que solo aparece en filas de un bloque pertenece a ese bloque;
- una variable que aparece en varios bloques se trata como variable de enlace;
- las variables puramente globales permanecen en el maestro.

La corrida actual usa `mode="year_day"`. Por tanto, cada bloque corresponde a
un par:

```text
(año, dia representativo)
```

Los cuatro dias representativos usados por `Mina_modelo` son normalmente
`15`, `105`, `196` y `288`, y el horizonte actual tiene 10 años. El orden de
magnitud esperado es, por ello, 40 bloques.

En los bloques quedan las decisiones operativas del año y dia: producción,
consumo, carga y descarga de baterias, estados operativos y variables
relacionadas con la generacion. En el maestro quedan las decisiones anuales o
globales y las restricciones que acoplan años y dias: inversiones, capacidades,
degradacion, estados heredados y enlaces entre copias de variables.

La exportacion se hace con capacidades separadas por bloque por defecto. Cada
bloque recibe su copia de una capacidad y el maestro impone la relacion de
enlace. Esto permite que la estructura de columnas represente correctamente
las capacidades compartidas, sin convertir por accidente una capacidad global
en una decision independiente de cada dia.

## 5. Flujo de `cg_propia.py`

El flujo de la implementacion propia es:

### 5.1 Construccion del monolitico

[`cg_propia.py`](../cg_propia.py) construye primero el modelo Pyomo completo
con 10 años y 4 dias por año. En la corrida actual se usan:

- `free_charging=True`;
- `free_maintenance=True`;
- `mccormick_degradation=True`;
- los datos de `Produccion_escalada20/zona_norte/05C/RED_GEN_BESS`.

Este paso puede tardar varios minutos y consume memoria considerable. Es
necesario porque el modelo completo se usa para exportar el MPS, reconstruir el
incumbente y escribir la solución final.

### 5.2 Carga del incumbente

La carpeta `--solucion` contiene JSON con los valores de las variables del
incumbente. La solución se reconstruye sobre el monolitico y se fijan las
decisiones enteras necesarias para validar su costo.

En la corrida actual el incumbente es:

```text
output/transferencias/RED_GEN_BESS_escalada_mejorar_ub_1/solucion
```

Su UB documentado es:

```text
2,628,627.929667105
```

Es importante diferenciar este formato JSON de `incumbente_respaldo.pkl`:
`cg_propia.py --solucion` espera la carpeta de reportes JSON.

### 5.3 Exportacion y estructura DW

El modelo se exporta a:

```text
<salida>/_mps/model.mps
<salida>/_mps/decomp.json
```

Después `EstructuraDW` lee el MPS y la descomposicion, crea el maestro,
identifica las filas de enlace y prepara los bloques de pricing.

### 5.4 Inicializacion

La solucion incumbente se transforma en un vector del MPS. Sus valores se
usan para:

- establecer el primer `UB`;
- crear columnas iniciales;
- dar un punto de partida a las heuristicas;
- evitar empezar desde cero cuando ya existe una solucion factible.

Además, la corrida normal usa la relajacion operacional para obtener una cota
inferior inicial y duales iniciales. Como se pasó
`--variantes_inicio ninguna`, se omite la busqueda de heuristicas alternativas
de arranque: se confia directamente en el incumbente transferido.

### 5.5 Maestro restringido y columnas artificiales

El RMP contiene inicialmente:

- variables globales del maestro;
- una variable `lambda` por columna;
- restricciones del maestro;
- restricciones de enlace;
- una restriccion de convexidad por bloque;
- variables artificiales para garantizar factibilidad inicial.

Las artificiales tienen una penalizacion grande. A medida que llegan columnas
operativas, el RMP puede reemplazarlas. La presencia de artificiales se
controla en la historia porque indica que la base todavía no representa
completamente una solucion operativa real.

### 5.6 Pricing por bloque

Cada bloque tiene un modelo Gurobi persistente. En cada ronda:

1. el maestro se resuelve;
2. se extraen sus duales;
3. los duales se estabilizan si corresponde;
4. cada bloque resuelve su MILP de costo reducido;
5. se agregan las columnas con costo reducido suficientemente negativo;
6. se actualiza el RMP y se repite.

La corrida actual usa:

```text
--jobs 4
--hilos 16
```

Esto significa hasta cuatro bloques en paralelo y hasta 16 hilos de Gurobi por
bloque. No equivale a 64 hilos necesariamente en todo momento, pero puede
acercarse a ese consumo durante el pricing. Por eso el uso de memoria y CPU
puede ser alto.

La implementacion usa varias aceleraciones:

- **pricing heuristico** con tope corto y gap relajado;
- **pricing exacto** para certificar la cota;
- **pool de soluciones** de Gurobi;
- **pricing parcial**, dejando descansar bloques que no producen columnas;
- **Column Sharing**, probando columnas nuevas en bloques compatibles;
- transferencia entre **bloques gemelos** de años/dias con la misma estructura;
- ejecucion paralela mediante `ThreadPoolExecutor`.

Una columna nueva no se conserva solo por ser distinta: debe superar el umbral
de costo reducido relativo `rc_min`, para evitar llenar el maestro con columnas
numericamente irrelevantes.

### 5.7 Estabilizacion dual

Los duales del RMP pueden oscilar fuertemente entre rondas. Eso provoca
pricing inestable y muchas columnas poco utiles. La implementacion combina:

- **Wentges**: mezcla los duales actuales con un centro estabilizado;
- **box-step**: restringe los duales a una caja alrededor del centro;
- opcionalmente un maestro con `barrier` para obtener duales mas centrados.

El centro se actualiza cuando la cota mejora. La caja puede crecer cuando la
busqueda deja de producir columnas. Las artificiales se usan para representar
el costo de apartarse de la caja dual.

### 5.8 Heuristicas de UB

La solucion fraccionaria del RMP no es directamente una solucion entera
factible del modelo original. Para construir UB, el código puede:

- tomar capacidades del maestro y redondearlas;
- fijar la infraestructura;
- resolver los dias por separado;
- pulir las variables continuas;
- refinar estados de degradacion;
- reconstruir y validar la solucion completa.

La corrida actual empieza con el UB transferido. Si una heuristica posterior
encuentra una solucion mejor, `UB` disminuye y se guarda la mejor trayectoria.

### 5.9 Branch-and-price

La relajacion de columnas puede dejar inversiones fraccionarias. La
implementacion agrega branch-and-price sobre enteras que viven en el maestro:

1. resuelve la raiz;
2. hace primero un *diving* fijando inversiones fraccionarias;
3. usa la heuristica de UB en los puntos enteros;
4. si es necesario, abre ramas y explora los nodos por mejor primero;
5. las cotas de las ramas alimentan la cota global.

Ramificar no significa volver a resolver el monolitico desde cero: cambia
cotas del maestro y las restricciones correspondientes de los pricing.

## 6. Lectura de la corrida actual

La salida de la corrida actual se encuentra en:

[RED_GEN_BESS_escalada_generacion_columnas_v2](../output/Resultados_finales_tesis/Mina_modelo/RED_GEN_BESS_escalada_generacion_columnas_v2)

Los archivos principales son:

- `historia_cg.json`: una fila por ronda;
- `trayectoria.json`: UB y LB contra el tiempo;
- `relajacion_operacional.npz`: datos de la cota inicial;
- `_mps/model.mps`: modelo exportado;
- `_mps/decomp.json`: bloques y filas;
- `resumen.json`: resumen final, cuando la corrida termina;
- `solucion/`: solución final JSON, cuando la corrida termina.

En una fila de `historia_cg.json`:

- `z_rmp`: objetivo del maestro restringido;
- `LB`: mejor cota inferior global disponible;
- `UB`: mejor solucion factible conocida;
- `gap_ub_lb`: brecha global;
- `nuevas`: columnas nuevas agregadas en la ronda;
- `cols`: columnas actualmente en el maestro;
- `pool`: columnas almacenadas fuera del maestro;
- `bloques`: bloques que se procesaron en esa ronda;
- `pricing_max_s`: tiempo del pricing mas lento;
- `artificiales`: magnitud residual de las variables artificiales.

Un `z_rmp` que baja o cambia no implica automáticamente una mejora del UB.
El objetivo principal de las rondas es mejorar la representacion dual y la
cota inferior; el UB solo cambia cuando una heuristica o el incumbente produce
una solucion entera factible mejor.

## 7. Diferencia con `gcg_bloque_anual.py`

El repositorio tiene dos caminos relacionados pero distintos:

### Prueba GCG

[`gcg_bloque_anual.py`](../gcg_bloque_anual.py) exporta un bloque anual y lo
entrega a GCG. Puede probar modos como:

```text
day_gurobi
vehicle_day_gurobi
vehicle_station_day_gurobi
```

Es útil para comparar descomposiciones y medir GCG sobre un bloque, pero no es
la corrida completa actual de 10 años.

### Implementacion propia

[`cg_propia.py`](../cg_propia.py) opera sobre el monolitico completo de 10 años,
usa bloques `year_day`, controla directamente el maestro y el pricing, incorpora
estabilizacion, Column Sharing, heuristicas y branch-and-price.

Por eso, decir que la corrida actual usa "`day_gurobi`" sería impreciso. La
corrida actual usa la configuracion mas completa de la implementacion propia:

```text
cg_propia.py
mode year_day
pricing Gurobi
jobs 4
hilos 16
variantes_inicio ninguna
incumbente transferido
```

## 8. Limitaciones e interpretacion

1. Un incumbente bueno no garantiza una cota buena. El incumbente fija un UB,
   pero la fuerza de la demostracion depende de la relajacion y del pricing.
2. Un pricing interrumpido por tiempo entrega una cota dual de Gurobi, pero no
   necesariamente una columna optima para ese bloque.
3. Mientras haya artificiales o el pricing no se haya cerrado, el RMP no debe
   interpretarse como certificado de optimalidad.
4. El gap de la corrida puede ser grande aunque el UB sea razonable: significa
   que todavía hay una distancia importante entre la solucion factible y la
   mejor cota inferior demostrada.
5. Los resultados de heuristicas son soluciones factibles; no son por sí solos
   pruebas de optimalidad.
6. Los archivos de la corrida pueden ser grandes. Conviene conservar
   `historia_cg.json`, `trayectoria.json`, `resumen.json` y la solucion final,
   pero revisar la politica del repositorio antes de versionar MPS voluminosos.

## 9. Literatura recomendada

### Fundamentos

1. **Dantzig, G. B.; Wolfe, P. (1960).** *Decomposition Principle for Linear
   Programs*. Operations Research, 8(1), 101–111.
   DOI: [10.1287/opre.8.1.101](https://doi.org/10.1287/opre.8.1.101)

   Articulo fundador del principio de descomposicion y de la interpretacion
   de las columnas como combinaciones de soluciones de subproblemas.

2. **Desrosiers, J.; Lübbecke, M. E. (2005).** *A Primer in Column Generation*.
   En *Column Generation*, Springer, 1–32.
   DOI: [10.1007/0-387-25486-2_1](https://doi.org/10.1007/0-387-25486-2_1)

   Es probablemente la lectura mas clara para entender RMP, pricing, costo
   reducido, dualidad y el paso de generacion de columnas a branch-and-price.

3. **Vanderbeck, F. (2000).** *On the Implementation of Extended Formulations
   for Integer Programming*. CORE Discussion Paper 2000/05.
   [CORE repository](https://ideas.repec.org/p/core/corecon/2000-05.html)

   Explica como implementar formulaciones extendidas y como las decisiones
   enteras del maestro afectan la descomposicion.

### Estabilizacion y eficiencia

4. **Pessoa, A. A.; Sadykov, R.; Uchoa, E.; Vanderbeck, F. (2018).**
   *Automation and combination of linear-programming based stabilization
   techniques in column generation*. INFORMS Journal on Computing, 30(2),
   339–360.
   DOI: [10.1287/ijoc.2017.0784](https://doi.org/10.1287/ijoc.2017.0784)

   Referencia para estabilizacion de duales, incluyendo ideas relacionadas con
   Wentges y centros duales.

5. **du Merle, O.; Villeneuve, D.; Desrosiers, J.; Hansen, P. (1999).**
   *Stabilized column generation*. Discrete Mathematics, 194(1–3), 229–237.
   DOI: [10.1016/S0012-365X(98)00265-3](https://doi.org/10.1016/S0012-365X(98)00265-3)

   Presenta una de las referencias clasicas sobre estabilizacion del problema
   maestro y oscilaciones de los duales.

6. **Ben Amor, H.; Desrosiers, J.; Valério de Carvalho, J. M. (2006).**
   *Dual-optimal inequalities for stabilized column generation*. Operations
   Research, 54(3), 454–463.
   DOI: [10.1287/opre.1050.0247](https://doi.org/10.1287/opre.1050.0247)

   Util para entender como controlar duales degenerados y mejorar el pricing.

### Branch-and-price

7. **Barnhart, C.; Johnson, E. L.; Nemhauser, G. L.; Savelsbergh, M. W. P.;
   Vance, P. H. (1998).** *Branch-and-price: Column generation for solving
   huge integer programs*. Operations Research, 46(3), 316–329.
   DOI: [10.1287/opre.46.3.316](https://doi.org/10.1287/opre.46.3.316)

   Referencia central para combinar generacion de columnas con branching y
   obtener una solucion entera con certificacion.

8. **Vanderbeck, F. (2006).** *Branching in price-and-cut algorithms*.
   EURO Journal on Computational Optimization, 4, 47–70.
   DOI: [10.1016/j.ejco.2015.11.001](https://doi.org/10.1016/j.ejco.2015.11.001)

   Discute como elegir reglas de branching que mantengan el pricing tratable.

### Libros y material didactico

9. **Desaulniers, G.; Desrosiers, J.; Solomon, M. M. (eds.) (2005).**
   *Column Generation*. Springer.
   DOI: [10.1007/0-387-25486-2](https://doi.org/10.1007/0-387-25486-2)

   Volumen de referencia con aplicaciones a ruteo, scheduling y problemas de
   transporte.

10. **Wolsey, L. A. (1998).** *Integer Programming*. Wiley.
    [Wiley catalog](https://www.wiley.com/en-us/Integer+Programming-p-9780471283669)

    Buen apoyo para dualidad, relajaciones, formulaciones extendidas,
    integralidad y cotas.

### Orden sugerido de lectura

Para entender rapidamente esta implementacion:

1. Desrosiers y Lübbecke (2005), secciones iniciales.
2. Dantzig y Wolfe (1960), para la idea formal.
3. Barnhart et al. (1998), para branch-and-price.
4. Pessoa et al. (2018) y du Merle et al. (1999), para estabilizacion.
5. Luego leer [`src/optimization/decomposition/cg_propia.py`](../src/optimization/decomposition/cg_propia.py)
   junto con [`cg_propia.py`](../cg_propia.py), siguiendo el flujo de la
   sección 5.
