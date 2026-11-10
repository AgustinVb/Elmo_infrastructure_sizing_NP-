# Caso 8: Base, norte, 2C, red + generación + BESS

| | Costo total [USD, VP 10 años] |
|---|---:|
| **BS ajustado** | **4,908,560.89** |
| **On-board** | **5,657,803.24** |
| BS ajustado vs OB | -13.24% |

Gap de optimalidad del L-shaped de swap: 3.92% (LB de lshaped_A4).

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

- Swap: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/2C/RED_GEN_BESS/lshaped_A4/solucion`
- On-board: `origin/carga_ob_multiaño:output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/2C/P_red_gen_bat/mejorar_ub_2/solucion`

Generado por `cost_comparisons/resultados_finales.py`; el detalle está en
`../resumen_swap_vs_ob_v4.xlsx`.
