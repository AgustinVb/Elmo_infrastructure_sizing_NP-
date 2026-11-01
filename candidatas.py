"""Candidatas de inversion alrededor del incumbente (busqueda local de UB).

DCH 2C: el incumbente compra el 2o cargador de las estaciones 1 y 2 en el año 7
(1,555 M descontados, ~ todo el gap) porque ese año (produccion maxima) la
bateria llega degradada (430 de 482 kWh); con 1 cargador por estacion y bateria
nueva los dias del año 7 son factibles en < 40 s (diagnostico_año.py, escenario
C). Cada candidata = calendario de reemplazos x cargadores desde --desde. Para
cada una:

  1. trayectoria de b_bar con la energia de los dias del incumbente
     (LShaped._b_bar_real); sin trayectoria la candidata no cabe en el maestro;
  2. costo estimado = UB + diferencia de inversion (coeficientes del maestro);
     se descartan las que quedan bajo la LB conocida (no pueden ser factibles)
     o sobre la UB;
  3. en orden de costo estimado: dias MILP con la inversion fija + pulido
     (LShaped.evaluar_inversion), partiendo de la operacion del incumbente.

    python -u candidatas.py --data_folder data/Resultados_finales_tesis/DCH/Bateria_482/Costo_fijo/2C/P_red \\
        --free_charging --pause_scheme dch --n_years 11 --consumption_model wp2 \\
        --solucion <incumbente> --ub_esperado <costo> --mejor_lb <LB> \\
        --reemplazos "5,7|5,7,11|5,7,10|6,8" --cargadores "1,1,1|2,1,1|1,2,1" --out <carpeta ASCII>
"""
import argparse
import itertools
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
from src.optimization.decomposition import particion                    # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.decomposition.lshaped import LShaped               # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--model", default="elmo_data.xlsx")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--days_per_year", type=int, choices=[2, 4], default=4)
    ap.add_argument("--consumption_model", choices=["wp1", "wp2"], default="wp1")
    ap.add_argument("--wp2_consumption_json", default=None)
    ap.add_argument("--free_charging", action="store_true")
    ap.add_argument("--free_maintenance", action="store_true")
    ap.add_argument("--pause_scheme", choices=["det", "dch"], default="det")
    ap.add_argument("--solucion", required=True, help="carpeta con los JSON del incumbente")
    ap.add_argument("--ub_esperado", type=float, default=None)
    ap.add_argument("--mejor_lb", type=float, default=None,
                    help="LB valida: las candidatas estimadas bajo ella no se evaluan")
    ap.add_argument("--reemplazos", default="5,7|5,7,11|5,7,10|6,8",
                    help="calendarios de reemplazo separados por |")
    ap.add_argument("--desde", type=int, default=7, help="primer año con los cargadores de --cargadores")
    ap.add_argument("--cargadores", default="1,1,1|2,1,1|1,2,1",
                    help="N_chargers por estacion desde --desde, separados por |")
    ap.add_argument("--excluir", default="", help="candidatas 'reemplazos/cargadores' a saltar, separadas por |")
    ap.add_argument("--t", type=float, default=600.0, help="tope por MILP del dia")
    ap.add_argument("--gap", type=float, default=0.01)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cpu", type=int, default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model=a.model, series="time_series.xlsx",
                   consumption_model=a.consumption_model,
                   wp2_consumption_json=a.wp2_consumption_json, n_years=a.n_years,
                   days_per_year=a.days_per_year)
    _s, ms, ts = build_mine(ns)
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=a.free_charging,
                  free_maintenance=a.free_maintenance, pause_scheme=a.pause_scheme)
    info, por_var = particion.export(om.model, os.path.join(a.out, "_mps"), mode="year_day")
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, cpu=a.cpu, out=a.out)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    UB0, completo, _e0 = bd.cargar_incumbente(vec0)
    print(f"[CA] incumbente pulido {UB0:,.2f} (esperado "
          f"{'-' if a.ub_esperado is None else f'{a.ub_esperado:,.2f}'})", flush=True)
    if a.ub_esperado is not None and abs(UB0 - a.ub_esperado) > 1e-6 * abs(a.ub_esperado):
        print("[CA] ABORTA: el incumbente no se reconstruye bien")
        return 1
    ls = LShaped(bd, om, por_var, out=a.out)
    ls.tiempo_max = float("inf")

    col = {n: j for j, n in enumerate(P.nombre)}
    # Años desde N_chargers: b_bar(1) esta fijo y no es columna (con los años de
    # b_bar el primer Delta_N_chargers salia del año 2 y la candidata rompia
    # Delta = N(y) - N(y-1)).
    nc = [n[len("N_chargers("):-1].rsplit("_", 1) for n in col if n.startswith("N_chargers(")]
    años = sorted({int(y) for _k, y in nc})
    estaciones = sorted({k for k, _y in nc})
    ent_m = [j for j in P.maestro_vars if P.vtype[j] != "C"]

    def armar(reemplazos, cargadores):
        x = completo.copy()
        for y in años:
            j = col.get(f"R({y})")
            if j is not None:
                x[j] = 1.0 if y in reemplazos else 0.0
            elif y in reemplazos:
                return None                     # R(y) fijo en el modelo (año 1)
        for k, nk in zip(estaciones, cargadores):
            prev = 0
            for y in años:
                n = x[col[f"N_chargers({k}_{y})"]] if y < a.desde else nk
                x[col[f"N_chargers({k}_{y})"]] = n
                jd = col.get(f"Delta_N_chargers({k}_{y})")
                if jd is not None:
                    if n < prev:
                        return None             # los cargadores no se retiran
                    x[jd] = n - prev
                prev = n
        return x

    cands = []
    for rs, cs in itertools.product(a.reemplazos.split("|"), a.cargadores.split("|")):
        reemplazos = {int(r) for r in rs.split(",") if r}
        cargadores = [int(c) for c in cs.split(",")]
        nombre = f"R{{{rs}}} N{a.desde}+={cs}"
        if f"{rs}/{cs}" in a.excluir.split("|"):
            print(f"[CA] {nombre}: excluida", flush=True)
            continue
        x = armar(reemplazos, cargadores)
        if x is None:
            print(f"[CA] {nombre}: no se puede armar", flush=True)
            continue
        delta = float(np.dot(P.obj[ent_m], x[ent_m] - completo[ent_m]))
        real = ls._b_bar_real(x, completo)
        if real is None:
            print(f"[CA] {nombre}: estimado {UB0 + delta:,.0f}; sin trayectoria de b_bar con la "
                  "energia del incumbente (descartada)", flush=True)
            continue
        for y, j in ls._cols("b_bar").items():
            x[j] = real[y]
        est = UB0 + delta
        tray = " ".join(f"{real[y]:.0f}" for y in sorted(real))
        if a.mejor_lb is not None and est < a.mejor_lb:
            veredicto = "bajo la LB: no puede ser factible"
        elif est >= UB0:
            veredicto = "no mejora la UB"
        else:
            veredicto = "a evaluar"
            cands.append((est, nombre, x))
        print(f"[CA] {nombre}: estimado {est:,.0f} ({delta:+,.0f}); b_bar {tray}; {veredicto}",
              flush=True)

    resultados = []
    for est, nombre, x in sorted(cands, key=lambda c: c[0]):
        print(f"[CA] === {nombre} (estimado {est:,.0f}, {time.time() - t0:.0f}s)", flush=True)
        t = time.time()
        costo, _vec = ls.evaluar_inversion(x, a.t, a.gap)
        print(f"[CA] {nombre}: {'sin operacion factible' if costo is None else f'{costo:,.2f}'} "
              f"en {time.time() - t:.0f}s; UB {bd.UB:,.2f}", flush=True)
        resultados.append({"candidata": nombre, "estimado": est, "costo": costo})
        with open(os.path.join(a.out, "candidatas.json"), "w", encoding="utf-8") as f:
            json.dump({"ub_inicial": UB0, "ub": bd.UB, "resultados": resultados}, f, indent=1)

    if bd.UB < UB0 * (1 - 1e-7):
        from src.io.printer import Printer
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            if vd is not None and not vd.fixed:
                vd.set_value(round(bd.mejor_vec[j]) if not vd.is_continuous() else bd.mejor_vec[j],
                             skip_validation=True)
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        print(f"[CA] solucion nueva en {destino}", flush=True)
    print(f"[CA] FINAL: UB {UB0:,.2f} -> {bd.UB:,.2f} ({(UB0 - bd.UB) / UB0:.3%}) en "
          f"{time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
