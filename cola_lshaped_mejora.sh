#!/bin/bash
# Cola del L-shaped con la version final del codigo (2026-10-30): paso final de
# mejorar UB (lshaped.py --mejora, como mejorar_ub.py), aviso de aceptaciones
# silenciosas y tope de tiempo dentro del callback.
#
#   Ranura A: solo el paso de mejorar UB (--solo_mejorar) sobre la solucion de
#             cada caso en lshaped_A2/ -> <caso>/lshaped_A2/mejora/.
#   Ranura B: corridas completas en lshaped_A3/: caso 7 (prueba integrada; en
#             A2 Gurobi acepto en silencio y la UB quedo sobre la conocida) y
#             caso 8 con la cota de 2 modulos de la subestacion (en A2 corrio
#             con 1).
#   Cada solucion nueva se valida en el monolitico (--solo_validar).
#
# 17:15: en lshaped_A3 el arbol del caso 8 cerro por aceptaciones silenciosas en
# 4.703.144, bajo la LB certificada, sin un solo corte entero: los cortes con
# violacion chica en absoluto (0,26 en el caso 7) "cortaban" para el callback pero
# Gurobi no los hace cumplir. Con el umbral tol_lp (1e-5 del objetivo, absoluto) y
# el reinicio del arbol con los cortes como restricciones se relanzan los casos 7
# y 8 en lshaped_A4/. Pruebas previas del caso 7: lshaped_A4_intfeastol/
# (IntFeasTol 1e-9), lshaped_A4_tol_lp/ (1e-4 del costo del dia),
# lshaped_A4_reinicio/ (diagnostico: enteras exactas, violacion 0,259):
#   RANURAS=b SUB_B=lshaped_A4 bash cola_lshaped_mejora.sh
#
#   nohup bash cola_lshaped_mejora.sh > $V/cola_lshaped_mejora.out 2>&1 &
PY=${PY:-~/miniconda3/envs/py310/bin/python}
V=output/Resultados_finales_tesis/Mina_modelo/casos_finales_v4
D=data/Tesis_final/Mina_modelo_v4
CPU=${CPU:-28}
MEJORA=${MEJORA:-600,900}
RANURAS=${RANURAS:-"a b"}
SUB_B=${SUB_B:-lshaped_A3}
cd "$(dirname "$0")"

msg() { echo "[lshaped mejora r640-01] $(date '+%F %T') $*" | tee -a $V/cola.log; }

ub_de() { $PY -c "import json; print(json.load(open('$1'))['ub_final'])"; }

validar() {  # carpeta con solucion/ y resumen.json, caso
  local d=$1 ub
  ub=$(ub_de $d/resumen.json)
  $PY -u lshaped.py --data_folder $D/$2 --solucion $d/solucion --ub_esperado $ub \
      --out $d/validacion --solo_validar --cpu 4 > $d/validacion.log 2>&1
  msg "validacion $2 ($d, ub_final $ub): $(grep -aE 'VALIDADA|ABORTA|Error' $d/validacion.log | tail -1)"
}

mejorar() {
  local c=$1 base=$V/$1/lshaped_A2 out=$V/$1/lshaped_A2/mejora ub
  ub=$(ub_de $base/resumen.json)
  mkdir -p $out
  msg "inicio mejorar UB $c (UB $ub, pasadas $MEJORA s por dia)"
  $PY -u lshaped.py --data_folder $D/$c --solucion $base/solucion --ub_esperado $ub \
      --out $out --solo_mejorar --mejora $MEJORA --cpu $CPU > $out/lshaped.log 2>&1
  msg "fin mejorar UB $c: $(grep -aE 'FINAL|ABORTA|Error' $out/lshaped.log | tail -1)"
  [ -d $out/solucion ] && validar $out $c
}

ranura_a() {
  for c in Produccion_escalada20/zona_norte/05C/RED_GEN_BESS \
           Produccion_escalada20/zona_norte/05C/RED \
           Produccion_base/zona_norte/05C/RED_GEN_BESS \
           Produccion_base/zona_norte/05C/RED_GEN \
           Produccion_base/zona_centro/05C/RED_GEN_BESS \
           Produccion_base/zona_norte/05C/RED \
           Produccion_base/zona_norte/2C/RED_GEN_BESS; do
    mejorar $c
  done
}

ranura_b() {
  LISTA_A="" LISTA_B="Produccion_base/zona_norte/1C/RED_GEN_BESS 2743895.04 2636527.63
Produccion_base/zona_norte/2C/RED_GEN_BESS 4949249.55 4703215" \
    HORAS=${HORAS:-5} CPU=$CPU SUB=$SUB_B bash cola_lshaped.sh
  for c in Produccion_base/zona_norte/1C/RED_GEN_BESS Produccion_base/zona_norte/2C/RED_GEN_BESS; do
    [ -d $V/$c/$SUB_B/solucion ] && validar $V/$c/$SUB_B $c
  done
}

msg "inicio cola de mejora (ranuras $RANURAS; A: solo mejorar UB sobre lshaped_A2; B: casos 7 y 8 en $SUB_B)"
[[ $RANURAS == *a* ]] && ranura_a &
[[ $RANURAS == *b* ]] && ranura_b &
wait
msg "cola de mejora terminada"
