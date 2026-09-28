from src import mine
from src.io.reader import Setting, Reader, Series
from src.time_series import timeseries
from src.optimization import OptimizationModel
import argparse, pprint
import pandas as pd
from os.path import join
import os
import sys
import xlrd

# En Windows la consola suele quedar en cp1252, que no puede codificar los
# emojis usados en los prints de progreso (✅/⚠️/etc.) a lo largo del
# pipeline (opt_model.py, printer.py, ...) -- eso lanzaba UnicodeEncodeError
# DESPUES de que Gurobi ya hubiera resuelto el modelo (visto en
# limited_infeasible_log tras un monolitico sin incumbente). Mismo arreglo
# que carga_ob_multiaño.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
xlrd.xlsx.ensure_elementtree_imported(False, None)
xlrd.xlsx.Element_has_iter = True




def resolve_wp2_json_path(args):
    json_path = args.wp2_consumption_json or 'electric_routes_within_time.json'
    if not os.path.isabs(json_path):
        json_path = os.path.join(args.data_folder, json_path)
    return json_path


def build_mine(args):
    """ building power system base function

    :param args: a argument based list, you can access to any
    attribute using args.property.

    :return:
    """

    model = Reader(join(args.data_folder, args.model), start_in=1)
    series = Series(join(args.data_folder, args.series))
    # --- Dias representativos ---------------------------------------------
    # La eleccion NO es libre: get_alpha_g busca el perfil renovable con la
    # clave ((day - 1) % 365) + 1 contra la columna 'day' de GenProfiles, asi
    # que solo son utilizables los dias-del-anio que esa hoja trae con
    # day <= 365. Hoy son exactamente {15, 105, 196, 288} (las filas con
    # day > 365 existen pero son inalcanzables para el lookup).
    #
    # OJO: get_alpha_g devuelve 0.0 cuando no encuentra el dia, SIN AVISAR. Una
    # lista anterior usaba los dias-del-anio 1 y 91, que no tienen perfil: la
    # generacion y el almacenamiento quedaban desactivados en silencio -- el
    # modelo invertia en solar/eolica, pagaba inversion y operacion, y recibia
    # cero energia. Si se agrega un dia hay que poblarlo antes en GenProfiles.
    #
    # Cobro de potencia: power_peak_limit solo ata P_pot entre los dias 91 y
    # 244 (abril-septiembre, meses de punta). Con 2 dias cae 1 de 2 (el 196);
    # con 4 caen 2 de 4 (105 y 196). La proporcion se mantiene, asi que las dos
    # configuraciones siguen siendo comparables en ese termino.
    DIAS_POR_ANIO = {
        2: [15, 196],            # verano sin cobro + invierno con cobro
        4: [15, 105, 196, 288],  # los cuatro que GenProfiles tiene poblados
    }
    ANIOS_HORIZONTE = 14  # plan minero completo (ExtractionGoal/FleetByYear traen 14)

    doys = DIAS_POR_ANIO[getattr(args, 'days_per_year', 4)]
    days = [(y - 1) * 365 + doy
            for y in range(1, ANIOS_HORIZONTE + 1)
            for doy in doys]
    # --n_years recorta el horizonte a los primeros N anios, para validaciones
    # cortas sin editar nada a mano.
    n_years = getattr(args, 'n_years', None)
    if n_years is not None:
        days = days[:n_years * len(doys)]
    time_series = timeseries.Timeseries(series, days, 8/60)
    #time_series = timeseries.Timeseries(series, [1, 91], 8/60)
    mine_system = mine.Mine(model)
    if args.consumption_model == 'wp2':
        wp2_json_path = resolve_wp2_json_path(args)
        time_series.mapper['Trips'] = time_series.get_trips(
            mine_system, consumption_model='wp2', wp2_consumption_json=wp2_json_path
        )
    else:
        time_series.mapper['Trips'] = time_series.get_trips(mine_system)
    return series, mine_system, time_series


def run_decomposed(args, mine_system, time_series, hybrid=False):
    """Resuelve con Nested Benders y genera la misma salida de siempre.

    Printer consume un OptModel monolitico, asi que despues de iterar se arma
    uno de solo lectura con la mejor solucion cargada (build_report_model): el
    reporte no se entera de que el problema se resolvio por bloques.
    """
    import json
    import time

    from src.io.printer import Printer
    from src.optimization.decomposition import passes as passes_module
    from src.optimization.decomposition.driver import NestedBendersSolver
    from src.optimization.opt_model import stop_file_path

    # La carpeta de salida se creaba recien en build_report_model, o sea al
    # terminar TODA la descomposicion. Crearla aca sirve para dos cosas: el
    # archivo STOP tiene donde vivir desde el minuto cero, y un
    # `| Tee-Object -FilePath <carpeta>/run.log` deja de fallar con
    # DirectoryNotFoundException al arrancar.
    os.makedirs(args.output_folder, exist_ok=True)
    stop_path = stop_file_path(args.output_folder)
    if os.path.exists(stop_path):
        os.remove(stop_path)
        print(f"[STOP] habia un archivo de parada de una corrida anterior: se borro.")
    passes_module.set_stop_file(stop_path)
    print(f"[STOP] para cortar a mano conservando la solucion, cree: {stop_path}")

    exogenous = None
    if args.fixed_stations_json:
        with open(args.fixed_stations_json, encoding='utf-8') as fh:
            crudo = json.load(fh)
        exogenous = {int(y): {k: int(v) for k, v in por_nave.items()}
                     for y, por_nave in crudo.items()}

    # En --mode hybrid la cota del LP monolitico no se calcula. No es que aporte
    # poco: no aporta nada. La fase monolitica posterior construye el MISMO
    # modelo y su propio root bound la pasa por arriba enseguida -- medido en
    # DET/Gen_Bat/241kW: 1,807,131 en 2,251 s aca, contra 2,149,198 a los 245 s
    # del solve de Gurobi que igual se iba a correr. Encima obliga a construir
    # el monolitico dos veces, que es justo el gasto que la descomposicion evita.
    #
    # Y ni siquiera son cotas del mismo problema: este LP va con X exogeno (mas
    # las cotas del presolve de capacidad), mientras que build_report_model arma
    # el OptModel sin exogenous_stations, con X libre. El conjunto factible del
    # monolitico es mas grande, asi que su optimo puede ser menor y este numero
    # NO es cota inferior valida para el. En el mismo log: 1,807,131 con X fijo
    # contra 1,623,012 de root relaxation con X libre, y la diferencia es casi
    # exactamente la apertura de naves que el X fraccionario esquiva.
    #
    # En --mode decomposed, en cambio, es la unica cota que hay: el backward no
    # la supero en ninguna corrida.
    monolithic_lp_bound = not args.no_monolithic_lp_bound and not hybrid
    if hybrid and not args.no_monolithic_lp_bound:
        print("[Hibrido] se omite la cota del LP monolitico: la fase monolitica "
              "la supera con su propio root bound en una fraccion del tiempo.")

    solver = NestedBendersSolver(
        mine_system, time_series,
        exogenous_stations_by_year=exogenous,
        gap_tol=args.gap_tol,
        max_iter=args.max_iter,
        autonomous_mode=args.autonomous_mode,
        solver_kwargs=dict(
            solvername=args.solver, gap=args.gap_tol,
            timelimit=args.solve_timelimit,
            # MIPFocus por bloque: llega a _solve como extra_options y de ahi a
            # Gurobi. Solo se pasa si se pidio, para no cambiar el default.
            **({'extra_options': {'MIPFocus': args.block_mip_focus}}
               if args.block_mip_focus is not None else {}),
        ),
        block_build_jobs=args.block_build_jobs,
        monolithic_lp_bound=monolithic_lp_bound,
        capacity_presolve=args.capacity_presolve,
        cut_type=args.cut_type,
        strengthened_timelimit=args.strengthened_timelimit,
        warm_start_cuts=args.warm_start_cuts,
        free_charging=args.free_charging,
        free_maintenance=args.free_maintenance,
        operational_bound=args.operational_bound,
        op_bound_timelimit=args.op_bound_timelimit,
        output_folder=args.output_folder,
        day_warm_start=args.day_warm_start,
        day_solver_overrides={'timelimit': args.day_timelimit, 'gap': args.day_gap},
        lb_inicial=args.lb_inicial,
        polish_each_iter=args.polish_each_iter,
        skip_last_backward=args.skip_last_backward,
        block_solver=args.block_solver,
        gcg_blocks=args.gcg_blocks,
    )
    resultado = solver.solve(verbose=True)

    print(f"[NestedBenders] termino en {resultado['iterations']} iteraciones "
          f"({resultado['total_time_sec']:.0f}s): UB={resultado['ub']:,.2f}  "
          f"LB={resultado['lb']:,.2f}  gap={resultado['gap']:.4%}"
          + ("  [INTERRUMPIDO]" if resultado['interrupted'] else ""))

    if resultado['best_full_solution'] is None:
        # Parada (STOP o Ctrl+C) antes de completar la primera pasada forward:
        # no existe ninguna trayectoria factible del horizonte, asi que no hay
        # nada que reportar. Se avisa y se sale en vez de reventar en
        # build_report_model.
        print("[STOP] Se corto antes de completar la primera iteracion: no hay "
              "solucion factible del horizonte que guardar. No se escribe salida.")
        return

    report, _informe = solver.build_report_model(args.output_folder)

    if hybrid:
        # La solucion descompuesta entra como MIP start (build_report_model deja
        # has_warm_start puesto). Gurobi arranca con un incumbente que le habria
        # costado encontrar y se dedica a cerrar la cota, que es lo que hace bien.
        t0 = time.time()
        gap_dec = resultado.get('gap')
        lb_dec = resultado.get('lb')

        # Si la descomposicion YA certifico la tolerancia, entrar al monolitico
        # es trabajo perdido: su cota propia es mucho peor que la que ya se
        # tiene (la de relajacion operacional o la del LP), no la puede usar, y
        # va a reportar un gap enorme mientras gasta el timelimit entero
        # cerrando algo que ya esta cerrado.
        saltear = (gap_dec is not None and gap_dec <= args.gap_tol
                   and not args.force_hybrid_mono)
        if saltear:
            print(f"[Hibrido] la descomposicion ya certifico gap={gap_dec:.4%} "
                  f"<= {args.gap_tol:.4%}: se OMITE la fase monolitica (no "
                  f"aportaria cota y su cota propia es peor). Usa "
                  f"--force_hybrid_mono para correrla igual.")
        else:
            print("[Hibrido] resolviendo el monolitico con la solucion descompuesta "
                  "como MIP start...")
            report.mip_focus = args.mip_focus
            report.improve_start_time = args.mono_improve_start_time

            # Corta en cuanto el incumbente alcance el gap objetivo MEDIDO
            # CONTRA LA COTA EXTERNA. Gurobi mide contra la suya, que es peor,
            # asi que sin esto no sabe cuando parar.
            if lb_dec not in (None, float('-inf')) and args.gap_tol < 1:
                report.best_obj_stop = lb_dec / (1.0 - args.gap_tol)
                print(f"[Hibrido] BestObjStop = {report.best_obj_stop:,.2f} "
                      f"(UB que cierra {args.gap_tol:.2%} contra LB={lb_dec:,.2f})")

            # La cota externa, ADEMAS, como fila del modelo. BestObjStop solo
            # dice cuando parar: no entra en la cota de Gurobi, que sigue
            # partiendo de su propio LP de raiz. Con la fila, en cambio:
            #   - es VALIDA: LB es cota inferior del optimo y toda solucion
            #     factible cumple obj >= optimo >= LB, asi que no corta ninguna;
            #   - todo LP de nodo queda con valor >= LB, asi que el BestBd que
            #     Gurobi reporta y usa para su MIPGap pasa a ser la cota REAL y
            #     el criterio de parada por gap vuelve a tener sentido;
            #   - la fila entra en presolve y en el fijado por costo reducido
            #     con un rango objetivo mucho mas chico.
            # El margen 1e-6 relativo protege de que la cota dual venga al filo
            # de las tolerancias de Gurobi. Si el monolitico diera INFACTIBLE
            # con la fila puesta, la cota no era valida: correr con
            # --no_mono_lb_cut para confirmarlo.
            if lb_dec not in (None, float('-inf')) and not args.no_mono_lb_cut:
                import pyomo.environ as _pyo
                lb_cut = lb_dec - abs(lb_dec) * 1e-6
                report.model.lb_externa = _pyo.Constraint(
                    expr=report.model.obj.expr >= lb_cut)
                print(f"[Hibrido] fila obj >= {lb_cut:,.2f} agregada al "
                      f"monolitico (cota externa; sin ella Gurobi parte de su "
                      f"LP de raiz)")

            report.solve_model(args.gap_tol, args.solver,
                               timelimit=args.mono_timelimit or args.solve_timelimit)
            mejora = resultado['ub'] - report.opt_cost_result
            print(f"[Hibrido] monolitico resuelto en {time.time() - t0:.0f}s: "
                  f"costo = {report.opt_cost_result:,.2f} "
                  f"(la descomposicion habia llegado a {resultado['ub']:,.2f}; "
                  f"mejora de {mejora:,.2f} = {mejora / resultado['ub']:.2%})")
            # Cota final = la mejor entre la externa y la de Gurobi: con la fila
            # obj >= LB, el BestBd arranca en la externa y solo puede subir.
            cotas = [c for c in (lb_dec, report.best_bound)
                     if c not in (None, float('-inf'))]
            if cotas and report.opt_cost_result:
                lb_final = max(cotas)
                origen = ("Gurobi (supero la externa)"
                          if report.best_bound is not None and lb_dec is not None
                          and report.best_bound > lb_dec + 1e-6 * abs(lb_dec)
                          else "externa (descomposicion / cota operacional)")
                gap_final = (report.opt_cost_result - lb_final) / abs(report.opt_cost_result)
                print(f"[Hibrido] cota final = {lb_final:,.2f}  [{origen}]  "
                      f"gap final = {gap_final:.4%}")

    printer = Printer(report, args.output_folder, time_series, mine_system)
    printer.create_all_plots()


def main():
    """ Main function building argument collection from setting
    default values.

    :return:
    """
    system_title = 'ELMOMINE'
    parser = argparse.ArgumentParser(description=system_title)
    parser.add_argument('--data_folder', default='data/')
    parser.add_argument('--model', default='elmo_data.xlsx')
    parser.add_argument('--series', default='time_series.xlsx')
    parser.add_argument('--output_folder', default='output/')
    parser.add_argument(
        '--days_per_year', type=int, choices=[2, 4], default=4,
        help='Dias representativos por anio. 4 (default): dias-del-anio 15, 105, '
             '196 y 288. 2: solo 15 y 196 (config historica de los escenarios '
             'P_red). Solo esos cuatro tienen perfil renovable '
             'cargado en GenProfiles; cualquier otro deja alpha_g = 0 en silencio. '
             'Pasar a 4 duplica el tamanio del modelo y baja el factor de escalado '
             'operacional de 182,5 a 91,25 dias por dia modelado.'
    )
    parser.add_argument(
        '--n_years', type=int, default=None,
        help='Recorta el horizonte a los primeros N años (2 dias representativos '
             'por año). Sin el flag se usa el horizonte completo de 11 años.'
    )
    parser.add_argument('--solver', default='glpk')
    parser.add_argument('--y_init_path', default=None,
                        help='Ruta opcional a Y.json para usar warm start en variable Y')
    parser.add_argument(
        '--init_solution_folder',
        default=None,
        help='Carpeta opcional con JSONs de variables (<VarName>.json) para warm start completo',
    )
    parser.add_argument(
        '--relax_operational', action='store_true',
        help='[--mode monolithic] relaja SOLO las variables de operacion (Y, Sv, Z, '
             'Z_swap, StartAssign, EndAssign, S, X_dch, X_ini, W) y deja enteras las '
             'de inversion (X, N_bays, N_chargers, N_batteries, n_ssee_k, R). El '
             'optimo es una cota inferior del MILP completo, mas apretada que la '
             'relajacion lineal total, que tambien afloja la inversion. NO es un plan '
             'reportable: la operacion queda fraccionaria.'
    )
    parser.add_argument(
        '--relax_integrality',
        action='store_true',
        help='Resuelve la relajación lineal del modelo sin cambiar las variables originales'
    )
    parser.add_argument('--consumption_model', choices=['wp1', 'wp2'], default='wp1',
                         help='wp1: calculo fisico interno. wp2: consumos y tiempos precalculados desde un JSON por nodo.')
    parser.add_argument('--wp2_consumption_json', default=None,
                         help='Ruta al JSON de consumos WP2 (relativa a data_folder salvo que sea absoluta). '
                              "Si se omite y --consumption_model wp2, se usa 'electric_routes_within_time.json'.")
    parser.add_argument(
        '--autonomous_mode',
        action='store_true',
        help='Escenario DET de vehiculos autonomos: durante la colacion el LHD '
             'puede ademas operar (viajar/extraer), no solo hacer swap o estar '
             'detenido. El cambio de turno (between_shifts) sigue restringido '
             'a swap-o-detenido en ambos modos.'
    )
    parser.add_argument(
        '--mccormick_degradation',
        action='store_true',
        help='[solo si hay hoja BatteryDegradation] linealiza los dos '
             'bilineales encadenados de la degradacion del pool de swap '
             '(N_ciclos*n_battery_fleet, N_total*b_bar) con la envolvente de '
             'McCormick (Camino A, ver degradacion_descomposicion_mccormick.md '
             'en la rama carga_ob_multiaño) en vez de resolverlos como '
             'restricciones cuadraticas no convexas (Gurobi NonConvex=2). El '
             'modelo queda MILP puro -- suele ser mas rapido, a costa de una '
             'aproximacion.'
    )

    parser.add_argument(
        '--mode', choices=['monolithic', 'decomposed', 'hybrid'], default='monolithic',
        help='monolithic (default): arma y resuelve el modelo completo de una vez, '
             'igual que siempre. decomposed: descomposicion temporal Nested Benders, '
             'un subproblema por año acoplado por cortes de Benders. hybrid: corre la '
             'descomposicion y le entrega su solucion al monolitico como MIP start. '
             'Los dos metodos tienen perfiles opuestos -- la descomposicion consigue '
             'un incumbente muy bueno enseguida pero su cota inferior se estanca, y el '
             'branch and bound sube la cota rapido pero le cuesta el incumbente --, '
             'asi que el hibrido le da a cada uno lo que al otro le falta. Renuncia a '
             'la ventaja de memoria: construye el monolitico completo. decomposed e '
             'hybrid requieren un solver con duales (gurobi).'
    )
    parser.add_argument(
        '--free_charging',
        action='store_true',
        help='[monolithic|decomposed|hybrid] swap liberado: el LHD puede hacer '
             'swap en cualquier intervalo salvo maintenance DET (donde la '
             'maquina esta en servicio). Por defecto rige el regimen '
             'restringido, que solo permite hacer swap en '
             'meal/road_clearing/between_shifts DET. NO cambia el esquema de '
             'detenciones: det_stop_all sigue impidiendo OPERAR durante '
             'maintenance y road_clearing. El flag conserva el nombre de la '
             'rama carga_ob_multiaño (donde libera la CARGA on-board) para que '
             'los comandos sean intercambiables entre ramas. Ver '
             'ConstraintRules.no_swap_maintenance_det en functions.py.'
    )
    parser.add_argument(
        '--free_maintenance',
        action='store_true',
        help='[monolithic|decomposed|hybrid] mantenimiento liberado: las '
             'ventanas de maintenance DET dejan de ser detenciones y el LHD '
             'puede OPERAR y hacer SWAP en ellas, como en cualquier intervalo '
             'libre. Por defecto maintenance esta en det_stop (impide operar) y '
             'fuera de toda regla de swap, lo que lo deja detenido puro. '
             'Combinable con --free_charging: juntos no dejan ninguna ventana '
             'con el swap prohibido. Ver OptSets.build_sets en functions.py.'
    )
    parser.add_argument(
        '--operational_bound',
        action='store_true',
        help='[decomposed|hybrid] antes de iterar, calcula una cota inferior '
             'resolviendo el monolitico con las binarias OPERACIONALES '
             'relajadas y las de INVERSION enteras, y la toma como LB inicial. '
             'Es MUCHO mejor que la del LP: medido en carga_ob_multiaño sobre '
             'P_red_gen_bat a 10 anios, 2.146.169 contra 1.593.859, o sea el '
             'gap del incumbente baja de 26,64%% a 1,22%%, en 34,5 min. El '
             'backward con cortes de Benders no puede superar la del LP por '
             'construccion, asi que esta es la unica forma barata de tener una '
             'cota decente. En --mode hybrid esa LB ademas entra al monolitico '
             'de la fase 2 (BestObjStop + fila obj >= LB).'
    )
    parser.add_argument(
        '--op_bound_timelimit', type=int, default=None,
        help='[con --operational_bound] timelimit en segundos de ese solve. '
             'Default 3600, PROPIO: no hereda --solve_timelimit, que es el tope '
             'por bloque anual y no tiene relacion con un solve monolitico. Si '
             'corta por tiempo la cota dual de ese momento sigue siendo valida, '
             'solo que mas floja: degrada con gracia.'
    )
    parser.add_argument(
        '--block_solver', choices=['gurobi', 'gcg'], default='gurobi',
        help='[decomposed|hybrid] solver del MILP de cada bloque anual en el '
             'forward. gcg: Dantzig-Wolfe / branch-and-price con GCG, que corre '
             'aparte en .venv_gcg (ver decomposition/gcg_block.py). El backward '
             'sigue con Gurobi. Usa --solve_timelimit y --gap_tol.'
    )
    parser.add_argument(
        '--gcg_blocks', choices=['day', 'vehicle_day'], default='day',
        help='[con --block_solver gcg] bloques del Dantzig-Wolfe. day (default): '
             '4 bloques, maestro de 36 filas (0,1%%) en el bloque del anio 4. '
             'vehicle_day: 16 bloques LHD-dia, maestro de 22.096 filas (36,5%%), '
             'porque el pool de baterias de la estacion y el balance de potencia '
             'los comparten todos los LHD intervalo por intervalo.'
    )
    parser.add_argument(
        '--polish_each_iter', action='store_true',
        help='[decomposed|hybrid] despues de cada forward, arma el monolitico con '
             'esa solucion y la pule (enteras fijas, LP sobre las continuas con '
             'todo el horizonte a la vista); el UB y la eleccion de la mejor '
             'solucion pasan a usar el costo pulido. Medido en Mina_modelo a 10 '
             'anios: el pulido final bajo el costo 8,8%%. Cuesta construir el '
             'monolitico en cada iteracion (~5 GB de pico, se libera despues).'
    )
    parser.add_argument(
        '--skip_last_backward', action='store_true',
        help='[decomposed|hybrid] la ultima iteracion no corre backward: sus '
             'cortes no los usa ningun forward y solo aportarian LB. Conviene '
             'cuando hay una cota externa fuerte (--operational_bound o '
             '--lb_inicial) que el backward no logra superar.'
    )
    parser.add_argument(
        '--mono_improve_start_time', type=float, default=None,
        help='[hybrid] Gurobi ImproveStartTime (segundos) de la fase monolitica: '
             'pasado ese tiempo Gurobi se dedica a MEJORAR el incumbente en vez '
             'de subir la cota. Con la cota externa como fila obj >= LB, 0 es '
             'razonable: la cota ya viene de afuera.'
    )
    parser.add_argument(
        '--lb_inicial', type=float, default=None,
        help='[decomposed|hybrid] cota inferior ya conocida, de una corrida '
             'anterior, que entra como LB inicial por max() y --en hybrid-- como '
             'BestObjStop y fila obj >= LB del monolitico. Sirve para no '
             'recalcular --operational_bound (determinista, ~20 min a 10 anios): '
             'pasar su valor y omitir ese flag. SOLO es valida si viene de '
             'EXACTAMENTE el mismo problema (datos, --n_years, --days_per_year, '
             '--free_charging/--free_maintenance, presolve). Una cota invalida '
             'por encima del optimo hace INFACTIBLE la fase monolitica del '
             'hibrido; si pasa, repetir con --no_mono_lb_cut para confirmarlo.'
    )
    parser.add_argument(
        '--force_hybrid_mono',
        action='store_true',
        help='[hybrid] corre la fase monolitica AUNQUE la descomposicion ya '
             'haya certificado gap <= --gap_tol. Por defecto se omite: en ese '
             'caso el monolitico no puede aportar cota (la suya es mucho peor '
             'que la de relajacion operacional, que no conoce) y solo podria '
             'mejorar el incumbente marginalmente, gastando el timelimit entero.'
    )
    parser.add_argument(
        '--no_mono_lb_cut',
        action='store_true',
        help='[hybrid] NO agrega al monolitico la fila obj >= LB con la cota '
             'externa de la descomposicion. Por defecto se agrega: toda '
             'solucion factible cumple obj >= optimo >= LB, asi que la fila no '
             'corta nada, y sin ella Gurobi arranca desde SU cota (la del LP '
             'completo, mucho peor), reporta un gap enorme y su MIPGap nunca se '
             'cumple.'
    )
    parser.add_argument(
        '--day_warm_start', choices=['off', 'first', 'always', 'fallback'],
        default='off',
        help='[decomposed|hybrid] MIP start de cada bloque anual del forward por '
             'descomposicion en DIAS (esquema --parallel_days de la rama '
             'battery_swapping, ver decomposition/day_blocks.py): fase 1 cada dia '
             'con infraestructura libre, maximo entre dias, fase 2 cada dia con '
             'la infraestructura fija, y pulido LP sobre el bloque anual. Cada '
             'sub-bloque tiene la cuarta parte de las enteras. Es una heuristica '
             '(da UB, no LB): sirve para que Gurobi tenga incumbente en los anios '
             'donde no encuentra ninguno solo. first: de entrada solo en la '
             'primera iteracion (la unica sin solucion previa), y en las '
             'siguientes como fallback. always: en cada forward. '
             'fallback: solo cuando el bloque anual llega al timelimit sin '
             'incumbente; no cuesta nada en los anios faciles.'
    )
    parser.add_argument(
        '--day_timelimit', type=int, default=120,
        help='[con --day_warm_start] timelimit en segundos de CADA solve diario '
             '(8 por anio: 4 dias x 2 fases). Propio, no hereda '
             '--solve_timelimit: el sub-bloque solo tiene que encontrar un '
             'punto, no demostrar el gap del bloque anual.'
    )
    parser.add_argument(
        '--day_gap', type=float, default=0.05,
        help='[con --day_warm_start] MIPGap de cada solve diario. Mas holgado '
             'que --gap_tol por la misma razon: medido en el anio 1, con 1%% '
             'cada dia encontraba el incumbente y gastaba el resto del tope sin '
             'poder cerrar el gap.'
    )
    parser.add_argument(
        '--block_mip_focus', type=int, choices=[0, 1, 2, 3], default=None,
        help='[decomposed|hybrid] Gurobi MIPFocus de CADA bloque anual del '
             'forward/backward. Distinto de --mip_focus, que solo afecta al '
             'monolitico de la fase 2. Sin el flag, Gurobi usa su default (0, '
             'balanceado). 1 prioriza ENCONTRAR incumbentes por sobre cerrar la '
             'cota: es lo que hay que usar cuando un bloque llega al '
             '--solve_timelimit sin ninguna solucion factible, que hace fallar '
             'al forward entero porque el bloque queda con variables sin valor. '
             'Pasa en los anios de mayor meta de produccion, y mas todavia con '
             '--free_charging/--free_maintenance, que agrandan el arbol.'
    )
    parser.add_argument(
        '--max_iter', type=int, default=20,
        help='[--mode decomposed] tope de iteraciones forward/backward.'
    )
    parser.add_argument(
        '--gap_tol', type=float, default=0.01,
        help='[--mode decomposed] gap relativo (UB-LB)/|UB| con el que se corta.'
    )
    parser.add_argument(
        '--solve_timelimit', type=int, default=900,
        help='[--mode decomposed] limite de tiempo, en segundos, de CADA resolucion '
             'de bloque anual (no del total).'
    )
    parser.add_argument(
        '--block_build_jobs', type=int, default=None,
        help='[--mode decomposed] procesos para construir los bloques anuales en '
             'paralelo. Sin el flag, min(años, cpus); 1 fuerza secuencial (util para '
             'depurar).'
    )
    parser.add_argument(
        '--fixed_stations_json', default=None,
        help='[--mode decomposed] JSON {"<año>": {"<nave>": 0/1}} con la apertura de '
             'naves, que en modo descompuesto es exogena. Si se omite se infiere de '
             'la hoja StationAssignment: se construye toda nave con al menos un equipo '
             'asignado.'
    )
    parser.add_argument(
        '--mono_timelimit', type=int, default=None,
        help='Timelimit en segundos del solve monolitico: en --mode monolithic '
             '(por defecto 172800 s, 48 h) y en la fase monolitica de --mode hybrid '
             '(por defecto el valor de --solve_timelimit). Sirve para el benchmark '
             'a presupuesto igual entre monolitico solo e hibrido.'
    )
    parser.add_argument(
        '--mip_focus', type=int, choices=[0, 1, 2, 3], default=3,
        help='Gurobi MIPFocus del solve monolitico (monolithic e hybrid). 3 (default, '
             'el historico) prioriza la cota; 1 prioriza encontrar incumbentes, que '
             'es lo que le falta al hibrido cuando llega con un MIP start bueno y no '
             'lo mejora.'
    )
    parser.add_argument(
        '--capacity_presolve', choices=['peak', 'all', 'off'], default='peak',
        help='[--mode decomposed] presolve de capacidad de subestacion (ver '
             'NestedBendersSolver._capacity_presolve): antes de iterar resuelve, '
             'por nave, el minimo n_ssee_k que hace factible a un anio con todo el '
             'estado heredado libre, y lo impone como cota inferior en el anio 1 y '
             'en el LP monolitico. Es una desigualdad valida (no invalida UB ni LB) '
             'y evita que la capacidad del pico de produccion llegue al anio 1 via '
             'cortes de factibilidad, cada uno de los cuales reinicia el forward. '
             'peak (default): solo el anio de mayor meta de produccion. all: todos '
             'los anios, cota mas fuerte pero |naves|*|anios| MILP. off: sin presolve.'
    )
    parser.add_argument(
        '--cut_type', choices=['benders', 'strengthened'], default='benders',
        help='[--mode decomposed/hybrid] familia de cortes del backward pass '
             '(Lara et al. 2018, EJOR 271:1037-1054). benders (default, ec. 59): '
             'la constante sale de la relajacion LINEAL del bloque hijo; barato, '
             'pero el LB converge como mucho a la relajacion lineal del '
             'monolitico. strengthened (ec. 63): reusa el mismo mu del dual del '
             'LP y recalcula la constante con la relajacion LAGRANGEANA, que '
             'conserva la integralidad; un MILP extra por bloque, sin el bucle de '
             'subgradiente del corte lagrangeano completo. Es el corte que rompe '
             'ese techo cuando la relajacion lineal es floja, que es el caso de '
             'este modelo (LP 1,807,131 contra 2,212,229 que Gurobi prueba).'
    )
    parser.add_argument(
        '--strengthened_timelimit', type=int, default=300,
        help='[--cut_type strengthened] segundos por MILP lagrangeano. Cortarlo '
             'NO invalida el corte: se usa la cota dual del solver, que subestima '
             'siempre, y si no llega a superar Phi^LP el corte queda igual al de '
             'Benders puro.'
    )
    parser.add_argument(
        '--warm_start_cuts', action='store_true',
        help='[--mode decomposed/hybrid] Accelerated Nested Decomposition (Lara '
             'et al. 2018, sec. 5.3): antes de la primera pasada forward corre un '
             'backward completo sobre la trayectoria de la relajacion lineal del '
             'monolitico, para que alpha no arranque sin cotas. En el paper baja '
             'de 7 a 4 iteraciones para el mismo gap, y la ganancia es mayor en la '
             'instancia grande. Reusa el LP que --no_monolithic_lp_bound ya '
             'resolvia, asi que no cuesta un solve extra.'
    )
    parser.add_argument(
        '--no_monolithic_lp_bound', action='store_true',
        help='[--mode decomposed] no calcular la relajacion lineal del monolitico '
             'como cota inferior inicial. Por defecto SI se calcula: suele ser una '
             'cota mucho mejor que la del backward y cuesta un solo LP, pero obliga '
             'a construir el monolitico completo, que es el gasto de memoria que la '
             'descomposicion evita. En --mode hybrid NO se calcula nunca (la fase '
             'monolitica la supera con su propio root bound), asi que el flag no '
             'tiene efecto ahi.'
    )

    args = parser.parse_args()
    series, mine_system, time_series = build_mine(args)

    if args.mode in ('decomposed', 'hybrid'):
        run_decomposed(args, mine_system, time_series, hybrid=(args.mode == 'hybrid'))
        return

    gap= 1/100;
    solver_name=args.solver
    output_folder=args.output_folder
    y_init_path=args.y_init_path
    init_solution_folder=args.init_solution_folder
    opt = OptimizationModel(
        mine_system,
        time_series,
        gap,
        solver_name,
        output_folder,
        y_init_path=y_init_path,
        init_solution_folder=init_solution_folder,
        relax_integrality=args.relax_integrality,
        relax_operational=args.relax_operational,
        autonomous_mode=args.autonomous_mode,
        mccormick_degradation=args.mccormick_degradation,
        mip_focus=args.mip_focus,
        free_charging=args.free_charging,
        free_maintenance=args.free_maintenance,
        **({'timelimit': args.mono_timelimit} if args.mono_timelimit else {}),
    )


if __name__ == '__main__':
    main()
