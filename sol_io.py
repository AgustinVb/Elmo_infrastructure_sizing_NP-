"""Fase 5.1 del plan GCG: escribir el incumbente de Nested Benders en formato
de solucion de SCIP, con los nombres de variable del MPS.

DESVIACION RESPECTO DEL PLAN, y por que
---------------------------------------
El plan proponia mapear a mano los nombres de las variables de los bloques de
Benders a los del monolitico. Eso es justo donde el propio plan pone el riesgo
"errores de nombres en la solucion de Benders".

Aca no hay mapeo manual. `NestedBendersSolver.build_report_model()` ya ensambla
un monolitico COMPLETO y le carga la mejor trayectoria; se vuelcan las
variables de ESE modelo, traducidas con el varmap.json que escribio
export_mps.py desde el symbol map del writer. Los nombres coinciden por
construccion, no por convencion.
"""
import json

from pyomo.environ import Var, value


def write_scip_sol(model, varmap_path, out_path, objective, tol=1e-9):
    """Vuelca las variables de `model` a `out_path` en formato .sol de SCIP.

    :param varmap_path: varmap.json de export_mps.py ({nombre_pyomo: etiqueta_mps})
    :param objective: valor a declarar en la cabecera
    :returns: (escritas, sin_valor, fuera_del_varmap)

    Las variables enteras y binarias se redondean: SCIP rechaza una solucion
    con 0.9999999 en una binaria. Las que valen cero se omiten, que es lo que
    el formato asume por defecto.
    """
    with open(varmap_path, encoding="utf-8") as f:
        varmap = json.load(f)

    escritas = sin_valor = fuera = 0
    lineas = []
    for vd in model.component_data_objects(Var, active=True):
        etiqueta = varmap.get(vd.name)
        if etiqueta is None:
            fuera += 1
            continue
        v = value(vd, exception=False)
        if v is None:
            sin_valor += 1
            continue
        if vd.is_integer() or vd.is_binary():
            v = float(round(v))
        if abs(v) > tol:
            lineas.append(f"{etiqueta} {v:.10g}")
            escritas += 1

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"objective value: {objective:.10g}\n")
        f.write("\n".join(lineas))
        f.write("\n")
    return escritas, sin_valor, fuera
