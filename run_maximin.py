#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Ejecuta el modelo con objetivo maximin (maximizar L = cota inferior de extracción)
"""
import sys
from os.path import join
from src import mine
from src.io.reader import Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel

def main():
    print("=" * 90)
    print("ELMO Maximin Extraction Objective (L)")
    print("=" * 90)
    
    # Configuración
    DATA_FOLDER = join('data', 'Escenarios_DET', 'Swap_costo_fijo')
    OUTPUT_FOLDER = 'output/test_maximin_L_gurobi'
    SOLVER = 'gurobi'
    GAP = 0.01  # 1% MIP gap
    TIMELIMIT = 3600  # 1 hora máximo
    OBJECTIVE_MODE = 'max_min_extraction'
    
    print(f"\n📂 Datos: {DATA_FOLDER}")
    print(f"📊 Objetivo: {OBJECTIVE_MODE} (maximizar L)")
    print(f"⚙️  Solver: {SOLVER}, Gap: {GAP}, Timelimit: {TIMELIMIT}s\n")
    
    # Cargar datos
    try:
        print("⏳ Cargando datos...")
        model = Reader(join(DATA_FOLDER, 'elmo_data.xlsx'), start_in=1)
        series = Series(join(DATA_FOLDER, 'time_series.xlsx'))
        time_series = timeseries.Timeseries(series, [1], 8/60)  # 1 día, delta_t=8/60
        mine_system = mine.Mine(model)
        time_series.mapper['Trips'] = time_series.get_trips(mine_system)
        print("✅ Datos cargados exitosamente.\n")
    except Exception as e:
        print(f"❌ Error cargando datos: {e}")
        sys.exit(1)
    
    # Ejecutar modelo
    try:
        print("⏳ Ejecutando modelo con Gurobi...")
        opt = OptimizationModel(
            mine_system=mine_system,
            time_series=time_series,
            gap=GAP,
            solver_name=SOLVER,
            output_folder=OUTPUT_FOLDER,
            timelimit=TIMELIMIT,
            objective_mode=OBJECTIVE_MODE
        )
        print(f"\n✅ Corrida completada exitosamente.")
        print(f"📁 Resultados guardados en: {OUTPUT_FOLDER}/")
        
        # Mostrar valor de L
        try:
            L_value = opt.opt_model.model.L()
            print(f"📈 Valor de L (mínima extracción por punto): {L_value:.2f}")
        except Exception as e:
            print(f"⚠️  No se pudo extraer L: {e}")
            
    except Exception as e:
        print(f"❌ Error durante la ejecución: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
