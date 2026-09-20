#!/bin/bash
cd "C:/Users/Agustin/Desktop/Elmo_infrastructure_NP" || exit 1
COMMON="--data_folder data/DCH/640kW_2dias/ --solver gurobi --consumption_model wp2 --n_years 6 --block_build_jobs 1"
LOG=output/_presolve
STATUS=$LOG/bench_ob_6y.status
echo "C3 hibrido 3 iteraciones, MIPFocus=1, 600 s  $(date +%H:%M:%S)" >> $STATUS
python -u setup.py $COMMON --mode hybrid --max_iter 3 --solve_timelimit 600 --mip_focus 1 \
    --output_folder output/_bench_ob/C3_hybrid_3iter_focus1/ > $LOG/bench_ob_C3_hybrid_3iter_focus1.log 2>&1
echo "C3 fin exit=$?  $(date +%H:%M:%S)" >> $STATUS
echo "listo $(date +%H:%M:%S)" >> $STATUS
bash output/_presolve/bench_swap_6y.sh
