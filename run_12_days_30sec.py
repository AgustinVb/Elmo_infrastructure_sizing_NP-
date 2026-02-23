"""
Script para correr 12 días separados con máximo 30 segundos cada uno
Permite ver cómo se guardan los resultados
"""
import os
import sys
import time
from src import mine
from src.io.reader import Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel


def build_mine(data_folder, day_index):
    """Construye el sistema minero para un día específico"""
    model = Reader(data_folder + 'elmo_data.xlsx', start_in=1)
    series = Series(data_folder + 'time_series.xlsx')
    time_series = timeseries.Timeseries(series, [day_index], 8/60)
    mine_system = mine.Mine(model)
    time_series.mapper['Trips'] = time_series.get_trips(mine_system)
    return series, mine_system, time_series


def run_single_day(data_folder, output_folder_base, day_index, gap=0.05, timelimit_seconds=30, solver='gurobi'):
    """Ejecuta simulación para un día específico"""
    output_folder = os.path.join(output_folder_base, f'Dia_{day_index}')
    os.makedirs(output_folder, exist_ok=True)
    
    print(f"\n{'='*70}")
    print(f"🚀 INICIANDO DÍA {day_index:3d} | Gap: {gap*100:.1f}% | TimeLimit: {timelimit_seconds}s")
    print(f"{'='*70}")
    
    try:
        t_start = time.time()
        series, mine_system, time_series = build_mine(data_folder, day_index)
        print(f"   ✓ Modelo construido")
        
        opt = OptimizationModel(mine_system, time_series, gap, solver, output_folder, timelimit_seconds)
        
        elapsed = time.time() - t_start
        print(f"✅ Día {day_index} completado en {elapsed:.1f}s")
        return True
        
    except Exception as e:
        elapsed = time.time() - t_start
        print(f"❌ Error en Día {day_index}: {e}")
        return False


def main():
    data_folder = 'data/Escenarios_DCH/Swap_variable_3estaciones/'
    output_folder = 'output/Dias_30seg_Test'
    solver = 'gurobi'
    gap = 0.05
    timelimit = 30
    
    # Lista de 12 días
    days = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]
    
    print("\n" + "="*70)
    print("🚀 EJECUTANDO 12 DÍAS CON MÁXIMO 30 SEGUNDOS CADA UNO")
    print("="*70)
    print(f"📁 Datos: {data_folder}")
    print(f"📁 Salida: {output_folder}")
    print(f"🔧 Solver: {solver}")
    print(f"⏱️  Timeout: {timelimit}s | Gap: {gap*100:.1f}%")
    print(f"📋 Días: {days}")
    
    results = {}
    start_time = time.time()
    
    for i, day in enumerate(days, 1):
        success = run_single_day(data_folder, output_folder, day, gap, timelimit, solver)
        results[day] = success
        print(f"\n[{i}/{len(days)}] Progreso: {i*100//len(days)}%")
    
    # Resumen
    total_time = time.time() - start_time
    successful = sum(1 for v in results.values() if v)
    
    print(f"\n{'='*70}")
    print(f"📊 RESUMEN FINAL")
    print(f"{'='*70}")
    print(f"✅ Exitosos: {successful}/{len(days)}")
    for day, success in results.items():
        status = "✅" if success else "❌"
        print(f"  {status} Día {day}")
    print(f"⏱️  Tiempo total: {total_time:.1f}s ({total_time/3600:.2f}h)")
    print(f"📁 Resultados: {output_folder}")
    print(f"{'='*70}\n")
    
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
