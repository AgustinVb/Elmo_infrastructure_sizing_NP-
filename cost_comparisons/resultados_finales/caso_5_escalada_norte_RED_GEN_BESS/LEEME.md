# Caso 5: Escalada, norte, red + generación + BESS (swap 0,5C, OB 1C)

| | Costo total [USD, VP 10 años] |
|---|---:|
| Battery swapping, modelo (UB del L-shaped) | 3,098,666.35 |
| Battery swapping, ajuste de energía | 3,081,786.39 |
| **Battery swapping, ajustado (comparable)** | **3,019,294.85** |
| **On-board (comparable)** | **4,567,767.54** |
| BS ajustado vs OB | -33.90% |

LB del swap: 2,983,268.87 (lshaped_A2), gap 3.72%.

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

- Swap: `output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_escalada20/zona_norte/05C/RED_GEN_BESS/lshaped_A2/mejora/solucion`
- On-board: `origin/carga_ob_multiaño:output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4/Produccion_escalada20/zona_norte/1C/P_red_gen_bat/benders`

Generado por `cost_comparisons/resultados_finales.py`; el detalle está en
`../resumen_swap_vs_ob_v4.xlsx`.
