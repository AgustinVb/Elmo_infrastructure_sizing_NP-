# Metodología: modelo multiaño de inversión y operación de LHD eléctricos en la mina modelo

Documento de contexto para redactar el capítulo de metodología de la tesis. Reúne:
- el caso de estudio (mina modelo) y sus datos;
- el modelo de optimización, en sus dos variantes: **carga on-board (OB)** y
  **battery swapping (BS)**;
- los métodos de solución considerados, con el detalle de los que forman el esquema
  final;
- el postprocesamiento y la metodología de comparación entre tecnologías;
- el diseño de las pruebas computacionales que entregan los resultados de la tesis.

No incluye resultados. Las cifras que aparecen son datos de entrada o parámetros de los
algoritmos. Las justificaciones de diseño se dan de forma cualitativa; las cifras que
las respaldan saldrán del experimento de la sección 6.

Estado al 2026-11-17. Código: rama `carga_ob_multiaño` (OB) y rama
`battery_swapping_multiaño` (BS) del repositorio `Elmo_infrastructure_sizing_NP-`.

---

## 1. Problema

Se decide, para un horizonte de 10 años, **cuánta infraestructura de carga instalar y
cuándo**, junto con la **operación diaria** de una flota de LHD eléctricos que debe
cumplir un plan de extracción:

- **Inversión:** estaciones de carga, cargadores (OB) o bahías, cargadores y baterías de
  recambio (BS), subestación eléctrica, generación renovable (solar y eólica),
  almacenamiento (BESS) y potencia contratada a la red.
- **Operación:** en cada intervalo de 8 minutos de cuatro días representativos por año,
  qué hace cada LHD (extraer en un punto, cargar o intercambiar batería, estar
  detenido), cuánta energía se carga y de qué fuente sale.
- **Degradación:** la capacidad de las baterías baja con los ciclos de uso. Bajo un
  piso de 80 % de la capacidad nominal hay que reemplazarlas, lo que acopla la
  operación de todos los años con la inversión.

El objetivo es minimizar el valor presente del costo total: inversión, energía, potencia
contratada, operación y mantención de generación y BESS, y reemplazos de baterías.

Es un problema **determinístico y multiperiodo**, de gran escala, con variables enteras
en los dos niveles (inversión y operación). Por eso hacen falta métodos de
descomposición.

**Dos tecnologías:**
- **OB:** cada LHD lleva su batería y se conecta a un cargador en la estación; mientras
  carga, está detenido.
- **BS:** el LHD cambia su batería descargada por una cargada en la estación, en un
  intervalo. Las baterías se cargan en racks de la estación, de forma independiente de
  la operación del LHD.

---

## 2. Caso de estudio: la mina modelo

### 2.1 Plan minero y layout

| Elemento | Valor |
|---|---|
| Horizonte | 10 años |
| Puntos de extracción | 40 (`PEX_1`–`PEX_40`) |
| LHD | 4 LH518B, activos los 10 años; cualquier LHD puede visitar cualquier punto |
| Estaciones | 1 (`station_1`) |
| Turnos | 3 de 8 h; el día parte a las 08:30 |

**Meta de extracción** (todos los puntos con el mismo perfil; producción base):

| Año | 1 | 2 | 3 | 4–7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|
| t/día por punto | 140 | 280 | 532 | 700 | 560 | 280 | 140 |
| t/día total | 4.700 | 9.400 | 17.860 | 23.500 | 18.800 | 9.400 | 4.700 |

La **producción escalada** multiplica todas las metas por 8/7 (700 → 800 t/día por punto
en régimen).

**Cotas por punto.** El número de visitas a cada punto en el día va entre
`⌊meta/carga⌋ − 1` y `⌈meta/carga⌉ + 2`.

### 2.2 Equipos y consumo

| Parámetro | Valor |
|---|---|
| LHD LH518B | balde de 18 t, factor de llenado 0,84 |
| Batería | 482 kWh nominales, SOC mínimo 20 %, eficiencias de carga y descarga 0,95 |
| Energía de tracción | modelo de consumo por ruta (WP1): energía por viaje entre cada punto y su descarga, de 2,75 a 5,11 kWh según el punto, multiplicada por los viajes que caben en un intervalo |

### 2.3 Infraestructura y costos

| Componente | Costo / límite |
|---|---|
| Estación | 219.495 USD por apertura |
| Bahía | 234.851 USD; hasta 4 bahías |
| Espacio de cargador | 35.228 USD |
| Cargador por tasa de carga | 0,5C: 241 kW, 241.000 USD · 1C: 482 kW, 482.000 USD · 2C: 964 kW, 964.000 USD |
| Cargadores por bahía | OB: 1 · BS: hasta 2 |
| Batería del pool (solo BS) | 299.000 USD + 26.971 USD de espacio; hasta 2 baterías por bahía |
| Subestación | módulos de 500 kW a 302 USD/kW |
| Red | tope de 3.000 kW; precio plano de 0,10 USD/kWh |
| Potencia contratada | 10 USD/kW-mes (120 USD/kW-año), cobrada en la ventana de punta de los días 105 y 196 |
| Descuento | 8 % anual, $\delta_y = (1+r)^{-y}$ con el año 1 descontado |

### 2.4 Generación y almacenamiento

| | Solar | Eólica | BESS |
|---|---|---|---|
| Unidad | 500 kW | 500 kW | 250 kW / 1.000 kWh |
| Inversión | 273 USD/kW | 1.119 USD/kW | 100.900 USD por unidad |
| O&M | 6,5 USD/kW-año | 21,6 USD/kW-año | 2.900 USD por unidad-año |
| Unidades máximas | 10 | 10 | 10 |
| Vida útil (valor residual lineal) | 35 años | 25 años | 20 años |
| Otros | — | — | eficiencia 0,98, SOC cíclico diario |

**Perfiles** (horas equivalentes por día, promedio de los 4 días representativos):

| Zona | Solar | Eólica (por día representativo) |
|---|---|---|
| Norte | ~7,3 | 7,56 (6,63 / 4,55 / 11,31 / 7,74) |
| Centro | ~4,3 | 5,33 (3,46 / 4,41 / 4,96 / 8,49) |

**Valor residual.** La inversión en generación y BESS se cobra neta del valor residual
lineal al final del horizonte:
$\phi = \delta_{1} - (1 - |\mathcal{Y}|/L)\,\delta_{|\mathcal{Y}|}$, con $L$ la vida
útil.

### 2.5 Degradación

| Parámetro | Valor |
|---|---|
| Pérdida por ciclo equivalente γ | 0,0188 (0,5C), 0,0273 (1C), 0,0451 (2C) kWh/ciclo |
| Piso | 80 % de la nominal (385,6 kWh) |
| Reemplazo | 299.000 USD por batería |
| Recuperación máxima por reemplazo (cota de b̄) | 0,3 · 482 kWh |

### 2.6 Tiempo

- **Días representativos:** 15, 105, 196 y 288, con peso 365/4 = 91,25 cada uno. Los
  días 105 y 196 caen en el periodo de cobro de potencia (días 91 a 244).
- **Intervalos:** Δt = 8 min, 180 por día.
- **Ventana de punta:** intervalos 72–101 (17:58–21:58). Un intervalo pertenece a la
  punta según su punto medio.

### 2.7 Detenciones (esquema DET) y régimen de carga

| Detención | Horario por turno (3 turnos) | Efecto |
|---|---|---|
| Entre turnos | 08:30–08:46, 16:30–16:46, 00:30–00:46 | no se opera; se puede cargar (OB) o intercambiar (BS) |
| Colación | 08:46–09:42, 16:46–17:42, 00:46–01:42 | ídem |
| Despeje de vías | 12:30–13:42, 20:30–21:42, 04:30–05:42 | ídem |
| Mantención | 10:54–11:26, 14:46–15:34, 18:54–19:26, 22:46–23:34, 02:54–03:18, 06:46–07:34 | **liberada** en todas las corridas: se puede operar, cargar o estar detenido |

**Régimen usado en todos los casos.** Carga libre (`--free_charging`): se puede cargar
o intercambiar en cualquier intervalo. Mantención liberada (`--free_maintenance`). En
BS la estación sigue cargando baterías en todas las detenciones.

### 2.8 Los 8 casos de estudio

| # | Producción | Zona | Tasa OB | Tasa BS | Fuentes |
|---|---|---|---|---|---|
| 1 | base | norte | 0,5C | 0,5C | red |
| 2 | base | norte | 0,5C | 0,5C | red + generación + BESS |
| 3 | base | norte | 0,5C | 0,5C | red + generación |
| 4 | escalada | norte | **1C** | 0,5C | red |
| 5 | escalada | norte | **1C** | 0,5C | red + generación + BESS |
| 6 | base | centro | 0,5C | 0,5C | red + generación + BESS |
| 7 | base | norte | 1C | 1C | red + generación + BESS |
| 8 | base | norte | 2C | 2C | red + generación + BESS |

- **"Red"** significa topes de generación y BESS en 0. **"Red + generación"** significa
  solar y eólica, sin BESS.
- **Casos 4 y 5.** En OB la producción escalada es infactible a 0,5C (los LHD no
  alcanzan a cargar sin quitarle tiempo a la extracción), así que OB usa 1C. BS carga
  en el rack y se mantiene en 0,5C.
- **Nombres de las carpetas de datos.** OB:
  `data/Resultados_finales_tesis/Mina_modelo_v4/<producción>/<zona>/<tasa>/P_red[_gen[_bat]]`.
  BS: `data/Tesis_final/Mina_modelo_v4/<producción>/<zona>/<tasa>/RED[_GEN[_BESS]]`.

---

## 3. Modelo matemático

### 3.1 Estructura común

| Tipo | Decisiones | Nota |
|---|---|---|
| Inversión por año | estación abierta, cargadores (OB); bahías, cargadores y baterías del pool (BS), con sus incrementos | stock = stock del año anterior + incremento; se paga el incremento descontado |
| Inversión única | módulos de subestación, unidades de generación y de BESS | se decide en el año 1 para todo el horizonte |
| Potencia contratada | $P^{pot}_y$ | cota de la compra a la red en punta |
| Batería | capacidad al inicio ($\bar b_y$) y al final ($D_y$) del año, ciclos ($N_y$), reemplazo ($R_y$, binaria) | recursión año a año |
| Operación | estado de cada LHD por intervalo, carga o intercambio, energía en batería, red, generación, BESS | binarias salvo potencias y energías |

**Decisiones de modelación comunes:**
- **Apertura en el año 1 o nunca.** Así el monolítico y la descomposición (donde la
  apertura queda fija) resuelven el mismo problema.
- **Capacidad físicamente consistente.** Sin reemplazo, $\bar b_y = D_{y-1}$; con
  reemplazo, $\bar b_y = b^{max}$. Con solo cotas superiores, el modelo podía "tirar"
  capacidad sin costo y forzar reemplazos espurios.
- **Ciclos por energía de tracción en las dos tecnologías.** OB contaba antes la energía
  entregada por el cargador, que con el SOC cíclico del día es tracción/(η_c·η_d): 10,8 %
  más ciclos que BS para la misma operación. Ahora las dos usan la misma base.
- **McCormick** para los productos bilineales de la degradación (3.2 y 3.3), de modo que
  el modelo sea un MILP.

### 3.2 Formulación OB

**Conjuntos:**

| Símbolo | Descripción |
|---|---|
| $\mathcal{K}$ | estaciones (1) |
| $\mathcal{I}$ | LHD (4) |
| $\mathcal{J}$ | puntos de extracción (40) |
| $\mathcal{Y}$ | años, con $y_1$ el primero, $y^-$ el anterior a $y$ y $\mathcal{Y}^+ = \mathcal{Y}\setminus\{y_1\}$ |
| $\mathcal{D}$ | días representativos $\{15,105,196,288\}$ |
| $\mathcal{D}^{pot}$ | días con cobro de potencia $\{105,196\}$ |
| $\mathcal{T}=\{1..180\}$, $\mathcal{T}_0 = \{0\}\cup\mathcal{T}$ | intervalos del día, con el estado inicial |
| $\mathcal{T}^{HP}$ | intervalos de punta |
| $\mathcal{T}^{det}$ | intervalos de detención: colación y despeje de vías (y mantención si no está liberada) |
| $\mathcal{T}^{et}$ | intervalos entre turnos |
| $\mathcal{J}_{iydt}$ | puntos que el LHD $i$ puede visitar en $(y,d,t)$ |
| $\mathcal{K}_i$ | estaciones donde puede cargar el LHD $i$ |
| $\mathcal{P}$ | pares $(i,i')$ de LHD consecutivos de una estación (rotura de simetría) |
| $\mathcal{G}$, $\mathcal{H}$ | generación (solar, eólica) y almacenamiento (BESS) |

**Parámetros principales:**

| Símbolo | Descripción |
|---|---|
| $\Delta t$, $\omega$ | duración del intervalo (8/60 h) y peso del día (91,25) |
| $\delta_y$ | factor de descuento |
| $m_{jy}$; $\underline v_{jy}, \overline v_{jy}$ | meta diaria del punto (t/día); visitas mínimas y máximas |
| $q_{ij}$, $e_{ij}$ | toneladas y energía de tracción por intervalo asignado |
| $b^{max}$, $\beta$, $\eta^c$, $\eta^d$ | capacidad nominal, SOC mínimo, eficiencias |
| $\gamma$, $B_L$, $B_U$, $\rho$ | pérdida por ciclo, piso, nominal, recuperación por reemplazo |
| $n_y$, $c^R$ | LHD activos y costo de reemplazo por batería |
| $c^X_k, c^{bay}_k, c^{esp}_k, c^C, p^C, \overline N_k$ | costos de estación, bahía, espacio y cargador; potencia del cargador; bahías máximas |
| $c^{ssee}_k$, $\pi$ | costo de subestación (USD/kW) y tamaño del módulo (500 kW) |
| $\overline P^{red}$, $c_{ydt}$, $c^{pot}$ | tope de red, precio de energía, cargo por potencia |
| $c^{inv}_g, c^{op}_g, p^{max}_g, \overline G_g, \alpha_{gdt}, \phi_g$ | generación |
| $c^{inv}_h, c^{op}_h, p^{max}_h, \eta_h, a^{min}_h, a^{max}_h, \overline H, \phi_h$ | BESS |

**Variables:**

| Variable | Dominio | Descripción |
|---|---|---|
| $X_{ky}, \Delta X_{ky}$ | $\{0,1\}$ | estación abierta y apertura |
| $N^C_{ky}, \Delta N^C_{ky}$ | $\mathbb Z_+$ | cargadores instalados y comprados |
| $n^{ssee}_k$; $P^{max}_k$ | $\mathbb Z_+$; $\ge 0$ | módulos de subestación y su potencia |
| $G_g$, $H$ | $\ge 0$ | generación y BESS |
| $P^{pot}_y$ | $\ge 0$ | potencia contratada |
| $R_y$ | $\{0,1\}$, $R_{y_1}=0$ | reemplazo de la flota al inicio del año |
| $\bar b_y$ | $[B_L,B_U]$, $\bar b_{y_1}=b^{max}$ | capacidad al inicio del año |
| $D_y$ | $[B_L,B_U]$ | capacidad al final del año |
| $S_y$ | $\ge 0$ | energía de tracción anual de la flota |
| $N_y$, $w_y$ | $[N^L_y,N^U_y]$, $\ge 0$ | ciclos por batería y auxiliar de McCormick |
| $Y_{ijydt}$ | $\{0,1\}$ | LHD $i$ extrae en $j$ |
| $Z_{iydt}$ | $\{0,1\}$ | detenido |
| $Z^{ch}_{kiydt}$, $\sigma^\pm_{kiydt}$ | $\{0,1\}$ | conectado a un cargador; inicio y fin de sesión |
| $P_{kiydt}$ | $\ge 0$ | potencia de carga |
| $B_{iydt}$ | $[0,b^{max}]$ | energía en batería |
| $P^{red}_{ydt}$, $P^{gen}_{gydt}$, $C_{gydt}$, $P^{bat}_{hydt}$, $A_{hydt}$ | | red, generación usada y vertida, BESS (libre en signo), energía del BESS |

**Objetivo:**

$$
\min\; C^{E} + C^{inv} + C^{ssee} + C^{gen} + C^{BESS} + C^{pot} + C^{R}
$$

$$
\begin{aligned}
C^{E} &= \omega \textstyle\sum_{y} \delta_y \sum_{d,t} c_{ydt}\, P^{red}_{ydt}\, \Delta t \\
C^{inv} &= \textstyle\sum_{y} \delta_y \sum_{k} \big[ c^X_k \Delta X_{ky} + (c^{bay}_k + c^C + c^{esp}_k)\, \Delta N^C_{ky} \big] \\
C^{ssee} &= \delta_{y_1} \textstyle\sum_k c^{ssee}_k\, P^{max}_k \\
C^{gen} &= \textstyle\sum_g c^{inv}_g p^{max}_g \phi_g G_g + \sum_y \delta_y \sum_g c^{op}_g p^{max}_g G_g \\
C^{BESS} &= \textstyle\sum_h c^{inv}_h \phi_h H + \sum_y \delta_y \sum_h c^{op}_h H \\
C^{pot} &= \textstyle\sum_y \delta_y \cdot 12\, c^{pot} P^{pot}_y \\
C^{R} &= \textstyle\sum_y \delta_y\, n_y\, c^R R_y
\end{aligned}
$$

En OB cada cargador ocupa una bahía, por eso el costo de la bahía va con $\Delta N^C$.

**Restricciones.**

*Inversión*

$$
\begin{aligned}
&X_{ky} = X_{ky^-} + \Delta X_{ky},\quad X_{ky_1}=\Delta X_{ky_1},\quad \Delta X_{ky}=0\ \ (y\in\mathcal Y^+) && \text{(A1–A2)}\\
&N^C_{ky} = N^C_{ky^-} + \Delta N^C_{ky},\quad N^C_{ky_1}=\Delta N^C_{ky_1} && \text{(A3)}\\
&N^C_{ky} \le \overline N_k X_{ky} && \text{(A4)}\\
&P^{max}_k = \pi\, n^{ssee}_k && \text{(A5)}\\
&G_g \le \overline G_g,\quad H\le \overline H && \text{(A6)}
\end{aligned}
$$

*Estado del LHD y carga* (para todo $i,y,d,t$ y $k\in\mathcal K_i$)

$$
\begin{aligned}
&Z_{iydt} + \textstyle\sum_k Z^{ch}_{kiydt} + \sum_{j\in\mathcal J_{iydt}} Y_{ijydt} = 1 && \text{(A7)}\\
&Z_{iydt} + \textstyle\sum_k Z^{ch}_{kiydt} = 1 \qquad t\in\mathcal T^{et}\cup\mathcal T^{det} && \text{(A8)}\\
&Z^{ch}_{kiydt}\le X_{ky},\qquad \textstyle\sum_i Z^{ch}_{kiydt}\le N^C_{ky} && \text{(A9–A10)}\\
&Z^{ch}_{kiydt} - Z^{ch}_{kiyd,t-1} = \sigma^+_{kiydt} - \sigma^-_{kiydt} && \text{(A11)}\\
&Z^{ch}_{kiydt} + Z^{ch}_{kiyd,t+1} \ge 2\sigma^+_{kiydt}\ \ (t<T),\qquad Z^{ch}_{kiydT}=0 && \text{(A12)}\\
&P_{kiydt}\le p^C Z^{ch}_{kiydt},\qquad \textstyle\sum_i P_{kiydt}\le P^{max}_k && \text{(A13–A14)}
\end{aligned}
$$

(A8) aplica el esquema DET: en las detenciones el LHD no opera, pero puede cargar. (A12)
impone sesiones de al menos 2 intervalos y ninguna abierta al cierre del día. Con carga
libre no hay más restricciones sobre cuándo cargar.

*Energía en la batería*

$$
\begin{aligned}
&B_{iydt} = B_{iyd,t-1} + \eta^c \textstyle\sum_k P_{kiydt}\Delta t - \frac{1}{\eta^d}\sum_j e_{ij} Y_{ijydt} && \text{(A15)}\\
&\beta\,\bar b_y \le B_{iydt}\le \bar b_y,\qquad B_{iyd0}=B_{iydT} && \text{(A16–A17)}\\
&B_{i'yd0}\le B_{iyd0}\qquad (i,i')\in\mathcal P && \text{(A18)}
\end{aligned}
$$

*Producción*

$$
\textstyle\sum_{i,j,t} q_{ij} Y_{ijydt} \ge \sum_j m_{jy},\qquad
\underline v_{jy} \le \sum_{i,t} Y_{ijydt} \le \overline v_{jy} \qquad \text{(A19–A20)}
$$

*Potencia, red, generación y BESS*

$$
\begin{aligned}
&P^{red}_{ydt} + \textstyle\sum_g P^{gen}_{gydt} + \sum_h P^{bat}_{hydt} = \sum_{k,i} P_{kiydt} && \text{(A23)}\\
&P^{red}_{ydt}\le \overline P^{red},\qquad P^{red}_{ydt}\le P^{pot}_y\ \ (d\in\mathcal D^{pot}, t\in\mathcal T^{HP}) && \text{(A24–A25)}\\
&P^{gen}_{gydt} + C_{gydt} = G_g\, p^{max}_g\, \alpha_{gdt} && \text{(A26)}\\
&-p^{max}_h H\le P^{bat}_{hydt}\le p^{max}_h H && \text{(A27)}\\
&A_{hydt}=A_{hyd,t-1} - \tfrac{P^{bat}_{hydt}}{\eta_h}\Delta t,\quad A_{hyd0}=0,\quad A_{hyd1}=A_{hydT},\quad a^{min}_h H\le A_{hydt}\le a^{max}_h H && \text{(A28–A29)}
\end{aligned}
$$

*Degradación*

$$
\begin{aligned}
&S_y = \omega \textstyle\sum_{d,t}\sum_{i,j} e_{ij} Y_{ijydt} && \text{(A30)}\\
&N_y\,\bar b_y = S_y/n_y && \text{(A31)}\\
&D_y = \bar b_y - \gamma N_y && \text{(A32)}\\
&\bar b_y \le D_{y^-} + \rho R_y,\quad \bar b_y\ge D_{y^-} - b^{max}R_y,\quad \bar b_y \ge b^{max}R_y\qquad y\in\mathcal Y^+ && \text{(A33)}
\end{aligned}
$$

(A30) usa la **energía de tracción** (consumo de los viajes, sin dividir por $\eta^d$),
igual que BS. (A33) deja $\bar b_y = D_{y^-}$ sin reemplazo y $\bar b_y = b^{max}$ con
reemplazo; con las cotas de $D_y$, la capacidad nunca baja del piso.

*Linealización de (A31).* El producto se reemplaza por su envolvente de McCormick:

$$
\begin{aligned}
&n_y w_y = S_y\\
&w_y \ge N^L_y\bar b_y + B_L N_y - N^L_y B_L,\qquad w_y\ge N^U_y \bar b_y + B_U N_y - N^U_y B_U\\
&w_y \le N^U_y\bar b_y + B_L N_y - N^U_y B_L,\qquad w_y\le N^L_y \bar b_y + B_U N_y - N^L_y B_U
\end{aligned}
$$

*Cotas de $N_y$, por año.* Una cota global deja la envolvente floja.
- $N^L_y=\lfloor E^{low}_y/(n_y B_U)\rfloor$, con $E^{low}_y$ la energía mínima para
  cumplir la meta: una mochila fraccionaria que toma primero los puntos de menor energía
  por tonelada.
- $N^U_y=\max\{N^L_y,\min(\lceil E^{cap}_y/(n_yB_L)\rceil,\lceil 1{,}2\,E^{high}_y/(n_yB_L)\rceil)\}$,
  con $E^{high}_y$ la misma mochila con visitas enteras y $E^{cap}_y$ la energía máxima
  que pueden entregar los cargadores en el año.

### 3.3 Formulación BS

Comparte con OB:
- la apertura de estaciones y la subestación (A1, A2, A5, A6);
- la producción (A19–A20);
- la red, la generación, el BESS y la potencia contratada (A24–A29).

Cambian el estado del LHD, la energía de la batería, la estación con su inventario de
baterías y la flota que se degrada. No existen $Z^{ch}$, $\sigma^\pm$ ni $P$.

**Conjuntos y parámetros adicionales:**

| Símbolo | Descripción |
|---|---|
| $L$ | intervalos que dura una carga completa: $\mathrm{round}\big(b^{max}(1-\beta)/(p^C\eta^c\Delta t)\big)$; 13 a 0,5C |
| $\mathcal A_t$ | inicios de carga activos en $t$: $a\in[\max(1,t-L+1),t]$ |
| $c^{bay}_k, c^{gr}_k, c^{bat}, c^{espb}_k$ | bahía, grúa (0), batería del pool y su espacio |
| $\overline N^{bay}_k,\overline N^{C/bay}_k,\overline N^{bat/bay}_k$ | 4 bahías, 2 cargadores y 2 baterías por bahía |
| $n^{peak}$ | baterías que pueden cargar a la vez con la red, $\lfloor \overline P^{red}/p^C\rfloor$ (12 a 0,5C) |
| $n^S_y$ | LHD activos |
| $F^{min}_y, F^{max}$, $\mathcal V_y$ | cotas de la flota (pool + una por LHD; máximo 12) y sus valores enteros posibles |
| $\mathcal S$, $[L_s,U_s]$ | 8 tramos iguales de $[B_L,B_U]$ para la McCormick por tramos |

**Variables adicionales:**

| Variable | Dominio | Descripción |
|---|---|---|
| $N^{bay}_{ky}, N^{bat}_{ky}$ (y sus incrementos) | $\mathbb Z_+$ | bahías y baterías del pool |
| $Z^{sw}_{kiydt}$ | $\{0,1\}$ | intercambio del LHD $i$ en la estación $k$ |
| $B^s_{iydt}$ | $\ge 0$ | energía del LHD después del intercambio |
| $\tilde P_{iydt}$ | $\ge 0$ | linealización de $\bar b_y\cdot\sum_k Z^{sw}_{kiydt}$ |
| $X^{ini}_{kydt}$, $V_{kydta}$ | $\mathbb Z_+$ | cargas que empiezan en $t$; baterías cargando en $t$ iniciadas en $a$ |
| $S_{kydt}$, $X^{dch}_{kydt}$, $W_{kydt}$ | $\mathbb Z_+$ | baterías cargadas, descargadas en espera e intercambios |
| $N^{pool}_y$, $F_y$ | $\mathbb Z_+$ | pool y flota total |
| $E_y$, $N^{tot}_y$, $Z^R_y$ | $\ge 0$ | energía de tracción de los días, ciclos de la flota, baterías reemplazadas |
| $u_{yv},\nu_{yv}$; $z_{ys},b_{ys},\tau_{ys},w_{ys}$ | | desagregación exacta de $N_yF_y$; McCormick por tramos |

**Objetivo.** Igual que OB, salvo:

$$
\begin{aligned}
C^{inv} &= \textstyle\sum_y\delta_y\sum_k\big[c^X_k\Delta X_{ky} + (c^{bay}_k+c^{gr}_k)\Delta N^{bay}_{ky} + (c^C + c^{esp}_k)\Delta N^C_{ky} + (c^{bat}+c^{espb}_k)\Delta N^{bat}_{ky}\big]\\
C^R &= \textstyle\sum_y\delta_y\, c^R Z^R_y
\end{aligned}
$$

**Restricciones.**

*Inversión de la estación*

$$
\begin{aligned}
&N^{bay}_{ky}=N^{bay}_{ky^-}+\Delta N^{bay}_{ky},\ \ N^C_{ky}=N^C_{ky^-}+\Delta N^C_{ky},\ \ N^{bat}_{ky}=N^{bat}_{ky^-}+\Delta N^{bat}_{ky} && \text{(B1)}\\
&N^{bay}_{ky}\le \overline N^{bay}_k X_{ky} && \text{(B2)}\\
&N^{bay}_{ky}\le N^C_{ky}\le \overline N^{C/bay}_k N^{bay}_{ky},\quad N^{bat}_{ky}\le \overline N^{bat/bay}_k N^{bay}_{ky},\quad N^C_{ky}\le N^{bat}_{ky} && \text{(B3)}
\end{aligned}
$$

*Estado del LHD e intercambio*

$$
\begin{aligned}
&Z_{iydt} + \textstyle\sum_j Y_{ijydt} + \sum_k Z^{sw}_{kiydt} = 1;\qquad Z_{iydt}+\sum_k Z^{sw}_{kiydt}=1\ \ (t\in\mathcal T^{et}\cup\mathcal T^{det}) && \text{(B4)}\\
&Z^{sw}_{kiydt}\le X_{ky},\qquad \textstyle\sum_k Z^{sw}_{kiydt}\le 1 && \text{(B5)}\\
&\textstyle\sum_i Z^{sw}_{kiydt}\le N^{bay}_{ky},\qquad W_{kydt}=\sum_i Z^{sw}_{kiydt} && \text{(B6)}
\end{aligned}
$$

*Energía en la batería del LHD* ($\zeta_{iydt}=\sum_k Z^{sw}_{kiydt}$)

$$
\begin{aligned}
&B_{iydt} = B^s_{iyd,t-1} - \tfrac{1}{\eta^d}\textstyle\sum_j e_{ij} Y_{ijydt} && \text{(B8)}\\
&B^s_{iyd,t-1}\le B_{iyd,t-1} + (1-\beta)\tilde P_{iydt},\qquad B^s_{iyd,t-1}\ge B_{iyd,t-1} && \text{(B9)}\\
&B^s_{iyd,t-1}\le \bar b_y,\qquad B^s_{iyd,t-1}\ge \beta\bar b_y + (1-\beta)\tilde P_{iydt} && \text{(B10)}\\
&\tilde P_{iydt}\le \bar b_y,\quad \tilde P_{iydt}\le b^{max}\zeta_{iydt},\quad \tilde P_{iydt}\ge \bar b_y - b^{max}(1-\zeta_{iydt}) && \text{(B11)}\\
&\beta\bar b_y\le B_{iydt}\le\bar b_y,\quad B^s_{iyd0}=B_{iyd0},\quad B_{iyd0}=B_{iydT} && \text{(B12)}
\end{aligned}
$$

Con intercambio, (B9)–(B11) dejan $B^s=\bar b_y$: el LHD sale con una batería llena de
la capacidad del año. Se permite intercambiar a cualquier SOC sobre el mínimo.

*Estación: carga en bloques e inventarios*

$$
\begin{aligned}
&V_{kydaa}=X^{ini}_{kyda},\qquad V_{kydta}=V_{kyd,t-1,a}\ \ (a<t\le a+L-1) && \text{(B13)}\\
&\textstyle\sum_{a\in\mathcal A_t}V_{kydta}\le N^C_{ky} && \text{(B14)}\\
&p^C\textstyle\sum_{a\in\mathcal A_t}V_{kydta}\le \pi n^{ssee}_k,\qquad \sum_k\sum_{a}V_{kydta}\le n^{peak} && \text{(B15)}\\
&X^{dch}_{kydt}=X^{dch}_{kyd,t-1}-X^{ini}_{kydt}+W_{kydt}\ \ (t\ge2),\qquad X^{ini}_{kyd1}\le X^{dch}_{kyd1} && \text{(B16)}\\
&S_{kyd,t+1}=S_{kydt}+V_{kydt,t-L+1}-W_{kyd,t+1} && \text{(B17)}\\
&S_{kyd1}=S_{kydT},\qquad X^{dch}_{kyd1}=X^{dch}_{kydT} && \text{(B18)}\\
&S_{kyd1}+X^{dch}_{kyd1}=N^{bat}_{ky} && \text{(B19)}
\end{aligned}
$$

Cada batería carga a potencia fija $p^C$ durante $L$ intervalos (bloque completo, del
SOC mínimo al 100 %). Una batería que llega con un intercambio en $t$ puede empezar a
cargar en ese mismo $t$.

*Balance de potencia*

$$
P^{red}_{ydt}+\textstyle\sum_g P^{gen}_{gydt}+\sum_h P^{bat}_{hydt} = p^C\sum_k\sum_{a\in\mathcal A_t}V_{kydta}\qquad\text{(B20)}
$$

*Degradación del pool*

$$
\begin{aligned}
&N^{pool}_y=\textstyle\sum_k N^{bat}_{ky},\qquad F_y=N^{pool}_y+n^S_y && \text{(B21)}\\
&E_y=\textstyle\sum_{i,d,t,j}e_{ij}Y_{ijydt} && \text{(B22)}\\
&N^{tot}_y=N_yF_y,\qquad N^{tot}_y\bar b_y=\omega E_y && \text{(B23)}\\
&D_y=\bar b_y-\gamma N_y,\qquad \text{(A33)} && \text{(B24)}\\
&Z^R_y\le F_y,\quad Z^R_y\le F^{max}R_y,\quad Z^R_y\ge F_y-F^{max}(1-R_y),\quad Z^R_y\ge F^{min}_y R_y && \text{(B25)}
\end{aligned}
$$

- **(B23) es trilineal y se reduce a dos productos:**
  - **$N_yF_y$, exacto.** La flota es entera y toma pocos valores, así que se desagrega
    por valor:
    $\sum_v u_{yv}=1$, $F_y=\sum_v v\,u_{yv}$, $N_y=\sum_v\nu_{yv}$,
    $N^L_yu_{yv}\le\nu_{yv}\le N^U_yu_{yv}$, $N^{tot}_y=\sum_v v\,\nu_{yv}$.
  - **$N^{tot}_y\bar b_y$, McCormick por tramos.** Con 8 tramos de $\bar b_y$:
    $\sum_s z_{ys}=1$, $\bar b_y=\sum_s b_{ys}$, $N^{tot}_y=\sum_s\tau_{ys}$,
    $\sum_s w_{ys}=\omega E_y$, $L_sz_{ys}\le b_{ys}\le U_sz_{ys}$, y las cuatro
    desigualdades de McCormick de cada tramo. Una sola envolvente en todo $[B_L,B_U]$
    subestimaba los ciclos.
- **(B25)** es la envolvente exacta de $R_yF_y$. Sin la cuarta desigualdad, la relajación
  deja $Z^R=0$ con $R$ fraccional y abarata el reemplazo.
- **(B22) usa la energía consumida**, no la cargada: una batería intercambiada tarde
  puede no terminar de cargar ese día.

*Desigualdad válida: corte de cargas* (activa por defecto)

$$
\textstyle\sum_k\sum_{t<T}X^{ini}_{kydt}\ \ge\ \left\lceil \dfrac{E^{min}_{yd}}{(1-\beta)\,b^{max}}\right\rceil\qquad\text{(B26)}
$$

$E^{min}_{yd}$ es la energía de tracción mínima para cumplir la meta del día (la mochila
de 3.2 sobre las cotas de visitas). Es válida porque el SOC del LHD es cíclico, la
energía solo se repone con intercambios y cada intercambio repone a lo sumo
$(1-\beta)b^{max}$. No cambia el óptimo entero; sube la relajación, que sin ella es
floja.

**Escala numérica.** En la implementación BS, la producción va en kt, los ciclos en miles
y la energía de la degradación en MWh, para evitar coeficientes de ~1e5 en las
descomposiciones. Las ecuaciones se escriben en unidades físicas.

### 3.4 Diferencias entre OB y BS

| Aspecto | OB | BS |
|---|---|---|
| Dónde se carga | El LHD se conecta y queda detenido mientras carga | La batería carga en el rack; el LHD intercambia en un intervalo |
| Inversión | estación, cargadores (1 por bahía), subestación | estación, bahías, cargadores, **pool de baterías**, subestación |
| Potencia de carga | continua: $P\le Z^{ch}p^C$ | bloques fijos de $L$ intervalos a $p^C$ |
| Flota que se degrada | una batería por LHD | pool + una por LHD |
| Ciclos | $N\bar b = S/n_y$ | $N\bar b = \omega E/F$: cada batería del pool baja los ciclos por batería |
| Reemplazo | $n_y R\, c^R$ | $F R\, c^R$, linealizado (B25) |
| Relajación de la operación | casi exacta | floja: cada carga es una batería completa |
| Cota superior | el paso hacia adelante del NB da buenas soluciones | el paso hacia adelante es miope frente a la degradación |

**Consecuencias para los algoritmos (motivan la sección 4):**
- En **OB**, la cota operacional (inversión entera, operación relajada) queda muy cerca
  del óptimo, y el Nested Benders produce buenas soluciones. El NB con cota operacional
  basta.
- En **BS**, la cota necesita el corte de cargas y cortes enteros. La cota superior
  necesita una heurística dedicada a la degradación (DP). El algoritmo que funciona es
  el L-shaped entero, partiendo de una buena solución.

---

## 4. Métodos de solución

### 4.1 Panorama

| Método | Rol | OB | BS | En el esquema final |
|---|---|---|---|---|
| Monolítico (Gurobi) | línea base, validación y pulido | ✓ | ✓ | solo para validar y pulir |
| Preprocesamiento de capacidad | cota válida de subestación | ✓ | ✓ | sí |
| Cota operacional | cota inferior y centro de estabilización | ✓ | ✓ (+ corte de cargas) | sí |
| Nested Benders por año | cota superior (y cortes) | ✓ | ✓ | OB: algoritmo final; BS: solo su iteración inicial |
| DP de degradación | repara la inversión de la cota operacional | — | ✓ | BS |
| Fijar reemplazos | evita la trampa del reemplazo | — | ✓ | BS (con la cota operacional) |
| L-shaped entero multicorte | cota inferior y superior | ✓ | ✓ | BS (desde la solución de la cota + DP) |
| Mejora de la operación | heurística de cota superior | ✓ | ✓ | dentro del L-shaped |
| Certificado por enumeración, generación de columnas, otras | explorados | | | no (4.10) |

**Algoritmo final por tecnología (híbrido, 4.8):**
- **OB:** cota operacional → Nested Benders.
- **BS:** cota operacional → reparación con DP (iteración 0 del NB) → L-shaped entero.

### 4.2 Monolítico

El modelo completo (sección 3) se resuelve con Gurobi.

- **Rol.** Línea base del experimento computacional (6.3). Además, con todas las enteras
  fijas, sirve para **validar** soluciones (reconstrucción, 5.1) y para **pulirlas** (LP
  de las continuas).
- **Limitación.** A partir de unos pocos años el monolítico deja de encontrar soluciones
  factibles en tiempos razonables (2 años: cerca del óptimo; 4 años: sin solución en las
  pruebas preliminares).

### 4.3 Preprocesamiento de capacidad

Antes de iterar, se resuelve el **año de mayor meta** con todo el estado heredado libre
y objetivo $\min n^{ssee}_k$, por estación y en suma. La cota dual del MILP, redondeada
hacia arriba, da una cota inferior válida para los módulos de subestación, que se
impone en el año 1.

Sin este paso, la necesidad del año pico viaja hacia atrás un año por iteración, con
cortes de factibilidad. En la mina modelo da ≥ 1 módulo (≥ 2 con cargadores 2C).

### 4.4 Cota operacional

Se resuelve el monolítico con la **inversión entera** y las **binarias de operación
relajadas**. Es la cota inferior del método.

- **Por qué hace falta.** Benders con cortes LP no supera la relajación LP de sus
  subproblemas: su cota inferior queda clavada en el LP del monolítico. La cota
  operacional mantiene la integralidad de la inversión y por eso es mucho más fuerte.
- **En OB** la operación diaria es casi exacta en su relajación, así que la cota queda
  pegada al óptimo.
- **En BS** se agrega el **corte de cargas por LHD** (4.9), calculado por caso con
  `corte_cargas_op.py`.
- **Otros usos:**
  - su inversión es el centro de la estabilización del NB (4.5);
  - su trayectoria es la que repara la DP en BS (4.6).
- **Parámetros:** gap 1e-4, hasta 5.400 s.

### 4.5 Nested Benders por año

Se basa en Lara et al. (2018) y Zou et al. (2019), en versión determinista y sin retiro
de activos.

- **Bloques.** Un subproblema MILP por año, construido una sola vez. Entre iteraciones
  solo cambian los valores heredados.
- **Estado entre años.**
  - Adyacente (año $y-1\to y$): cargadores, bahías y baterías acumulados; capacidad de
    batería.
  - De decisión única: subestación, generación y BESS. Se deciden en el año 1 y en los
    demás años se fijan por igualdad. Su costo se cobra solo en el bloque del año 1.
  - Las estaciones quedan fijas (abiertas en el año 1).
  - Cada valor heredado entra como copia continua fijada por igualdad. El dual de esa
    igualdad forma el corte.
- **Paso hacia adelante** (cota superior). Cada año se resuelve como MILP con el estado
  del año anterior.
  - **Arranque por días.** El punto de partida de cada año se arma en 4 pasos:
    1. cada día con la infraestructura libre;
    2. el máximo de la infraestructura entre días;
    3. cada día con esa infraestructura fija;
    4. pulido LP del año.
  - **Pulido.** La solución de los 10 años se pule con el monolítico (enteras fijas, LP
    de las continuas). Esto reconcilia energía y degradación entre años.
- **Paso hacia atrás** (cortes). Se resuelve el LP de cada año en el estado del paso
  hacia adelante y se genera un corte de Benders hacia el año anterior.
  - El corte se **fortalece** con una evaluación lagrangiana en el multiplicador del LP
    (corte de Benders fortalecido, Zou et al. 2019; Lara et al. 2018, ec. 63).
  - Se usa la cota dual del lagrangiano, con piso en el LP. Así el corte vale aunque el
    MILP termine por tiempo.
- **Factibilidad.** Si un año sale infactible con el estado recibido, se genera un corte
  de factibilidad desde un LP con holguras y el paso hacia adelante se reinicia. Si el
  corte toca solo decisiones únicas, va directo al año 1. Si la infactibilidad es de
  integralidad (la relajación elástica no viola nada), un corte de factibilidad no la
  corrige: el algoritmo se detiene conservando la mejor solución.
- **Estabilización de caja** (Box-step; Göke et al., 2024).
  - Las enteras de inversión quedan a ±Δ del centro, que es la inversión de la cota
    operacional. Una iteración de cada 3 va sin caja.
  - Δ = 1 en OB y Δ = 2 en BS.
  - Sin caja, el paso hacia adelante oscila o cae en cascadas de factibilidad.
- **Cota inferior.** La cota operacional; en BS con R fijo, la cota del paso hacia atrás
  no entra a la LB.
- **Parada:**
  - gap ≤ 1 %;
  - 4 iteraciones seguidas sin mejora de la cota superior;
  - tope de tiempo (12 h en las corridas de la tesis);
  - o 50 iteraciones.
- **Respaldo.** En cada mejora se guarda la solución, los cortes y la cota, para retomar
  sin recalcular la cota operacional.

**Parámetros comunes (los de las corridas):**
- bloques anuales: MILP con 120 s y MIPFocus 1;
- arranque por días: 120 s y gap 5 % por día, 4 días en paralelo;
- cortes fortalecidos: 300 s;
- pulido en cada iteración; sin paso hacia atrás tras la última iteración.

**Específico de BS:**
- **Corte de cargas** en la cota operacional.
- **Fijar R** (`--fijar_R op`): $R$ fijo en el valor de la cota operacional en todos los
  bloques, hacia adelante y hacia atrás. La relajación LP de $R$ en los cortes abarata
  el reemplazo y el NB cae en soluciones que reemplazan toda la flota; con $R$ fijo, los
  cortes aprenden que llegar al final con poca capacidad obliga a comprar baterías.
- **Iteración 0 = trayectoria de la cota operacional reparada con la DP** (4.6),
  evaluada con los días MILP y pulida.

### 4.6 Heurísticas de cota superior

**DP de degradación (BS).** Programación dinámica exacta sobre el estado (año, baterías
del pool, bahías, capacidad de batería).
- **Problema que resuelve.** La inversión de la cota operacional es buena, pero su
  batería llega al último año justo en el piso con la operación relajada; con la
  operación entera le falta capacidad y el último año queda infactible o compra muchas
  baterías de golpe.
- **Qué hace.** Calcula el ajuste más barato de la trayectoria: agregar o adelantar
  compras de baterías, bahías y cargadores, o reemplazar la flota. Nunca quita lo que la
  trayectoria ya decidió. La flota queda 0,5 kWh sobre el piso.
- **Energía.**
  - En el NB (iteración 0) usa la energía nominal de la trayectoria.
  - En el L-shaped desde cero (UB inicial) se repite hasta 4 pasadas con la energía real
    de los días evaluados.

**Mejora de la operación.** Con la inversión fija, reoptimiza los días como MILP más
largos, partiendo de la operación actual, y pule.
- Pasada 1: todos los años, 600 s por día.
- Pasada 2: 900 s por día, solo si la primera mejoró.

**Pulido.** LP del monolítico con todas las enteras fijas. Reconcilia los años y
entrega soluciones factibles del modelo completo. Las enteras se redondean antes de
entregar una solución de partida.

### 4.7 L-shaped entero multicorte

Se basa en Laporte y Louveaux (1993), en versión multicorte (Birge y Louveaux, 1988).
Usa cortes LP y enteros alternados (Angulo et al., 2016) y se implementa como
branch-and-cut con cortes perezosos (Fischetti et al., 2017). El esqueleto (maestro con
presupuestos, subproblemas en paralelo, dos fases) sigue a Jacobson et al. (2023) y a
Pecci y Jenkins (2025).

**Maestro (MILP chico).** Contiene:
- toda la inversión de los 10 años y los reemplazos;
- la degradación con McCormick, con los ciclos calculados desde presupuestos de
  energía;
- **presupuestos**: potencia contratada por año y energía por (año, día);
- una variable de costo $\theta_{y,d}$ por (año, día).

**Subproblemas.** 10 años × 4 días = 40 subproblemas independientes: la operación de un
día con la primera etapa fija (estación, subestación, generación, BESS, $\bar b_y$,
$P^{pot}_y$ y el presupuesto de energía). Cada valor fijado entra como copia continua,
para obtener los duales. Tienen holguras elásticas penalizadas ($M = 10^6$), así que
siempre son factibles. Se resuelven en paralelo (28 procesos).

**Fase 1 (cortes LP).**
1. Maestro continuo y luego entero, con estabilización de nivel (α = 0,5).
2. Cortes LP de los 40 días, más el **corte de cargas en función de $\bar b_y$** (4.9).

Da la primera cota inferior y el conjunto de cortes.

**Solución inicial.**
- Desde cero: la inversión del maestro de la Fase 1, reparada con la DP y evaluada con
  los días MILP más pulido.
- Desde una solución dada (híbrido): esa solución, **y además** la inversión del
  maestro de la Fase 1 reparada con la DP; se queda con la mejor de las dos. La DP
  tiene que partir del maestro y no de la solución dada, porque nunca quita lo que la
  trayectoria ya decidió: si esa solución trae baterías de más (DP del NB con energía
  nominal), solo la DP desde el maestro, con la energía real, puede evitarlas.
- En los dos casos, la DP hace hasta 4 pasadas, cada una con la energía real de los días
  evaluados en la anterior.

**Fase 2 (branch-and-cut).** El maestro entero se resuelve con un callback de Gurobi
(`MIPSOL`, `cbLazy`). En cada solución entera del maestro:
1. Se agregan **cortes LP**, con la envolvente de cargas y con el escalón del **nivel de
   capacidad** $w_{y,l}$ de la solución.
2. Si no separan, se agregan **cortes enteros locales** por día. Las copias enteras van
   fijas; las continuas, por dualidad lagrangiana. Siguen el esquema de Riley et al.
   (2025):
   - gaps 0,1 / 0,01 / 0;
   - tiempo de 120 s, que se duplica hasta 1.920 s;
   - cota = **mejor cota del MILP**, nunca la mejor solución, para que el corte valga
     aunque el MILP no cierre.
3. Un corte cuenta como separador solo si viola más de 1e-5 del objetivo del maestro.
   Gurobi no hace cumplir violaciones chicas en filas con coeficientes grandes.
4. **Cota superior.** Cada inversión entera nueva y prometedora se evalúa afuera de
   Gurobi: días MILP (120 s, gap 1 %) más pulido.
   - Si el $\bar b$ del maestro supera la capacidad real, se re-resuelve con la real.
   - Si un día falla por potencia contratada, se re-resuelve pagando más potencia.

El callback respeta el tope de tiempo. Si el árbol cierra, se reinicia con los cortes
perezosos convertidos en restricciones.

**Paso final.** Mejora de la operación (4.6) sobre la mejor inversión, con días a
600 s y luego 900 s.

**Validación.** La solución se reconstruye en el monolítico con todas las enteras fijas
(LP), y su costo tiene que coincidir con la cota superior (tolerancia 1e-6).

**Parámetros:**
- maestro con 8 hilos;
- iteraciones de la Fase 1: 30 y 60;
- cota de subestación del preprocesamiento (≥ 1 módulo; ≥ 2 en 2C);
- tope de subestación de 1.500 kW;
- gap de parada de 1e-4 (campaña de resultados) o 1 % (experimento computacional).

**Gap de dualidad.** Las copias continuas ($P^{pot}$, $G$, $H$, $\bar b$, energía) no
se discretizan. Por eso el método puede cerrar el árbol con un gap remanente: el de la
dualidad lagrangiana de esas copias (Zou et al., 2019). La LB sigue siendo válida.

### 4.8 Algoritmo híbrido (algoritmo final)

Combina la aceleración de la cota operacional con el L-shaped:

| | OB | BS |
|---|---|---|
| Arranque | cota operacional + **una iteración del NB** (primer paso hacia adelante, centrado en la cota) | cota operacional + **DP** (iteración 0 del NB) |
| Mejora | L-shaped entero desde ese incumbente, con el tiempo restante del presupuesto | ídem |

Para los resultados de la tesis (6.2), OB usa el NB con cota operacional completo y BS
el híbrido. En el experimento computacional (6.3) se corre el híbrido en las dos
tecnologías.

### 4.9 Desigualdades válidas y cortes

Notación: $U=(1-\beta)\bar b$ es la energía máxima que repone un intercambio, y $\sum
X^{ini}$ es el número de cargas del día ($t<T$).

| Corte | Forma | Por qué es válido | Dónde se usa |
|---|---|---|---|
| Benders | $\theta_{y-1}\ge\Phi^{LP}+\mu(\hat x-x)$ | dualidad LP del bloque anual | NB |
| Benders fortalecido | igual, con $\Phi$ = cota dual del lagrangiano en el $\mu$ del LP, con piso $\Phi^{LP}$ | dualidad lagrangiana; vale aunque el MILP corte por tiempo | NB |
| Factibilidad | desde un LP con holguras; al año 1 si solo toca decisiones únicas | lema de Farkas | NB |
| Preprocesamiento de capacidad | $n^{ssee}_k\ge\lceil$cota dual$\rceil$ | relajación del año pico | NB, L-shaped |
| **Cargas por día** (B26) | $\sum X^{ini}\ge\lceil E^{min}_{yd}/((1-\beta)b^{max})\rceil$ | SOC cíclico; la energía solo se repone con intercambios | BS, en el modelo |
| **Cargas por LHD** | $\sum X^{ini}\ge$ óptimo del MIP chico $\min\sum_i s_i$ s.a. $Us_i\ge\sum_{j\in i}e_{ij}v_j$, visitas enteras en sus cotas y producción ≥ meta | la batería de cada LHD es cíclica y solo la reponen sus intercambios | BS, cota operacional |
| Cargas por LHD, condicional al reemplazo | $\sum X^{ini}\ge n^{sinR}_{yd}-(n^{sinR}_{yd}-n^{nom}_{yd})\sum_{s\le y}R_s$ | $n^{nom}$ vale siempre; $n^{sinR}$ vale si no hubo reemplazo hasta $y$ | BS, cota operacional |
| **Cargas en función de la capacidad** | $\sum X^{ini}\ge g_d(\bar b_y)$, con $g_d$ la envolvente convexa del escalón $n_d(B)$; versión por niveles con binarias $w_{y,l}$ que dan el escalón exacto en soluciones enteras | $n_d(B)$ no crece con $B$ y es válido para cada $B$ | BS, L-shaped (fases 1 y 2) |
| Enteros locales | $\theta_{yd}\ge$ (mejor cota MILP − L)·(indicador de $\hat x$) + L | cota del MILP del día en la vecindad entera | L-shaped |
| Rotura de simetría | (A18) $B_{i'0}\le B_{i0}$ en cada estación | en OB los LHD son intercambiables en el SOC inicial | OB, en el modelo. En BS la precedencia por índice no es válida y está desactivada |

### 4.10 Métodos explorados y no incluidos en el esquema final

Sirven para justificar las elecciones del capítulo:

| Método | Motivo de no incluirlo |
|---|---|
| Certificado por enumeración de inversiones (pool de candidatas bajo umbral, Benders LP por candidata) | solo cierra con gaps ya chicos (~5 % o menos); con más gap el pool se llena. El L-shaped entrega cotas iguales o mejores sin enumerar |
| Nested Benders sin estabilización | el paso hacia adelante oscila o cae en cascadas de factibilidad |
| Estabilización por nivel (*level-set*) en el NB | la inversión no se movía; peor que la caja |
| Híbrido NB → monolítico con solución de partida | útil solo cuando no hay una buena cota inferior |
| Generación de columnas propia (Dantzig–Wolfe + branch-and-price) | prometedora en 2 años, pero el maestro crece y con generación renovable la cota DW queda bajo la operacional |
| Dantzig–Wolfe automático (GCG) | el subproblema de precios no cierra; cota bajo la operacional |
| Cortes Dantzig–Wolfe en el NB | no suben la cota |
| Pisos de intercambios por LHD | no superan al corte agregado |
| Variantes de la cota operacional (dejar enteras partes de la operación) | ganancia de cota marginal con mucho más tiempo |

---

## 5. Postprocesamiento y metodología de comparación

### 5.1 Validación de cada solución

1. **Reconstrucción.** Se arma el monolítico, se fijan todas las enteras y se resuelve
   el LP de las continuas. Su costo tiene que coincidir con la cota superior informada
   (tolerancia relativa 1e-6). Se revisan los residuos fila por fila.
2. **Degradación exacta.** Se recalcula la capacidad con la ecuación bilineal y se
   compara con la envolvente de McCormick, porque el residuo crece en los años tardíos.
3. **Reconciliación del informe.** Los términos de costo tienen que sumar el costo total,
   y este tiene que ser la cota superior.

### 5.2 Informe de la solución

Tablas de:
- costos en valor presente por término;
- balance energético;
- extracción por punto frente a la meta;
- operación por LHD;
- generación y BESS;
- degradación por año (capacidad al inicio y al final, ciclos equivalentes, reemplazos).

Indicadores de control:
- energía de tracción por tonelada (kWh/t);
- potencia comprada en punta frente a la contratada.

### 5.3 Ajustes para comparar OB con BS

El costo total del modelo no se compara directamente entre tecnologías. Se aplican dos
ajustes, que no cambian las cotas del modelo:

1. **Energía real de cada intercambio (solo BS).**
   - **El problema.** El modelo cobra cada recarga como un bloque completo (del SOC
     mínimo al 100 %). La batería que llega en realidad necesita (capacidad del año −
     energía de llegada)/η.
   - **Asignación.** Las baterías descargadas se asignan a las cargas en orden de llegada,
     de forma cíclica en el día.
   - **Sobrante y déficit.** El sobrante se descuenta en orden de mérito: red, luego
     descarga del BESS, luego generación. El déficit se compra a la red, extendiendo la
     carga.
   - **Control.** Sobrante − déficit tiene que ser igual a la energía de los bloques
     menos la real.
2. **Redimensionamiento de generación, BESS y red (las dos tecnologías).**
   - **El LP.** Con la carga real fija y la inversión en estaciones, subestación y
     reemplazos también fija, un LP con las mismas ecuaciones y costos del modelo vuelve
     a elegir solar, eólica, BESS, compra de red y potencia contratada.
   - **Control obligatorio.** Con la carga de la propia solución, el LP reproduce lo que
     pagó el modelo: puede costar menos, nunca más.

El costo comparable de BS es el del ajuste 2; OB pasa por el mismo LP con su propia
carga.

### 5.4 Indicador de holgura operativa

Cada intervalo de cada LHD cae en exactamente una categoría:
- operación;
- carga o intercambio fuera de detenciones;
- detención cargando;
- detención sin cargar;
- **holgura**: inactivo fuera de las detenciones.

Se promedia en los años de régimen (meta máxima), con los pesos de los días, y se
expresa en % del tiempo disponible; también se reporta el peor día.
- **OB:** se calcula además la holgura potencial, como si toda la carga fuera a potencia
  máxima.
- **BS:** utilización de los cargadores e inventario de baterías cargadas.

### 5.5 Figuras de la comparación

- costo total desagregado;
- despacho lado a lado en un año de régimen;
- estado de la flota por intervalo;
- baterías en carga frente a cargadores;
- circulación de baterías (BS);
- uso de cargadores (OB);
- degradación año a año;
- infraestructura por año.

---

## 6. Diseño de las pruebas computacionales

### 6.1 Entorno y condiciones comunes

| Elemento | Valor |
|---|---|
| Servidor | 2 × Intel Xeon Gold 6130 (16 núcleos, 2,10 GHz; 64 hilos lógicos), 503 GB de RAM, Linux |
| Software | Python 3.10, Pyomo 6.9.1, Gurobi 13.0.2 |
| Ranuras | 2 corridas en paralelo, **una por socket** (32 CPU lógicas cada una, fijadas con `taskset`) |
| Hilos de Gurobi | 16 por resolución (`gurobi.env`), la mitad de lo que Gurobi usaría con la máquina completa; los días en paralelo reparten las CPU de la ranura (4 días × 8 hilos) |
| Presupuesto | 12 h de reloj por corrida |
| Datos | v4 de la mina modelo; 4 días representativos; régimen de carga libre y mantención liberada |

En cada caso, OB y BS corren en el mismo socket, para que las dos tecnologías tengan las
mismas condiciones.

### 6.2 Prueba 1: resultados de los 8 casos

Su objetivo es entregar la solución de cada caso en cada tecnología, para la comparación
OB–BS (sección 5).

| Paso | OB | BS |
|---|---|---|
| 1 | Nested Benders desde cero con cota operacional (4.5), Δ = 1, hasta 12 h | Nested Benders desde cero con cota operacional + corte de cargas + fijar R + DP en la iteración 0 (4.5–4.6), Δ = 2, hasta 12 h |
| 2 | — | L-shaped entero (4.7) desde la mejor solución del NB, 10 h |
| 3 | informe, validación y ajustes (sección 5) | ídem |

- **Sin certificado.** En BS la cota inferior final la entrega el L-shaped; en OB, la
  cota operacional.
- **Reparto.** Socket A: casos 1, 3, 5 y 7. Socket B: casos 2, 4, 6 y 8. Primero los NB
  de OB, después los de BS y al final los L-shaped de BS.
- **Se reporta por caso:** cota superior, cota inferior, gap, tiempo y desglose de
  costos; luego el costo comparable BS frente a OB.

### 6.3 Prueba 2: comparación de algoritmos

Su objetivo es medir cómo escala cada método y cuánto aporta la aceleración. Sigue el
estilo de Flores-Quiroz y Strunz (2021), que comparan métodos en instancias de tamaño
creciente, y de Lara et al. (2018), que evalúan variantes de aceleración de un método.

**Instancias.** Caso 2 (base, norte, 0,5C, red + generación + BESS) en OB y en BS, con
horizontes de **4 y 10 años**. Los días representativos se mantienen fijos en 4 por
año.

**Configuraciones** (aceleración = cota operacional + DP; en OB no hay DP):

| Configuración | Descripción | OB | BS |
|---|---|---|---|
| Monolítico | Gurobi sobre el modelo completo (BS con McCormick), MIPGap 1 %, 12 h de solver | ✓ | ✓ |
| NB sin aceleración | sin cota operacional (LB del paso hacia atrás; caja centrada en la primera solución); BS además sin DP, sin corte de cargas y con R libre | ✓ | ✓ |
| NB con aceleración | configuración de 6.2 | ✓ | ✓ |
| L-shaped sin aceleración | desde cero; en BS sin la DP de la UB inicial | ✓ | ✓ |
| Híbrido | cota operacional + (OB: una iteración del NB; BS: DP) → L-shaped | ✓ | ✓ |

**Condiciones comunes:**
- cortes fortalecidos y estabilización activa en todos los NB;
- gap objetivo de **1 %** en todos los métodos;
- 12 h por corrida;
- mismo entorno que 6.1.

**Orden.** Las instancias corren de menor a mayor tamaño: primero 4 años y luego 10.
Las de 10 años con NB con aceleración (OB y BS) e híbrido de BS se reutilizan de la
prueba 1. Son 17 corridas nuevas.

**Métricas** (como en los dos trabajos de referencia):
- tamaño de cada formulación: variables y restricciones del monolítico, del bloque anual
  del NB y del maestro y del día del L-shaped;
- tiempo hasta la primera solución factible, hasta gap de 5 % y hasta 1 %;
- cota superior, cota inferior y gap a las 12 h; número de iteraciones;
- curvas de gap contra tiempo en la instancia de 10 años.

**Preguntas que responde:**
1. ¿Desde qué horizonte el monolítico deja de ser competitivo?
2. ¿Cuánto aporta la aceleración (cota operacional + DP) al NB y al L-shaped?
3. ¿Cuál es el mejor método para cada tecnología, y por qué difieren?

### 6.4 Reglas y supuestos de las pruebas

- Todos los resultados se validan por reconstrucción en el monolítico (5.1).
- Las cotas inferiores reportadas son válidas para el modelo con McCormick. El residuo
  de McCormick se reporta aparte.
- Las corridas usan los valores por defecto del código, salvo lo que fija cada cola:
  `cola_tesis_traccion.sh` (prueba 1) y `cola_tesis_experimento.sh` (prueba 2).

---

## 7. Supuestos y limitaciones metodológicas

- **Determinístico.** No hay incertidumbre en precios, metas ni recursos.
- **Cuatro días representativos por año.** La operación de cada año se resume en 4 días
  con peso igual. No se estudia la sensibilidad al número de días.
- **Bloque completo de carga en BS.** El modelo cobra cada carga del SOC mínimo al
  100 %. El ajuste de 5.3 corrige la energía para la comparación, pero no la decisión.
- **McCormick.** Es una relajación de la degradación: en los años tardíos la solución
  satisface la envolvente y no el bilineal exacto. Se verifica en la validación.
- **Gap de dualidad.** Las copias continuas del NB y del L-shaped no se discretizan, así
  que la convergencia finita no está garantizada.
- **Miopía del paso hacia adelante.** Cada año del NB ve solo la batería que le dejaron.
  En BS lo mitigan la DP y fijar R, pero no lo eliminan.
- **DP con energía nominal en el NB.** La reparación de la iteración 0 usa la energía de
  la trayectoria de la cota. Cuando la energía real difiere mucho, la DP puede comprar
  más baterías de las necesarias; el L-shaped posterior lo corrige solo en parte.

---

## 8. Equivalencias entre la notación y el código

| Notación | Código | Notación | Código |
|---|---|---|---|
| $X$, $\Delta X$ | `X`, `Delta_X` | $Y$, $Z$ | `Y`, `Z` |
| $N^C$, $\Delta N^C$ | `N_chargers`, `Delta_N_chargers` | $Z^{ch}$ | `Z_charge` |
| $n^{ssee}$, $P^{max}$ | `n_ssee_k`, `P_max_k` | $\sigma^+$, $\sigma^-$ | `StartCharge`, `EndCharge` |
| $P^{pot}$ | `P_pot` | $P$, $B$ | `P`, `B` |
| $R$ | `R` | $P^{red}$ | `P_red` |
| $\bar b$, $D$ | `b_bar`, `D` | $P^{gen}$, $C$ | `P_gen`, `Curt_g` |
| $S$ (OB) | `S` | $P^{bat}$, $A$ | `P_bat`, `A_h` |
| $N$, $w$ | `N_ciclos`, `w_deg` | $G$, $H$ | `G_g`, `H` |
| $Z^{sw}$ | `Z_swap` | $V$ | `Sv` |
| $B^s$ | `B_s` | $S$ (BS), $X^{dch}$ | `S`, `X_dch` |
| $\tilde P$ | `P_bbar_zagg` | $X^{ini}$, $W$ | `X_ini`, `W` |
| $N^{bay}$, $N^{bat}$ | `N_bays`, `N_batteries` | $N^{pool}$, $F$ | `N_batteries_total`, `n_battery_fleet` |
| $E$, $N^{tot}$ | `EnergyConsumed`, `N_total` | $Z^R$ | `Z_repl` |

**Archivos de los métodos:**

| Método | Archivo |
|---|---|
| Modelo (conjuntos, parámetros, variables, restricciones, objetivo) | `src/optimization/functions.py` (`OptSets`, `OptParameters`, `BoundRules`, `ConstraintRules`, `ObjectiveRules`) |
| Nested Benders | `src/optimization/decomposition/driver.py`, `year_block.py`, `passes.py`, `cuts.py`, `day_blocks.py`, `respaldo.py`; entrada `setup.py --mode decomposed` |
| DP de degradación | `src/optimization/decomposition/degradacion_dp.py` |
| Cortes de cargas | `corte_cargas_op.py`, `cota_cargas_lhd.py`, `min_charges_cut` en `functions.py` |
| L-shaped | `lshaped.py`, `src/optimization/decomposition/lshaped.py`, `benders_dias.py` |
| Mejora de la operación | `mejorar_ub.py` y `--mejora` de `lshaped.py` |
| Postprocesamiento | `consumer.py`, `escribir_parameters.py`, `json_plotter.py`; comparación en `cost_comparisons/` (`ajuste_energia_swap.py`, `redimensionar_gen_bess.py`, `resultados_finales.py`); holgura en `scripts/indicador_holgura.py` |
| Colas de las pruebas | `cola_tesis_traccion.sh`, `cola_tesis_experimento.sh`; en OB, `cola_tesis_ob_v4.sh` |

---

## Referencias

- Angulo, G., Ahmed, S. y Dey, S. S. (2016). Improving the integer L-shaped method. *INFORMS Journal on Computing*, 28(3), 483–499.
- Birge, J. R. y Louveaux, F. V. (1988). A multicut algorithm for two-stage stochastic linear programs. *European Journal of Operational Research*, 34(3), 384–392.
- Fischetti, M., Ljubić, I. y Sinnl, M. (2017). Redesigning Benders decomposition for large-scale facility location. *Management Science*, 63(7), 2146–2162.
- Flores-Quiroz, A. y Strunz, K. (2021). A distributed computing framework for multi-stage stochastic planning of renewable power systems with energy storage as flexibility option. *Applied Energy*, 291, 116736.
- Göke, L., Schmidt, F. y Kendziorski, M. (2024). Estabilización Box-step de Benders para planificación energética (Algoritmos 2 y 6). *European Journal of Operational Research*, 316. (Título exacto por verificar.)
- Jacobson, A., Pecci, F., Sepulveda, N., Xu, Q. y Jenkins, J. (2023). A computationally efficient Benders decomposition for energy systems planning problems with detailed operations and time-coupling constraints. *INFORMS Journal on Optimization*.
- Lara, C. L., Mallapragada, D. S., Papageorgiou, D. J., Venkatesh, A. y Grossmann, I. E. (2018). Deterministic electric power infrastructure planning: Mixed-integer programming model and nested decomposition algorithm. *European Journal of Operational Research*, 271(3), 1037–1054.
- Laporte, G. y Louveaux, F. V. (1993). The integer L-shaped method for stochastic integer programs with complete recourse. *Operations Research Letters*, 13(3), 133–142.
- Pecci, F. y Jenkins, J. D. (2025). Regularized Benders decomposition for high performance capacity expansion models. *IEEE Transactions on Power Systems*, 40(4), 3105–3116.
- Riley, B. P., Daoutidis, P. y Zhang, Q. (2025). Integer L-shaped method with non-supporting no-good optimality cuts. arXiv:2511.06340.
- Zou, J., Ahmed, S. y Sun, X. A. (2019). Stochastic dual dynamic integer programming. *Mathematical Programming*, 175, 461–502.
