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

## Caso 2 v4 (Produccion_base/zona_norte/05C/RED_GEN_BESS), 2026-10-23 (equipo 2)

Benders (cola v4): cota operacional **2.387.721** (39 min); k=1 UB 3.085.163 (gap 22,6 %),
cortado tras k=1. Se reparo la solucion **de la v3** (mismo caso, ya no factible con la
degradacion corregida) con `reparar_inversion.py`, un script local de este equipo (cambios de
inversion a mano, b_bar supuesto lineal para los dias, MIP de cierre). **Se elimino el
2026-10-26 en favor de `reparar_reemplazo.py` (raiz), mas completo**: predice la trayectoria de
b_bar desde el incumbente, tamiza candidatas, reintenta dias y repite hasta que la b_bar cuadre.
Las carpetas `reparacion_v3*` quedan como registro:

| Paso | Estacion (baterias por año) | UB | gap |
|---|---|---|---|
| v3 + bateria y 2o cargador en año 3, bahia en año 4, R = 0 (`reparacion_v3c`) | 1,1,2,3,3,... | infactible: la flota de 7 cae ~13 kWh/año y baja del piso en el año 9 | - |
| idem con R libre (`reparacion_v3d`) | 1,1,2,3,3,... | 3.584.689 (reemplaza 7 baterias en el año 9) | 33 % |
| **+ 4a bateria en el año 5 (`reparacion_v3e`)** | **1,1,2,3,4,4,...** | **2.752.159**, R = 0, capacidad final 397,3 kWh | 13,2 % |
| mejorar_ub pasada 1 (años 1-10, 600 s) | | 2.706.425 | 11,8 % |
| mejorar_ub pasada 2 (años 3-9, 900 s) | | **2.703.610** (`mejorar_ub_rep2/solucion`) | **11,7 %** |

Lecciones: (1) suponer el piso de capacidad en todos los años al reoptimizar los dias es
demasiado conservador (dias infactibles en los años de mas produccion); una trayectoria lineal
de ~10,7 kWh/año funciono. (2) Un dia "sin solucion" puede ser falta de tiempo (status 9), no
infactibilidad: reintentar con mas tiempo. (3) Con la estacion con otro numero de baterias que
la operacion guardada, hay que dejar libre su inventario (`--estacion_libre`).

Pendiente: certificado (puede subir la LB; en la v3 la subio +1,7 %) y ajustes finos de la
inversion (p.ej. la 4a bateria en el año 6 ahorraria ~16 mil descontados). Para repetir una
reparacion, usar `reparar_reemplazo.py` (ver su docstring; `--candidatos año:n`).

## Cambios en el algoritmo (2026-10-23, detras de flags; apagados = igual que antes)

1. `--evaluar_trayectoria_op` (iteracion 0): la cota operacional guarda su trayectoria de
   inversion y su R (`NestedBenders.tray_op`, `R_op`). Antes de la iteracion 1 se corre un
   forward con esa inversion FIJA (caja de radio 0) y R fijo en el de la cota
   (`ForwardPass.fijar_R`; si un año no tiene solucion asi, se repite con R libre y se
   anota). Se pule y, si mejora, queda como UB, centro de la caja y respaldo (k=0).
2. `--reparar_reemplazo`: si esa solucion paga reemplazo, se evalua (mismo mecanismo) con
   +1 bateria desde 4 años antes del primer reemplazo y R = 0; si algun año todavia
   necesita reemplazo, +2 baterias (desde 6 y 3 años antes). Bahias y cargadores se suben
   lo justo para que quepan.
3. `--r_endurecido` (`ELMO_R_ENDURECIDO`): b_y_link con coeficiente b_max - B_L (96,4) en
   vez de 0,3*b_max (144,6), en el monolitico y en el bloque anual. Valido porque D >= B_L;
   no cambia ninguna solucion entera ni la cota operacional (que deja R entera), solo la
   relajacion LP de los bloques (cortes). El McCormick de D*R con D en [B_L, b_max] se
   proyecta en esta misma desigualdad, asi que no se implemento aparte.

Verificado: con los flags apagados el LP escrito es identico; con `--r_endurecido` solo
cambia el coeficiente de R en b_y_link (-144,6 -> -96,4). Prueba chica (2 años, 1 dia):
la iteracion 0 dio UB 1.161.197 sin reemplazo (gap 0,67 %) antes de la iteracion 1.
Corrida del caso 2 con los tres flags: `casos_finales_v4/.../05C/RED_GEN_BESS/it0/`.
4. `--fijar_R {op,0}`: fija el reemplazo R en TODOS los bloques anuales (forward y backward):
   `op` = el de la cota operacional, `0` = sin reemplazo. Se resuelve el problema
   restringido: su UB vale para el original, la cota del backward no (se imprime pero no
   entra a la LB, que queda en la operacional o `--lb_inicial`). Fijarlo tambien en el
   backward hace que los cortes vean que llegar con poca capacidad cuesta baterias, no un
   reemplazo relajado barato. Prueba chica OK (R fijo en los bloques, LB = operacional).

Resultado de la iteracion 0 en el caso 2 (`it0/`, 2026-10-23): la trayectoria de la cota
operacional (3 baterias desde el año 5, 2 bahias, 2 cargadores, sin reemplazo) dio UB
3.728.543: sin reemplazo, pero con la operacion de baja calidad del forward (bloques
anuales a 120 s con gaps de 30-42 %); en el año 10 la capacidad (387,3) no alcanzo con la
inversion fija y se resolvio sin caja. La iteracion 1 volvio a reemplazar (R no esta en la
caja) y la corrida murio por access violation (CPU) en el año 9. Siguiente prueba:
`fijarR/` = `--fijar_R 0 --lb_inicial 2387721.18 --incumbente_inicial mejorar_ub_rep2/solucion`.

Resultado de `fijarR/` (caso 2, 2026-10-23 13:32 -> 22:10, tope de 10 h; 6 iteraciones): k=1..5
pasos nulos (UB del forward 3,84 -> 3,67 M: el forward seguia llegando al año 10 sin margen y
compraba baterias de golpe; la cota del backward del problema sin reemplazo subio 1,35 -> 1,86 M),
**k=6 paso serio: UB 2.683.027** (-20.583 frente a la solucion reparada; gap 11,01 % contra la
cota operacional). Inversion: estacion 1,1,1,2,3,3,4,... (baterias en los años 4, 5 y 7), 2o
cargador año 4, 2a bahia año 5, solar 2,24, BESS 2,48, sin reemplazo, capacidad final 388,9 kWh.
El algoritmo encontro solo una solucion mejor que la reparada a mano, pero tardo 5 iteraciones.
