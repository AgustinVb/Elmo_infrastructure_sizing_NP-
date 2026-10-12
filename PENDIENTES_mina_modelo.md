# Pendientes — Mina_modelo en carga on-board (comparación con swap)

Rama `carga_ob_multiaño`. Estado al 2026-10-08. Ver también el
`PENDIENTES_mina_modelo.md` de `battery_swapping_multiaño` (sección 0), que es
el equivalente en swap y de donde se portó la metodología.

---

## 0a. VERSIÓN 1 de resultados (2026-10-09, r640-01) — congelada

Primera comparación OB vs swap. Metodología en las dos ramas: Nested Benders →
`mejorar_ub.py` → `certificar_inversiones.py`. Las LB son certificadas; los
intervalos [LB; UB] de cada z\* son los que valen para comparar.

**OB** (esta rama; corridas `<ESC>_v3`, `mejorar_ub_<ESC>_v3`, `certificado_<ESC>_v3`;
comandos de la sección 0, `--gap_objetivo` = min(0,5 %, 0,8·gap operacional)):

| Escenario | UB Benders (iter.) | UB final | LB certificada | Gap |
|---|---:|---:|---:|---:|
| P_red | 2.863.276,09 (1) | 2.862.291,64 | 2.845.199,25 | 0,60 % |
| P_red_gen | 2.465.279,20 (6, tope 11,75 h) | 2.454.142,83 | 2.404.679,11 | 2,02 % |
| P_red_gen_bat | 2.398.522,87 (4) | 2.383.306,25 | 2.375.055,34 | 0,35 % |

| Comparación OB | Ahorro garantizado | ¿Válida? |
|---|---|---|
| Generación vs solo red | 391.056 – 457.613 (13,7 – 16,1 %) | Sí |
| Gen + BESS vs solo red | 461.893 – 487.236 (16,1 – 17,1 %) | Sí |
| Valor del BESS | 21.373 – 79.087 (0,9 – 3,3 %) | **Sí** (en swap NO) |

P_red reemplaza la batería en el año 6 (`R[6] = 1`; en OB la batería se degrada
~25 kWh/año en los años 4–5 contra ~5–7 en swap). En P_red_gen los días MILP
de `mejorar_ub` cierran a 0,5–3,4 % a 600 s (en swap 13–39 %): el gap de 2 %
está en la cota y una LB con cotas MILP por día lo bajaría a ~0,3–0,5 %.

**Swap** (`battery_swapping_multiaño`, sección 0 de su PENDIENTES):

| Escenario | UB final | LB certificada | Gap |
|---|---:|---:|---:|
| RED | 2.569.771,29 | 2.555.303,48 | 0,56 % |
| RED_GEN | 2.131.851,53 | 2.047.595,74 | 3,95 % |
| RED_GEN_BESS | 2.079.895,34 | 2.035.547,42 | 2,13 % |

Valor del BESS en swap: no concluyente (≤ 96.304 certificado).

**Diferencias de método entre las dos ramas en la v1** (a corregir en la final):
- swap se corrió ANTES del arreglo del O&M en el MIP start por días (ítem 3);
- las corridas de Benders de swap no son todas con los mismos flags que las de
  OB (RED_GEN de swap: `P_red_gen`, con el MIP start por días pero sin la
  estabilización/flags de la sección 0; RED y RED_GEN_BESS vienen de otras
  versiones, `P_red_only_r4` y `v6_estab`), y `mejorar_ub` se aplicó con otros
  años/tiempos (swap: varias pasadas a 600–900 s);
- en OB el armado de los modelos era ~20 veces más lento (`timeseries.py` sin
  las caches de swap, corregido en 149574ded): P_red_gen usó 6 iteraciones en
  11,75 h, con el arreglo serían ~10–12.

## 0b. Qué falta para la VERSIÓN FINAL (las dos ramas)

Datos/modelo (aplicar en las dos ramas a la vez, ver sección 1):
1. Perfil de generación con interpolación lineal (ítem 2).
2. Tasa de descuento del 8 % (ítem 4), en los seis escenarios.
3. (Ítem 5, capacidad física: ya está en las dos; en swap no cambió los
   resultados de la v1.)

Corridas (después de 1–2, los seis escenarios con el MISMO pipeline y flags):
4. Benders con los comandos de la sección 0 (en swap, el equivalente con sus
   carpetas `data/Tesis_final/Mina_modelo/<ESC>`), con el O&M prorrateado
   (ítem 3) y el código rápido de `timeseries.py`.
5. `mejorar_ub` (años 1–10, 600 s) + `certificar_inversiones` con el mismo
   criterio de `--gap_objetivo` en las dos ramas.
6. Opcional: LB con cotas MILP por día para los escenarios con generación
   (en OB cerraría P_red_gen; en swap no alcanzó, ver su PENDIENTES ítem 1).

Pendientes de código/documentación:
7. Swap: tests largos sin correr con el ítem 11 (`test_apertura_primer_anio_swap`,
   `test_corte_fuera_del_ancla`) y la falla previa de `test_bloque_anual_swap`.
8. Swap: `docs/restricciones_modelo.tex` describe una formulación vieja de la
   degradación del pool; reescribir esa sección.
9. Para la tesis: comparar OB vs swap solo con la versión final (misma tanda de
   datos y método); la v1 sirve como resultado preliminar.

---

## 0. Escenarios equivalentes a los de swap

`data/Resultados_finales_tesis/Mina_modelo/<ESC>`, armados desde los mismos
datos que swap (`data/Tesis_final/Mina_modelo/<ESC>` en la otra rama). Los tres
comparten `time_series.xlsx` (mismo hash) y `elmo_data.xlsx` solo cambia en:

| OB | swap | `g_max` (Solar, Wind) | `h_max` |
|---|---|---|---|
| `P_red` | `RED` | 0, 0 | 0 |
| `P_red_gen` | `RED_GEN` | 2, 2 | 0 |
| `P_red_gen_bat` | `RED_GEN_BESS` | 2, 2 | 2 |

**Alineación con swap (2026-10-08).** Se comparó el `parameters.json` completo
de OB contra el de swap: quedan iguales salvo lo propio de cada tecnología
(`elhd_set`; `max_chargers_per_bay` = 1 en OB y 2 en swap, que no ata: tope de
4 cargadores y las soluciones usan 1–2). Para llegar ahí se cambió en OB:

- cargador 241 kW / 241.000 USD / `p_peak_dist` 3.000 (antes 482 kW /
  482.000 / 1.500), como en swap;
- O&M solar `c_op` = 6,5 (estaba en 6,9);
- `get_alpha_g` (timeseries.py): el perfil horario de generación pasa a
  intervalos en ESCALÓN, como swap (OB interpolaba linealmente: ~1,4–2,9 % más
  de solar por día y bastante más en la ventana de punta).

Mismo código en las dos ramas para: ventanas DET, ventana de punta (intervalos
72–101, días 91–244), costo de potencia (12·10·P_pot), módulo de subestación
de 500 kW, tasa de descuento (10 %) y su indexación.

### Comandos (regimen liberado, igual que swap)

Benders (uno por escenario; `<ESC>` = `P_red`, `P_red_gen`, `P_red_gen_bat`):
```
python -u setup.py --data_folder data/Resultados_finales_tesis/Mina_modelo/<ESC> --solver gurobi --days_per_year 4 --free_charging --free_maintenance --block_build_jobs 1 --output_folder output/Resultados_finales_tesis/Mina_modelo/<ESC>_v3 --mode decomposed --n_years 10 --gap_tol 0.01 --max_iter 50 --max_hours 11.75 --operational_bound --op_bound_gap 1e-4 --op_bound_timelimit 5400 --no_monolithic_lp_bound --stabilization box --stab_center operational --box_delta_int 1 --strengthen --strengthened_timelimit 300 --block_mip_focus 1 --solve_timelimit 120 --day_warm_start always --day_timelimit 120 --day_gap 0.05 --day_jobs 4 --polish_each_iter --skip_last_backward
```
Cierre del gap (después de cada uno):
```
python -u mejorar_ub.py --data_folder data/Resultados_finales_tesis/Mina_modelo/<ESC> --free_charging --free_maintenance --respaldo output/Resultados_finales_tesis/Mina_modelo/<ESC>_v3/incumbente_respaldo.pkl --anios 1,2,3,4,5,6,7,8,9,10 --timelimit 600 --jobs 4 --out output/Resultados_finales_tesis/Mina_modelo/mejorar_ub_<ESC>_v3
python -u certificar_inversiones.py --data_folder data/Resultados_finales_tesis/Mina_modelo/<ESC> --free_charging --free_maintenance --solucion <carpeta solucion> --gap_objetivo 0.005 --evaluar_vivas --jobs 4 --out output/Resultados_finales_tesis/Mina_modelo/certificado_<ESC>_v3
```
`--solucion`: `mejorar_ub_<ESC>_v3/solucion` si mejoró, si no la carpeta de
Benders. `--gap_objetivo` tiene que quedar por DEBAJO del gap contra la cota
operacional (si no, no hay candidatas y el certificado sale peor que la cota).
Métricas: `escribir_parameters.py` + `python consumer.py <solucion>`.

---

## 1. Pendientes (comunes a las dos ramas)

1. **Correr los tres escenarios de arriba** para la primera comparación con
   swap. Diferencia conocida con los resultados de swap actuales: el MIP start
   por días de OB ya prorratea el O&M anual de generación/BESS y el de swap se
   corrió con el sesgo (ver 3). Solo afecta la heurística, no el modelo.
2. **Perfil de generación con interpolación lineal** entre horas en vez de
   escalón, en las DOS ramas a la vez (`get_alpha_g` en timeseries.py). Hoy
   ambas usan escalón para ser comparables con lo ya corrido.
3. **Volver a correr los escenarios de ambas ramas con el O&M prorrateado** en
   el MIP start por días (`day_blocks._day_objective`: gen_op_cost/bess_op_cost
   son anuales y se cobraban completos en cada día). Corregido en el código de
   las dos ramas el 2026-10-07; los resultados de swap son de antes.
4. **Tasa de descuento del 8 %** (hoy 10 %, hoja `BatteryDegradation`,
   columna `discount_rate`) en todos los escenarios de las dos ramas.

5. **Capacidad de batería físicamente consistente** (cambio de MODELO).
   `b_y_link` (`b_bar[y] <= D[y-1] + 0,3·b_max·R[y]`) es solo cota superior:
   sin reemplazo el modelo podía elegir una capacidad MENOR que la que quedó,
   sin costo dentro del año, y la "tiraba". Medido:
   - en OB el forward de k=1 bajaba la batería ~90 kWh/año (la degradación
     real es ~4–5 kWh/año) hasta el piso (80 %) y forzaba reemplazos;
   - las soluciones finales de swap también lo hacen en los últimos años (RED:
     el año 9 parte con 432,7 kWh habiendo terminado el 8 con 438,1; RED_GEN y
     RED_GEN_BESS, el año 10 con 424–429 contra ~435).

   **Arreglo** — agregar, para los años `y > y_1`:
   ```
   b_bar[y] >= D[y-1] - B_U·R[y]     (b_y_link_lower)
   b_bar[y] >= b_max·R[y]            (b_y_replace)
   ```
   Con `b_y_link` y `b_bar <= B_U = b_max` queda `b_bar[y] = D[y-1]` si
   `R[y] = 0` y `b_bar[y] = b_max` si `R[y] = 1`.

   - **OB: IMPLEMENTADO el 2026-10-08** (`functions.py`: `b_y_link_lower`,
     `b_y_replace`; `year_block.py`: `b_y_link_lower_local`,
     `b_y_replace_local` con `D_prev`). Los tres escenarios de la sección 0 se
     corren YA con el modelo corregido. El macrobloque ya era consistente
     (`macroblocks.coordinate_b_bar`).
   - **Swap: IMPLEMENTADO el 2026-10-08** (commit 36d578710 de esa rama). Los
     incumbentes de RED, RED_GEN y RED_GEN_BESS pulidos con el modelo nuevo dan
     el mismo costo al centavo: los resultados de swap no cambian.

Conviene juntar 2–4 (y 5 en swap) en una sola tanda de corridas finales en las
dos ramas.

## 1b. Arreglo del MIP start por días en OB (2026-10-08)

En OB el pulido del start por días salía INFACTIBLE (P_red, años 3 y 7 del
forward de k=1; en swap, 0 de ~390): los días no ven la degradación, dejaban
`b_bar` en el piso (indiferente dentro del día) y fijaban `R = 0`; con la
batería heredada en el piso el año necesita `R = 1`. Ahora `day_blocks` fija
`b_bar` de los días en la capacidad heredada (`D_hat`) y el pulido decide `R`.
Verificado: con `D` heredado = 386 el pulido reemplaza (`R = 1`) y da MIP start;
con 477,7 los días usan 477,7. Solo cambia la heurística, no el modelo.

## 2. Notas del porte (2026-10-07)

- Portado de swap: estabilización box, pulido por iteración, respaldo .pkl y
  reanudación, tope de tiempo, archivo STOP, MIP start por días, mejorar_ub,
  certificado. Ver los mensajes de los commits 222b87099 y 0c51495cc.
- Diagnóstico de la relajación (`diagnostico_gap_dias.py`, P_red viejo): en OB
  la operación diaria es exacta en su relajación (B − LP = 0), así que no hace
  falta el corte de cargas de swap. El gap que quedaba (0,81 %) está en
  `P_pot`: con Z_charge relajado se carga parte de un intervalo en punta sin
  perder producción.
- En OB el archivo STOP corta la descomposición pero no la fase monolítica del
  modo híbrido (en swap sí).
