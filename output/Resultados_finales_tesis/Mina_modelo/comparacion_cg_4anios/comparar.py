"""Comparacion a presupuesto igual (2 h c/u): monolitico, Nested Benders y CG
con mejoras, 4 años. Lee lo que dejo cada corrida en esta carpeta y escribe
comparacion.png (UB y LB contra el tiempo) y comparacion.json (valores a 10,
30, 60 y 120 min). Mismos colores que comparacion_cg_2anios.

    python comparar.py
"""
import json
import os
import re

import matplotlib.pyplot as plt

AQUI = os.path.dirname(os.path.abspath(__file__))
INF = float("inf")
COLORES = {"Monolítico": "#2a78d6", "Nested Benders": "#eb6834",
           "CG con mejoras": "#008300", "CG con mejoras, R coherente": "#e87ba4"}


def _num(s):
    return float(s.replace(",", ""))


def leer_mono():
    """Log de Gurobi: (t, incumbente, cota) por linea de nodo."""
    path = os.path.join(AQUI, "mono", "gurobi.log")
    if not os.path.exists(path):
        return []
    pts = []
    # Linea de nodo de Gurobi: ... Incumbent BestBd Gap It/Node Time, con "-"
    # en Incumbent/Gap/It mientras no hay solucion.
    patron = re.compile(r"^\s*[H*]?\s*\d+\s+\d+\s.*\s(\d+)s\s*$")
    for linea in open(path, encoding="utf-8", errors="replace"):
        m = patron.match(linea)
        if not m:
            continue
        tok = linea.split()
        try:
            inc = INF if tok[-5] == "-" else float(tok[-5])
            bd = float(tok[-4])
            pts.append((float(m.group(1)), inc, bd))
        except (ValueError, IndexError):
            continue
    return pts


def leer_nb():
    """Log de setup.py decomposed: cota operacional y UB/LB por iteracion,
    con el tiempo acumulado (preparacion + tiempo_iteracion)."""
    path = os.path.join(AQUI, "nb.log")
    if not os.path.exists(path):
        return []
    txt = open(path, encoding="utf-8", errors="replace").read()
    t = sum(float(x) for x in re.findall(r"LP monolitico = [\d,.]+\s+\((\d+)s\)", txt))
    m = re.search(r"cota operacional = ([\d,.]+)\s+\(.*?(\d+)s\)", txt)
    pts = []
    if m:
        t += float(m.group(2))
        pts.append((t, INF, _num(m.group(1))))
    for ub, lb, ti in re.findall(r"k=\d+\s+UB=([\d.]+)\s+LB=([\d.]+)\s+gap=\S+\s+"
                                 r"tiempo_iteracion=([\d.]+)s", txt):
        t += float(ti)
        pts.append((t, float(ub), float(lb)))
    return pts


def leer_cg(carpeta, archivo):
    path = os.path.join(AQUI, carpeta, archivo)
    if not os.path.exists(path):
        return []
    filas = json.load(open(path, encoding="utf-8"))
    pts = []
    for f in filas:
        ub = f.get("UB", INF)
        lb = f.get("LB", -INF)
        pts.append((float(f["t"]), INF if ub is None else float(ub),
                    -INF if lb is None else float(lb)))
    return pts


def acumular(pts):
    """Mejor UB y mejor LB hasta cada instante."""
    out, ub, lb = [], INF, -INF
    for t, u, l in sorted(pts):
        ub, lb = min(ub, u), max(lb, l)
        out.append((t, ub, lb))
    return out


def valor_en(serie, t):
    v = [(u, l) for tt, u, l in serie if tt <= t]
    return v[-1] if v else (INF, -INF)


def main():
    series = {"Monolítico": acumular(leer_mono()),
              "Nested Benders": acumular(leer_nb()),
              "CG con mejoras": acumular(leer_cg("cg", "trayectoria.json")),
              "CG con mejoras, R coherente": acumular(leer_cg("cg_v2", "trayectoria.json"))}
    series = {k: v for k, v in series.items() if v}
    tabla = {}
    for nombre, s in series.items():
        tabla[nombre] = {}
        for minutos in (10, 30, 60, 120):
            u, l = valor_en(s, 60 * minutos + 1)
            gap = (u - l) / u if u < INF and l > -INF else None
            tabla[nombre][f"{minutos} min"] = {"UB": None if u == INF else u,
                                               "LB": None if l == -INF else l, "gap": gap}
    json.dump(tabla, open(os.path.join(AQUI, "comparacion.json"), "w", encoding="utf-8"),
              indent=2)
    for nombre, filas in tabla.items():
        print(nombre)
        for k, v in filas.items():
            ub = f"{v['UB']:,.0f}" if v["UB"] else "-"
            lb = f"{v['LB']:,.0f}" if v["LB"] else "-"
            gp = f"{v['gap']:.2%}" if v["gap"] is not None else "-"
            print(f"   {k:>6}: UB {ub:>12}  LB {lb:>12}  gap {gp:>8}")

    graf = {k: k for k in COLORES}
    fig, ax = plt.subplots(figsize=(8, 4.6), dpi=200)
    sel = {graf[k]: (s, COLORES[k]) for k, s in series.items() if k in graf}
    ref = min(u for s, _c in sel.values() for _, u, _l in s)
    y0, y1 = ref * 0.98, ref * 1.02
    for nombre, (s, c) in sel.items():
        t = [p[0] / 60 for p in s]
        ax.step(t, [p[1] if p[1] < INF else None for p in s], where="post", color=c,
                lw=2, label=f"{nombre} (UB)")
        ax.step(t, [p[2] if p[2] > -INF else None for p in s], where="post", color=c,
                lw=2, ls="--", label=f"{nombre} (LB)")
        # Optimo demostrado: la LB alcanza a la UB (las dos lineas se tapan).
        cerr = next(((tt, u) for tt, u, l in s if u < INF and (u - l) / u <= 1e-4), None)
        if cerr:
            ax.plot(cerr[0] / 60, cerr[1], marker="o", ms=8, color=c,
                    markeredgecolor="white", markeredgewidth=2, zorder=5)
            ax.annotate(f"{nombre}: óptimo demostrado a los {cerr[0] / 60:.0f} min",
                        (cerr[0] / 60, cerr[1]), xytext=(8, 10), textcoords="offset points",
                        fontsize=7.5, color="#222222")
        if s[-1][1] >= INF:
            ax.text(8, y1 - 0.0008 * ref, f"{nombre}: sin solución factible en 2 h", ha="left",
                    va="top", fontsize=7.5, color="#222222")
        if y1 < s[-1][1] < INF:
            ax.text(122, y1 - 0.0008 * ref, f"UB {nombre}: {s[-1][1] / 1e6:.2f} M (fuera de escala)",
                    ha="right", va="top", fontsize=7, color="#444444")
    ax.set_ylim(y0, y1)
    ax.set_xlim(0, 124)
    ax.set_xlabel("Tiempo (min)", fontsize=9, color="#444444")
    ax.set_ylabel("Costo total (USD)", fontsize=9, color="#444444")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v / 1e6:.3f} M"))
    ax.grid(axis="y", color="#e6e6e6", lw=0.8)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    ax.tick_params(length=0, labelsize=8, colors="#444444")
    ax.set_title("Mina modelo RED, 4 años: cotas contra el tiempo", fontsize=11)
    ax.legend(fontsize=7, frameon=False, ncol=2, loc="upper right", bbox_to_anchor=(1.0, 0.74))
    fig.tight_layout()
    fig.savefig(os.path.join(AQUI, "comparacion.png"), facecolor="white")
    print("comparacion.png y comparacion.json escritos")


if __name__ == "__main__":
    main()
