# Resultados finales v4: battery swapping vs on-board

Costo total del horizonte (USD, VP 10 años). Swap: solución final del L-shaped,
**después** de los dos ajustes (energía real de cada swap y generación, BESS y red
redimensionadas); on-board: su solución final por el mismo LP.

| Caso | Escenario | BS ajustado | On-board | BS vs OB | Gap BS |
|---:|---|---:|---:|---:|---:|
| 1 | Base, norte, 0,5C, solo red | 3,101,299 | 3,083,536 | +0.58% | 0.93% |
| 2 | Base, norte, 0,5C, red + generación + BESS | 2,419,370 | 2,383,673 | +1.50% | 1.70% |
| 3 | Base, norte, 0,5C, red + generación | 2,486,503 | 2,487,888 | -0.06% | 1.96% |
| 4 | Escalada, norte, solo red (swap 0,5C, OB 1C) | 3,801,192 | 5,399,073 | -29.60% | 3.04% |
| 5 | Escalada, norte, red + generación + BESS (swap 0,5C, OB 1C) | 3,019,295 | 4,567,768 | -33.90% | 3.72% |
| 6 | Base, centro, 0,5C, red + generación + BESS | 2,648,802 | 2,608,964 | +1.53% | 1.74% |
| 7 | Base, norte, 1C, red + generación + BESS | 2,711,776 | 2,935,693 | -7.63% | 3.25% |
| 8 | Base, norte, 2C, red + generación + BESS | 4,908,561 | 5,657,803 | -13.24% | 3.92% |

## Contenido

- `resumen_swap_vs_ob_v4.xlsx`: hojas Resumen, Costos (cinco variantes por caso),
  Infraestructura (evolución y capacidades), Infraestructura anual, Energía (MWh del
  horizonte), Degradación, Consumer (todas las tablas de consumer.py de los dos lados) y
  Notas (definiciones y controles).
- `costo_total_8_casos.png`: BS ajustado vs OB, desagregado.
- `costo_total_base_norte.png`: lo mismo para base norte 0,5C (red, red + generación y
  red + generación + BESS).
- `despacho_<escenario>_<estacion>.png`: despacho de on-board y battery swapping
  (ajustado), base norte 0,5C, año 5 (meta de producción máxima): solo red en invierno,
  red + generación + BESS en invierno y en verano, y red + generación en otoño. Días
  muestreados: verano 15 de enero (sin cargo por potencia), otoño 15 de abril e invierno
  15 de julio (con cargo de 18:00 a 22:00).
- `estado_flota_<escenario>_<estacion>.png`, `baterias_en_carga_<escenario>_<estacion>.png` y
  `circulacion_baterias_<escenario>_<estacion>.png`: operación de base norte 0,5C en el año 5,
  invierno (con cargo por potencia) y verano (más horas de sol). Estado de cada LHD (asignado a extracción, carga o swap, inactivo) en on-board y
  battery swapping; baterías cargando a la vez con los cargadores instalados y las
  horas con sol; y recorrido de cada batería de la estación de swap (identidad FIFO
  reconstruida desde S, X_dch, X_ini y Sv, contrastada intervalo a intervalo).
- `cargadores_ob_<escenario>_<estacion>.png`: el equivalente on-board de la circulación de
  baterías, mismos casos, año y días: una fila por cargador instalado con el LHD conectado y su
  nivel de potencia (máxima, parcial o conectado sin potencia), sobre las detenciones
  operacionales. El modelo solo cuenta los LHD conectados (sum Z_charge <= N_chargers): cada
  sesión de carga va al primer cargador libre por orden de inicio, que nunca usa más cargadores
  que los instalados. Título con la ocupación (conectado, % del día) y la energía cargada
  respecto de la capacidad nominal de los cargadores.
- `estado_flota_produccion_<escenario>_<estacion>.png`: estado de cada LHD con producción
  base (izquierda) y escalada (derecha), on-board arriba y battery swapping abajo, solo red y
  red + generación + BESS, año 5, invierno y verano. Con producción escalada on-board usa
  cargadores de 1C y battery swapping sigue en 0,5C.
- `costo_total_tasa_carga.png`, `degradacion_tasa_carga.png`,
  `estado_flota_tasa_carga_<estacion>.png`, `tabla_tasa_carga.tex` y `tabla_costos_tasa_carga.tex`:
  sensibilidad a la tasa de carga (0,5C, 1C y 2C) en producción base, norte, red + generación
  + BESS (casos 2, 7 y 8), battery swapping y on-board. También
  `potencia_peak_tasa_carga.png`, `circulacion_baterias_tasa_carga_<estacion>.png` y
  `cargadores_ob_tasa_carga_<estacion>.png` (un panel por tasa; a 2C el cargador on-board de
  964 kW queda limitado a 500 kW por la subestación). El peak de battery swapping es el del modelo (cargadores × potencia por cargador): el del
  despacho ajustado a 1C y 2C pasa la potencia del único cargador, porque el ajuste alarga las cargas
  con déficit (baterías casi nuevas) más allá del bloque y se montan con la siguiente. A 0,5C son
  iguales.
- `costo_total_ubicacion.png`, `despacho_ubicacion_<estacion>.png`, `tabla_ubicacion.tex` y
  `tabla_costos_ubicacion.tex`: sensibilidad a la ubicación en producción base 0,5C: solo red
  (caso 1; norte y centro solo difieren en el perfil de generación, así que sin generación es el
  mismo caso) y red + generación + BESS en norte (caso 2) y centro (caso 6).
- `costo_total_produccion.png`: costo total con producción base y escalada lado a lado,
  agrupado por escenario (solo red y red + generación + BESS) y tecnología.
- `degradacion_produccion.png`: capacidad por batería año a año con producción base y
  escalada (solo red; los demás escenarios difieren en 2 kWh o menos), battery swapping y
  on-board.
- `caso_<n>_<escenario>/`: `comparacion/` (BS ajustado vs OB), `battery_swapping_modelo/`
  (json_plotter, antes del ajuste), `battery_swapping_ajustado/` (despacho y energía de BS
  ajustado) y `on_board/` (json_plotter de Elmo_ob); ver el LEEME.md de cada caso.

## Regenerar

Cada solución swap necesita `parameters.json` y `consumer.log`, y cada solución OB
(en ../Elmo_ob) `parameters.json`:

    python -u escribir_parameters.py data/Tesis_final/Mina_modelo_v4/<caso> <solucion>
    python consumer.py <solucion> > <solucion>/../consumer.log
    (Elmo_ob) python -u escribir_parameters.py --data_folder \
        data/Resultados_finales_tesis/Mina_modelo_v4/<caso OB> --free_charging --free_maintenance <solucion OB>
    python cost_comparisons/resultados_finales.py --jobs 16
