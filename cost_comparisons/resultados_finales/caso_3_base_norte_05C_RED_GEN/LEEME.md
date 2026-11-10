# Caso 3: Base, norte, 0,5C, red + generación

| | Costo total [USD, VP 10 años] |
|---|---:|
| **BS ajustado** | **2,486,502.81** |
| **On-board** | **2,487,887.67** |
| BS ajustado vs OB | -0.06% |

Gap de optimalidad del L-shaped de swap: 1.96% (LB de lshaped_A2).

## Carpetas

- `comparacion/`: costos, infraestructura por año y degradación, BS ajustado contra OB.
- `battery_swapping_modelo/`: `json_plotter.py` de esta rama sobre la solución del
  L-shaped. **Son gráficos del modelo, antes del ajuste**: cada recarga aparece como el
  bloque completo bmin → 100 % y la generación/BESS son las que eligió el modelo.
- `battery_swapping_ajustado/`: despacho de BS ajustado (energía real de cada swap y
  generación, BESS y red redimensionadas) en cada año y día muestreado;
  `energia_anual.png` con el consumo, la compra a la red y la generación usada por año.
- `on_board/`: `json_plotter.py` de Elmo_ob sobre la solución on-board.

## Fuentes

- Swap: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/05C/RED_GEN/lshaped_A2/mejora/solucion`
- On-board: `origin/carga_ob_multiaño:output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/05C/P_red_gen/mejorar_ub_2/solucion`

Generado por `cost_comparisons/resultados_finales.py`; el detalle está en
`../resumen_swap_vs_ob_v4.xlsx`.
