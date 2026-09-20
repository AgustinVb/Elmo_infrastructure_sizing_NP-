#!/bin/bash
cd "C:/Users/Agustin/Desktop/Elmo_swap_wt" || exit 1
COMMON="--data_folder data/DCH/160kW_2dias/ --solver gurobi --consumption_model wp2 --n_years 6 --block_build_jobs 1"
LOG=C:/Users/Agustin/Desktop/Elmo_infrastructure_NP/output/_presolve
OUT=C:/Users/Agustin/Desktop/Elmo_infrastructure_NP/output/_bench_swap
STATUS=$LOG/bench_swap_6y.status
echo "C2 hibrido 1 iteracion, MIPFocus=1, 600 s, arranque pulido  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode hybrid --max_iter 1 --solve_timelimit 600 --mip_focus 1 \
    --output_folder $OUT/C2_hybrid_focus1_pulido/ > $LOG/bench_swap_C2_hybrid_focus1_pulido.log 2>&1
echo "C2 fin exit=$?  $(date +%H:%M:%S)" >> $STATUS
