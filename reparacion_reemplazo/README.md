# Reparacion del reemplazo de flota (swap v4)

Con la degradacion corregida (ciclos fisicos completos), el incumbente del Nested
Benders puede quedar pagando el reemplazo de toda la flota cuando una bateria mas
en la estacion lo evita a menor costo.

## Caso 6 v4 (Produccion_base/zona_centro/05C/RED_GEN_BESS)

| | UB | gap vs LB op. 2.639.629 |
|---|---|---|
| Benders (4 iteraciones, 6 h) | 3.334.448 (reemplazo de 6 baterias en el año 8, ~1,05 M desc.) | 20,8 % |
| Reparado: +1 bateria, +1 bahia y 2o cargador adelantado al año 3, R = 0 | **2.821.625** | ~6,45 % |

Flota reparada: estacion 1,1,2,3,...,3 + 4 LHD (7 baterias desde el año 4);
capacidad final 386,8 kWh (piso 385,6), sin reemplazo.

Referencia on-board v4 (rama carga_ob_multiaño, P_red_gen_bat centro 05C):
UB 2.608.964, LB certificada 2.595.752 (gap 0,51 %), 1 reemplazo (753.683).
La LB de swap (2,64 M) ya supera el costo de on-board: on-board es mas barato.

## Por que el Benders no la ve

- La pasada forward decide año a año con cortes de la relajacion LP; con R
  fraccional, `b_bar <= D + 0,3*b_max*R` recupera capacidad pagando solo
  `Z_repl >= NF_min*R`: la degradacion futura queda 5-10x mas barata que el
  reemplazo real. La bateria extra del año 3 (~481 mil) no se compra.
- El lagrangiano fortalece solo los bloques diarios; la cadena R/b_bar/D vive en
  el maestro anual (Phi^LR = Phi^LP).
- La misma debilidad deja la LB plana (cota operacional).

Mejoras validas para la LB (version siguiente): coeficiente b_max - B_L (96,4)
en lugar de 0,3*b_max (144,6); o `b_bar <= D + (b_max - D)*R` con McCormick de D*R.

## Scripts

1. `eval_bateria_extra.py DATA RESPALDO OUT ANIO`: incumbente + bateria/bahia/cargador
   en ANIO con la operacion fija (guarda OUT/x.npy).
2. `eval_sin_reemplazo.py DATA RESPALDO OUT`: R = 0, b_bar degradado en los años
   8-10 (BB, ajustar por caso), reoptimiza esos dias y pule (OUT/x_sin_reemplazo.npy).
3. `guardar_sol.py DATA RESPALDO OUT`: escribe OUT/solucion (JSON, usable con
   --solucion / --incumbente_inicial).
4. `cola_caso6_v4_reparado.sh` (raiz): mejorar_ub 1 y 2 + consumer + certificado
   desde `<caso>/solucion_reparada`.
