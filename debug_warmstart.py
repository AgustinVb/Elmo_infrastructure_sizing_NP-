#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Debug warm-start loading to find why tokens don't match model indices"""

import json
from pathlib import Path
import sys
from os.path import join

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from src import mine
from src.io.reader import Setting, Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel

# Load data like setup.py does
data_folder = "data/Escenarios_DCH/Costo_fijo/Swap_fixed_3estaciones"

model = Reader(join(data_folder, "elmo_data.xlsx"), start_in=1)
series = Series(join(data_folder, "time_series.xlsx"))
time_series = timeseries.Timeseries(series, [1], 8/60)
mine_system = mine.Mine(model)
time_series.mapper['Trips'] = time_series.get_trips(mine_system)

# Create model (without warm-start)
opt_model = OptimizationModel(mine_system, time_series, "output/test_debug", y_init_path=None, init_solution_folder=None)
model_pyomo = opt_model.model

# Check Y variable
Y = model_pyomo.Y
print(f"Y variable info:")
print(f"  Name: {Y.name}")
print(f"  Num indices: {len(Y)}")

# Get first 3 keys
y_keys_sample = list(Y.keys())[:3]
print(f"\nFirst 3 keys from model.Y:")
for i, key in enumerate(y_keys_sample):
    print(f"  {i+1}. {key}")
    print(f"     Types: {tuple(type(k).__name__ for k in key)}")

# Load Y.json
y_json_path = Path("output/DCH_pruebas/costo_fijo/1_mb_sin_precedencia/Y.json")
if y_json_path.exists():
    print(f"\nLoading {y_json_path}")
    with open(y_json_path) as f:
        y_data = json.load(f)
    
    # Flatten manually to get sample tokens
    def flatten_sample(tree, n=3):
        records = []
        def walk(node, tokens):
            if isinstance(node, dict):
                for axis_name, axis_values in node.items():
                    if not isinstance(axis_values, dict):
                        return
                    for axis_value, child in axis_values.items():
                        walk(child, tokens + [(axis_name, str(axis_value))])
            else:
                records.append(tokens)
                if len(records) >= n:
                    return
        walk(tree, [])
        return records[:n]
    
    sample_tokens_raw = flatten_sample(y_data, n=3)
    print(f"\nFirst 3 token tuples from Y.json:")
    for i, tokens in enumerate(sample_tokens_raw):
        print(f"  {i+1}. {tokens}")
    
    # Now extract just the values in order ['i', 'j', 'd', 't']
    var_axis_order = ['i', 'j', 'd', 't']
    print(f"\nReordering to {var_axis_order}:")
    for i, tokens in enumerate(sample_tokens_raw):
        axis_dict = {name: value for name, value in tokens}
        ordered_tokens = [axis_dict.get(axis, None) for axis in var_axis_order]
        ordered_tokens = [t for t in ordered_tokens if t is not None]
        print(f"  {i+1}. ordered tokens: {ordered_tokens}")
        
        # Try to cast and match
        sample_key = y_keys_sample[0]
        casted = []
        for pos, token in enumerate(ordered_tokens):
            if pos < len(sample_key):
                example_value = sample_key[pos]
                if isinstance(example_value, str):
                    casted.append(str(token))
                elif isinstance(example_value, int):
                    casted.append(int(float(token)))
                elif isinstance(example_value, float):
                    casted.append(float(token))
                else:
                    casted.append(token)
            else:
                casted.append(token)
        
        idx_tuple = tuple(casted)
        print(f"       casted:  {idx_tuple} (types: {tuple(type(k).__name__ for k in idx_tuple)})")
        print(f"       expected: {sample_key} (types: {tuple(type(k).__name__ for k in sample_key)})")
        print(f"       match: {idx_tuple == sample_key}")
        print(f"       in Y: {idx_tuple in Y}")
        print()
else:
    print(f"File not found: {y_json_path}")
