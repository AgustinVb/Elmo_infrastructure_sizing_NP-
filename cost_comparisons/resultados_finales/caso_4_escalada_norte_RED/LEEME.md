# Caso 4: Escalada, norte, solo red (swap 0,5C, OB 1C)

| | Costo total [USD, VP 10 años] |
|---|---:|
| Battery swapping, modelo (UB del L-shaped) | 4,006,626.43 |
| Battery swapping, ajuste de energía | 3,801,191.50 |
| **Battery swapping, ajustado (comparable)** | **3,801,191.50** |
| **On-board (comparable)** | **5,399,072.57** |
| BS ajustado vs OB | -29.60% |

LB del swap: 3,884,865.90 (lshaped_A2), gap 3.04%.

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

- Swap: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_escalada20/zona_norte/05C/RED/lshaped_A2/mejora/solucion`
- On-board: `origin/carga_ob_multiaño:output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_escalada20/zona_norte/1C/P_red/mejorar_ub_2/solucion`

Generado por `cost_comparisons/resultados_finales.py`; el detalle está en
`../resumen_swap_vs_ob_v4.xlsx`.
