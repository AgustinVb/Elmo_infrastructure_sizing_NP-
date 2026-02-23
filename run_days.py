"""
Script para correr simulaciones de días individuales con parámetros personalizados.
Primero prueba con 30 segundos, luego corre todos los días con 5% gap y máximo 1 hora.
"""
import os
import sys
import time
import argparse
from src import mine
from src.io.reader import Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel


def build_mine(data_folder, day_index):
    """
    Construye el sistema minero para un día específico
    
    :param data_folder: carpeta de datos
    :param day_index: índice del día (ej: 1, 32, 60, etc.)
    :return: series, mine_system, time_series
    """
    model = Reader(data_folder + 'elmo_data.xlsx', start_in=1)
    series = Series(data_folder + 'time_series.xlsx')
    time_series = timeseries.Timeseries(series, [day_index], 8/60)
    mine_system = mine.Mine(model)
    time_series.mapper['Trips'] = time_series.get_trips(mine_system)
    return series, mine_system, time_series


def run_single_day(data_folder, output_folder_base, day_index, gap, timelimit_seconds, solver='gurobi'):
    """
    Ejecuta simulación para un día específico
    
    :param data_folder: carpeta con datos
    :param output_folder_base: carpeta base de salida
    :param day_index: día a correr
    :param gap: MIPGap (ej: 0.05 para 5%)
    :param timelimit_seconds: tiempo máximo en segundos
    :param solver: solver a usar (gurobi o glpk)
    """
    print(f"\n{'='*70}")
    print(f"🚀 INICIANDO DÍA {day_index}")
    print(f"   Gap: {gap*100:.1f}% | Tiempo máximo: {timelimit_seconds}s ({timelimit_seconds/3600:.2f}h)")
    print(f"{'='*70}")
    
    # Crea carpeta de salida para este día
    output_folder = os.path.join(output_folder_base, f'Dia_{day_index}')
    os.makedirs(output_folder, exist_ok=True)
    
    try:
        t_start = time.time()
        
        # Construye modelo para el día específico
        series, mine_system, time_series = build_mine(data_folder, day_index)
        
        # Ejecuta optimización
        opt = OptimizationModel(
            mine_system, 
            time_series, 
            gap, 
            solver, 
            output_folder,
            timelimit_seconds
        )
        
        elapsed = time.time() - t_start
        print(f"\n✅ Día {day_index} completado en {elapsed:.1f}s ({elapsed/3600:.2f}h)")
        return True
        
    except Exception as e:
        elapsed = time.time() - t_start
        print(f"\n❌ Error en Día {day_index} (después de {elapsed:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description='Ejecutar simulaciones de días individuales')
    parser.add_argument('--data_folder', default='data/Escenarios_DCH/Swap_variable_3estaciones/', 
                        help='Carpeta con datos')
    parser.add_argument('--output_folder', default='output/Prueba_Dias_Separados',
                        help='Carpeta base de salida')
    parser.add_argument('--solver', default='gurobi', choices=['gurobi', 'glpk'],
                        help='Solver a usar')
    parser.add_argument('--gap', type=float, default=0.05,
                        help='MIPGap (ej: 0.05 = 5%)')
    parser.add_argument('--timelimit', type=int, default=3600,
                        help='Tiempo máximo en segundos (default=3600 = 1 hora)')
    
    args = parser.parse_args()
    
    # Lista de días a procesar
    all_days = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]
    
    # FASE 1: TEST con 30 segundos solo en día 1
    print("\n" + "="*70)
    print("🧪 FASE 1: PRUEBA RÁPIDA (30 segundos en Día 1)")
    print("="*70)
    
    test_output = os.path.join(args.output_folder, 'TEST_30seg')
    os.makedirs(test_output, exist_ok=True)
    
    test_success = run_single_day(
        args.data_folder,
        test_output,
        1,
        0.05,
        30,
        args.solver
    )
    
    if not test_success:
        print("\n⚠️  La prueba falló. Abortando.")
        return 1
    
    print("\n✅ Prueba exitosa. Continuando...")
    
    # Confirmar antes de corrida completa
    response = input("\n❓ ¿Continuar con corrida completa (5% gap, 1 hora por día)? [s/N]: ").strip().lower()
    if response != 's':
        print("❌ Abortado por usuario.")
        return 0
    
    # FASE 2: CORRIDA COMPLETA con 5% y 1 hora
    print("\n" + "="*70)
    print("🚀 FASE 2: CORRIDA COMPLETA (5% gap, 3600s = 1 hora por día)")
    print(f"📋 Días a procesar: {len(all_days)}")
    print(f"📁 Salida: {args.output_folder}")
    print(f"🔧 Solver: {args.solver}")
    print("="*70)
    
    # Ejecuta cada día
    results = {}
    start_time = time.time()
    
    for i, day in enumerate(all_days, 1):
        print(f"\n[{i}/{len(all_days)}]", end=" ")
        success = run_single_day(
            args.data_folder,
            args.output_folder,
            day,
            args.gap,
            args.timelimit,
            args.solver
        )
        results[day] = success
    
    # Resumen final
    total_time = time.time() - start_time
    successful = sum(1 for v in results.values() if v)
    
    print(f"\n{'='*70}")
    print(f"📊 RESUMEN FINAL")
    print(f"{'='*70}")
    print(f"✅ Días exitosos: {successful}/{len(all_days)}")
    for day, success in results.items():
        status = "✅" if success else "❌"
        print(f"  {status} Día {day}")
    print(f"⏱️  Tiempo total: {total_time:.1f}s ({total_time/3600:.2f}h)")
    print(f"📁 Resultados en: {args.output_folder}")
    print(f"{'='*70}\n")
    
    # Retorna 0 si todos exitosos, 1 si hay fallas
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
