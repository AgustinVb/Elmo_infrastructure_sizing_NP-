Warm start para generacion de columnas

Caso: Produccion_escalada20/zona_norte/05C/RED_GEN_BESS
Origen: Nested Benders + mejorar_ub pasada 1
UB: 2628627.929667105

La carpeta solucion contiene los JSON de variables y se pasa a cg_propia.py
con --solucion. No usar incumbente_respaldo.pkl directamente con cg_propia.py.

Ejemplo desde la raiz del repositorio:

python cg_propia.py \
  --data_folder data/Tesis_final/Mina_modelo/Produccion_escalada20/zona_norte/05C/RED_GEN_BESS \
  --n_years 10 --days_per_year 4 \
  --solucion output/transferencias/RED_GEN_BESS_escalada_mejorar_ub_1/solucion \
  --out output/transferencias/RED_GEN_BESS_escalada_cg

La pasada 2 de mejorar_ub aun estaba ejecutandose al crear este paquete.