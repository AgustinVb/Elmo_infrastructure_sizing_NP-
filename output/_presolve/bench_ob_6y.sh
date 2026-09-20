#!/bin/bash
# Benchmark de tres vias en carga on board, 640kW_2dias, 6 anios, con el
# presolve de capacidad. Fases secuenciales (una sola corrida de Gurobi a la
# vez), cada una con su log. Ver CONTEXTO_pendientes_descomposicion.md sec. 4.
cd "C:/Users/Agustin/Desktop/Elmo_infrastructure_NP" || exit 1
COMMON="--data_folder data/DCH/640kW_2dias/ --solver gurobi --consumption_model wp2 --n_years 6 --block_build_jobs 1"
LOG=output/_presolve
STATUS=$LOG/bench_ob_6y.status
echo "inicio $(date +%H:%M:%S)" > $STATUS

echo "B  descompuesto solo, 3 iteraciones  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode decomposed --max_iter 3 --solve_timelimit 600 \
    --output_folder output/_bench_ob/B_decomposed/ > $LOG/bench_ob_B_decomposed.log 2>&1
echo "B  fin exit=$?  $(date +%H:%M:%S)" >> $STATUS

echo "A2 monolitico solo, 2260 s (presupuesto del hibrido)  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode monolithic --mono_timelimit 2260 \
    --output_folder output/_bench_ob/A2_monolithic_2260s/ > $LOG/bench_ob_A2_monolithic.log 2>&1
echo "A2 fin exit=$?  $(date +%H:%M:%S)" >> $STATUS

echo "C1 hibrido 1 iteracion, MIPFocus=1, 600 s  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode hybrid --max_iter 1 --solve_timelimit 600 --mip_focus 1 \
    --output_folder output/_bench_ob/C1_hybrid_focus1/ > $LOG/bench_ob_C1_hybrid_focus1.log 2>&1
echo "C1 fin exit=$?  $(date +%H:%M:%S)" >> $STATUS

echo "listo $(date +%H:%M:%S)" >> $STATUS
