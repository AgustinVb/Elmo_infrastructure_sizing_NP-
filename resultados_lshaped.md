# Resultados: L-shaped entero multicorte en los 8 casos swap v4

Entregable de `lshaped_entero_swap.md` §7. Código: `lshaped.py` (runner) y
`src/optimization/decomposition/lshaped.py` (clase `LShaped`), sobre el Benders por
(año, día) de `BendersDias`. Colas: `cola_lshaped.sh`, `cola_lshaped_cierre.sh`,
`cola_lshaped_mejora.sh`. Salidas: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/<caso>/lshaped_A2/`
(y `lshaped_A2/mejora/`, `lshaped_A3/`, `lshaped_A4/`).

---

## 1. Qué quedó implementado

| Paso | Implementación | Diferencia con el plan de `lshaped_entero_swap.md` |
|---|---|---|
| Fase 1 | Etapas 1 y 2 de `BendersDias` (maestro continuo y luego entero, level-set α = 0,5), cortes LP de los 40 días con el **corte de cargas en función de b̄_y** (envolvente convexa del escalón n_d(B), `agregar_corte_cargas`) | Igual |
| UB inicial | Inversión del maestro de la Fase 1 reparada con la DP de degradación (`ub_inicial_dp`, hasta 4 pasadas con la energía real de los días) → días MILP con la inversión fija + pulido | Sin `cbSetSolution`: la UB se lleva afuera de Gurobi |
| Fase 2 | *Branch-and-cut* del maestro entero con callback `MIPSOL` y `cbLazy`: (1) cortes LP con la envolvente y con el escalón del **nivel de capacidad** w_{y,l} de la solución; (2) si no cortan, cortes **enteros locales** por día (copias enteras fijas, continuas por dualidad lagrangiana) con el esquema de Riley et al. (2025): gaps 0,1 / 0,01 / 0 y tiempo 120 s que se duplica hasta 1.920 s; R = mejor cota del MILP | No se discretizan las copias continuas (P_pot, G, H, b̄, energía): ver §5 |
| UB en la Fase 2 | Cada inversión entera nueva y prometedora: días MILP (120 s, gap 1 %) + pulido flexible (`pulir_flexible`). Si el b̄ del maestro supera la capacidad real, se re-resuelve con b̄ = capacidad real − 0,1; si un día falla por P_pot, se re-resuelve con P_pot libre hacia arriba pagando su costo (`Subproblema.ub(subir=...)`) | Correcciones agregadas en la cola |
| Paso final | `mejorar_ub`: con la inversión de la mejor UB fija, los 40 días MILP con 600 s y gap 1e-4 partiendo de su operación, y pulido; si el pulido conjunto falla, acepta año por año. Segunda pasada a 900 s solo si la primera mejoró y queda tiempo | Equivale a `mejorar_ub.py` del esquema anterior, ahora dentro del runner (`--mejora`) |
| Validación | `lshaped.py --solo_validar`: reconstruye la solución en el monolítico con todas las enteras fijas (LP) y exige el costo de `resumen.json` (tolerancia 1e-6) | — |

Controles agregados al callback (versión del 30-oct): respeta el tope de `--horas`
dentro de los cortes enteros (los MILP del día se acortan al tiempo restante; antes
el caso 2 se pasó 64 min), y avisa con `ALERTA aceptacion silenciosa` cuando Gurobi
toma como incumbente una solución a la que el callback le había agregado cortes que
no la violaban más que su tolerancia (pasó en el caso 7).

Uso:

```
python -u lshaped.py --data_folder data/Tesis_final/Mina_modelo_v4/<caso> \
    --out <carpeta> --horas 5 --cpu 28 --capacidad station_1=<n> \
    [--mejor_ub U --mejor_lb L]           # solo para comparar y alarmar cotas invalidas
python -u lshaped.py ... --solucion <carpeta>/solucion --ub_esperado <UB> --solo_validar
python -u lshaped.py ... --solucion <carpeta>/solucion --ub_esperado <UB> --solo_mejorar
```

`--capacidad station_1=2` en los casos con cargadores 2C (cota del presolve de
capacidad; `cola_lshaped.sh` la toma de los `benders.log` del caso).

---

## 2. Resultados del branch-and-cut (`lshaped_A2/`, 5 h, 28 núcleos, desde cero)

Todas las UB validadas en el monolítico. "Antes" = mejor conocido del esquema
NB + DP + `mejorar_ub` + certificado (`cola_swap_v4_algoritmo.md` §1).

| Caso | UB L-shaped | LB L-shaped | Gap L-shaped | Gap antes | Gap combinado | LB Fase 1 | Cortes LP / nivel / enteros | Tiempo |
|---|---|---|---|---|---|---|---|---|
| 1 base/norte/0,5C/RED | 3.294.143,94 | 3.263.609,06 | 0,927 % | 0,927 % | 0,927 % | 3.223.821 | 4 / 142 / 17 | 0,6 h |
| 2 base/norte/0,5C/RED_GEN_BESS | 2.492.783,69 | 2.441.084,20 | 2,074 % | 2,098 % | 2,074 % | 2.403.408 | 81 / 149 / 61 | 6,1 h |
| 3 base/norte/0,5C/RED_GEN | 2.548.510,39 | 2.488.853,10 | 2,341 % | 2,676 % | 2,341 % | 2.443.075 | 87 / 211 / 97 | 4,9 h |
| 4 escalada20/norte/0,5C/RED | 4.009.428,23 | 3.884.865,90 | 3,107 % | 5,018 % | 3,107 % | 3.858.248 | 12 / 157 / 22 | 1,5 h |
| 5 escalada20/norte/0,5C/RED_GEN_BESS | 3.114.708,43 | 2.983.268,87 | 4,220 % | 3,731 % | 3,731 % | 2.962.323 | 72 / 144 / 29 | 2,8 h |
| 6 base/centro/0,5C/RED_GEN_BESS | 2.756.226,03 | 2.697.625,61 | 2,126 % | 2,603 % | 2,126 % | 2.657.083 | 181 / 302 / 66 | 5,6 h |
| 7 base/norte/1C/RED_GEN_BESS | 2.753.422,07 | 2.636.527,75 | 4,245 % | 3,913 % | 3,913 % | 2.626.735 | 86 / 112 / 0 | 0,6 h |
| 8 base/norte/2C/RED_GEN_BESS ¹ | 4.988.177,05 | 4.735.817,21 | 5,059 % | 4,971 % | 4,312 % | 4.614.050 | 1.585 / 639 / 352 | 3,0 h |

¹ Corrió con `--capacidad station_1=1` (debía ser 2); relanzado en `lshaped_A3/` (§3).

- **LB:** iguala o mejora la certificada en los 8 casos (mejora en 2, 3, 6 y 8), sin
  enumerar inversiones. El corte de cargas en la Fase 1 deja la LB a 1–2,5 % de la
  final; los cortes enteros y por nivel aportan el resto.
- **UB:** en 7 casos la UB final es la inicial de la DP; el branch-and-cut casi no
  la mejora porque evalúa cada inversión con días a 120 s y gap 1 %. Mejora la
  conocida en 2, 3, 4 y 6 (caso 4: −80.665, −2,0 %) y queda peor en 5, 7 y 8, que
  son los que en el esquema anterior bajaron con `mejorar_ub`. De ahí el paso final.
- **Cierres con status 2 antes del tope** (casos 1, 4, 5, 7): el callback acepta la
  solución cuando los cortes enteros ya no la separan (θ = cota lagrangiana local).
  La LB es válida, pero el gap que queda es el de dualidad de las copias continuas (§5).
- Gap promedio combinado: 3,24 % → 2,82 %.

---

## 3. Versión final: paso de mejora y umbral de corte (cola del 30-31 oct)

`cola_lshaped_mejora.sh`. Dos cosas cambiaron respecto del §2:

- **Paso final de mejora** (`--mejora 600,900`): con la inversión de la mejor UB fija,
  los 40 días MILP a 600 s (y a 900 s si la primera pasada mejoró), y pulido. En los
  casos 1–6 se aplicó a la solución de `lshaped_A2/` (`--solo_mejorar`, en
  `lshaped_A2/mejora/`); los casos 7 y 8 se corrieron completos con el código final.
- **Cortes que Gurobi no hace cumplir.** En los casos 7 y 8 el árbol cerraba en la
  cota LP con 0 cortes enteros. Diagnóstico (caso 7): el incumbente final pasó por el
  callback, con las enteras exactas, y viola un corte en 0,26 en absoluto (2e-4 del
  costo de su día); Gurobi no hace cumplir esa violación en filas con coeficientes de
  hasta 1e6 (duales de la penalidad elástica), el callback "cortaba" y nunca pasaba a
  los cortes enteros. No era la tolerancia de integralidad (`IntFeasTol` 1e-9 no
  cambia nada). Arreglo: un corte cuenta como separador solo si viola más de
  `tol_lp` = 1e-5 del objetivo del maestro (≈ 26 en el caso 7); si no, se agrega igual
  y se sigue con los cortes enteros. De respaldo, si el árbol termina con un incumbente
  que el callback no aceptó, los cortes lazy pasan a restricciones y se reoptimiza.
  Con eso el caso 7 genera 86 cortes enteros (LB +11.790 sobre la certificada) y el 8,
  74 (+36.624). La cota puede quedar hasta ~40 × `tol_lp` × objetivo bajo la exacta
  (~0,04 %).

Resultado final (todas las UB validadas en el monolítico):

| Caso | UB | LB | Gap | Gap antes | Δ UB | Δ LB | Origen |
|---|---|---|---|---|---|---|---|
| 1 base/norte/0,5C/RED | 3.294.143,94 | 3.263.609,06 | 0,93 % | 0,93 % | 0 | 0 | A2 (la mejora no encontró nada) |
| 2 base/norte/0,5C/RED_GEN_BESS | 2.483.322,02 | 2.441.084,20 | 1,70 % | 2,10 % | −9.718 | +336 | A2 + mejora |
| 3 base/norte/0,5C/RED_GEN | 2.538.590,51 | 2.488.853,10 | 1,96 % | 2,68 % | −11.687 | +6.817 | A2 + mejora |
| 4 escalada20/norte/0,5C/RED | 4.006.626,43 | 3.884.865,90 | 3,04 % | 5,02 % | −83.467 | 0 | A2 + mejora |
| 5 escalada20/norte/0,5C/RED_GEN_BESS | 3.098.666,35 | 2.983.268,87 | 3,72 % | 3,73 % | −209 | 0 | A2 + mejora |
| 6 base/centro/0,5C/RED_GEN_BESS | 2.745.492,65 | 2.697.625,61 | 1,74 % | 2,60 % | −22.417 | +1.766 | A2 + mejora |
| 7 base/norte/1C/RED_GEN_BESS | 2.737.234,87 | 2.648.318,12 | 3,25 % | 3,91 % | −6.660 | +11.790 | UB de A3, LB de A4 |
| 8 base/norte/2C/RED_GEN_BESS | 4.932.977,72 | 4.739.838,52 | 3,92 % | 4,97 % | −16.272 | +36.624 | A4 (2 módulos) |

Gap promedio: 3,24 % → **2,53 %**. La UB mejora en 7 casos y la LB en 5, sin enumerar
inversiones ni certificado aparte.

---

## 4. Conclusión

El L-shaped entero queda como algoritmo definitivo para los casos swap: en una sola
corrida de ~5–7 h por caso da UB y LB que igualan o mejoran las del esquema anterior
(NB + DP + `mejorar_ub` + certificado) en los 8 casos, con gap promedio 2,53 % (antes
3,24 %). La UB viene sobre todo de la DP inicial y del paso final de mejora; la LB, del
corte de cargas en la Fase 1 y de los cortes enteros locales. Lo que queda del gap es
el de dualidad de las copias continuas (§5) y el gap de los MILP del día. Los casos 1,
4 y 5 no movieron la LB certificada.

---

## 5. Limitaciones conocidas

- **Gap de dualidad de las copias continuas.** El corte entero local fija las copias
  enteras y trata P_pot, G, H, b̄ y la energía por dualidad lagrangiana: su R es la
  cota lagrangiana, que puede quedar bajo el costo real Q(x) (Zou, Ahmed y Sun, 2019).
  Cuando ningún corte separa, el callback acepta y el árbol cierra con esa cota. Para
  cerrar más: discretizar esas copias (plan original, §1 de `lshaped_entero_swap.md`)
  o cortes con Q(x) evaluado en la solución.
- **Tolerancia de los cortes lazy.** Gurobi acepta una solución si los cortes la violan
  menos que su tolerancia de factibilidad; ahora queda registrado en el log.
- **UB dentro del árbol.** Días a 120 s y gap 1 %: la UB depende de la DP inicial y
  del paso final.
- **Capacidad de la subestación.** Sin la cota del presolve (`--capacidad`) el maestro
  propone un módulo y los días de los casos 2C salen infactibles.

---

## 6. Versión 5 de la operación del swap (sensibilidad, casos 1–3)

Dos cambios de la operación, por entorno y apagados por defecto (`functions.py`), que
dejan intacta la v4:

- `ELMO_SWAP_SOC_MAX=0.30`: el LHD solo hace swap si llega con SOC ≤ 30 %.
- `ELMO_CARGA_CAPACIDAD_REAL=1`: la carga dura lo que pide la capacidad del año (índice
  de nivel `u_tc`).

Con cualquiera de los dos, `carga_parcial()` quita `b_y_link_lower` (b̄_y ≤ D_{y−1} en vez
de la igualdad): los dos acotan b̄ por arriba para una operación fija y, con la
igualdad, la UB salía infactible casi siempre (RED cerró con 31 % de gap). Cambios del
L-shaped para la v5 (`cola_lshaped_v5.sh`, tercera vuelta, salidas en
`casos_finales_v5/<caso>/lshaped_r/`):

- **Energía mínima entera en el maestro.** `rango_presupuestos(entero_tl=120)` acota cada
  presupuesto de energía con la cota del MILP del día, no solo con su LP.
- **Reparación de las inversiones aceptadas.** Si la UB de una inversión sale infactible
  porque su flota no alcanza con la energía real de los días, `LShaped._reparar_v5` la
  repara con la DP de degradación y evalúa la inversión reparada.
- **Cotas restauradas.** `update()` después de cada restauración de cotas en el maestro de
  capacidad y en `Maestro.regularizar`/`rangos`: Gurobi no actualiza los atributos hasta
  `update()`, y la vuelta anterior de RED_GEN y RED_GEN_BESS corrió con cotas corruptas.

Resultados (21 núcleos, los tres en paralelo; todas las UB validadas en el monolítico):

| Caso | UB v5 | LB v5 | Gap v5 | Gap v4 (§3) | LB Fase 1 | Cortes LP / nivel / enteros | Tiempo |
|---|---|---|---|---|---|---|---|
| 1 base/norte/0,5C/RED ¹ | 3.217.236,44 | 3.151.372,49 | 2,05 % | 0,93 % | 3.015.885 | 4 / 52 / 100 | 1,7 h |
| 2 base/norte/0,5C/RED_GEN_BESS | 2.460.277,29 | 2.349.865,64 | 4,49 % | 1,70 % | 2.280.747 | 119 / 224 / 76 | 8,4 h (tope) |
| 3 base/norte/0,5C/RED_GEN | 2.511.592,69 | 2.378.166,92 | 5,31 % | 1,96 % | 2.292.026 | 127 / 236 / 164 | 7,5 h (tope) |

¹ Parte de la solución de la segunda vuelta (UB 3.244.652,50) y cierra el árbol a la 1,7 h.

Comparación con on-board (`cost_comparisons/comparar_swap_ob_v5.py --sub lshaped_r`,
`swap_vs_ob_v5_casos.csv`). El swap se ajusta a la energía real de los días y se
redimensiona, igual que en v4:

| Caso | Swap modelo | Swap redimensionado | OB | Swap vs OB v5 | Swap vs OB v4 |
|---|---|---|---|---|---|
| 1 RED | 3.217.236 | 3.110.048 | 3.083.536 | +0,86 % | +0,65 % |
| 2 RED_GEN_BESS | 2.460.277 | 2.428.859 | 2.383.673 | +1,90 % | +1,63 % |
| 3 RED_GEN | 2.511.593 | 2.483.186 | 2.487.888 | −0,19 % | +0,19 % |

- **El patrón contra OB es el mismo que en v4.** Swap y OB quedan dentro de ±2 % en los
  tres casos. RED_GEN es el caso más favorable al swap, el único donde gana en v5, y
  RED_GEN_BESS el menos favorable. La BESS le sirve más a OB (−104.215 entre RED_GEN y
  RED_GEN_BESS) que al swap (−54.327), que ya tiene baterías en la estación.
- **Los gaps de la v5 son mayores.**
  - La LB de la Fase 1 queda 6–9 % bajo la UB, contra 2–4 % en v4: sin
    `b_y_link_lower` la relajación es más débil.
  - El maestro subestima la energía de los días: los MILP de energía mínima no cierran
    y los días resueltos a costo mínimo gastan ~1 % más que su mínimo.
  - Por eso casi toda inversión entera aceptada cruza el piso B_L y sale infactible.
    La reparación le agrega baterías y su costo real queda 6–12 % sobre lo que estimaba
    el maestro.
  - Cada callback entero cuesta ~20 min y sube la LB unos miles; RED_GEN y RED_GEN_BESS
    terminaron por tiempo con la LB todavía subiendo.
- **Uso en la tesis.** La v5 queda como análisis de sensibilidad de la operación del
  swap, y las cifras principales salen de la v4. Con gaps de 4,5–5,3 %, el signo de la
  diferencia contra OB en RED_GEN y RED_GEN_BESS no está certificado.

## Referencias

Ver `lshaped_entero_swap.md`; además Zou, J., Ahmed, S. y Sun, X. A. (2019).
Stochastic dual dynamic integer programming. *Mathematical Programming*, 175, 461–502.
