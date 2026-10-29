# Cola swap v4: casos pendientes y algoritmo a aplicar

Rama `battery_swapping_multiaño`, 2026-10-26. Escrito para quien lance la cola en el otro
equipo (usuaria o sesión de Claude Code).

**Objetivo:** dejar todos los casos de swap v4 bajo **5 % de gap**, idealmente cerca de **1 %**.

## 1. Estado al 2026-10-29

Todos los casos con LB **certificada** (`certificar_inversiones.py`, pool completo). La
carpeta es la de la solución final (`<caso>/<carpeta>/mejorar_ub_N/solucion`).

| # | Caso (`data/Tesis_final/Mina_modelo_v4/...`) | UB | LB (cert.) | Gap | Carpeta y cómo se llegó |
|---|---|---:|---:|---:|---|
| 1 | Produccion_base/zona_norte/05C/RED | 3.294.144 | 3.263.609 | **0,93 %** | `dp_real/`: esquema completo de §8 con dos vueltas de DP (k=0 3.338.409 en `dp_nom/` → 3.304.776) → `mejorar_ub` p1; certificado de 264 candidatas, cerró al segundo intento |
| 2 | Produccion_base/zona_norte/05C/RED_GEN_BESS | 2.493.040 | 2.440.748 | **2,10 %** | `dp_it0/`: k=0 reparada con la DP 2.521.834 (33 min; antes 2.664.498, 10,39 %) → `mejorar_ub` en 2 pasadas; 241 candidatas |
| 3 | Produccion_base/zona_norte/05C/RED_GEN | 2.550.278 | 2.482.036 | **2,68 %** | `dp_real_b/`: segunda vuelta de DP con la energía de la k=0 de `dp_it0/` (2.615.019 → 2.585.380) → `mejorar_ub` p1. La LB es la del certificado de `dp_it0/`, que no depende del UB; el de `dp_real_b/` cayó por *access violation* sin cerrar. `dp_real/` se descartó porque comparaba contra un incumbente de otra etapa |
| 4 | Produccion_escalada20/zona_norte/05C/RED | 4.090.093 | 3.884.866 | **5,02 %** | `dp_real/`: segunda vuelta de DP con la energía de `dp_it0/` (k=0 4.503.513 → 4.120.261; la energía nominal compraba una 5ª batería en el año 7) → `mejorar_ub` p1; p2 sin mejora. Faltan ~760 USD de UB para bajar de 5 % |
| 5 | Produccion_escalada20/zona_norte/05C/RED_GEN_BESS | 3.098.875 | 2.983.269 | **3,73 %** | otro equipo, `milp_sinR/it0_energia_sol/` (commit a47d69dcb). En este equipo `dp_it0/` no encontró solución factible (UB=inf) |
| 6 | Produccion_base/zona_centro/05C/RED_GEN_BESS | 2.767.909 | 2.695.860 | **2,60 %** | `dp_real_t/`: dos vueltas de DP (k=0 2.811.552 en `dp_nom/` → 2.790.394) → `mejorar_ub` p1. La cota op. recalculada en `dp_real/` no cerró en 5.400 s; `dp_real_t/` reusa la trayectoria de `dp_nom/` (`--tray_op_archivo`) |
| 7 | Produccion_base/zona_norte/1C/RED_GEN_BESS | 2.743.895 | 2.636.528 | **3,91 %** | `dp_it0/`: k=0 2.768.936 (reemplazo en el año 6) → `mejorar_ub` en 2 pasadas |
| 8 | Produccion_base/zona_norte/2C/RED_GEN_BESS | 4.949.250 | 4.703.215 | **4,97 %** | `dp_it0/`: k=0 4.987.039 (reemplazo en los años 5 y 7) → `mejorar_ub` en 3 pasadas (la 3ª: años 4–8, 1.200 s) |

Los certificados de los casos 3, 4 y 8 cayeron por *access violation* (CPU inestable, ver
memoria del equipo 2), pero quedaron cerrados: la LB mínima de las candidatas evaluadas es
menor que la cota operacional de las que faltaban (están ordenadas por esa cota).

La segunda vuelta de DP no se corrió en los casos 2, 7 y 8. El caso 2 ya usaba la energía
del incumbente. En los casos 7 y 8, la DP con la energía real da el mismo plan que con la
nominal (chequeo con la DP sola, sin correr el modelo).

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
   ella 7 veces. Por eso esta cola corre con una caja **relajada (±2, libre cada 3)** y en
   paralelo se prueba **sin estabilización** en el caso 2 (ver §4).

## 3. Algoritmo por caso

| Paso | Qué | Cómo | Automático en la cola |
|---|---|---|---|
| 0 | Corte de cargas por LHD | `corte_cargas_op.py` | sí |
| 1 | Cota operacional (LB) + Nested Benders sin reemplazo | `setup.py ... --operational_bound --corte_cargas_lhd <json> --fijar_R op` + estabilización según §4; se corta tras `SIN_MEJORA` = 4 iteraciones sin mejora o al tope de `HORAS` | sí (`BENDERS_EXTRA="--fijar_R op"`) |
| 2 | Reparación de la inversión | `reparar_reemplazo.py --respaldo <benders>/incumbente_respaldo.pkl --out <caso>/reparar_reemplazo` | **no, manual** (ver abajo) |
| 3 | Mejorar la operación | `mejorar_ub.py` pasada 1 (años 1–10, 600 s) y pasada 2 (años 3–9, 900 s). Con `PASADA2=umbral`, la pasada 2 solo corre si tras la pasada 1 el gap queda a ≤ 0,3 puntos **sobre** 1 % o 5 % (`UMBRALES_P2`, `MARGEN_P2`): en los casos 2, 3 y 7 bajó el UB solo 0,13–0,31 % y tardó 1,1–1,6 h | sí |
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

## 4. Estabilización

**Cambio de plan de la usuaria (2026-10-26), las dos variantes en paralelo:**
- **Otro equipo, la cola (casos 3, 7, 8, 5 y 4):** caja **relajada de ±2** con **una
  iteración sin caja cada 3** (es la "fase B" de abajo, aplicada desde el inicio):

      ESTAB="--stabilization box --stab_center operational --box_delta_int 2 --stab_free_every 3"

- **Equipo 2: prueba SIN estabilización en el caso 2,** desde su mejor solución (2.664.498)
  con `--fijar_R 0`, hasta 10 h. Se compara con la corrida de ±1 del mismo caso (`fijarR/`:
  6 iteraciones, −0,8 %). Salida en `.../05C/RED_GEN_BESS/sincaja/`.
  **Resultado: descartado.** Sin caja, el forward de la iteración 1 es miope (no ve el
  desgaste futuro). No compró baterías en los años 5 y 6 y cayó en una cascada de cortes de
  factibilidad: 4 reinicios desde el año 1 en ~3 h, sin terminar la iteración 1. Se detuvo a
  mano. **No usar `ESTAB=""`.** Lo que sí funcionó fue reparar la iteración 0 con la DP (§8).

Con los dos resultados se decide qué estabilización usar en las corridas siguientes. Lo que
sigue describe el criterio de oscilación, que sirve para evaluar ambas.

**Fase A: SIN estabilización** (`ESTAB=""`).

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
    ESTAB="--stabilization box --stab_center operational --box_delta_int 2 --stab_free_every 3" \
      BENDERS_EXTRA="--fijar_R op" TOPE_H=90 \
      nohup bash cola_tesis_swap_v4.sh 3 7 8 5 4 > $V/cola_r1.out 2>&1 &

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

## 8. Nuevo (2026-10-26): reparación de la iteración 0 con la DP de degradación

**Qué resuelve.** La inversión de la cota operacional es muy buena, pero su batería llega al
año 10 justo en el piso del 80 % con la operación relajada. Con la operación entera le faltan
~1,5 kWh: el año 10 queda infactible o compra 5 baterías de golpe (caso 2: UB 3,73 M en la
corrida `it0`). La DP de degradación (`src/optimization/decomposition/degradacion_dp.py`)
calcula el ajuste más barato: agregar o adelantar compras de baterías, bahías y cargadores,
o reemplazar la flota. Nunca quita lo que la trayectoria ya decidió. La flota queda
`--dp_margen` kWh sobre el piso.

**Resultado en el caso 2:** la reparación adelantó la 3ª batería y la 2ª bahía del año 5 al
4. La evaluación (iteración 0, 33 min) dio un **UB de 2.521.834 (gap 5,32 %)**, contra
2.664.498 (10,39 %) que había costado una reparación manual más 6 iteraciones de Benders.

**Flags nuevos** (`setup.py`, `--mode decomposed`):
- `--reparar_dp`: con `--evaluar_trayectoria_op`, repara la trayectoria de la cota antes
  de evaluarla. Usa la energía del incumbente (`--incumbente_inicial`) o, sin incumbente,
  la nominal del plan de producción, que queda 0,3–2 % más alta (conservadora).
- `--dp_margen` (default 0,5 kWh).
- `--tray_op_archivo <tray_op.pkl>`: la cota operacional ahora guarda su trayectoria en
  `<benders>/tray_op.pkl`. Con este flag se reusa sin recalcularla; la LB se pasa con
  `--lb_inicial`.

**En la cola** (casos que todavía no corrieron), el esquema completo que se usó en los casos
3, 7, 8, 5 y 4 (2026-10-26/27): sin iteraciones de Benders (`ITER=0`: solo la iteración 0
reparada), resultados en `<caso>/dp_it0/` (`SUB`) y pasada 2 solo si puede cruzar un umbral
(`PASADA2=umbral`, §3). En Windows agregar `PY=.venv_elmo/Scripts/python.exe`.

    ITER=0 SUB=dp_it0 PASADA2=umbral \
      ESTAB="--stabilization box --stab_center operational --box_delta_int 2 --stab_free_every 3" \
      BENDERS_EXTRA="--fijar_R op --evaluar_trayectoria_op --reparar_dp --dp_margen 0.5" TOPE_H=90 \
      nohup bash cola_tesis_swap_v4.sh <casos> > $V/cola_r2.out 2>&1 &

Las iteraciones de Benders no aportaron en swap: el forward de k=1 es miope frente a la
degradación (pospone baterías) y en el caso 2 se detuvo sin mejorar la iteración 0.

**No editar `cola_tesis_swap.sh` mientras corre una cola:** bash lee el script a medida
que avanza y un cambio en el lugar desordena lo que queda (probado). Si hay que cambiarlo,
escribir una copia y reemplazarlo con `mv`: la cola en curso sigue con la versión antigua.

Hasta el 2026-10-27 la cola lanzaba los certificados en Windows con `--gap_objetivo` vacío
(el `\r` de python quedaba pegado a la LB leída del log) y fallaban al instante; ya está
corregido. Los de la cola dp_it0 se corren aparte al terminar.

Si la cola ya está corriendo con el código anterior, dejar terminar el caso en curso,
detenerla y relanzarla con los casos que faltan.

**Para un caso que ya corrió con el código anterior** (no tiene `tray_op.pkl`), relanzar el
Benders recalculando la cota (~45 min), con iteración 0 reparada y desde su mejor solución:

    python -u setup.py --data_folder data/Tesis_final/Mina_modelo_v4/<caso> --solver gurobi \
      --days_per_year 4 --free_charging --free_maintenance --block_build_jobs 1 \
      --output_folder <V>/<caso>/dp_it0/benders --mode decomposed --n_years 10 --gap_tol 0.01 \
      --max_iter 50 --max_hours 10 --no_monolithic_lp_bound --operational_bound \
      --corte_cargas_lhd <V>/<caso>/corte_cargas_lhd.json \
      --cut_type strengthened --strengthened_timelimit 300 --block_mip_focus 1 \
      --solve_timelimit 120 --day_warm_start always --day_timelimit 120 --day_gap 0.05 \
      --day_jobs 4 --polish_each_iter --skip_last_backward --fijar_R op \
      --incumbente_inicial <V>/<caso>/benders/incumbente_respaldo.pkl \
      --evaluar_trayectoria_op --reparar_dp --dp_margen 0.5 \
      --stabilization box --stab_center operational --box_delta_int 2 --stab_free_every 3 \
      > <V>/<caso>/dp_it0/benders.log 2>&1

Con `tray_op.pkl` ya guardado, cambiar `--operational_bound --corte_cargas_lhd …` por
`--tray_op_archivo <…>/tray_op.pkl --lb_inicial <cota operacional>`.

### Segunda vuelta de DP (2026-10-28): el mejor esquema

El esquema que dio los mejores resultados (casos 1, 3, 4 y 6):

    cota operacional (R libre) → R óptimo (--fijar_R op) → DP con la energía nominal → iteración 0
      → DP con la energía de esa solución → iteración 0 → mejorar_ub → certificado

La energía nominal del plan de producción queda 0,3–2 % sobre la real. Con planes justo
sobre el piso de degradación, eso basta para que la DP compre de más o antes. En el caso 4
agregaba una 5ª batería, una 3ª bahía y un 3er cargador en el año 7. Con la energía real
de la k=0, el UB bajó de 4.503.513 a 4.120.261.

No hace falta código nuevo. `--incumbente_inicial <benders de la primera vuelta>` hace que la
DP use la energía de esa solución. Para no recalcular la cota (en el caso 6 la segunda cota no
cerró en 5.400 s), se reusa la trayectoria de la primera vuelta:

    python -u setup.py ... --output_folder <V>/<caso>/dp_real/benders --max_iter 0 \
      --tray_op_archivo <V>/<caso>/dp_nom/benders/tray_op.pkl --lb_inicial <cota operacional> \
      --fijar_R 0 --evaluar_trayectoria_op --reparar_dp --dp_margen 0.5 \
      --incumbente_inicial <V>/<caso>/dp_nom/benders   # resto de flags como arriba

Con eso termina el Benders. Después se lanza la cola sobre la misma carpeta:
`CASOS_ENV="<caso>" SUB=dp_real ITER=0 PASADA2=umbral bash cola_tesis_swap_v4.sh 1`. La cola
ve el Benders terminado y sigue con `mejorar_ub` y el certificado.

`--fijar_R 0` corresponde a cuando la cota no tuvo reemplazo; si lo tuvo, fijar el de la cota.

**Ojo:** el driver guarda la k=0 solo si mejora al incumbente. El incumbente tiene que ser la
k=0 de la primera vuelta y no la solución después de `mejorar_ub`, que es de otra etapa. Si
no, la segunda vuelta se descarta aunque mejore la k=0 (pasó en el caso 3, `dp_real/`).

**Idea sin probar para bajar los gaps de los casos base 05C (1, 2, 3 y 6).** La candidata que
fija la LB del certificado compra la 3ª batería y la 2ª bahía en el **año 5** y termina
0,46 kWh sobre el piso. La DP, con margen 0,5, las compra en el año 4. Con `--dp_margen 0.3`,
que todavía cubre la holgura de McCormick (~0,2 kWh), se podría elegir ese plan.

**Qué mirar en el log:**
- `--reparar_dp (energia …, margen …): y4 baterias 2->3, …; capacidad final … kWh`;
- `k=0 trayectoria de la cota operacional reparada con la DP: UB …`.

Si la DP elige reemplazar, el log lo dice (`yN REEMPLAZO`) y vuelve a fijar R en los bloques
según ese plan.

**Diagnóstico sin correr nada** (2 s): `python dp_degradacion.py --data_folder <datos> --solucion
<carpeta de JSON> --reparar` valida la DP contra la solución y entrega el plan óptimo de
degradación, el valor de estados dados (`--estado año,pool,naves,D`) y la reparación del plan.

**Tests:** `python tests/test_degradacion_dp.py`. La DP coincide con la enumeración
exhaustiva de planes en un caso sintético.

**Pendiente (propuesto, no implementado):** usar la DP también dentro del Benders, como parte
exacta del costo futuro (θ = V_degradación + θ_resto con cortes). Así cada forward vería el
costo de dejar la batería corta, y se podría quitar `--fijar_R`.
