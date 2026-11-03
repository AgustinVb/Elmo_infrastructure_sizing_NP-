# Caso 2: Base, norte, 0,5C, red + generación + BESS

| | Costo total [USD, VP 10 años] |
|---|---:|
| Battery swapping, modelo (UB del L-shaped) | 2,483,322.01 |
| Battery swapping, ajuste de energía | 2,464,147.67 |
| **Battery swapping, ajustado (comparable)** | **2,419,370.28** |
| **On-board (comparable)** | **2,383,672.61** |
| BS ajustado vs OB | +1.50% |

LB del swap: 2,441,084.20 (lshaped_A2), gap 1.70%.

## Carpetas

- `comparacion/`: costos (modelo, ajustes y OB), infraestructura por año y degradación.
- `battery_swapping_modelo/`: `json_plotter.py` de esta rama sobre la solución del
  L-shaped. **Son gráficos del modelo, antes del ajuste**: cada recarga aparece como el
  bloque completo bmin → 100 % y la generación/BESS son las que eligió el modelo.
- `battery_swapping_ajustado/`: despacho de cada año y día muestreado con BS modelo,
  BS ajustado (energía real de cada swap y generación, BESS y red redimensionadas)
  y on-board; `energia_anual.png` compara consumo y compra a la red por año.
- `on_board/`: `json_plotter.py` de Elmo_ob sobre la solución on-board.

## Fuentes

- Swap: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/05C/RED_GEN_BESS/lshaped_A2/mejora/solucion`
- On-board: `origin/carga_ob_multiaño:output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/05C/P_red_gen_bat/mejorar_ub_2/solucion`

Generado por `cost_comparisons/resultados_finales.py`; el detalle está en
`../resumen_swap_vs_ob_v4.xlsx`.
