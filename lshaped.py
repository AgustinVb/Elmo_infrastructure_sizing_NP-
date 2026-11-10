"""L-shaped entero multicorte en branch-and-cut (lshaped_entero_swap.md), sobre el
Benders por (año, dia) de BendersDias. Ver src/optimization/decomposition/lshaped.py.

  Fase 1: etapas 1 y 2 de BendersDias (maestro continuo y luego entero, cortes LP
          de los dias) con el corte de cargas en los dias, funcion de b_bar[y]
          (envolvente de la cota por LHD) -> LB y cortes.
  Fase 2: branch-and-cut del maestro entero, con niveles de capacidad B_y, y
          callback: cortes LP (envolvente y escalon del nivel), y si no
          separan, cortes enteros locales con el esquema de Riley et al. (2025).
          UB afuera de Gurobi (dias MILP + pulido).

    python -u lshaped.py --data_folder data/Tesis_final/Mina_modelo_v4/<caso> \\
        --solucion <JSON del incumbente> --ub_esperado <costo> --out <carpeta> --horas 10
"""
import argparse
import json
import os
import sys
import time
from argparse import Namespace

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from pyomo.environ import value                                          # noqa: E402

from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.decomposition.lshaped import LShaped               # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def guardar_tray_op(bd, por_var, lb, out):
    """<out>/tray_op.pkl con la inversion del maestro entero al cerrar la Fase 1
    (el optimo de la relajacion operacional, salvo la tolerancia de la etapa 2),
    como {variable: {indice: valor}}; NestedBenders._cargar_tray_op la pasa al
    formato de extract_state con los state_links de sus bloques."""
    import pickle
    P = bd.P
    cota = bd.maestro.resolver(entero=True)
    xm, _e, _th = bd.maestro.punto()
    valores = {}
    for j in P.maestro_vars:
        vd = por_var.get(P.nombre[j])
        if vd is None:
            continue
        v = float(round(xm[j])) if not vd.is_continuous() else float(xm[j])
        nombre, idx = vd.parent_component().name, vd.index()
        if idx is None:
            valores[nombre] = v
        else:
            valores.setdefault(nombre, {})[idx if isinstance(idx, tuple) else (idx,)] = v
    ruta = os.path.join(out, "tray_op.pkl")
    with open(ruta, "wb") as f:
        pickle.dump({"vars": valores, "lb_operational": lb}, f, protocol=pickle.HIGHEST_PROTOCOL)
    resumen = {}
    for fam in ("N_batteries", "N_bays", "N_chargers", "R", "n_ssee_k"):
        v = valores.get(fam)
        if isinstance(v, dict):
            resumen[fam] = {"_".join(str(i) for i in k): x for k, x in sorted(v.items(), key=str) if x}
    print(f"[LS] inversion del maestro entero (cota {cota:,.2f}) en {ruta}: "
          + "; ".join(f"{fam} {d}" for fam, d in resumen.items()), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, choices=[1, 2, 4], default=4,
                    help="dias representativos por año (setup.py): 4 = 15, 105, 196, 288; 2 = 15 y 196 (sin y con cobro de punta)")
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp1")
    ap.add_argument("--pause_scheme", choices=["det", "dch"], default="det",
                    help="esquema de detenciones (DCH: dch, con el swap libre fuera de mantencion)")
    ap.add_argument("--solucion", default=None,
                    help="carpeta con los JSON de un incumbente (opcional: sin el, parte de cero)")
    ap.add_argument("--ub_esperado", type=float, default=None)
    ap.add_argument("--solo_validar", action="store_true",
                    help="solo reconstruye --solucion en el monolitico (todas las enteras fijas, LP) "
                         "y la compara con --ub_esperado")
    ap.add_argument("--solo_mejorar", action="store_true",
                    help="solo el paso final de mejorar UB sobre --solucion (sin Fase 1 ni 2)")
    ap.add_argument("--mejor_ub", type=float, default=None, help="mejor UB conocida, solo para comparar")
    ap.add_argument("--mejor_lb", type=float, default=None, help="mejor LB conocida, solo para comparar")
    ap.add_argument("--out", required=True)
    ap.add_argument("--horas", type=float, default=10.0, help="tiempo total")
    ap.add_argument("--jobs", type=int, default=8, help="dias resueltos a la vez")
    ap.add_argument("--M", type=float, default=1e6, help="penalidad de los enlaces elasticos")
    ap.add_argument("--alpha", type=float, default=0.5, help="level-set de la Fase 1")
    ap.add_argument("--max_it", default="30,60", help="iteraciones de las etapas 1 y 2 (Fase 1)")
    ap.add_argument("--alfas", default="0.1,0.01,0", help="gaps de los MILP del dia (Riley)")
    ap.add_argument("--t_inicial", type=float, default=120.0, help="tiempo inicial por MILP del dia")
    ap.add_argument("--t_max_dia", type=float, default=1920.0, help="tope del tiempo por MILP del dia")
    ap.add_argument("--ub_timelimit", type=float, default=120.0)
    ap.add_argument("--ub_gap", type=float, default=0.01)
    ap.add_argument("--ub_timelimit_inicial", type=float, default=300.0,
                    help="tiempo por dia MILP al evaluar la UB inicial de la DP")
    ap.add_argument("--pasadas_dp", type=int, default=4)
    ap.add_argument("--mejora", default="600,900",
                    help="tiempo por dia MILP de cada pasada final de mejorar UB (vacio: no se hace); "
                         "la segunda y siguientes solo si la anterior mejoro y queda tiempo de --horas")
    ap.add_argument("--gap", type=float, default=1e-4, help="parada (UB - LB) / UB")
    ap.add_argument("--hilos_maestro", type=int, default=8)
    ap.add_argument("--cpu", type=int, default=None, help="nucleos para los dias (por defecto todos)")
    ap.add_argument("--p_ssee_max", type=float, default=1500.0, help="tope de la subestacion (kW)")
    ap.add_argument("--por_estacion", action="store_true",
                    help="macrobloques: cada (año, dia) partido en un bloque por estacion y uno de "
                         "red, acoplados en el maestro por la potencia de cada intervalo y la "
                         "produccion del dia (BendersDias, Particion)")
    ap.add_argument("--capacidad", default="station_1=1",
                    help="cotas del presolve de capacidad (n_ssee_k >= n), k=n por coma")
    ap.add_argument("--cortes_capacidad", default=None,
                    help="JSON de cortes_capacidad.py: N[k,y] >= n minimo probado por estacion "
                         "y año (cota inferior de la columna del maestro, valida en todo diseño)")
    ap.add_argument("--corte_produccion", default=None,
                    help="JSON de cortes_capacidad.py --produccion: meta del dia <= produccion "
                         "maxima de las estaciones segun sus baterias y b_bar "
                         "(LShaped.agregar_corte_produccion)")
    ap.add_argument("--solo_fase1", action="store_true",
                    help="solo la Fase 1, sin evaluar UB: es la cota por relajacion operacional "
                         "(inversion entera, dias LP). Escribe <out>/tray_op.pkl con la inversion "
                         "entera final de la etapa 2, para setup.py --tray_op_archivo")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model=a.consumption_model,
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True,
                  pause_scheme=a.pause_scheme)
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    capacidad = {k: int(n) for k, n in (p.split("=") for p in a.capacidad.split(",") if p)}
    bd = BendersDias(info, om.model, por_var, M=a.M, jobs=a.jobs, alpha=a.alpha,
                     cotas_capacidad=capacidad, out=a.out, cpu=a.cpu, por_estacion=a.por_estacion)
    P = bd.P
    costo0 = float("inf")
    cortes_cap = {}
    if a.cortes_capacidad:
        with open(a.cortes_capacidad, encoding="utf-8") as f:
            cortes_cap = json.load(f)["cortes"]
        n_sube = 0
        for j in P.maestro_vars:
            n_min = cortes_cap.get(P.nombre[j])
            if n_min is not None and n_min > P.lb[j]:
                # Tambien en P.lb: las copias de los dias toman sus rangos de ahi.
                P.lb[j] = n_min
                bd.maestro.x[bd.maestro.pos[int(j)]].LB = n_min
                n_sube += 1
        bd.maestro.m.update()
        print(f"[LS] cortes de capacidad de {a.cortes_capacidad}: {n_sube} cotas inferiores "
              "del maestro suben", flush=True)

    def escribir(vec):
        """Solucion en JSON (usable como --solucion)."""
        from src.io.printer import Printer
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            if vd is not None and not vd.fixed:
                vd.set_value(round(vec[j]) if not vd.is_continuous() else vec[j], skip_validation=True)
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        return destino
    if a.solucion:
        vec0 = np.zeros(P.nv)
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            v = value(vd, exception=False) if vd is not None else None
            vec0[j] = 0.0 if v is None else v
        viola = [f"{n}={vec0[j]:g}<{cortes_cap[n]}" for j, n in enumerate(P.nombre)
                 if n in cortes_cap and vec0[j] < cortes_cap[n] - 1e-6]
        if viola:
            # Una solucion factible que viola un corte prueba que el corte es invalido.
            print(f"[LS] ABORTA: la incumbente viola cortes de capacidad: {viola}")
            return 1
        costo0, completo0, e0 = bd.cargar_incumbente(vec0)
        if P.por_estacion:
            # La produccion se reparte entre estaciones (daily_extraction es de toda la
            # mina): en la UB cada estacion produce al menos lo de la incumbente, que es
            # alcanzable; el reparto natural (meta de sus nodos) puede no serlo.
            P.piso = {k: float(P.energia(completo0, k)) for k in P.piso_natural}
        desvio = abs(costo0 - a.ub_esperado) / abs(a.ub_esperado)
        print(f"[LS] incumbente pulido {costo0:,.2f} (esperado {a.ub_esperado:,.2f}, desvio {desvio:.2e})",
              flush=True)
        if desvio > 1e-6:
            print("[LS] ABORTA: el incumbente no se reconstruye bien")
            return 1
        if a.solo_validar:
            print(f"[LS] VALIDADA: la solucion es factible en el monolitico y cuesta {costo0:,.2f}", flush=True)
            return 0

    ls = LShaped(bd, om, por_var, alfas=[float(x) for x in a.alfas.split(",")],
                 t_inicial=a.t_inicial, t_max_dia=a.t_max_dia, ub_timelimit=a.ub_timelimit,
                 ub_gap=a.ub_gap, out=a.out, ub_timelimit_inicial=a.ub_timelimit_inicial,
                 pasadas_dp=a.pasadas_dp, p_ssee_max=a.p_ssee_max)
    ls.t0 = t0
    ls.ub_referencia = a.mejor_ub      # solo para detectar una LB invalida
    if ms.battery_degradation is not None:
        from src.optimization.decomposition.degradacion_dp import parametros_desde
        ls.P_dp = parametros_desde(ms, ts)     # reparacion de inversiones en la version 5
    if a.solo_mejorar:
        ls.mejorar_ub([float(x) for x in a.mejora.split(",")])
        resumen = {"ub_inicial": costo0, "ub_final": bd.UB, "tiempo_total_s": time.time() - t0,
                   "args": vars(a)}
        if bd.UB < costo0 * (1 - 1e-7):
            resumen["solucion"] = escribir(bd.mejor_vec)
        with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
            json.dump(resumen, f, indent=2, default=str)
        print(f"[LS] FINAL mejorar UB: {costo0:,.2f} -> {bd.UB:,.2f} ({(costo0 - bd.UB) / costo0:.3%}, "
              f"{time.time() - t0:.0f}s)", flush=True)
        return 0
    ls.agregar_corte_cargas()
    if a.corte_produccion:
        viola = ls.agregar_corte_produccion(a.corte_produccion, completo0 if a.solucion else None)
        if viola:
            print(f"[LS] ABORTA: la incumbente viola el corte de produccion: {viola}")
            return 1

    # Fase 1. Sin incumbente arranca en el optimo del maestro relajado (con la
    # cota de theta de cada dia): es un punto factible del maestro.
    if not a.solucion:
        bd._cota_theta()
        bd.maestro.resolver(entero=False)
        completo0, e0, _ = bd.maestro.punto()
        print("[LS] sin incumbente: la Fase 1 arranca en el maestro relajado", flush=True)
    # La DP de la UB inicial (ub_inicial_dp) es de UNA estacion: con varias (DCH,
    # 3 estaciones) sus mapas {año: columna} de N_batteries/N_bays/N_chargers se
    # quedarian con una sola. Sin incumbente, la UB sale entonces de evaluar las
    # inversiones del maestro entero en la etapa 2 (dias MILP + pulido).
    estaciones = {n.rsplit("_", 1)[0] for n in P.nombre if n.startswith("N_batteries(")}
    usar_dp = not a.solucion and len(estaciones) == 1 and not a.solo_fase1
    ub_etapa2 = not a.solucion and not usar_dp and not a.solo_fase1
    if ub_etapa2:
        print(f"[LS] {len(estaciones)} estaciones: sin la DP de UB inicial (es de una estacion); "
              "la UB sale de las inversiones de la etapa 2", flush=True)
    it1, it2 = (int(x) for x in a.max_it.split(","))
    L = bd.correr(completo0, e0, max_it=(it1, it2, 0), tol=(1e-3, 1e-4), etapas=(1, 2),
                  ub_en_etapa2=ub_etapa2, ub_timelimit=a.ub_timelimit_inicial, ub_gap=a.ub_gap)
    lb1 = L.get(2)
    print(f"[LS] Fase 1 ({time.time() - t0:.0f}s): LB {lb1:,.2f}, {bd.maestro.n_cortes} cortes",
          flush=True)
    if a.solo_fase1:
        guardar_tray_op(bd, por_var, lb1, a.out)
        return 0
    # UB inicial con la DP (seccion 5.1), salvo que ya haya incumbente.
    if usar_dp:
        ls.ub_inicial_dp(ms, ts)
    if bd.UB < 1e20:
        print(f"[LS] UB inicial {bd.UB:,.2f} ({time.time() - t0:.0f}s); gap con la LB de la Fase 1 "
              f"{(bd.UB - lb1) / bd.UB:.3%}", flush=True)
    else:
        print(f"[LS] sin UB inicial ({time.time() - t0:.0f}s): la daran las soluciones del "
              f"branch-and-cut", flush=True)

    # Fase 2
    ls.preparar_maestro()
    lb2, status = ls.branch_and_cut(a.horas * 3600, gap_obj=a.gap, hilos=a.hilos_maestro)
    lb = max(x for x in (lb1, lb2) if x is not None)
    print(f"[LS] Fase 2 terminada (status {status}, {time.time() - t0:.0f}s)", flush=True)
    ub_bc = bd.UB
    if a.mejora:
        ls.mejorar_ub([float(x) for x in a.mejora.split(",")])
        print(f"[LS] mejorar UB ({time.time() - t0:.0f}s): {ub_bc:,.2f} -> {bd.UB:,.2f}", flush=True)

    if bd.mejor_vec is not None and bd.UB < costo0 * (1 - 1e-7):
        destino = escribir(bd.mejor_vec)
        print(f"[LS] UB mejorada {costo0:,.2f} -> {bd.UB:,.2f}; JSON en {destino}", flush=True)

    resumen = {"ub_inicial": costo0, "ub_bc": ub_bc, "ub_final": bd.UB, "lb_fase1": lb1, "lb_fase2": lb2,
               "lb_final": lb, "gap_final": (bd.UB - lb) / bd.UB, "status_maestro": status,
               "cortes_lp_bc": ls.n_lp, "cortes_nivel": ls.n_niv, "cortes_enteros": ls.n_ent,
               "cortes_descartados_big_M": ls.n_sin_cota, "callbacks": ls.n_cb,
               "aceptaciones_silenciosas": ls.n_silenciosas,
               "t_lp": ls.t_lp, "t_milp": ls.t_milp, "n_corte": ls.n_corte,
               "cap_corte": ls.cap, "tiempo_total_s": time.time() - t0, "args": vars(a)}
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=2, default=str)
    def num(v):
        return f"{v:,.2f}" if abs(v) < 1e20 else "-"
    ub_fin = bd.UB if bd.UB < 1e20 else float("inf")
    print(f"[LS] FINAL: UB {num(bd.UB)}  LB {lb:,.2f}  gap {(ub_fin - lb) / ub_fin if ub_fin < 1e20 else 1:.3%}  "
          f"(Fase 1 {lb1:,.2f}; cortes LP {ls.n_lp}, por nivel {ls.n_niv}, enteros {ls.n_ent})", flush=True)
    if a.mejor_ub and a.mejor_lb:
        print(f"[LS] contra lo mejor conocido: UB {a.mejor_ub:,.2f}  LB {a.mejor_lb:,.2f}  gap "
              f"{(a.mejor_ub - a.mejor_lb) / a.mejor_ub:.3%}  |  UB "
              f"{(f'{bd.UB - a.mejor_ub:+,.2f}' if bd.UB < 1e20 else 'sin UB')}  "
              f"LB {lb - a.mejor_lb:+,.2f}  gap combinado "
              f"{(min(bd.UB, a.mejor_ub) - max(lb, a.mejor_lb)) / min(bd.UB, a.mejor_ub):.3%}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
