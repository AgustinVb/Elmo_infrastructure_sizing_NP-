"""Reparacion del UB: baterias extra en el pool en vez de reemplazar la flota.

El Benders anidado decide la inversion año a año y ve el futuro solo por cortes
de la relajacion LP, donde el reemplazo R es fraccional: b_bar <= D + 0,3 b_max R
recupera capacidad pagando Z_repl >= NF_min R, una fraccion del reemplazo real.
Los cortes valoran barato llegar degradado al final del horizonte, el maestro no
compra baterias de mas temprano y el forward termina en el año en que b_bar
cruza el piso B_L, donde la unica salida entera es reemplazar TODA la flota.

Con mas baterias en el pool cada una hace menos ciclos (N_ciclos = N_total /
flota, con N_total = E / b_bar fijado por la produccion) y se degrada menos. Este
script toma el incumbente, agrega n baterias en la estacion desde el año y0 (con
las naves y cargadores que pidan max_batteries_per_bay y bays <= chargers), fija
R = 0 en todos los años y evalua:

  0  tamizado: trayectoria de b_bar predicha escalando el desgaste anual del
     incumbente, fade' = fade * (flota * b_bar) / (flota' * b_bar'). Si cruza
     el piso por mas de --margen_piso, la candidata se descarta sin resolver.
  1  dias MILP con la inversion nueva y b_bar en la trayectoria predicha (en
     paralelo, start del incumbente).
  2  MILP chico sobre el monolitico: enteras de los dias e inversion fijas
     (R = 0), las enteras derivadas libres (flota, tramos de b_bar, ...) y todas
     las continuas libres. Su solucion es factible para el monolitico: el costo
     es una UB valida.
  3  si la b_bar resultante se aleja de la usada en los dias mas de --tol_bbar,
     se repiten 1-2 con la b_bar nueva (hasta --rondas).

Si alguna candidata baja el UB, la mejor se escribe en JSON (<out>/solucion,
sirve como --solucion de mejorar_ub.py o --incumbente_inicial de setup.py).

    python -u reparar_reemplazo.py --data_folder data/Tesis_final/Mina_modelo_v4/... \
        --respaldo .../benders/incumbente_respaldo.pkl --candidatos 1:1,2:1,3:1,1:2 \
        --out .../reparar_reemplazo
"""
import argparse
import json
import math
import os
import sys
import time
from argparse import Namespace

import numpy as np

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from gurobipy import GRB                                                 # noqa: E402
from pyomo.environ import value                                          # noqa: E402

from diagnostico_gap_dias import cargar_respaldo                         # noqa: E402
from setup import build_mine                                             # noqa: E402
from src.optimization.decomposition import gcg_block as gb               # noqa: E402
from src.optimization.decomposition.benders_dias import BendersDias      # noqa: E402
from src.optimization.opt_model import OptModel                          # noqa: E402

# Enteras del maestro que son la INVERSION (se fijan). Las demas enteras del
# maestro (n_battery_fleet, u_nf, z_bb, ...) quedan libres: las determina la
# inversion y la degradacion.
INVERSION = {"X", "Delta_X", "N_bays", "Delta_N_bays", "N_chargers", "Delta_N_chargers",
             "N_batteries", "Delta_N_batteries", "n_ssee_k", "Delta_n_ssee_k"}
STOCKS = (("N_bays", "Delta_N_bays"), ("N_chargers", "Delta_N_chargers"),
          ("N_batteries", "Delta_N_batteries"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_folder", required=True)
    ap.add_argument("--n_years", type=int, default=10)
    ap.add_argument("--respaldo", default=None, help="incumbente_respaldo.pkl de Nested Benders")
    ap.add_argument("--solucion", default=None,
                    help="carpeta con los JSON de una solucion (alternativa a --respaldo)")
    ap.add_argument("--ub_esperado", type=float, default=None,
                    help="costo esperado con --solucion; el pulido tiene que reproducirlo")
    ap.add_argument("--candidatos", default="3:1,2:1,1:1,4:1,4:2,3:2",
                    help="año_desde:baterias_extra, por coma")
    ap.add_argument("--estacion", default=None,
                    help="estacion donde se agregan (default: la de mas baterias)")
    ap.add_argument("--cargadores_extra", type=int, default=0,
                    help="cargadores extra desde el mismo año (acotados por las baterias)")
    ap.add_argument("--margen_piso", type=float, default=2.0,
                    help="kWh bajo B_L que tolera el tamizado antes de descartar")
    ap.add_argument("--sin_tamizado", action="store_true")
    ap.add_argument("--timelimit", type=float, default=300, help="segundos por dia MILP")
    ap.add_argument("--gap", type=float, default=1e-3, help="MIPGap de los dias")
    ap.add_argument("--tl_reintento", type=float, default=1200,
                    help="segundos para los dias que no encontraron solucion")
    ap.add_argument("--tl_cierre", type=float, default=900, help="segundos del MILP chico")
    ap.add_argument("--rondas", type=int, default=3)
    ap.add_argument("--tol_bbar", type=float, default=0.2, help="kWh")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--desde_dias", default=None,
                    help="dias_<cand>_r<k>.npz de una corrida anterior: sin resolver dias, "
                         "solo cadena, cierre y escritura de la solucion")
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
    om = OptModel(ms, ts, os.path.join(a.out, "_monolitico"), init_solution_folder=a.solucion,
                  mccormick_degradation=True, free_charging=True, free_maintenance=True)
    m = om.model
    if a.respaldo:
        esperado = cargar_respaldo(om, ms, ts, a.respaldo)["best_cost"]
    else:
        esperado = a.ub_esperado
    info, por_var = gb.export(m, os.path.join(a.out, "_mps"), mode="year_day", split=False)
    bd = BendersDias(info, m, por_var, jobs=a.jobs, out=None)
    P = bd.P
    vec0 = np.zeros(P.nv)
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        v = value(vd, exception=False) if vd is not None else None
        vec0[j] = 0.0 if v is None else v
    UB0, xB, _eB = bd.cargar_incumbente(vec0)
    print(f"[rep] incumbente pulido {UB0:,.2f} (esperado "
          f"{'-' if esperado is None else f'{esperado:,.2f}'})", flush=True)
    if esperado is not None and abs(UB0 - esperado) > 1e-6 * abs(UB0):
        print("[rep] ABORTA: el incumbente no se reconstruye bien")
        return 1

    # ------------------------------------------------ posiciones en el MPS --
    pos = {}
    for j, n in enumerate(P.nombre):
        vd = por_var.get(n)
        if vd is not None:
            pos[(vd.parent_component().name, vd.index())] = j
    ent = [j for j, t in enumerate(P.vtype) if t != "C"]
    ent_dia = [j for j in ent if P.var_blk[j] >= 0]
    ent_inv, libres = [], {}
    for j in ent:
        if P.var_blk[j] >= 0:
            continue
        nombre = por_var[P.nombre[j]].parent_component().name
        if nombre in INVERSION or nombre == "R":
            ent_inv.append(j)
        else:
            libres[nombre] = libres.get(nombre, 0) + 1
    print(f"[rep] enteras: {len(ent_dia):,} de dias y {len(ent_inv)} de inversion/R fijas; "
          f"libres en el cierre: {libres}", flush=True)

    years = sorted(m.years)
    y1 = years[0]
    estaciones = list(m.stations_set)
    k = a.estacion or max(estaciones, key=lambda e: xB[pos[("N_batteries", (e, years[-1]))]])
    B_L, B_U = value(m.B_L), value(m.B_U)

    def val(vec, comp, idx):
        """Valor en vec; las variables fijas en el modelo (b_bar y R del
        primer año) no son columnas del MPS y se leen de pyomo."""
        if (comp, idx) in pos:
            return float(vec[pos[(comp, idx)]])
        return float(value(getattr(m, comp)[idx]))

    def poner(vec, comp, idx, v):
        if (comp, idx) in pos:
            vec[pos[(comp, idx)]] = v

    inc = {y: {"b": val(xB, "b_bar", y), "D": val(xB, "D", y), "R": round(val(xB, "R", y)),
               "flota": round(val(xB, "n_battery_fleet", y))} for y in years}
    print(f"[rep] incumbente ({k}):", flush=True)
    for y in years:
        print(f"[rep]   año {y:>2}  naves {val(xB, 'N_bays', (k, y)):.0f}  cargadores "
              f"{val(xB, 'N_chargers', (k, y)):.0f}  baterias {val(xB, 'N_batteries', (k, y)):.0f}  "
              f"flota {inc[y]['flota']}  R {inc[y]['R']}  b_bar {inc[y]['b']:.1f}  D {inc[y]['D']:.1f}",
              flush=True)
    print(f"[rep] piso B_L {B_L:.1f}, b_max {B_U:.1f}", flush=True)

    # ------------------------------------------------------- candidatas --
    def inversion_nueva(y0, n_bat):
        """Stocks de la estacion k con n_bat baterias (y --cargadores_extra
        cargadores) mas desde y0, completados con naves/cargadores hasta
        cumplir las cotas por nave, monotonos, y sus Delta. None si no cabe."""
        mpb = value(m.max_batteries_per_bay_k[k])
        mcb = value(m.max_chargers_per_bay_k[k])
        mb = value(m.max_bays_k[k])
        st = {s: {y: round(val(xB, s, (k, y))) for y in years} for s, _ in STOCKS}
        for y in years:
            if y >= y0:
                st["N_batteries"][y] += n_bat
                st["N_chargers"][y] = min(st["N_chargers"][y] + a.cargadores_extra,
                                          st["N_batteries"][y])
            st["N_bays"][y] = max(st["N_bays"][y], math.ceil(st["N_batteries"][y] / mpb))
            st["N_chargers"][y] = max(st["N_chargers"][y], st["N_bays"][y])
        for s, _ in STOCKS:
            for i, y in enumerate(years[1:], 1):
                st[s][y] = max(st[s][y], st[s][years[i - 1]])
        for y in years:
            nb, nc, nbat = st["N_bays"][y], st["N_chargers"][y], st["N_batteries"][y]
            if nb > mb or nc > mcb * nb or nc > nbat or nbat > mpb * nb:
                return None
        cambios = {}
        for s, ds in STOCKS:
            prev = 0
            for y in years:
                cambios[(s, (k, y))] = st[s][y]
                cambios[(ds, (k, y))] = st[s][y] - prev
                prev = st[s][y]
        return cambios, st

    def b_bar_predicha(extra_pool):
        """fade_y = gamma*N_ciclos = gamma*E*scal/(flota*b_bar): con la misma
        energia del incumbente, el desgaste escala con 1/(flota*b_bar)."""
        tray = {}
        for i, y in enumerate(years):
            b = B_U if y == y1 else tray[years[i - 1]][1]     # R = 0: hereda D[y-1]
            fade = inc[y]["b"] - inc[y]["D"]
            f_inc, f_new = inc[y]["flota"], inc[y]["flota"] + extra_pool[y]
            d = b - fade * (f_inc * inc[y]["b"]) / (f_new * b)
            tray[y] = (b, d)
        return tray

    def cierre(vec):
        """MILP chico: enteras de dias e inversion fijas en vec, el resto libre."""
        F = P.F
        vs = F.getVars()
        fijas = set(ent_dia) | set(ent_inv)
        F.setAttr("LB", [vs[j] for j in ent],
                  [float(round(vec[j])) if j in fijas else float(P.lb[j]) for j in ent])
        F.setAttr("UB", [vs[j] for j in ent],
                  [float(round(vec[j])) if j in fijas else float(P.ub[j]) for j in ent])
        F.Params.TimeLimit = a.tl_cierre
        F.Params.MIPGap = 1e-6
        F.optimize()
        if F.SolCount == 0:
            return None, None, F.Status
        return F.ObjVal, np.array(F.getAttr("X", vs)), F.Status

    ent_inv_set = set(ent_inv)
    piso_idx = {pos[(c, y)] for c in ("D", "b_bar") for y in years if (c, y) in pos}

    def cadena(vec, con_piso=True):
        """Solo las filas del maestro (stock, flota, cadena de degradacion), con
        la parte de los dias como constante tomada de vec y la inversion y R
        fijas. Son ~700 variables: dice en segundos si la degradacion cabe
        sobre el piso con la energia REAL de los dias, y da la b_bar coherente
        con R = 0 (b_bar[y] = D[y-1]) para resolver los dias con ella. Sin
        con_piso, D y b_bar bajan hasta 0: mide cuanto se cruza el piso."""
        import gurobipy as gp
        mm = gp.Model(env=bd.env_M)
        mv = {}
        for j in P.maestro_vars:
            lb, ub = float(P.lb[j]), float(P.ub[j])
            if j in ent_inv_set:
                lb = ub = float(round(vec[j]))
            elif not con_piso and j in piso_idx:
                lb = 0.0
            mv[j] = mm.addVar(lb=lb, ub=ub, obj=float(P.obj[j]), vtype=P.vtype[j])
        for i in P.filas_maestro:
            idx, coef = P.filas[i]
            esm = P.var_blk[idx] < 0
            const = float(np.dot(coef[~esm], vec[idx[~esm]]))
            mm.addLConstr(gp.LinExpr(coef[esm].tolist(), [mv[j] for j in idx[esm]]),
                          P.sense[i], float(P.rhs[i]) - const)
        mm.Params.TimeLimit = 300
        mm.optimize()
        if mm.SolCount == 0:
            return None, mm.Status
        out = vec.copy()
        for j, v in mv.items():
            out[j] = v.X
        return out, mm.Status

    resultados, mejor = [], (UB0, None, None)
    if a.desde_dias:
        # Dias ya resueltos (np.savez de la ronda): x trae la inversion, R y la
        # b_bar con que se resolvieron; cada clave (año, dia), su operacion.
        g = np.load(a.desde_dias)
        x = g["x"]
        vec = x.copy()
        for b in range(P.nb):
            vec[bd.subs[b].loc] = g[P.claves[b]]
        txt = os.path.basename(a.desde_dias)
        fila = {"candidata": txt, "estado": None}
        resultados.append(fila)
        cad, st_cad = cadena(vec)
        if cad is None:
            fila["estado"] = f"la degradacion cruza el piso (status {st_cad})"
        else:
            dif = max(abs(val(cad, "b_bar", y) - val(x, "b_bar", y)) for y in years)
            print(f"[rep] {txt}: cadena OK, D min {min(val(cad, 'D', y) for y in years):.1f}  "
                  f"|b_bar - usada| {dif:.2f}", flush=True)
            costo, completo, status = cierre(vec)
            if costo is None:
                fila["estado"] = f"cierre infactible (status {status})"
            else:
                fila.update({"estado": "ok", "costo": costo})
                print(f"[rep] {txt}: costo {costo:,.2f} ({(costo - UB0) / UB0:+.2%})  R "
                      f"{[round(val(completo, 'R', y)) for y in years]}", flush=True)
                if costo < mejor[0]:
                    mejor = (costo, completo, txt)
    for txt in ([] if a.desde_dias else a.candidatos.split(",")):
        y0, n_bat = (int(t) for t in txt.split(":"))
        tag = f"+{n_bat} bat desde año {y0}"
        fila = {"candidata": txt, "estado": None}
        resultados.append(fila)
        r = inversion_nueva(y0, n_bat)
        if r is None:
            fila["estado"] = "no cabe en la estacion"
            print(f"\n[rep] {tag}: no cabe en la estacion", flush=True)
            continue
        cambios, st = r
        extra_pool = {y: st["N_batteries"][y] - round(val(xB, "N_batteries", (k, y))) for y in years}
        tray = b_bar_predicha(extra_pool)
        d_min = min(d for _, d in tray.values())
        print(f"\n[rep] {tag}: naves {[st['N_bays'][y] for y in years]}  cargadores "
              f"{[st['N_chargers'][y] for y in years]}  baterias {[st['N_batteries'][y] for y in years]}",
              flush=True)
        print(f"[rep]   b_bar predicha {[round(b, 1) for b, _ in tray.values()]}  "
              f"D minima {d_min:.1f} (piso {B_L:.1f})", flush=True)
        fila["D_min_predicha"] = d_min
        if d_min < B_L - a.margen_piso and not a.sin_tamizado:
            fila["estado"] = "descartada: cruza el piso"
            print(f"[rep]   descartada: cruza el piso por {B_L - d_min:.1f} kWh", flush=True)
            continue

        x = xB.copy()
        for clave, v in cambios.items():
            poner(x, *clave, v)
        for y in years:
            poner(x, "R", y, 0.0)
            poner(x, "b_bar", y, tray[y][0])
        costo = completo = None
        for ronda in range(1, a.rondas + 1):
            t_r = time.time()
            res = bd._paralelo(lambda b: bd.subs[b].ub(x, a.timelimit, a.gap))
            # Un dia sin solucion casi siempre es TIME_LIMIT sin incumbente (9):
            # con otra b_bar y otro pool el horario de swaps del incumbente ya no
            # sirve de start. Se reintenta con mas tiempo; INFEASIBLE (3) es real.
            fallidos = [b for b, rr in enumerate(res) if not rr["ok"]]
            if fallidos:
                print(f"[rep]   ronda {ronda}: sin solucion en "
                      f"{[(P.claves[b], res[b]['status']) for b in fallidos]} (clave, status); "
                      f"reintento con {a.tl_reintento:.0f}s", flush=True)
                otra = bd._paralelo(lambda b: bd.subs[b].ub(x, a.tl_reintento, a.gap), fallidos)
                for b, rr in zip(fallidos, otra):
                    res[b] = rr
                    print(f"[rep]     {P.claves[b]}: "
                          f"{'ok, gap ' + format(rr['gap'], '.2%') if rr['ok'] else 'status ' + str(rr['status'])}"
                          f" ({rr['t']:.0f}s)", flush=True)
            malos = [P.claves[b] for b, rr in enumerate(res) if not rr["ok"]]
            if malos:
                fila["estado"] = f"dias sin operacion factible {malos[:4]}"
                print(f"[rep]   ronda {ronda}: {len(malos)} dias sin operacion factible "
                      f"({malos[:4]})", flush=True)
                costo = None
                break
            vec = x.copy()
            for b, rr in enumerate(res):
                vec[bd.subs[b].loc] = rr["x"]
            np.savez(os.path.join(a.out, f"dias_{txt.replace(':', '_')}_r{ronda}.npz"),
                     x=x, **{P.claves[b]: np.asarray(rr["x"]) for b, rr in enumerate(res)})
            gap_dias = max(rr["gap"] for rr in res)

            # Cadena de degradacion con la energia real de los dias.
            cad, st_cad = cadena(vec)
            if cad is None:
                rel, _ = cadena(vec, con_piso=False)
                d_txt = ("-" if rel is None else
                         str([round(val(rel, "D", y), 1) for y in years]))
                fila["estado"] = "la degradacion cruza el piso"
                print(f"[rep]   ronda {ronda}: la degradacion NO cabe sobre el piso con la energia "
                      f"de los dias (status {st_cad}); D sin piso {d_txt}", flush=True)
                costo = None
                break
            b_cad = {y: val(cad, "b_bar", y) for y in years}
            dif = max(abs(b_cad[y] - val(x, "b_bar", y)) for y in years)
            print(f"[rep]   ronda {ronda}: cadena OK, b_bar {[round(b_cad[y], 1) for y in years]}  "
                  f"D min {min(val(cad, 'D', y) for y in years):.1f}  |b_bar - usada en los dias| "
                  f"{dif:.2f}  gap dias max {gap_dias:.2%}  ({time.time() - t_r:.0f}s)", flush=True)
            if dif > a.tol_bbar and ronda < a.rondas:
                # Los dias se resolvieron con otra b_bar: se repiten con la de la
                # cadena antes del cierre (con R = 0 la b_bar no tiene holgura).
                for y in years:
                    poner(x, "b_bar", y, b_cad[y])
                continue

            costo, completo, status = cierre(vec)
            if costo is None:
                fila["estado"] = f"cierre infactible (status {status})"
                print(f"[rep]   ronda {ronda}: cierre sin solucion (status {status}) con "
                      f"|b_bar - usada| {dif:.2f}", flush=True)
                break
            b_new = {y: val(completo, "b_bar", y) for y in years}
            r_new = [round(val(completo, "R", y)) for y in years]
            print(f"[rep]   ronda {ronda}: costo {costo:,.2f} ({costo - UB0:+,.2f}, "
                  f"{(costo - UB0) / UB0:+.2%})  b_bar {[round(b_new[y], 1) for y in years]}  "
                  f"R {r_new}  ({time.time() - t_r:.0f}s)", flush=True)
            fila.update({"costo": costo, "rondas": ronda, "b_bar": b_new})
            break
        if costo is not None:
            fila["estado"] = "ok"
            if costo < mejor[0]:
                mejor = (costo, completo, txt)

    print(f"\n[rep] UB inicial {UB0:,.2f}", flush=True)
    for f in resultados:
        c = f.get("costo")
        print(f"[rep]   {f['candidata']:>6}  {f['estado']}"
              f"{'' if c is None else f'  {c:,.2f} ({(c - UB0) / UB0:+.3%})'}", flush=True)
    resumen = {"respaldo": a.respaldo, "solucion_inicial": a.solucion, "ub_inicial": UB0,
               "ub_final": mejor[0], "mejor_candidata": mejor[2], "candidatas": resultados,
               "tiempo_s": time.time() - t0}
    if mejor[1] is not None and mejor[0] < UB0 * (1 - 1e-7):
        # Pulido final con todas las enteras fijas: deja las continuas en el
        # optimo del LP, como las soluciones de mejorar_ub.py.
        costo, completo = bd.pulir(mejor[1])
        if costo is None:
            costo, completo = mejor[0], mejor[1]
        from src.io.printer import Printer
        for j, n in enumerate(P.nombre):
            vd = por_var.get(n)
            if vd is not None and not vd.fixed:
                xj = completo[j]
                vd.set_value(round(xj) if not vd.is_continuous() else xj, skip_validation=True)
        destino = os.path.join(a.out, "solucion")
        Printer(om, destino, ts, ms).write_variables_jsons()
        resumen.update({"ub_final": costo, "solucion": destino})
        print(f"[rep] UB {UB0:,.2f} -> {costo:,.2f} ({mejor[2]}); solucion en {destino}", flush=True)
    else:
        print("[rep] ninguna candidata mejora el incumbente", flush=True)
    with open(os.path.join(a.out, "resumen.json"), "w", encoding="utf-8") as fh:
        json.dump(resumen, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
