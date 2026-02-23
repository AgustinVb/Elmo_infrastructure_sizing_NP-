"""
Script para monitorear el progreso de la ejecución sin interrumpirla
Muestra carpetas creadas, archivos y resumen en tiempo real
"""
import os
import time
import json
from pathlib import Path

def count_results(output_folder):
    """Cuenta resultados por día"""
    results = {}
    if os.path.exists(output_folder):
        for day_folder in sorted(os.listdir(output_folder)):
            day_path = os.path.join(output_folder, day_folder)
            if os.path.isdir(day_path):
                files = os.listdir(day_path)
                files_count = len(files)
                has_summary = 'summary.txt' in files
                has_log = 'ELMO_log.txt' in files
                results[day_folder] = {
                    'files': files_count,
                    'has_summary': has_summary,
                    'has_log': has_log
                }
    return results

def show_status(output_folder):
    """Muestra estado actual"""
    results = count_results(output_folder)
    
    print("\n" + "="*80)
    print("MONITOREO DE EJECUCIÓN".center(80))
    print("="*80)
    print(f"Carpeta: {output_folder}")
    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("-"*80)
    
    if not results:
        print("Aun no hay resultados...")
    else:
        completed = sum(1 for r in results.values() if r['has_summary'])
        total = len(results)
        
        print(f"\nProgreso: {completed}/{total} días completados ({completed*100//total}%)\n")
        
        for day_name in sorted(results.keys()):
            data = results[day_name]
            status = "✓" if data['has_summary'] else "±"
            files = f"{data['files']} archivos"
            print(f"  [{status}] {day_name:15s} {files:15s} log={'SI' if data['has_log'] else 'NO'}")
    
    print("-"*80)
    print(f"Tiempo actual: {time.time():.0f}s desde inicio")
    print("="*80 + "\n")

if __name__ == '__main__':
    output_folder = 'output/Dias_1hora_Test'
    
    # Monitorear continuamente
    try:
        while True:
            show_status(output_folder)
            time.sleep(60)  # Actualizar cada 60 segundos
    except KeyboardInterrupt:
        print("\nMonitoreo detenido.")
