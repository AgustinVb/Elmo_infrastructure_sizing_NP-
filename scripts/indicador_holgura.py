"""Indicador de holgura operativa de la flota LHD, swap vs on-board (casos v4).

Solo lee las soluciones guardadas (no modifica el modelo ni vuelve a optimizar).

Estados por LHD e intervalo (el modelo impone Z + sum Y + sum Z_charge/Z_swap = 1):
  detenciones operacionales  cambio de turno, colacion y despeje de calles (ventanas DET);
                             dentro de ellas Y = 0 por construccion
  T_op                       Y = 1 fuera de las detenciones operacionales
  T_carga                    Z_charge = 1 (on-board, conectado aunque P = 0) o
                             Z_swap = 1 (swap, 1 intervalo por intercambio) fuera de ellas
  carga en detenciones       lo mismo dentro de ellas: no descuenta holgura
  T_holgura                  Z = 1 fuera de ellas = disponible - T_op - T_carga
On-board puede cargar a potencia parcial (al modelo no le cuesta tener al LHD
conectado), asi que tambien se da T_carga equivalente = energia cargada fuera de las
detenciones / potencia maxima (cargador, limitada por la subestacion) y la holgura
potencial = T_holgura + T_carga - T_carga equivalente.
Disponible = 24 h - detenciones operacionales. maintenance_det no es detencion: en
estas corridas los LHD extraen y cargan en ella.

Agregacion: regimen = años con la meta de produccion maxima (4-7), dias
representativos con su peso (iguales, 365/4), minimo de holgura entre LHD x dias x
años de regimen. Swap: utilizacion de cargadores con Sv del modelo (no con el
despacho ajustado) y baterias cargadas disponibles con S.

Uso: python scripts/indicador_holgura.py [casos...]   (por defecto los 8)
Salida: cost_comparisons/resultados_finales/holgura_operativa/
"""
import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "cost_comparisons"))
from comparar_swap_ob_v4 import CASOS, V4          # noqa: E402
from resultados_finales import FINALES, OB_REPO    # noqa: E402

OUT = REPO / "cost_comparisons" / "resultados_finales" / "holgura_operativa"
DETENCIONES = ("between_shifts_det", "meal_det", "road_clearing_det")
LADOS = (("Swap", "Battery swapping"), ("OB", "Carga on-board"))


def leer(sol, nombre):
    return json.loads((sol / nombre).read_text(encoding="utf-8"))


def activos(data, raiz):
    """{lhd: {(y, d): set(t)}} con valor >= 0.5; raiz es la ruta hasta 'i'. Suma
    sobre puntos de extraccion (Y) y estaciones (Z_charge, Z_swap)."""
    out = {}
    nodos = [data]
    for k in raiz:
        nodos = [n[k] for n in nodos] if k != "*" else [v for n in nodos for v in n.values()]
    for nodo in nodos:
        for lhd, blk in nodo.items():
            dst = out.setdefault(lhd, {})
            for rama in (blk["j"].values() if "j" in blk else [blk]):
                for y, by in rama["y"].items():
                    for d, bd in by["d"].items():
                        dst.setdefault((int(y), int(d)), set()).update(
                            int(float(t)) for t, v in bd["t"].items() if float(v) >= 0.5)
    return out


def potencia_ob(sol):
    """{lhd: {(y, d): {t: P [kW]}}} de on-board."""
    out = {}
    for est in leer(sol, "P.json")["k"].values():
        for lhd, blk in est["i"].items():
            for y, by in blk["y"].items():
                for d, bd in by["d"].items():
                    out.setdefault(lhd, {})[(int(y), int(d))] = {int(float(t)): float(v) for t, v in bd["t"].items()}
    return out


def potencia_maxima_ob(sol, p):
    """Potencia a la que puede cargar un LHD: la del cargador, limitada por la
    subestacion (on-board 2C: 964 kW con 500 kW instalados)."""
    pmax = min(float(v) for v in leer(sol, "P_max_k.json")["k"].values())
    return min(float(p["p_charger"]), pmax)


def regimen(params):
    """Años con la meta de produccion maxima (la misma en todos los puntos)."""
    metas = {}
    for blk in params["m_j"]["_1"].values():
        for y, v in blk["_2"].items():
            metas[int(y)] = max(metas.get(int(y), 0.0), float(v))
    tope = max(metas.values())
    return sorted(y for y, v in metas.items() if v >= tope - 1e-6)


def por_lhd_dia(clave, lado, sol):
    p = leer(sol, "parameters.json")
    T, dt = int(p["t_fin"]), float(p["delta_t"])
    det = set()
    for k in DETENCIONES:
        det |= {int(float(t)) for t in p.get("time_intervals_" + k + "_set") or []}
    pesos = {int(d): 365.0 / len(p["days"]) for d in p["days"]}
    if abs(365.0 / len(p["days"]) - float(p["scaling_factor_op_cost"])) > 1e-6:
        raise SystemExit("{} {}: los dias no tienen el mismo peso".format(clave, lado))
    Y, Z = activos(leer(sol, "Y.json"), ["i"]), activos(leer(sol, "Z.json"), ["i"])
    C = activos(leer(sol, "Z_charge.json" if lado == "OB" else "Z_swap.json"), ["k", "*", "i"])
    P = potencia_ob(sol) if lado == "OB" else {}
    p_ef = potencia_maxima_ob(sol, p) if lado == "OB" else None
    filas = []
    for lhd in sorted(Y):
        for y in p["years"]:
            for d in pesos:
                yd = (int(y), d)
                y_, z_, c_ = (s.get(lhd, {}).get(yd, set()) for s in (Y, Z, C))
                cuenta = [sum(t in s for s in (y_, z_, c_)) for t in range(1, T + 1)]
                libre = set(range(1, T + 1)) - det
                f = {"caso": clave, "lado": lado, "lhd": lhd, "anio": int(y), "dia": d, "peso": pesos[d],
                     "h_total": T * dt, "h_detenciones": len(det) * dt, "h_disponible": len(libre) * dt,
                     "h_op": len(y_ & libre) * dt, "h_carga": len(c_ & libre) * dt,
                     "h_holgura": len(z_ & libre) * dt, "h_carga_detenciones": len(c_ & det) * dt,
                     "h_op_en_detenciones": len(y_ & det) * dt,
                     "intervalos_sin_estado_unico": sum(n != 1 for n in cuenta)}
                if lado == "OB":
                    pt = P.get(lhd, {}).get(yd, {})
                    f["h_conectado_sin_potencia"] = sum(pt.get(t, 0.0) <= 1.0 for t in c_ & libre) * dt
                    # Tiempo fuera de las detenciones si la misma energia se cargara a
                    # la potencia maxima: separa la carga necesaria de la que el modelo
                    # estira a potencia parcial (no le cuesta tener al LHD conectado).
                    f["h_carga_equiv"] = sum(pt.get(t, 0.0) for t in c_ & libre) * dt / p_ef
                else:
                    f["h_conectado_sin_potencia"], f["h_carga_equiv"] = 0.0, f["h_carga"]
                f["h_holgura_potencial"] = f["h_holgura"] + f["h_carga"] - f["h_carga_equiv"]
                f["residuo_h"] = f["h_total"] - (f["h_op"] + f["h_carga"] + f["h_holgura"] + f["h_detenciones"])
                for k in ("op", "carga", "holgura", "carga_equiv", "holgura_potencial"):
                    f["pct_" + k] = 100 * f["h_" + k] / f["h_disponible"]
                f["pct_carga_detenciones"] = 100 * f["h_carga_detenciones"] / f["h_detenciones"]
                f["pct_conectado_sin_potencia"] = 100 * f["h_conectado_sin_potencia"] / f["h_disponible"]
                filas.append(f)
    return pd.DataFrame(filas), p


def estacion_swap(clave, sol, p):
    """Por año y dia: utilizacion de cargadores [%] y baterias cargadas (media, minimo)."""
    nch = leer(sol, "N_chargers.json")["k"]
    sv, S = leer(sol, "Sv.json")["k"], leer(sol, "S.json")["k"]
    filas = []
    for k in nch:
        for y in p["years"]:
            n = float(nch[k]["y"][str(y)])
            for d in p["days"]:
                carg = [sum(float(v) for v in blk["a"].values())
                        for blk in sv[k]["y"][str(y)]["d"][str(d)]["t"].values()]
                s = [float(v) for v in S[k]["y"][str(y)]["d"][str(d)]["t"].values()]
                filas.append({"caso": clave, "estacion": k, "anio": int(y), "dia": int(d),
                              "cargadores": n, "pct_util_cargadores": 100 * sum(carg) / (n * len(carg)) if n else 0.0,
                              "baterias_cargadas_media": sum(s) / len(s), "baterias_cargadas_min": min(s)})
    return pd.DataFrame(filas)


def resumir(df, est, anios, titulo, clave, lado):
    r = df[df["anio"].isin(anios)]
    w = lambda col: (r[col] * r["peso"]).sum() / r["peso"].sum()
    fila = {"caso": clave, "escenario": titulo, "tecnologia": dict(LADOS)[lado],
            "anios_regimen": "-".join(map(str, (anios[0], anios[-1]))),
            "holgura_prom_pct": w("pct_holgura"), "holgura_min_pct": r["pct_holgura"].min(),
            "fraccion_carga_pct": w("pct_carga"), "carga_detenciones_pct": w("pct_carga_detenciones"),
            "operacion_pct": w("pct_op"), "holgura_prom_h": w("h_holgura"), "carga_prom_h": w("h_carga"),
            "conectado_sin_potencia_pct": w("pct_conectado_sin_potencia") if lado == "OB" else None,
            "fraccion_carga_equiv_pct": w("pct_carga_equiv"), "holgura_potencial_prom_pct": w("pct_holgura_potencial"),
            "holgura_potencial_min_pct": r["pct_holgura_potencial"].min(),
            "util_cargadores_pct": None, "baterias_cargadas_media": None, "baterias_cargadas_min": None}
    peor = r.loc[r["pct_holgura"].idxmin()]
    fila["peor_caso"] = "{} año {} dia {}".format(peor["lhd"], peor["anio"], peor["dia"])
    if est is not None:
        e = est[est["anio"].isin(anios)]
        fila.update(util_cargadores_pct=e["pct_util_cargadores"].mean(),
                    baterias_cargadas_media=e["baterias_cargadas_media"].mean(),
                    baterias_cargadas_min=e["baterias_cargadas_min"].min())
    return fila


def verificar(df, clave, lado):
    malos = df[(df["residuo_h"].abs() > 1e-9) | (df["intervalos_sin_estado_unico"] > 0)
               | (df["h_op_en_detenciones"] > 0)]
    print("[{} {}] {} LHD-dia, residuo max {:.2e} h, intervalos sin estado unico {}, "
          "operacion en detenciones {:.2f} h -> {}".format(
              clave, lado, len(df), df["residuo_h"].abs().max(), int(df["intervalos_sin_estado_unico"].sum()),
              df["h_op_en_detenciones"].sum(), "OK" if malos.empty else "{} FILAS CON DIFERENCIAS".format(len(malos))),
          flush=True)
    return malos


def _c(v):
    return "--" if v is None or pd.isna(v) else "{:.1f}".format(v).replace(".", ",")


def tabla_tex(res, path):
    cab = (r"\textbf{Escenario} & \textbf{Tecnología} & \textbf{Holgura prom. [\%]} & "
           r"\textbf{Holgura mín. [\%]} & \textbf{Fracción de carga [\%]} & "
           r"\textbf{Carga en detenciones operacionales [\%]} & \textbf{Utilización cargadores [\%]} & "
           r"\textbf{Holgura con carga a potencia máx. [\%]} \\")
    filas = []
    for i, (caso, g) in enumerate(res.groupby("caso", sort=False)):
        if i:
            filas.append(r"\addlinespace")
        for j, (_, f) in enumerate(g.iterrows()):
            tec = (r"\textit{Battery swapping}" if f["tecnologia"] == "Battery swapping"
                   else r"Carga \textit{on-board}")
            filas.append("{} & {} & {} & {} & {} & {} & {} & {} \\\\".format(
                f["escenario"] if j == 0 else "", tec, _c(f["holgura_prom_pct"]), _c(f["holgura_min_pct"]),
                _c(f["fraccion_carga_pct"]), _c(f["carga_detenciones_pct"]), _c(f["util_cargadores_pct"]),
                _c(f["holgura_potencial_prom_pct"] if f["tecnologia"] != "Battery swapping" else None)))
    tex = "\n".join([
        r"\begin{table}[htbp]", r"\centering",
        r"\caption{Holgura operativa de la flota LHD en régimen (años de producción máxima), promedio "
        r"ponderado por días representativos. Porcentajes del tiempo operativo disponible (24 h menos "
        r"detenciones operacionales), salvo la carga en detenciones operacionales, que es porcentaje del "
        r"tiempo de detención. La holgura mínima es el peor LHD y día. La última columna (solo "
        r"\textit{on-board}) es la holgura si la energía cargada fuera de las detenciones se cargara a "
        r"la potencia máxima.}",
        r"\label{tab:holgura-operativa}", r"\scriptsize", r"\setlength{\tabcolsep}{3pt}",
        r"\begin{tabular}{p{3.6cm}p{1.9cm}*{6}{>{\centering\arraybackslash}p{1.3cm}}}", r"\toprule", cab,
        r"\midrule", *filas, r"\bottomrule", r"\end{tabular}", r"\end{table}", ""])
    path.write_text(tex, encoding="utf-8")


def main(claves):
    OUT.mkdir(parents=True, exist_ok=True)
    detalle, estaciones, resumen, problemas = [], [], [], []
    for clave in claves:
        titulo = CASOS[clave]["titulo"]
        sols = {"Swap": REPO / V4 / FINALES[clave][0] / FINALES[clave][1] / "solucion",
                "OB": OB_REPO / CASOS[clave]["ob"]["caso"] / CASOS[clave]["ob"]["sol"]}
        for lado, _ in LADOS:
            df, p = por_lhd_dia(clave, lado, sols[lado])
            problemas.append(verificar(df, clave, lado))
            est = estacion_swap(clave, sols[lado], p) if lado == "Swap" else None
            resumen.append(resumir(df, est, regimen(p), titulo, clave, lado))
            detalle.append(df)
            if est is not None:
                estaciones.append(est)
    res = pd.DataFrame(resumen)
    res.to_csv(OUT / "holgura_operativa.csv", index=False, encoding="utf-8-sig")
    pd.concat(detalle).to_csv(OUT / "holgura_por_lhd_dia.csv", index=False, encoding="utf-8-sig")
    pd.concat(estaciones).to_csv(OUT / "estacion_swap_por_dia.csv", index=False, encoding="utf-8-sig")
    pd.concat(problemas).to_csv(OUT / "verificacion_diferencias.csv", index=False, encoding="utf-8-sig")
    tabla_tex(res, OUT / "tabla_holgura_operativa.tex")
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.float_format", "{:.1f}".format):
        print(res[["caso", "tecnologia", "operacion_pct", "holgura_prom_pct", "holgura_min_pct",
                   "fraccion_carga_pct", "carga_detenciones_pct", "conectado_sin_potencia_pct",
                   "fraccion_carga_equiv_pct", "holgura_potencial_prom_pct", "holgura_potencial_min_pct",
                   "util_cargadores_pct", "baterias_cargadas_media", "baterias_cargadas_min"]].to_string())
    print("->", OUT)


if __name__ == "__main__":
    main(sys.argv[1:] or sorted(FINALES))
