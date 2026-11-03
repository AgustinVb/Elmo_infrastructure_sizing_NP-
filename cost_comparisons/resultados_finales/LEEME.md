# Resultados finales v4: battery swapping vs on-board

Costo total del horizonte (USD, VP 10 años). Swap: solución final del L-shaped,
**después** de los dos ajustes (energía real de cada swap y generación, BESS y red
redimensionadas); on-board: su solución final por el mismo LP.

| Caso | Escenario | BS modelo (UB) | BS ajustado | On-board | BS vs OB | Gap BS |
|---:|---|---:|---:|---:|---:|---:|
| 1 | Base, norte, 0,5C, solo red | 3,294,144 | 3,101,299 | 3,083,536 | +0.58% | 0.93% |
| 2 | Base, norte, 0,5C, red + generación + BESS | 2,483,322 | 2,419,370 | 2,383,673 | +1.50% | 1.70% |
| 3 | Base, norte, 0,5C, red + generación | 2,538,591 | 2,486,503 | 2,487,888 | -0.06% | 1.96% |
| 4 | Escalada, norte, solo red (swap 0,5C, OB 1C) | 4,006,626 | 3,801,192 | 5,399,073 | -29.60% | 3.04% |
| 5 | Escalada, norte, red + generación + BESS (swap 0,5C, OB 1C) | 3,098,666 | 3,019,295 | 4,567,768 | -33.90% | 3.72% |
| 6 | Base, centro, 0,5C, red + generación + BESS | 2,745,493 | 2,648,802 | 2,608,964 | +1.53% | 1.74% |
| 7 | Base, norte, 1C, red + generación + BESS | 2,737,235 | 2,711,776 | 2,935,693 | -7.63% | 3.25% |
| 8 | Base, norte, 2C, red + generación + BESS | 4,932,978 | 4,908,561 | 5,657,803 | -13.24% | 3.92% |

## Contenido

- `resumen_swap_vs_ob_v4.xlsx`: hojas Resumen, Costos (cinco variantes por caso),
  Infraestructura (evolución y capacidades), Infraestructura anual, Energía (MWh del
  horizonte), Degradación, Consumer (todas las tablas de consumer.py de los dos lados) y
  Notas (definiciones y controles).
- `costo_total_8_casos.png`: BS ajustado vs OB, desagregado.
- `caso_<n>_<escenario>/`: `comparacion/`, `battery_swapping_modelo/` (json_plotter, antes
  del ajuste), `battery_swapping_ajustado/` y `on_board/` (json_plotter de Elmo_ob); ver
  el LEEME.md de cada caso.

## Regenerar

Cada solución swap necesita `parameters.json` y `consumer.log`, y cada solución OB
(en ../Elmo_ob) `parameters.json`:

    python -u escribir_parameters.py data/Tesis_final/Mina_modelo_v4/<caso> <solucion>
    python consumer.py <solucion> > <solucion>/../consumer.log
    (Elmo_ob) python -u escribir_parameters.py --data_folder \
        data/Resultados_finales_tesis/Mina_modelo_v4/<caso OB> --free_charging --free_maintenance <solucion OB>
    python cost_comparisons/resultados_finales.py --jobs 16
