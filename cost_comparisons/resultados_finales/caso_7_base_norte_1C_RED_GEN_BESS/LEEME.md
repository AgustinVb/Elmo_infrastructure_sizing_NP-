# Caso 7: Base, norte, 1C, red + generación + BESS

| | Costo total [USD, VP 10 años] |
|---|---:|
| Battery swapping, modelo (UB del L-shaped) | 2,737,234.87 |
| Battery swapping, ajuste de energía | 2,733,819.03 |
| **Battery swapping, ajustado (comparable)** | **2,711,776.05** |
| **On-board (comparable)** | **2,935,693.03** |
| BS ajustado vs OB | -7.63% |

LB del swap: 2,648,318.12 (lshaped_A4), gap 3.25%.

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

- Swap: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/1C/RED_GEN_BESS/lshaped_A3/solucion`
- On-board: `origin/carga_ob_multiaño:output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_base/zona_norte/1C/P_red_gen_bat/mejorar_ub_1/solucion`

Generado por `cost_comparisons/resultados_finales.py`; el detalle está en
`../resumen_swap_vs_ob_v4.xlsx`.
