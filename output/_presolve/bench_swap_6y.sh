#!/bin/bash
# Benchmark de tres vias en battery swapping, 160kW_2dias (3 naves, g_max=0),
# 6 anios, codigo actual (presolve + apertura). Se corre DESPUES de la cadena
# de OB: una sola corrida de Gurobi a la vez. Protocolo original de sec. 4:
# 600 s por fase monolitica; la fase B con 3 iteraciones.
cd "C:/Users/Agustin/Desktop/Elmo_swap_wt" || exit 1
COMMON="--data_folder data/DCH/160kW_2dias/ --solver gurobi --consumption_model wp2 --n_years 6 --block_build_jobs 1"
LOG=C:/Users/Agustin/Desktop/Elmo_infrastructure_NP/output/_presolve
OUT=C:/Users/Agustin/Desktop/Elmo_infrastructure_NP/output/_bench_swap
STATUS=$LOG/bench_swap_6y.status
mkdir -p $OUT
echo "inicio $(date +%H:%M:%S)" > $STATUS

echo "A  monolitico solo, 600 s  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode monolithic --mono_timelimit 600 \
    --output_folder $OUT/A_monolithic_600s/ > $LOG/bench_swap_A_monolithic.log 2>&1
echo "A  fin exit=$?  $(date +%H:%M:%S)" >> $STATUS

echo "B  descompuesto solo, 3 iteraciones  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode decomposed --max_iter 3 --solve_timelimit 600 \
    --output_folder $OUT/B_decomposed/ > $LOG/bench_swap_B_decomposed.log 2>&1
echo "B  fin exit=$?  $(date +%H:%M:%S)" >> $STATUS

echo "C  hibrido 1 iteracion, 600 s  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode hybrid --max_iter 1 --solve_timelimit 600 \
    --output_folder $OUT/C_hybrid/ > $LOG/bench_swap_C_hybrid.log 2>&1
echo "C  fin exit=$?  $(date +%H:%M:%S)" >> $STATUS

echo "listo $(date +%H:%M:%S)" >> $STATUS
