# Resultados: L-shaped entero multicorte en los 8 casos swap v4

Entregable de `lshaped_entero_swap.md` §7. Código: `lshaped.py` (runner) y
`src/optimization/decomposition/lshaped.py` (clase `LShaped`), sobre el Benders por
(año, día) de `BendersDias`. Colas: `cola_lshaped.sh`, `cola_lshaped_cierre.sh`,
`cola_lshaped_mejora.sh`. Salidas: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/<caso>/lshaped_A2/`
(y `lshaped_A2/mejora/`, `lshaped_A3/`).

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

## 3. Paso final de mejora (cola del 30-oct, `cola_lshaped_mejora.sh`)

_Parcial al 30-oct 19:50 UTC; la cola sigue (casos 3, 6, 1 y 8 de la ranura A, y
casos 7 y 8 completos en `lshaped_A4/`)._

`--solo_mejorar` sobre la solución de `lshaped_A2/` (600 s por día; 900 s en la
segunda pasada si la primera mejoró), salvo el caso 7, que es la corrida completa de
`lshaped_A3/` (branch-and-cut + mejora). Todas validadas en el monolítico.

| Caso | UB antes de la mejora | UB final | Mejora | Mejor conocida antes | Gap combinado |
|---|---|---|---|---|---|
| 2 | 2.492.783,69 | 2.483.322,02 | −0,38 % | 2.493.040,16 | 1,70 % (2,10) |
| 3 | 2.548.510,39 | 2.541.928,08 ¹ | −0,26 % | 2.550.277,54 | 2,09 % (2,68) |
| 4 | 4.009.428,23 | 4.006.626,43 | −0,07 % | 4.090.093,05 | 3,04 % (5,02) |
| 5 | 3.114.708,43 | 3.098.666,35 | −0,52 % | 3.098.875,18 | 3,72 % (3,73) |
| 7 (A3) | 2.761.098,79 | 2.737.234,87 | −0,86 % | 2.743.895,04 | 3,68 % (3,91) |

¹ Primera pasada; la segunda está en curso y la solución aún no está validada.

Con esto los casos 5 y 7, que quedaban sobre la mejor conocida, la mejoran. Gap
combinado promedio de los 8 casos: 3,24 % → 2,70 % (casos 1, 6 y 8 todavía sin el paso
de mejora).

**Cortes que Gurobi no hace cumplir.** Al relanzar el caso 8 con 2 módulos
(`lshaped_A3/`) el árbol cerró a los 65 min en 4.703.144, bajo la LB certificada, con
0 cortes enteros; lo mismo el caso 7 (A2 y A3, 36 min). Diagnóstico (caso 7): el
incumbente final pasó por el callback, con las enteras exactas, y viola un corte en
0,26 en absoluto (2e-4 del costo de su día). Gurobi no hace cumplir esa violación en
filas con coeficientes de hasta 1e6 (duales de la penalidad elástica), así que el
callback "cortaba", nunca pasaba a los cortes enteros, y Gurobi aceptaba. No era la
tolerancia de integralidad (`IntFeasTol` 1e-9 no cambia nada). Arreglo, en prueba en
`lshaped_A4/`: un corte cuenta como separador solo si viola más de `tol_lp` = 1e-5
del objetivo del maestro (≈ 26 en el caso 7); si no, se agrega igual y se sigue con
los cortes enteros. De respaldo, si el árbol termina con un incumbente que el callback
no aceptó, los cortes lazy pasan a restricciones del maestro y se reoptimiza.

---

## 4. Conclusión

_Pendiente: se completa al terminar la cola._

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

## Referencias

Ver `lshaped_entero_swap.md`; además Zou, J., Ahmed, S. y Sun, X. A. (2019).
Stochastic dual dynamic integer programming. *Mathematical Programming*, 175, 461–502.
