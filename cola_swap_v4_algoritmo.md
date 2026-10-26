# Cola swap v4: casos pendientes y algoritmo a aplicar

Rama `battery_swapping_multiaño`, 2026-10-26. Escrito para quien lance la cola en el otro
equipo (usuaria o sesión de Claude Code).

**Objetivo:** dejar todos los casos de swap v4 bajo **5 % de gap**, idealmente cerca de **1 %**.

## 1. Estado al 2026-10-26

| # | Caso (`data/Tesis_final/Mina_modelo_v4/...`) | UB | LB | Gap | Estado |
|---|---|---:|---:|---:|---|
| 1 | Produccion_base/zona_norte/05C/RED | 3.338.990 | 3.206.386 (op.) | 3,97 % | reparado sin reemplazo; certificado no terminó (644 candidatas) |
| 2 | Produccion_base/zona_norte/05C/RED_GEN_BESS | 2.664.498 | 2.387.721 (op.) | 10,39 % | `--fijar_R 0` + `mejorar_ub`; certificado inviable (pool de 5.000 lleno) |
| 3 | Produccion_base/zona_norte/05C/RED_GEN | 3.289.543 | 2.424.994 (op.) | 26,3 % | Benders detenido en k=2, paga reemplazo → **en la cola** |
| 4 | Produccion_escalada20/zona_norte/05C/RED | — | 3.831.682 (op.) | — | el año 4 no tuvo solución (120 s y 1.200 s) → **al final de la cola** |
| 5 | Produccion_escalada20/zona_norte/05C/RED_GEN_BESS | — | — | — | **en la cola** |
| 6 | Produccion_base/zona_centro/05C/RED_GEN_BESS | 2.786.794 | 2.695.861 (cert.) | 3,26 % | cerrado |
| 7 | Produccion_base/zona_norte/1C/RED_GEN_BESS | — | — | — | **en la cola** |
| 8 | Produccion_base/zona_norte/2C/RED_GEN_BESS | — | — | — | **en la cola** |

Las alternativas 1C de "escalada 20 %" (casos 4 y 5) **no tienen datos v4**. Si 0,5C no
tiene solución, hay que armarlos aplicando a los datos de swap solo los `GenProfiles` y los
topes de la v4, sin copiar los de OB.

## 2. Lo que aprendimos (por qué este algoritmo)

1. **El Benders de la v4 cae en soluciones que reemplazan toda la flota de baterías.** La
   relajación LP de `R` en los cortes abarata el reemplazo, y el año que debería comprar
   baterías no lo ve. Las soluciones **sin reemplazo** (más baterías y antes) resultaron
   mucho más baratas: caso 1 −14 %, caso 2 −21 %, caso 6 −16 %.
2. **`--fijar_R op`** fija `R` en el valor de la cota operacional (en los casos v4,
   **ningún reemplazo**) en todos los bloques anuales, en forward **y backward**. Así los
   cortes aprenden que llegar al final con poca capacidad obliga a comprar baterías.
   Resultados de esta corrida en el caso 2, con caja de ±1:
   - Iteraciones 1–5: pasos nulos.
   - Iteración 6: paso serio, encontró sola una solución mejor que la reparada a mano.
   - `mejorar_ub` le bajó un 0,7 % más.
3. **La LB es la cota operacional,** que no se mueve con las iteraciones. Con `--fijar_R`,
   la cota del backward es la del problema sin reemplazo y **no entra a la LB** (se
   imprime solo como referencia). La única forma de subir la LB es el **certificado**, y
   solo completa cuando el gap ya es ≲5–6 %. Con más gap, el pool de candidatas se llena
   y no certifica (caso 2: 5.000 candidatas).
4. **La operación del forward es de baja calidad:** cada año se resuelve en 120 s, con
   gaps de 15–40 %. `mejorar_ub`, con 600–900 s por día, recupera entre 0,7 % y 1,7 %.
5. **La caja de estabilización de ±1 por variable y año resultó restrictiva:** quedó
   activa en las 6 iteraciones del caso 2, y los años 9–10 tuvieron que resolverse sin
   ella 7 veces. Por eso esta cola prueba primero **sin estabilización** (ver §4).

## 3. Algoritmo por caso

| Paso | Qué | Cómo | Automático en la cola |
|---|---|---|---|
| 0 | Corte de cargas por LHD | `corte_cargas_op.py` | sí |
| 1 | Cota operacional (LB) + Nested Benders sin reemplazo | `setup.py ... --operational_bound --corte_cargas_lhd <json> --fijar_R op` + estabilización según §4; se corta tras `SIN_MEJORA` = 4 iteraciones sin mejora o al tope de `HORAS` | sí (`BENDERS_EXTRA="--fijar_R op"`) |
| 2 | Reparación de la inversión | `reparar_reemplazo.py --respaldo <benders>/incumbente_respaldo.pkl --out <caso>/reparar_reemplazo` | **no, manual** (ver abajo) |
| 3 | Mejorar la operación | `mejorar_ub.py` pasada 1 (años 1–10, 600 s) y pasada 2 (años 3–9, 900 s) | sí |
| 4 | Certificado | `certificar_inversiones.py`. Si el pool queda incompleto, **ahora termina de inmediato** sin evaluar candidatas (antes perdía horas); `MAX_POOL` sube el tope | sí |

**Cuándo hacer el paso 2 (a mano, entre el Benders y `mejorar_ub`):**
- si la mejor solución del Benders compra 2 o más baterías de golpe en un año tardío (8–10);
- si su capacidad en el año 10 queda a menos de ~2 kWh del piso (385,6);
- o si paga reemplazo, cosa que con `--fijar_R op` no debería pasar.

`reparar_reemplazo.py` prueba candidatas "+n baterías desde el año y0" con `R = 0`.
Si mejora el UB, `mejorar_ub` y el certificado se corren sobre su solución (la cola
toma la que deja `mejorar_ub`, así que conviene lanzarlos a mano para ese caso).

**Paso 5 (no implementado todavía), si el gap sigue sobre 5 %:** búsqueda local de la
inversión: mover las compras de baterías, cargadores y bahías un año antes o después,
filtrando con la degradación predicha.

## 4. Estabilización: primero sin caja; si oscila, caja grande con iteraciones libres

**Fase A, por defecto en esta cola: SIN estabilización** (`ESTAB=""`).

**Criterio de "demasiadas oscilaciones":** mirar `benders.log` después de la iteración 4.
Hay oscilación si se cumplen las dos condiciones:
- no hubo ningún paso que mejorara el UB desde la iteración 1 (líneas
  `k=… UB=…` y `pulido de la solucion: A -> B`);
- y el costo pulido del forward salta **más de 10 %** entre iteraciones consecutivas al
  menos 2 veces, o queda **más de 25 % sobre el mejor UB** en 3 de las 4 iteraciones.

Nota: la cola ya corta sola el Benders tras 4 iteraciones sin mejora (`SIN_MEJORA=4`) y
sigue con `mejorar_ub` y el certificado. Un caso así no se pierde, solo queda con el UB que
tenía.

**Fase B, si hay oscilación:** caja de **±2** y **una iteración libre cada 3**:

    ESTAB="--stabilization box --stab_center operational --box_delta_int 2 --stab_free_every 3"

Se aplica a los casos siguientes de la cola. Para **relanzar el Benders de un caso que
osciló** sin perder lo hecho, se parte de su respaldo, que incluye la mejor solución y
todos los cortes:

    python -u setup.py --data_folder data/Tesis_final/Mina_modelo_v4/<caso> --solver gurobi \
      --days_per_year 4 --free_charging --free_maintenance --block_build_jobs 1 \
      --output_folder <V>/<caso>/r2/benders --mode decomposed --n_years 10 --gap_tol 0.01 \
      --max_iter 50 --max_hours 10 --lb_inicial <cota operacional del benders.log> \
      --no_monolithic_lp_bound --stabilization box --stab_center operational \
      --box_delta_int 2 --stab_free_every 3 --cut_type strengthened --strengthened_timelimit 300 \
      --block_mip_focus 1 --solve_timelimit 120 --day_warm_start always --day_timelimit 120 \
      --day_gap 0.05 --day_jobs 4 --polish_each_iter --skip_last_backward --fijar_R 0 \
      --incumbente_inicial <V>/<caso>/benders/incumbente_respaldo.pkl \
      > <V>/<caso>/r2/benders.log 2>&1

(`--fijar_R 0` y `--lb_inicial` evitan recalcular la cota operacional, unos 45 min. En los
casos v4, `op` y `0` coinciden: la cota operacional no reemplaza. Verificarlo en el log:
`--fijar_R op: R fijo en 10 bloques (reemplazo en ningun año)`.)

Si la fase B tampoco mejora, la opción siguiente (no implementada) es un **vecindario L1**
(*local branching*, Rei et al. 2009): acotar el cambio total de la inversión en vez de ±k
por variable.

## 5. Cómo lanzar la cola en el otro equipo

    git pull                                   # trae este documento, la cola y los flags nuevos
    V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4
    rm -f $V/.inicio_r1                        # si no, el tope global de 68 h corre desde la cola anterior
    ESTAB="" BENDERS_EXTRA="--fijar_R op" nohup bash cola_tesis_swap_v4.sh 3 7 8 5 4 > $V/cola_r1.out 2>&1 &

- Los casos corren **en el orden dado** (3, 7, 8, 5, 4). Benders con hasta `HORAS` = 10 h
  por caso (el 5, escalada con BESS, hasta `HORAS_ESCALADA_BESS` = 24 h); tope global
  `TOPE_H` = 68 h. Para que alcance para los 5 casos: `TOPE_H=90`.
- `PY` toma por defecto `~/miniconda3/envs/py310/bin/python`. En Windows:
  `PY=.venv_elmo/Scripts/python.exe`.
- **No incluir los casos 1, 2 y 6:** ya tienen resultados y la cola los reprocesaría.
- Seguimiento: `$V/cola.log` (una línea por etapa) y `<caso>/benders.log`.
- **Caídas por `access violation`** (CPU inestable, pendiente actualizar la BIOS): el Benders
  deja `benders/incumbente_respaldo.pkl` en cada paso serio. Se relanza ese caso con el
  comando de §4 (con la estabilización que corresponda) desde ese respaldo.

## 6. Al terminar cada caso

- Registrar en `$V/cola.log` lo hecho a mano (reparación, relanzamientos) con el prefijo del
  equipo.
- Subir los resultados **sin** `*.pkl`, `*.npy`/`*.npz`, `parameters.json` (se regenera con
  `escribir_parameters.py`), `_mps/`, `_monolitico/` ni `_pulido_por_iteracion/`. De las
  soluciones intermedias, solo logs y `resumen.json`.
- Actualizar la tabla de §1.

## 7. Cambios de código que trae este commit

- `cola_tesis_swap.sh`: `ESTAB` (estabilización; por defecto la caja de ±1 de siempre),
  `BENDERS_EXTRA` (flags extra del Benders), `SIN_MEJORA` y `MAX_POOL` como variables de
  entorno. Sin ellas, la cola hace exactamente lo mismo que antes.
- `certificar_inversiones.py`: con el pool incompleto termina de inmediato
  (`--evaluar_incompleto` mantiene el comportamiento anterior).
- `--fijar_R {op,0}` (`driver.py` y `setup.py`): fija el reemplazo en todos los bloques
  anuales, en forward y backward, y saca la cota del backward de la LB. **Viene en este
  commit:** sin él, `BENDERS_EXTRA="--fijar_R op"` falla. `--evaluar_trayectoria_op`,
  `--reparar_reemplazo` y `--r_endurecido` ya estaban en el commit 31f72835d (detalle en
  `reparacion_reemplazo/README.md`).
