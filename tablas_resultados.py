"""Tablas de resultados de los 8 casos finales de la tesis: carga on-board (OB, esta
rama, v3) y battery swapping (rama battery_swapping_multiaño), lado a lado.

FUENTES. OB se lee del disco. Swap se lee de git (`git show <ref>:<ruta>`), sin
cambiar de rama: por defecto origin/battery_swapping_multiaño (hacer `git fetch`
antes para traer lo ultimo). En git no estan los JSON de la solucion de todos los
casos de swap, asi que las dos ramas se leen con la MISMA logica, solo de archivos
que siempre existen:
  certificado.log  UB, LB certificada, gap y la inversion entera del incumbente por
                   año (cargadores; en swap ademas bahias y baterias del pool;
                   n_ssee_k): linea "<- INCUMBENTE"
  consumer.log     costos en valor presente, solar/eolica instaladas, inversion
                   bruta del BESS, energia de red y años con reemplazo de baterias
                   (tabla DEGRADACION, columna Reemplazo = SI)
  elmo_data.xlsx   potencia del cargador, c_inv del BESS y de la subestacion, tasa
De la inversion bruta salen el BESS (H = bruta * (1+r) / c_inv_h, por 1.000 kWh) y
la subestacion (kW = costo * (1+r) / c_inv_ssee): la inversion se descuenta con
DF(año 1) = 1/(1+r) (year_position es 1-indexada).

RECONCILIACION. El COSTO TOTAL del consumer tiene que coincidir con el UB del
certificado; si no, el script aborta (el reporte no seria de la solucion certificada).

CASOS (numeracion de cola_tesis_ob.sh y cola_tesis_swap.sh):
  OB v3: los casos 3, 5 y 6 son las re-corridas con topes de generacion y BESS en 10
    (<caso>/topes10), como swap; el 6 dio la misma solucion que con tope 2 (su
    reporte es el del caso base). Los casos 2, 7 y 8 se corrieron con tope 2, no
    activo en su solucion. Los casos 4 y 5 son 1C: con 0,5C la escalada al 20 % es
    infactible en OB.
  Swap: los casos 4 y 5 son 0,5C. El 5 es la corrida desde la solucion del caso 4
    (inc_caso4). El 6 con topes 10 no esta en git: se usa la corrida con tope 2.

    python tablas_resultados.py
    python tablas_resultados.py --swap_ref battery_swapping_multiaño --out <carpeta>
"""
import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys

import pandas as pd

REPO = os.path.dirname(os.path.abspath(__file__))
os.chdir(REPO)

O = "output/Resultados_finales_tesis/Mina_modelo/casos_finales"

# n, descripcion, carpeta del certificado, carpeta del consumer (None = la misma),
# carpeta de datos, nota
CASOS_OB = [
    (1, "Base, norte, 0,5C, solo red", "Produccion_base/zona_norte/05C/P_red", None,
     "Produccion_base/zona_norte/05C/P_red", ""),
    (2, "Base, norte, 0,5C, red + gen + BESS", "Produccion_base/zona_norte/05C/P_red_gen_bat", None,
     "Produccion_base/zona_norte/05C/P_red_gen_bat", "tope 2 (no activo)"),
    (3, "Base, norte, 0,5C, red + gen", "Produccion_base/zona_norte/05C/P_red_gen/topes10", None,
     "Produccion_base/zona_norte/05C/P_red_gen", "topes 10"),
    (4, "Escalada 20 %, norte, 1C, solo red", "Produccion_escalada20/zona_norte/1C/P_red", None,
     "Produccion_escalada20/zona_norte/1C/P_red", ""),
    (5, "Escalada 20 %, norte, 1C, red + gen + BESS", "Produccion_escalada20/zona_norte/1C/P_red_gen_bat/topes10",
     None, "Produccion_escalada20/zona_norte/1C/P_red_gen_bat", "topes 10"),
    (6, "Base, centro, 0,5C, red + gen + BESS", "Produccion_base/zona_centro/05C/P_red_gen_bat/topes10",
     "Produccion_base/zona_centro/05C/P_red_gen_bat", "Produccion_base/zona_centro/05C/P_red_gen_bat",
     "topes 10 (misma solucion que tope 2)"),
    (7, "Base, norte, 1C, red + gen + BESS", "Produccion_base/zona_norte/1C/P_red_gen_bat", None,
     "Produccion_base/zona_norte/1C/P_red_gen_bat", "tope 2 (no activo)"),
    (8, "Base, norte, 2C, red + gen + BESS", "Produccion_base/zona_norte/2C/P_red_gen_bat", None,
     "Produccion_base/zona_norte/2C/P_red_gen_bat", "tope 2 (no activo)"),
]
CASOS_SWAP = [
    (1, "Base, norte, 0,5C, solo red", "Produccion_base/zona_norte/05C/RED", None,
     "Produccion_base/zona_norte/05C/RED", ""),
    (2, "Base, norte, 0,5C, red + gen + BESS", "Produccion_base/zona_norte/05C/RED_GEN_BESS", None,
     "Produccion_base/zona_norte/05C/RED_GEN_BESS", ""),
    (3, "Base, norte, 0,5C, red + gen", "Produccion_base/zona_norte/05C/RED_GEN", None,
     "Produccion_base/zona_norte/05C/RED_GEN", ""),
    (4, "Escalada 20 %, norte, 0,5C, solo red", "Produccion_escalada20/zona_norte/05C/RED", None,
     "Produccion_escalada20/zona_norte/05C/RED", ""),
    (5, "Escalada 20 %, norte, 0,5C, red + gen + BESS", "Produccion_escalada20/zona_norte/05C/RED_GEN_BESS/inc_caso4",
     None, "Produccion_escalada20/zona_norte/05C/RED_GEN_BESS", "desde la solucion del caso 4"),
    (6, "Base, centro, 0,5C, red + gen + BESS", "Produccion_base/zona_centro/05C/RED_GEN_BESS_tope2", None,
     "Produccion_base/zona_centro/05C/RED_GEN_BESS",
     "tope 2; la corrida con topes 10 (gap 3,247 %) no esta en git"),
    (7, "Base, norte, 1C, red + gen + BESS", "Produccion_base/zona_norte/1C/RED_GEN_BESS", None,
     "Produccion_base/zona_norte/1C/RED_GEN_BESS", ""),
    (8, "Base, norte, 2C, red + gen + BESS", "Produccion_base/zona_norte/2C/RED_GEN_BESS", None,
     "Produccion_base/zona_norte/2C/RED_GEN_BESS", ""),
]


class Fuente:
    """Archivos del disco (ref=None) o de un commit/rama de git."""

    def __init__(self, nombre, data, ref=None):
        self.nombre, self.data, self.ref = nombre, data, ref

    def leer_bytes(self, ruta):
        if self.ref is None:
            with open(ruta, "rb") as fh:
                return fh.read()
        r = subprocess.run(["git", "show", f"{self.ref}:{ruta}"], capture_output=True)
        if r.returncode != 0:
            raise FileNotFoundError(f"{self.ref}:{ruta}")
        return r.stdout

    def leer(self, ruta):
        return self.leer_bytes(ruta).decode("utf-8", errors="replace")


def _lista(txt, clave):
    m = re.search(rf"{clave} ([\d,]+)", txt)
    return [int(v) for v in m.group(1).split(",")] if m else None


def certificado(f, carpeta):
    t = f.leer(f"{O}/{carpeta}/certificado.log")
    m = re.findall(r"LB CERTIFICADA ([\d,.]+)\s+->\s+gap ([\d.]+)%", t)
    if not m:
        sys.exit(f"ABORTA: {f.nombre} {carpeta}/certificado.log sin linea LB CERTIFICADA")
    ub = re.findall(r"\(contra UB ([\d,.]+)\)", t) or re.findall(r"\[cert\] UB ([\d,.]+)", t)
    inc = re.findall(r"\[([^\]]*chargers[^\]]*)\]\s+<- INCUMBENTE", t)
    if not inc:
        sys.exit(f"ABORTA: {f.nombre} {carpeta}/certificado.log sin la inversion del INCUMBENTE")
    return {"lb": float(m[-1][0].replace(",", "")), "gap_pct": float(m[-1][1]),
            "ub": float(ub[-1].replace(",", "")),
            "chargers": _lista(inc[-1], "chargers"), "bays": _lista(inc[-1], "bays"),
            "batteries": _lista(inc[-1], "batteries"), "n_ssee": (_lista(inc[-1], "n_ssee_k") or [0])[0]}


def _fila(t, nombre):
    """Valores numericos de la fila '| nombre | ... |' de una tabla del consumer."""
    m = re.search(rf"^\| {re.escape(nombre)}\s*\|([^\n]*)", t, re.M)
    if not m:
        return None
    return [float(x) for x in (c.strip() for c in m.group(1).split("|")) if re.fullmatch(r"-?[\d.]+", x)]


def _costo(t, patron):
    m = re.search(rf"^\| {patron}(?: \(USD\))?\s*\|\s*([\d.]+)", t, re.M)
    return float(m.group(1)) if m else 0.0


def consumer(f, carpeta):
    t = f.leer(f"{O}/{carpeta}/consumer.log")
    solar, wind = _fila(t, "Solar_PV") or [0, 0, 0], _fila(t, "Wind") or [0, 0, 0]
    bess = _fila(t, "Bess")
    deg = t[t.find("DEGRADACION BATERIA"):]
    return {
        "folder": re.search(r"^\| Folder\s*\|\s*(\S+)", t, re.M).group(1).replace("\\", "/"),
        "solar_kw": solar[2], "eolica_kw": wind[2],
        "bess_inv_bruta": bess[4] if bess and len(bess) > 4 else 0.0,
        "reemplazos": [int(a) for a in re.findall(r"^\| (\d+)\s+\|[^\n]*\|\s*SI\s*\|", deg, re.M)],
        "red_mwh": float(re.search(r"\[MWh\]\s*\|\s*([\d.]+)", t).group(1)),
        "c_red": _costo(t, r"Costo energ.{1,3}a red P_red"),
        "c_punta": _costo(t, r"Costo potencia (?:punta|pico)"),
        "c_estaciones": _costo(t, r"Costo inversi.{1,3}n estaciones"),
        "c_subestacion": _costo(t, r"Costo inversi.{1,3}n subestaci.{1,3}n"),
        "c_gen_inv": _costo(t, r"Costo inversi.{1,3}n generaci.{1,3}n"),
        "c_gen_op": _costo(t, r"Costo operaci.{1,3}n generaci.{1,3}n"),
        "c_bess_inv": _costo(t, r"Costo inversi.{1,3}n BESS"),
        "c_bess_op": _costo(t, r"Costo operaci.{1,3}n BESS"),
        "c_reemplazo": _costo(t, r"Costo reemplazo bater.{1,3}a"),
        "c_total": _costo(t, r"COSTO TOTAL"),
    }


def parametros(f, carpeta):
    x = pd.ExcelFile(io.BytesIO(f.leer_bytes(f"{f.data}/{carpeta}/elmo_data.xlsx")))
    hoja = lambda s: pd.read_excel(x, sheet_name=s, header=0).iloc[1]   # fila 0 = unidades
    st, bd = hoja("stations"), hoja("BatteryDegradation")
    return {"cargador_kw": float(hoja("chargers")["charger_power"]),
            "c_inv_h": float(hoja("Storage")["c_inv"]),
            "c_inv_ssee": float(st["c_inv_ssee"]),
            "r": float(bd["discount_rate"]), "gamma": float(bd["gamma_coef"]),
            "c_bat": float(bd["c_bat_replace"]), "piso": float(bd["min_capacity_fraction"]),
            "b_max": float(hoja("LHD")["battery_capacity"])}


# ---------------------------------------------------------------------------
# Recalculo de la degradacion con los ciclos FISICOS
# ---------------------------------------------------------------------------
# Las dos ramas modelan igual la degradacion por año (functions.py, ec. 2-5 y
# b_y_link_lower / b_y_replace):
#   N[y]    = E[y] / (b_bar[y] * flota[y])     ciclos equivalentes por bateria
#   D[y]    = b_bar[y] - gamma * N[y]          capacidad al cierre del año
#   D[y], b_bar[y] >= piso * b_max             (cotas B_L de b_bar y D)
#   b_bar[y1] = b_max; sin reemplazo b_bar[y] = D[y-1]; con reemplazo b_bar[y] = b_max
#   costo   = sum_{y con R} flota[y] * c_bat_replace / (1+r)^y   (year_position 1-indexada)
# En swap E es EnergyConsumed[y] * 365/dias y flota = n_battery_fleet (pool + LHD
# activos); en OB, S[y] y n_elhd. La ec. N*b_bar*flota = E es trilineal en swap y
# su McCormick con cota global deja contar ~40-50 % de los ciclos (ver la memoria
# del proyecto); aca se recalcula con la igualdad exacta, dejando FIJA la operacion
# (energia y flota de cada año) y eligiendo el plan de reemplazos de menor costo
# entre los 2^(N-1) posibles. La operacion NO se reoptimiza: con menos capacidad
# podria necesitar mas swaps/cargas, asi que el costo recalculado es una
# estimacion, no un UB factible garantizado.

def _por_anio(d):
    d = d["y"] if "y" in d else (next(iter(d.values())) if len(d) == 1 else d)
    return {int(k): float(v) for k, v in d.items()}


def plan_reemplazos(E, flota, p):
    anios = sorted(E)
    piso = p["piso"] * p["b_max"]
    mejor = None
    for mascara in range(2 ** (len(anios) - 1)):
        R = {a for i, a in enumerate(anios[1:]) if mascara >> i & 1}
        b, traza, ok = p["b_max"], [], True
        for a in anios:
            if a in R:
                b = p["b_max"]
            n = E[a] / (b * flota[a])
            d = b - p["gamma"] * n
            if d < piso - 1e-9:
                ok = False
                break
            traza.append((a, b, n, d))
            b = d
        if not ok:
            continue
        costo = sum(flota[a] * p["c_bat"] / (1 + p["r"]) ** a for a in R)
        if mejor is None or costo < mejor[0] - 1e-6:
            mejor = (costo, sorted(R), traza)
    if mejor is None:
        return None
    return {"costo": mejor[0], "anios": mejor[1], "traza": mejor[2],
            "ciclos_10": sum(t[2] for t in mejor[2])}


def degradacion_fisica(f, sol, p, dias_por_anio=4):
    """E y flota de la solucion `sol` y su plan de reemplazos con ciclos fisicos."""
    if f.nombre == "Swap":
        E = {a: v * 365.0 / dias_por_anio for a, v in _por_anio(json.loads(f.leer(f"{sol}/EnergyConsumed.json"))).items()}
        flota = _por_anio(json.loads(f.leer(f"{sol}/n_battery_fleet.json")))
    else:
        E = _por_anio(json.loads(f.leer(f"{sol}/S.json")))
        flota = {a: 4.0 for a in E}       # una bateria por LHD (n_elhd_bd = 4 en los 8 casos)
    nc = _por_anio(json.loads(f.leer(f"{sol}/N_ciclos.json")))
    plan = plan_reemplazos(E, flota, p)
    return {"plan": plan, "ciclos_modelo_10": sum(nc.values()), "flota": flota}


def _evolucion(v):
    """[1,1,1,2,...] -> '1 → 2 (año 4)'."""
    if not v:
        return "—"
    txt = str(v[0])
    for i in range(1, len(v)):
        if v[i] != v[i - 1]:
            txt += f" → {v[i]} (año {i + 1})"
    return txt


def caso(f, n, desc, cert, cons, datos, nota):
    c = certificado(f, cert)
    k = consumer(f, cons or cert)
    if abs(k["c_total"] - c["ub"]) > 1e-6 * c["ub"] + 0.01:
        sys.exit(f"ABORTA {f.nombre} caso {n}: consumer {k['c_total']:,.2f} != UB {c['ub']:,.2f}")
    p = parametros(f, datos)
    # Solucion certificada: en OB, la del consumer (en disco); en swap, la carpeta
    # mejorar_ub_* en git cuyo ub_final es el UB certificado (el consumer de los
    # casos 1-3 apunta a v3/, que no esta en git).
    sol = k["folder"]
    if f.nombre == "Swap":
        sol = None
        for m in ("mejorar_ub_2", "mejorar_ub_1"):
            try:
                ubm = json.loads(f.leer(f"{O}/{cert}/{m}/resumen.json"))["ub_final"]
            except (FileNotFoundError, KeyError, json.JSONDecodeError):
                continue
            if abs(ubm - c["ub"]) <= 1e-6 * c["ub"] + 0.01:
                sol = f"{O}/{cert}/{m}/solucion"
                break
        if sol is None:
            sys.exit(f"ABORTA Swap caso {n}: ninguna mejorar_ub_* reproduce el UB {c['ub']:,.2f}")
    dg = degradacion_fisica(f, sol, p)
    if dg["plan"] is None:
        sys.exit(f"ABORTA {f.nombre} caso {n}: ni reemplazando todos los años se respeta el piso")
    ub_fis = c["ub"] - k["c_reemplazo"] + dg["plan"]["costo"]
    return {"tecnologia": f.nombre, "caso": n, "descripcion": desc, "nota": nota, **c, **k,
            "solucion": sol, "c_reemplazo_fis": dg["plan"]["costo"],
            "reemplazos_fis_txt": ", ".join(map(str, dg["plan"]["anios"])) or "—",
            "ub_fis": ub_fis, "gap_fis_pct": 100.0 * (ub_fis - c["lb"]) / ub_fis,
            "ciclos_modelo_10": dg["ciclos_modelo_10"], "ciclos_fis_10": dg["plan"]["ciclos_10"],
            "traza_fis": dg["plan"]["traza"], "flota_bat": int(dg["flota"][max(dg["flota"])]),
            "cargador_kw": p["cargador_kw"],
            "cargadores": _evolucion(c["chargers"]), "bahias": _evolucion(c["bays"]),
            "baterias_pool": _evolucion(c["batteries"]),
            "subestacion_kw": k["c_subestacion"] * (1 + p["r"]) / p["c_inv_ssee"],
            "bess_kwh": 1000.0 * k["bess_inv_bruta"] * (1 + p["r"]) / p["c_inv_h"],
            "reemplazos_txt": ", ".join(map(str, k["reemplazos"])) or "—"}


def fmt(x, dec=0):
    return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--swap_ref", default="origin/battery_swapping_multiaño")
    ap.add_argument("--out", default="output/Resultados_finales_tesis/Mina_modelo/tablas_v3")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    fuentes = [(Fuente("OB", "data/Resultados_finales_tesis/Mina_modelo"), CASOS_OB, "resumen_ob"),
               (Fuente("Swap", "data/Tesis_final/Mina_modelo", a.swap_ref), CASOS_SWAP, "resumen_swap")]
    campos = ["caso", "descripcion", "nota", "ub", "ub_fis", "lb", "gap_pct", "gap_fis_pct", "cargadores",
              "cargador_kw", "bahias", "baterias_pool", "subestacion_kw", "solar_kw", "eolica_kw", "bess_kwh",
              "reemplazos_txt", "reemplazos_fis_txt", "red_mwh", "c_red", "c_punta", "c_estaciones",
              "c_subestacion", "c_gen_inv", "c_gen_op", "c_bess_inv", "c_bess_op", "c_reemplazo",
              "c_reemplazo_fis", "c_total", "flota_bat", "ciclos_modelo_10", "ciclos_fis_10", "solucion"]
    detalle = ("| Tecnología | # | Baterías | Ciclos/batería 10 años: modelo | físicos | Reemplazos: modelo "
               "| físicos | Costo reemplazos: modelo [USD] | físicos [USD] | Δ costo total |\n"
               "|---|---|---:|---:|---:|---|---|---:|---:|---:|\n")
    for f, casos, base in fuentes:
        filas = [caso(f, *c) for c in casos]
        swap = f.nombre == "Swap"
        cols = [c for c in campos if swap or c not in ("bahias", "baterias_pool", "ub_fis", "gap_fis_pct")]
        with open(os.path.join(a.out, f"{base}.csv"), "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", delimiter=";")
            w.writeheader()
            w.writerows(filas)
        if swap:
            cab = ("| # | Caso | Costo modelo (UB) [USD] | Costo con ciclos físicos [USD] | LB certificada [USD] "
                   "| Gap modelo | Gap con ciclos físicos | Cargadores | Bahías | Baterías pool | Subestación [kW] "
                   "| Solar [kW] | Eólica [kW] | BESS [kWh] | Reemplazo baterías (año): modelo → físico "
                   "| Energía de red [MWh] |\n|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|---:|---:|---|---:|\n")
            md = cab + "".join(
                f"| {r['caso']} | {r['descripcion']} | {fmt(r['ub'])} | **{fmt(r['ub_fis'])}** | {fmt(r['lb'])} "
                f"| {fmt(r['gap_pct'], 2)} % | **{fmt(r['gap_fis_pct'], 2)} %** "
                f"| {r['cargadores']} × {fmt(r['cargador_kw'])} kW | {r['bahias']} | {r['baterias_pool']} "
                f"| {fmt(r['subestacion_kw'])} | {fmt(r['solar_kw'])} | {fmt(r['eolica_kw'])} | {fmt(r['bess_kwh'])} "
                f"| {r['reemplazos_txt']} → **{r['reemplazos_fis_txt']}** | {fmt(r['red_mwh'])} |\n" for r in filas)
        else:
            cab = ("| # | Caso | Costo total VP (UB) [USD] | LB certificada [USD] | Gap | Cargadores "
                   "| Subestación [kW] | Solar [kW] | Eólica [kW] | BESS [kWh] | Reemplazo baterías (año) "
                   "| Energía de red [MWh] |\n|---|---|---:|---:|---:|---|---:|---:|---:|---:|---|---:|\n")
            md = cab + "".join(
                f"| {r['caso']} | {r['descripcion']} | {fmt(r['ub'])} | {fmt(r['lb'])} | {fmt(r['gap_pct'], 2)} % "
                f"| {r['cargadores']} × {fmt(r['cargador_kw'])} kW | {fmt(r['subestacion_kw'])} "
                f"| {fmt(r['solar_kw'])} | {fmt(r['eolica_kw'])} | {fmt(r['bess_kwh'])} "
                f"| {r['reemplazos_txt']} | {fmt(r['red_mwh'])} |\n" for r in filas)
        detalle += "".join(
            f"| {f.nombre} | {r['caso']} | {r['flota_bat']} | {fmt(r['ciclos_modelo_10'])} | {fmt(r['ciclos_fis_10'])} "
            f"| {r['reemplazos_txt']} | {r['reemplazos_fis_txt']} | {fmt(r['c_reemplazo'])} "
            f"| {fmt(r['c_reemplazo_fis'])} | {fmt(r['c_reemplazo_fis'] - r['c_reemplazo'])} |\n" for r in filas)
        notas = "".join(f"- Caso {r['caso']}: {r['nota']}\n" for r in filas if r["nota"])
        open(os.path.join(a.out, f"{base}.md"), "w", encoding="utf-8").write(md + "\n" + notas)
        print(f"== {f.nombre}\n{md}\n{notas}")
        print(f"Escrito en {a.out}/{base}.csv y {base}.md\n")
    open(os.path.join(a.out, "recalculo_degradacion.md"), "w", encoding="utf-8").write(detalle)
    print("== Recalculo de la degradacion con ciclos fisicos (OB = validacion del metodo)\n" + detalle)


if __name__ == "__main__":
    main()
