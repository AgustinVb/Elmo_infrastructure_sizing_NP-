"""
Script para correr 12 días separados con máximo 1 hora (3600 segundos) cada uno
5% gap como criterio de parada
Muestra progreso en tiempo real en la terminal
"""
import os
import sys
import time
import sys
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


def run_single_day(data_folder, output_folder_base, day_index, current, total, gap=0.05, timelimit_seconds=3600, solver='gurobi'):
    """Ejecuta simulación para un día específico"""
    output_folder = os.path.join(output_folder_base, f'Dia_{day_index}')
    os.makedirs(output_folder, exist_ok=True)
    
    percent = int((current-1)*100 / total)
    print(f"\n[{current:2d}/{total}] [{percent:3d}%] Day {day_index:3d} | ", end="", flush=True)
    sys.stdout.flush()
    
    try:
        t_start = time.time()
        series, mine_system, time_series = build_mine(data_folder, day_index)
        
        opt = OptimizationModel(mine_system, time_series, gap, solver, output_folder, timelimit_seconds)
        
        elapsed = time.time() - t_start
        status = "OK"
        print(f"{status:4s} ({elapsed:6.1f}s)", flush=True)
        sys.stdout.flush()
        return True
        
    except Exception as e:
        elapsed = time.time() - t_start
        status = "FAIL"
        print(f"{status:4s} ({elapsed:6.1f}s)", flush=True)
        sys.stdout.flush()
        return False


def main():
    data_folder = 'data/Escenarios_DCH/Swap_variable_3estaciones/'
    output_folder = 'output/Dias_1hora_Test'
    solver = 'gurobi'
    gap = 0.05
    timelimit = 3600  # 1 hora
    
    # Lista de 12 días
    days = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]
    
    print("\n" + "="*75)
    print("EXECUTING 12 DAYS WITH 1-HOUR TIMEOUT (3600 seconds) EACH")
    print("="*75)
    print(f"Data: {data_folder}")
    print(f"Output: {output_folder}")
    print(f"Solver: {solver} | Gap: {gap*100:.1f}% | Timeout: {timelimit}s (1h)")
    print("="*75)
    sys.stdout.flush()
    
    results = {}
    start_time = time.time()
    
    for i, day in enumerate(days, 1):
        success = run_single_day(data_folder, output_folder, day, i, len(days), gap, timelimit, solver)
        results[day] = success
        sys.stdout.flush()
    
    # Resumen
    total_time = time.time() - start_time
    successful = sum(1 for v in results.values() if v)
    
    print(f"\n{'='*75}")
    print(f"FINAL SUMMARY")
    print(f"{'='*75}")
    print(f"Successful: {successful}/{len(days)}")
    for day, success in results.items():
        status = "OK" if success else "FAIL"
        print(f"  [{status}] Day {day}")
    print(f"Total time: {total_time:.1f}s ({total_time/3600:.2f}h)")
    print(f"Results: {output_folder}")
    print(f"{'='*75}\n")
    sys.stdout.flush()
    
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
