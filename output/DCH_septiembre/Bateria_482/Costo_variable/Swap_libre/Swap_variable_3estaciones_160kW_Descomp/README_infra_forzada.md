# Re-corrida con infraestructura forzada a la del caso de costo fijo

**Fecha:** 2026-09-09. Reemplaza la Fase 2 de la corrida original (commit `0e30beac7`).

## Por que

La corrida original salia con **4 cargadores / 3 bahias / 4 baterias** (inversion
630.981 USD) y por eso resultaba mas cara que su equivalente de costo fijo, que
resuelve con 3/3/3 (524.019 USD). No era un resultado economico: era un
artefacto de la Fase 1.

La infraestructura se fija como el **maximo entre los 12 dias**. Once dias de
`station_2` convergieron a gap 0,8-2,7 % con **1 cargador + 1 bateria**. El dia
213 agoto el timelimit de 1200 s con **36,1 % de gap** y devolvio **2 + 2**, y
por la regla del maximo esa solucion sin converger le impuso la infraestructura
a todo el anio.

Que era falla del solver y no economia lo confirma la cota inferior, identica en
los tres casos (180.544,30):

| subproblema `station_2_d213`      | objetivo   | cota       | gap    |
|-----------------------------------|-----------:|-----------:|-------:|
| Fase 1 original (infra libre)     | 282.547,64 | 180.544,30 | 36,1 % |
| Fase 2 original (infra 2+2)       | 282.547,64 | 281.635,00 |  0,32 %|
| Fase 2 de esta corrida (infra 1+1)| 182.593,49 | 180.544,30 |  1,12 %|

Con 1+1 ese dia resuelve limpio y sale ~99.954 USD mas barato en objetivo.

## Que se hizo

Solo **Fase 2**, con la infraestructura forzada a **1 cargador / 1 bahia /
1 bateria por estacion** (identica al caso de costo fijo), reusando los
warm starts de la Fase 1 original:

    python run_descomposicion.py \
      --data_folder data/Escenarios_DCH_septiembre/Bateria_482/Costo_variable/Swap_variable_3estaciones_160kW/ \
      --output_folder <esta carpeta> \
      --consumption_model wp2 --pause_scheme dch --swap_window libre \
      --parallel_days --days 1,32,60,91,121,152,182,213,244,274,305,335 \
      --solver gurobi --gap 0.05 --timelimit 1200 \
      --reuse_fase1_from <copia de los *_stage1 con station_2_d213 bajado a 1+1>

## Resultado

| concepto           | original (4/3/4) | esta corrida (3/3/3) |        delta |
|--------------------|-----------------:|---------------------:|-------------:|
| Inversion          |       630.981    |          **524.019** |  -106.962    |
| Carga real / eta   |       246.951    |          **273.687** |   +26.736    |
| Potencia pico      |        19.200    |           19.200     |        0     |
| **TOTAL**          |   **897.132**    |      **816.906**     |  **-80.226** |

Extraccion anualizada 14.809.700 t (original 14.810.160), o sea comparable.

Con esto variable/160 pasa a ser mas barato que fijo/160 (825.631), como manda
la logica economica, pero sigue por encima de variable/320 (795.022).

El costo de energia SUBE 26.736: con 1 cargador en vez de 2 en `station_2` hay
menos potencia para concentrar la carga en horas baratas, asi que se pierde
parte del arbitraje. Ese es el trade-off real.

## Advertencias

1. **34 de los 36 subproblemas son de esta corrida.** `station_1_d121` y
   `station_1_d213` conservan el resultado de la corrida original: son el mismo
   subproblema (la infraestructura de `station_1` no cambio, era 1/1/1 en ambas)
   y su solucion original es factible y mejor que la que llevaba esta corrida al
   momento de cortarla (`station_1_d121`: 181.181 con gap 1,00 % vs 198.645 con
   gap 9,70 %).
2. Las carpetas `*_stage1` son las de la **Fase 1 original**, sin tocar: dejan
   el registro de que el dia 213 salio 2+2. La infraestructura de esta corrida
   NO se deriva de ellas.
3. El cargo por potencia de 19.200 USD no viene de `station_2` sino de
   `station_1_d213`, que carga 160 kW en t=68 (17:56-18:04, primer intervalo de
   la ventana de punta). Ese subproblema es identico en ambas corridas.
4. Bajo tarifa variable, la metrica "costo carga real / eta_charge" hereda el
   precio promedio que logro el schedule del solver, resuelto con gap 5 % sobre
   un objetivo dominado por la inversion.

Para volver atras: `git checkout 0e30beac7 -- <esta carpeta>`
