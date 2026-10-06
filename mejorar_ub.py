"""Mejora el incumbente de Nested Benders reoptimizando la operacion de algunos
dias como MILP con la inversion FIJA.

diagnostico_gap_dias.py mostro que en el año 4 la operacion del incumbente de
r4 no es la optima para su propia inversion (B - mejor MILP = 12.016, con la
cota MILP igual al incumbente MILP: optimo probado). Aca se resuelven esos dias
con el incumbente como MIP start, se reemplaza su operacion y se PULE el
monolitico con todas las enteras fijas (LP de las continuas), que reconcilia la
energia y la degradacion entre años. Si el costo baja, la solucion se escribe
en JSON: sirve como --incumbente_inicial de setup.py (carpeta, no .pkl).

    python -u mejorar_ub.py --respaldo output/.../P_red_only_r4/incumbente_respaldo.pkl \
        --anios 4 --out output/.../mejorar_ub_r4
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

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", default="data/Tesis_final/Mina_modelo/RED")
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--respaldo", default=None, help="incumbente_respaldo.pkl de Nested Benders")
    ap.add_argument("--solucion", default=None,
                    help="carpeta con los JSON de una solucion (alternativa a --respaldo)")
    ap.add_argument("--ub_esperado", type=float, default=None,
                    help="costo esperado con --solucion; el pulido tiene que reproducirlo")
    ap.add_argument("--anios", default="4", help="años cuyos dias se reoptimizan, por coma")
    ap.add_argument("--timelimit", type=float, default=900)
    ap.add_argument("--gap", type=float, default=1e-4)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--dias_guardados", default=None,
                    help="dias.npz de una corrida anterior: no se vuelven a resolver")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if (a.respaldo is None) == (a.solucion is None):
        ap.error("se necesita exactamente uno de --respaldo o --solucion")
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    ns = Namespace(data_folder=a.data_folder, model="elmo_data.xlsx",
                   series="time_series.xlsx", consumption_model="wp1",
                   wp2_consumption_json=None, n_years=a.n_years, days_per_year=4)
    _s, ms, ts = build_mine(ns)
    # Con --solucion, OptModel carga los JSON como warm start (las enteras sin
    # valor quedan en 0, igual que en benders_dias.py).
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    if a.respaldo:
        esperado = cargar_respaldo(om, ms, ts, a.respaldo)["best_cost"]
    else:
        esperado = a.ub_esperado
    info, por_var = gb.export(om.model, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, om.model, por_var, jobs=a.jobs, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    UB0, xB, _eB = bd.cargar_incumbente(vec0)
    print(f"[ub] incumbente pulido {UB0:,.2f} (esperado "
          f"{'-' if esperado is None else f'{esperado:,.2f}'})", flush=True)
    if esperado is not None and abs(UB0 - esperado) > 1e-6 * abs(UB0):
        print("[ub] ABORTA: el incumbente no se reconstruye bien")
        return 1

    anios = {int(x) for x in a.anios.split(",")}
    bloques = [b for b, k in enumerate(P.claves) if eval(k)[0] in anios]

    def tarea(b):
        s = bd.subs[b]
        opex_B = float(np.dot(P.obj[s.loc], xB[s.loc]))
        res = s.ub(xB, a.timelimit, a.gap)
        res["B"] = opex_B
        return res

    ruta_dias = os.path.join(a.out, "dias.npz")
    if a.dias_guardados:
        # Operaciones MILP de una corrida anterior (mismo modelo e incumbente).
        guardado = np.load(a.dias_guardados, allow_pickle=True)
        filas = [guardado[P.claves[b]].item() for b in bloques]
        print(f"[ub] {len(filas)} dias leidos de {a.dias_guardados}", flush=True)
    else:
        filas = bd._paralelo(tarea, bloques)
        np.savez(ruta_dias, **{P.claves[b]: np.array(r, dtype=object)
                               for b, r in zip(bloques, filas)})
    por_anio = {}
    for b, res in zip(bloques, filas):
        if not res["ok"]:
            print(f"[ub] {P.claves[b]}: sin solucion (status {res['status']})", flush=True)
            continue
        mejora = res["B"] - res["valor"]
        print(f"[ub] {P.claves[b]:>10}  B {res['B']:>12,.2f}  MILP {res['valor']:>12,.2f}  "
              f"gap {res['gap']:.2%}  mejora {mejora:>10,.2f}  ({res['t']:.0f}s)", flush=True)
        if mejora > 1e-6 * max(1.0, abs(res["B"])):
            y = eval(P.claves[b])[0]
            por_anio.setdefault(y, {"mejora": 0.0, "dias": []})
            por_anio[y]["mejora"] += mejora
            por_anio[y]["dias"].append((b, res["x"]))

    if not por_anio:
        print("[ub] ningun dia mejora: el incumbente queda igual")
        return 0

    # Los dias se reoptimizaron con b_bar (salud de la bateria) FIJO y la
    # energia libre: si la operacion nueva consume mas, la degradacion de los
    # años siguientes cambia y, con las enteras fijas, el pulido puede quedar
    # infactible. Primero se prueban todos juntos; si falla, año por año en
    # orden de mejora, aceptando solo lo que el pulido mantiene factible.
    def con(vec, y):
        v = vec.copy()
        for b, x in por_anio[y]["dias"]:
            v[bd.subs[b].loc] = x
        return v

    vec_todo = xB.copy()
    for y in por_anio:
        vec_todo = con(vec_todo, y)
    costo, completo = bd.pulir(vec_todo)
    aceptados = sorted(por_anio)
    if costo is None or costo >= UB0:
        print(f"[ub] todos los años juntos: "
              f"{'INFACTIBLE' if costo is None else f'{costo:,.2f} (no mejora)'}; "
              f"se prueba año por año", flush=True)
        vec, costo, completo, aceptados = xB.copy(), UB0, xB, []
        for y in sorted(por_anio, key=lambda y: -por_anio[y]["mejora"]):
            c_y, comp_y = bd.pulir(con(vec, y))
            ok = c_y is not None and c_y < costo * (1 - 1e-9)
            print(f"[ub]   año {y:>2} (mejora de los dias {por_anio[y]['mejora']:>10,.2f}): "
                  f"{'INFACTIBLE' if c_y is None else f'{c_y:,.2f}'} -> "
                  f"{'ACEPTADO' if ok else 'rechazado'}", flush=True)
            if ok:
                vec, costo, completo = con(vec, y), c_y, comp_y
                aceptados.append(y)
    reemplazados = [P.claves[b] for y in aceptados for b, _ in por_anio[y]["dias"]]
    print(f"\n[ub] años aceptados {sorted(aceptados)} ({len(reemplazados)} dias)")
    print(f"[ub] UB {UB0:,.2f} -> {costo:,.2f}  ({UB0 - costo:,.2f} menos, "
          f"{(UB0 - costo) / UB0:.3%})", flush=True)

    resumen = {"respaldo": a.respaldo, "solucion_inicial": a.solucion, "ub_inicial": UB0, "ub_final": costo,
               "dias_reemplazados": reemplazados, "tiempo_s": time.time() - t0}
    if costo < UB0 * (1 - 1e-7):
        from src.io.printer import Printer
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            if vd is not None and not vd.fixed:
                x = completo[j]
                vd.set_value(round(x) if not vd.is_continuous() else x, skip_validation=True)
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        resumen["solucion"] = destino
        print(f"[ub] solucion mejorada en {destino} (usable como --incumbente_inicial)")
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as fh:
        json.dump(resumen, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
