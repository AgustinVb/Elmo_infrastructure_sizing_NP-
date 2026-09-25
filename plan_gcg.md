# Plan de implementación: GCG para el modelo inversión–operación

**Objetivo.** Evaluar si la descomposición de Dantzig–Wolfe (branch-and-price con GCG) produce mejores cotas inferiores que Gurobi y que Nested Benders, y usarla para cerrar el gap sobre el incumbente de Benders.

**Dependencias fijas**

| Paquete | Versión |
|---|---|
| PyGCGOpt | 1.0.0b0 |
| PySCIPOpt | 6.0.0 |

**Principio de diseño.** Lo más simple posible. Nada de integrar Benders y GCG en un mismo módulo: cada etapa es un comando independiente que se comunica con la siguiente mediante archivos.

```
[modelo actual] --export_mps.py--> model.mps
                                     |
[Nested Benders] --(nuevo flag)--> benders_best.sol
                                     |
                gcg_run.py  <--------+  (incumbente opcional)
                    |
              results.json + gcg.log + decomposition.dec
```

**Referencias de la instancia de 10 años**

| Referencia | Valor |
|---|---|
| LP monolítica (Gurobi) | 1.593.858,58 |
| BestBd monolítico Gurobi (9 h 14 min, nodo raíz) | 1.865.760,26 |
| Mejor UB Nested Benders | 2.172.572,69 |
| Gap actual (UB Benders vs BestBd Gurobi) | 14,12 % |

---

## Fase 0. Entorno

1. Instalar las versiones fijadas y verificar que el import funcione:
   ```bash
   pip install "PySCIPOpt==6.0.0" "PyGCGOpt==1.0.0b0"
   python -c "import pygcgopt as gcg; m = gcg.Model(); print('ok')"
   ```
2. PyGCGOpt 1.0.0b0 es una versión beta. Antes de cualquier corrida larga, confirmar con un modelo de juguete que existen estos métodos, porque el resto del plan depende de ellos: `readProblem`, `getConss`, `getValsLinear`, `createPartialDecomposition`, `fixConssToMaster`, `fixConssToBlock`, `addPreexistingPartialDecomposition`, `readSolFile`, `addSol`, `setObjlimit`, `getDualbound`, `getDualboundRoot`.

**Entregable:** un script `check_env.py` que imprima qué métodos existen.

---

## Fase 1. Exportar el monolítico a MPS

No reescribir el modelo en pygcgopt. GCG lee el mismo modelo que ya se usa con Gurobi.

1. Crear `export_mps.py`, que construya el monolítico **linealizado** (con McCormick, igual que en Benders) usando el constructor actual y lo escriba en formato MPS.
   ```bash
   python export_mps.py --years 1  --free_charging --out runs/y01/model.mps
   python export_mps.py --years 2  --free_charging --out runs/y02/model.mps
   python export_mps.py --years 10 --free_charging --out runs/y10/model.mps
   ```
2. **Nombres.** Toda la clasificación de la Fase 2 depende de los nombres de las restricciones. Abrir las primeras líneas de la sección `ROWS` del MPS y confirmar cómo quedaron escritos (por ejemplo, `state_unique_elhd[1,3,2,15]`). Si la herramienta de modelado los transforma, ajustar la expresión regular de la Fase 2.
3. **Opcional:** fijar X_{k,y} = 1 en la exportación (hay una sola nave y la producción exige carga desde el primer año). Esto elimina el acople de (3.10) entre la inversión y cada bloque.

**Entregable:** los tres archivos MPS y una nota con el formato de nombres observado.

---

## Fase 2. Definir la descomposición (manual)

Se replica el mecanismo del código del colega (`add_truck_block_decomposition`), con dos cambios: las restricciones se leen del MPS en lugar de registrarse al construirlas, y los bloques son **LHD–día–año** en lugar de "camión".

### 2.1 Reglas (`decomp_rules.py`)

Cada prefijo de restricción va a un bloque (indicando qué posiciones del índice son i, y, d) o al maestro. Solo tres nombres son conocidos. El resto son marcadores que hay que reemplazar por los nombres reales.

```python
import re

# Ajustar si el MPS escribe los índices de otra forma
NAME_RE = re.compile(r"^(?P<prefix>[^\[]+)(?:\[(?P<idx>.*)\])?$")

# prefijo -> posiciones de (i, y, d) dentro del índice
BLOCK_RULES = {
    "state_unique_elhd":   (0, 1, 2),   # (3.75)
    "between_shifts_elhd": (0, 1, 2),   # (3.79)
    "det_stop_all":        (0, 1, 2),   # (3.88) / (3.90)
    # "charge_start_end":  (1, 2, 3),   # (3.14)-(3.16), índice k,i,y,d,t
    # "charge_min_dur":    (1, 2, 3),   # (3.17)
    # "charge_power":      (1, 2, 3),   # (3.18)
    # "soe_balance":       (0, 1, 2),   # (3.23)
    # "soe_bounds":        (0, 1, 2),   # (3.24)  <- contiene B_y (enlace)
    # "soe_cyclic":        (0, 1, 2),   # (3.25)
}

MASTER_PREFIXES = {
    "link_charger_stock", "ssee_discreta",
    # inversión (3.3)-(3.9), (3.92)-(3.93)
    # capacidad de cargadores (3.12), subestación (3.70)
    # balance de potencia (3.95), red (3.72)-(3.74)
    # producción (3.77)-(3.78), degradación (3.36)-(3.47)
    # generación y BESS (3.94), (3.97)-(3.101)
}

def parse(name):
    m = NAME_RE.match(name)
    idx = m.group("idx")
    return m.group("prefix"), tuple(s.strip() for s in idx.split(",")) if idx else ()

def classify(conss):
    master, blocks, unknown = [], {}, {}
    for c in conss:
        prefix, idx = parse(c.name)
        if prefix in MASTER_PREFIXES:
            master.append(c)
        elif prefix in BLOCK_RULES:
            key = tuple(idx[p] for p in BLOCK_RULES[prefix])
            blocks.setdefault(key, []).append(c)
        else:
            unknown[prefix] = unknown.get(prefix, 0) + 1
    return master, blocks, unknown
```

### 2.2 Reglas de validación (obligatorias antes de optimizar)

1. **Nada sin clasificar.** Si `unknown` no está vacío, el script aborta e imprime los prefijos faltantes. Una descomposición incompleta deja que GCG la complete por su cuenta.
2. **Reporte de variables de enlace.** Para cada restricción de bloque, leer sus variables con `getValsLinear` y contar las que aparecen en más de un bloque. Se esperan B_y (por (3.24)) y X_{k,y} (por (3.10), salvo que se fije). Si aparecen muchas otras, hay una restricción mal clasificada.
3. **Tamaño.** Imprimir el número de bloques (se esperan 160 en la instancia de 10 años), el número de restricciones del maestro y el número de variables de enlace.

### 2.3 Variante para battery swapping

Si se evalúa battery swapping, seguir la división del colega: el inventario de baterías (3.60)–(3.68) y (3.71) va al maestro, y el SOE con swap (3.26)–(3.35) va en el bloque del LHD.

---

## Fase 3. Script único de ejecución de GCG (`gcg_run.py`)

Un solo script sirve para las dos experiencias. La diferencia es el flag `--incumbent`.

```python
import argparse, json, os, time
import pygcgopt as gcg
from decomp_rules import classify

def safe(f, default=None):
    try:
        return f()
    except Exception:
        return default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mps", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--time-limit", type=float, default=36000)
    ap.add_argument("--gap", type=float, default=1e-4)
    ap.add_argument("--incumbent", default=None)       # .sol exportado por Benders
    ap.add_argument("--objlimit", type=float, default=None)
    ap.add_argument("--no-presolve", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    m = gcg.Model()
    m.setLogfile(os.path.join(a.out, "gcg.log"))
    m.readProblem(a.mps)
    m.setParam("limits/time", a.time_limit)
    m.setParam("limits/gap", a.gap)
    if a.no_presolve:
        m.setParam("presolving/maxrounds", 0)
    safe(lambda: m.setParam("detection/enabled", False))

    # Descomposición manual
    master, blocks, unknown = classify(m.getConss())
    if unknown:
        raise SystemExit(f"Restricciones sin clasificar: {unknown}")
    d = m.createPartialDecomposition()
    d.fixConssToMaster(master)
    for b, key in enumerate(sorted(blocks)):         # bloques numerados 0..n-1
        d.fixConssToBlock(blocks[key], b)
    m.addPreexistingPartialDecomposition(d)

    # Incumbente de Benders (Experiencia 2)
    if a.incumbent:
        sol = m.readSolFile(a.incumbent)
        m.addSol(sol)
    if a.objlimit is not None:
        m.setObjlimit(a.objlimit)

    t0 = time.time()
    m.optimize()
    res = {
        "status": str(safe(m.getStatus)),
        "primal": safe(m.getPrimalbound),
        "dual": safe(m.getDualbound),              # sin fallback a primal
        "dual_root": safe(m.getDualboundRoot),
        "gap": safe(m.getGap),
        "nodes": safe(m.getNNodes),
        "time_sec": time.time() - t0,
        "n_blocks": len(blocks),
        "n_master_conss": len(master),
    }
    json.dump(res, open(os.path.join(a.out, "results.json"), "w"), indent=2)

if __name__ == "__main__":
    main()
```

**Correcciones respecto del código del colega**

1. `get_best_bound()` devolvía el primal si `getDualbound()` fallaba. Aquí la cota dual se reporta como `None` si falla. Nunca debe confundirse con la UB.
2. `_parse_first_dual_bound()` lee la columna 13 del log, lo cual depende de la versión. Se reemplaza por `getDualboundRoot()`. Mantener el parser solo como respaldo y validarlo a mano contra un log real.
3. Escribir la descomposición usada (reutilizando `_write_selected_decomposition_file`) en `out/decomposition.dec`, para compararla con las reglas definidas.

---

## Fase 4. Experiencia 1: monolítico con GCG

**Objetivo:** medir la cota de Dantzig–Wolfe en el nodo raíz frente a la de Gurobi, escalando la instancia.

```bash
python gcg_run.py --mps runs/y01/model.mps --out runs/y01/gcg_mono --time-limit 7200  --no-presolve
python gcg_run.py --mps runs/y02/model.mps --out runs/y02/gcg_mono --time-limit 14400 --no-presolve
# Solo si 1 y 2 años justifican escalar:
python gcg_run.py --mps runs/y10/model.mps --out runs/y10/gcg_mono --time-limit 36000 --no-presolve
```

**Referencias de Gurobi en las mismas instancias.** Para 1 y 2 años hay que obtener la LP y el BestBd de Gurobi con el mismo límite de tiempo. Para 10 años ya se tienen (ver tabla inicial).

**Chequeos en el log de la primera corrida (1 año)**
- El número de bloques y de restricciones del maestro coincide con lo que reportó la validación de la Fase 2.
- GCG no ejecutó detección propia ni reemplazó la descomposición.
- Tiempo del LP del maestro en el nodo raíz. Es el principal riesgo, porque GCG usa SoPlex y no Gurobi.

---

## Fase 5. Experiencia 2: Benders y luego GCG (secuencial)

### 5.1 Exportar el incumbente desde Benders

Agregar un flag al comando actual de Benders (por ejemplo, `--export-sol ruta.sol`) que, al terminar, escriba la **mejor trayectoria completa** con los **nombres de variables del monolítico**, en formato de solución de SCIP:

```python
def write_scip_sol(values, objective, path):
    """values: {nombre_variable_monolitico: valor}"""
    with open(path, "w") as f:
        f.write(f"objective value: {objective:.10g}\n")
        for name, v in values.items():
            if abs(v) > 1e-9:
                f.write(f"{name} {v:.10g}\n")
```

- Incluir **todas** las variables: inversión, operación y auxiliares de McCormick. Las que falten se interpretan como cero y la solución puede quedar infactible.
- Excluir las variables propias de Benders que no existen en el monolítico: las copias `*_prev` y los θ.
- Redondear las variables enteras y binarias.

### 5.2 Validar el incumbente antes de usarlo

Con PySCIPOpt puro (sin GCG), para aislar errores de nombres o de valores:

```python
from pyscipopt import Model
m = Model(); m.readProblem("runs/y10/model.mps")
sol = m.readSolFile("runs/y10/benders_best.sol")
print(m.checkSol(sol, printreason=True, original=True))
```

Si reporta violaciones, corregir el mapeo de nombres en Benders antes de continuar.

### 5.3 Ejecución secuencial

```bash
<comando actual de Benders> --years 10 --free_charging --export-sol runs/y10/benders_best.sol
python gcg_run.py --mps runs/y10/model.mps --out runs/y10/gcg_warm \
                  --incumbent runs/y10/benders_best.sol --time-limit 36000 --no-presolve
```

**Verificar en el log** que SCIP aceptó la solución. Debe aparecer una línea del tipo `1/1 feasible solution given by solution candidate storage` y la cota primal inicial debe coincidir con la UB de Benders. Probar esto primero en la instancia de 1 año.

**Respaldo si GCG no acepta la solución:** correr con `--objlimit 2172572.69`. El solver poda todo nodo cuya cota supere la UB de Benders. Si termina sin encontrar una solución mejor, eso demuestra que ninguna solución es mejor que la de Benders (dentro de la tolerancia). La contrapartida es que el estado final aparecerá como "infeasible", y hay que interpretarlo en ese sentido.

---

## Fase 6. Comparación y criterios de decisión

### Tabla de resultados

| Instancia | Método | LB raíz | LB final | UB | Gap | Tiempo |
|---|---|---|---|---|---|---|
| 1 año | Gurobi | | | | | |
| 1 año | GCG monolítico | | | | | |
| 2 años | Gurobi | | | | | |
| 2 años | GCG monolítico | | | | | |
| 10 años | Gurobi | 1.593.858,58 (LP) | 1.865.760,26 | sin incumbente | – | 9 h 14 min |
| 10 años | Nested Benders | – | LP monolítica | 2.172.572,69 | 26,64 % | 3 h 42 min |
| 10 años | GCG monolítico | | | | | |
| 10 años | Benders → GCG | | | 2.172.572,69 | | |

Gap combinado reportable: (UB_Benders − máx(LB_GCG, BestBd_Gurobi)) / UB_Benders.

### Criterios

1. **¿Escalar a 10 años?** Solo si, en 1 y 2 años, la LB raíz de GCG supera claramente el BestBd de Gurobi en la misma instancia y el tiempo del nodo raíz es razonable.
2. **¿GCG aporta en 10 años?** Solo si su LB final supera 1.865.760,26.
3. **¿Vale la pena la experiencia 2?** Solo si GCG aporta cota pero no encuentra por sí mismo soluciones tan buenas como las de Benders.
4. **Si GCG no supera a Gurobi en la cota:** detenerse. El cuello de botella no está en la operación intra-anual y la línea siguiente es la de cortes lagrangeanos en Benders.

---

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El LP del maestro es lento en SoPlex | Escalar de 1 a 2 y luego 10 años; medir el tiempo del nodo raíz antes de corridas largas |
| Los bloques no son idénticos, así que no hay agregación | Aceptarlo. Si el número de bloques es un problema, probar bloques LHD–año (40) |
| El presolve altera la descomposición | Usar `--no-presolve` al inicio y comparar con el `.dec` escrito |
| Hay variables de enlace inesperadas | El reporte de la Fase 2.2 aborta antes de optimizar |
| Errores de nombres en la solución de Benders | Validación 5.2 con `checkSol` |
| GCG no acepta la solución inicial | Respaldo con `--objlimit` |
| La API de la versión beta difiere | `check_env.py` en la Fase 0 |

## Orden de trabajo

1. Fase 0 y Fase 1 (exportar 1, 2 y 10 años).
2. Fase 2 con la instancia de 1 año hasta que la validación pase limpia.
3. Fase 4 con 1 y 2 años, y decidir según el criterio 1.
4. Fase 5.1 y 5.2 (se pueden hacer en paralelo con la Fase 4).
5. Fase 4 y Fase 5.3 en 10 años, si los criterios lo justifican.
6. Llenar la tabla de la Fase 6.
