# Fase 4: experimento de cota del flujo de la estación

Script: `experimento_estacion_flujo.py`. Salida: `output/Resultados_finales_tesis/Mina_modelo/experimento_estacion_flujo/caso3_v4/` (`experimento.json`, `experimento.log`, logs de Gurobi en `gurobi/`). Rama `battery_swapping_multiaño` (HEAD 0b911ba9c más cambios locales), 2026-10-22/23.

## Montaje

- **Caso:** caso 3 de swap v4 (`Mina_modelo_v4/.../05C/RED_GEN`, sin BESS, 0,5C, L = 13). Incumbente k=2 (`incumbente_respaldo_k2.pkl`), llevado al pipeline cíclico y pulido a 3.289.542,58.
- **MILP del día:** bloque year_day de `BendersDias` armado en gurobipy.
  - Inversión fija en el incumbente: N_bat = 2, N_C = 2, n_ssee = 1, P_pot = 241 y la G del solar.
  - Presupuestos libres.
  - **Y, Z y Z_swap libres.** Gurobi decide los swaps.
  - El único costo del día es la energía de la red (P_red).
- **Pipeline:** cíclico en todas las variantes (`ELMO_PIPELINE_CICLICO=1`), porque el flujo es una circulación.
- **Límites:** 900 s por variante, 6 hilos y MIPGap 1e-4. Los parámetros de Gurobi son los por defecto.

**Variantes:**
- **actual:** el bloque de estación del MILP (S, Sv, X_ini, X_dch, W).
- **power_link:** la estación se reemplaza por una circulación en el grafo de la DP: nodos (t, s) con 105 estados por t (N = 2, N_C = 2, L = 13) y unos 27.000 arcos (w, x) con Q. Se enlaza con `Σ_i Z_swap[t] = Σ w_a f_a`, y Q entra al balance de potencia. Los arcos se filtran por cargadores, subestación, pico y LHD que pueden hacer swap en t.
- **arc_cost:** como power_link, pero con el costo de energía en los arcos (`coef_t · max(0, p_c·Q_a − A_t)`) y P_red fuera del objetivo. Además filtra Q por `P_red ≤ p_peak` y, en horas de punta, por `P_red ≤ P_pot`.
- **\*_u:** el enlace se desagrega por número de swaps: `u[t,w] = Σ_{a: w_a = w} f_a`, con u binaria.

## Chequeo del grafo (Y, Z y Z_swap fijos en el incumbente; LP de arc_cost)

| Día | Flujo, arcos con w_a = W_t | DP (`validar_dp_estacion`) | Flujo, enlace agregado | **Modelo actual (LP), mismos LHD fijos** |
|---|---:|---:|---:|---:|
| (4,196) | 24.150,81, f entero | 24.150,81 | 24.150,81 | **24.150,81** |
| (5,288) | 16.868,69, f entero | 16.868,69 | 16.868,69 | **16.868,69** |
| (7,105) | 21.766,08, f entero | 21.766,08 | 21.766,08 | **21.714,36** |

(Corregido el 2026-10-23. La primera versión del chequeo con enlace agregado daba 17.489 / 8.886 / 11.596 por un error: se fijaban las cotas de Y, Z y Z_swap pero se llamaba a `relax()` sin `update()`, así que ese LP corría con los swaps **libres** y repetía el LP libre. Se corrigió el script y se volvió a medir.)

- **El grafo es correcto.** Con los arcos restringidos al w del incumbente, el LP es entero e igual a la DP (Martin-Rardin-Campbell).
- **Con los swaps enteros, la estación es exacta o casi exacta en todas las formulaciones:** el flujo con enlace agregado da la DP en los 3 días, y el LP del modelo actual la da en dos y queda un 0,24 % bajo en el tercero.
- Como el LP libre es 17.489 / 8.886 / 11.596 en todas las formulaciones, **todo el hueco del LP viene de que los swaps son fraccionarios (lado LHD)**, no de la estación.

## Resultados (900 s)

| Día | Variante | Filas / columnas | LP | Raíz | Cota 180 s | Cota final | Mejor solución |
|---|---|---|---:|---:|---:|---:|---:|
| (4,196), incumbente 24.471 | **actual** | 15.098 / 15.135 | 17.489 | 18.041 | 18.041 | **18.470** | **20.949** |
| | power_link | 30.939 / 39.435 | 17.489 | 18.041 | 18.041 | 18.041 | 24.096 |
| | arc_cost | 27.949 / 35.467 | 17.489 | 18.041 | 18.041 | 18.041 | 22.506 |
| | power_link_u | 31.479 / 39.975 | 17.489 | 18.041 | 18.041 | 18.041 | 23.746 |
| | arc_cost_u | 28.489 / 36.007 | 17.489 | 18.041 | 18.041 | 18.041 | 22.653 |
| (5,288), incumbente 17.268 | actual | 15.068 / 15.134 | 8.886 | 10.910 | 10.910 | 10.910 | 16.716 |
| | power_link | 30.909 / 39.434 | 8.886 | 10.910 | 10.910 | 10.910 | 17.597 |
| | arc_cost | 30.909 / 39.434 | 8.886 | 10.910 | 10.910 | 10.910 | 18.152 |
| | power_link_u | 31.449 / 39.974 | 8.886 | 10.910 | 10.910 | 10.910 | 16.669 |
| | **arc_cost_u** | 31.449 / 39.974 | 8.886 | 10.910 | 10.910 | 10.910 | **16.070** |
| (7,105), incumbente 22.108 | **actual** | 15.098 / 15.135 | 11.596 | 16.368 | 16.368 | **16.435** | **20.493** |
| | power_link | 30.939 / 39.435 | 11.596 | 16.368 | 16.368 | 16.368 | 20.851 |
| | arc_cost | 28.027 / 35.572 | 11.596 | 16.368 | 16.368 | 16.368 | 20.664 |
| | power_link_u | 31.479 / 39.975 | 11.596 | 16.368 | 16.368 | 16.368 | 20.835 |
| | arc_cost_u | 28.567 / 36.112 | 11.596 | 16.368 | 16.368 | 16.368 | 23.059 |

"Raíz" es la cota tras el presolve y los cortes de la raíz. El día 288 está fuera de la temporada de punta, por eso arc_cost tiene ahí los mismos arcos que power_link.

**Suma de los 3 días:**

| Variante | Mejor solución | Respecto al incumbente |
|---|---:|---:|
| Incumbente | 63.847 | — |
| **actual** | **58.158** | **−8,9 %** |
| power_link_u | 61.250 | −4,1 % |
| arc_cost | 61.322 | −4,0 % |
| arc_cost_u | 61.782 | −3,2 % |
| power_link | 62.544 | −2,0 % |

**Fraccionalidad del LP:**
- **Z_swap fraccionarias:** entre 106 y 190 de 720 en todas las variantes.
- **Q_t fraccionario:** entre 85 y 144 de 180 intervalos. El flujo no deja Q entero, porque mezcla caminos.

## Conclusión

1. **El flujo no mejora la relajación.** El LP y la raíz son idénticos al centavo en las 5 formulaciones y en los 3 días, y en ninguna variante de flujo se mueve la cota en 900 s. El modelo actual es el único cuya cota sube, y poco: +2,4 % en (4,196) y +0,4 % en (7,105).
2. **El hueco LP–incumbente viene de que los swaps son fraccionarios, es decir, del lado LHD.** Esto corrige la versión anterior de esta conclusión. Con los swaps enteros, la formulación actual de la estación es casi exacta (tabla del chequeo). La estación no necesita una reformulación; lo que hay que convexificar es cada LHD (su calendario de swaps junto con su SOC y su producción). Eso apunta a una descomposición por vehículo como la de Fernando Soto, con pricing exacto.
3. **Como heurística, el modelo actual es mejor en conjunto:** gana en 2 de 3 días y en la suma. Además los flujos son 2–2,5 veces más grandes y exploran entre 4 y 7 veces menos nodos. `arc_cost_u` ganó solo en (5,288).
4. **No conviene integrar el flujo al constructor ni al Benders (Fase 3).** Lo que sí conviene conservar es el pipeline cíclico (ver `validacion_dp_estacion.md`).
5. **Pruebas siguientes sobre el lado LHD (2026-10-23, día (4,196), mismas condiciones):**
   - **Prioridad de ramificación en los swaps** (`experimento_estacion_flujo.py`, variantes `actual_pz`, con `Z_swap` primero, y `actual_pn`, con la cantidad de swaps por LHD primero): la cota no se movió de la raíz (18.041) y la mejor solución fue 23.093, contra 18.433 / 20.949 con la ramificación por defecto. En (5,288) también quedó igual. **Descartada.** Salida en `experimento_estacion_flujo/caso3_v4_prioridad/`, cortada antes de (7,105).
   - **Lagrangiana por LHD** (`lagrangiana_lhd.py`): 4 bloques LHD más el bloque estación y potencia, con las 361 filas de acople dualizadas (enlace de swaps, límite de naves y meta diaria). Los duales del LP en esas filas son todos **cero**: el LP del día (17.489) es exactamente "n_min cargas en las horas más baratas" de la estación sola.
     - Con subgradiente (24 iteraciones), ningún precio superó 17.489.
     - Con generación de columnas (maestro DW con Wentges), primero con enlace por igualdad, el maestro quedó clavado en el incumbente (24.471) durante 360 rondas.
     - Con enlace por desigualdad (`W ≥ Σ Z_swap`, como en Fernando Soto), 146 rondas en 32 min: el maestro bajó apenas a 24.452 y la cota quedó en **17.489**.
     - **Descartada como estrategia:** no converge ni en el caso más chico (un día, inversión fija, 5 bloques). El valor teórico de la cota por LHD queda sin determinar, entre 17.489 y 24.452. Hipótesis: con `--free_charging` cada LHD puede hacer swap casi en cualquier intervalo, y mezclando calendarios se imita casi cualquier horario, así que la cota quedaría cerca del LP. No se demostró.
   - **Dónde se hacen los swaps en la v3** (soluciones finales de 7 casos): entre 39 % y 50 % en horario operativo; el resto se reparte entre colación (10–17 %), limpieza de calles (18–22 %), cambio de turno (4–9 %) y mantención liberada (12–17 %). Con el régimen restringido (sin `--free_charging`), los swaps quedarían en 54 de los 180 intervalos y la relajación debería ser más ajustada. No se ha medido; es una decisión de modelo, para la v5.
6. **Dato aparte:** con la inversión del incumbente, el MILP del día (modelo actual, cíclico) encuentra operaciones entre 3 % y 14 % más baratas que las del incumbente k=2. Parte del gap del caso 3 es de solución, no solo de cota. Una parte de esa mejora puede venir del propio pipeline cíclico, que relaja el borde del día.
