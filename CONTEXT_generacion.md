# Contexto de implementación: Generación renovable y almacenamiento

## Estado actual (2026-06-02)

Implementación funcional de generación solar (Solar_PV) integrada al modelo MILP de
sizing de infraestructura de carga. El modelo resuelve y produce `P_gen.json` y `P_red.json`
con valores coherentes.

---

## Arquitectura general

```
elmo_data.xlsx          → hoja Generators  (parámetros estáticos)
time_series.xlsx        → hoja GenProfiles (perfiles horarios alpha[g,d,t])
src/mine/generators.py  → clase Generators (getters)
src/mine/__init__.py    → self.generators, get_system_generators()
src/io/reader.py        → init['GenProfiles'] = 3
src/time_series/timeseries.py → sample_gen_profiles(), get_alpha_g(), build_mappers()
src/optimization/functions.py → sets, params, vars, constraints, objetivo
json_plotter.py         → plot_power_dispatch() (batch plotter)
```

---

## Hojas Excel requeridas

### `elmo_data.xlsx` → hoja `Generators`

| id | name     | c_inv       | c_op      | p_max | g_max |
|----|----------|-------------|-----------|-------|-------|
| 1  | Solar_PV | 653400000   | 8500000   | 1000  | 10    |

- `c_inv` [CLP/unidad], `c_op` [CLP/año/unidad], `p_max` [kW/unidad], `g_max` [unidades máx]
- Sin esta hoja el modelo corre igual (gen_set vacío, comportamiento idéntico al baseline)

### `time_series.xlsx` → hoja `GenProfiles`

| id | name     | day | 1   | 2   | ... | N   |
|----|----------|-----|-----|-----|-----|-----|
| 1  | Solar_PV | 1   | 0.0 | 0.0 | ... | 0.8 |

- Columnas `1..N` = intervalos del día, valores alpha ∈ [0,1]
- `init['GenProfiles'] = 3` en reader.py (id, name, day son metadatos)
- Se cachea como `GenProfiles.npy` igual que MarginalCost

**Nota de caché**: al modificar la hoja, borrar:
```
simple_time_series.xlsx, GenProfiles.npy, series.ini
```

---

## Modelo matemático

### Sets nuevos
```python
model.gen_set    # G — nombres de generadores (vacío si no hay hoja Generators)
```

### Parámetros nuevos
```python
model.c_inv_g[g]      # Costo inversión por unidad [CLP]
model.c_op_g[g]       # Costo O&M anual por unidad [CLP/año]
model.p_max_g[g]      # Potencia nominal por unidad [kW]
model.g_max_g[g]      # Cantidad máxima de unidades
model.alpha_g[g,d,t]  # Perfil disponibilidad ∈ [0,1]
model.costo_red[d,t]  # Costo energía de red [USD/kWh] (igual a costo_marginal del primer ELHD)
```

### Variables nuevas
```python
model.G_g[g]            # INTEGER ≥ 0  — unidades de generación instaladas
model.P_gen[g, d, t]    # CONT ≥ 0     — potencia generada [kW]
model.P_red[d, t]       # CONT ≥ 0     — potencia comprada a la red [kW]
```

### Restricciones nuevas / modificadas

| Nombre | Expresión |
|--------|-----------|
| `power_balance` | `P_red[d,t] + Σ_g P_gen[g,d,t] = Σ_{k,i} P[k,i,d,t]` ∀d,t |
| `grid_limit` | `P_red[d,t] ≤ p_peak` ∀d,t |
| `gen_limit` | `P_gen[g,d,t] ≤ G_g[g] · p_max_g[g] · alpha_g[g,d,t]` ∀g,d,t |
| `gen_max_units` | `G_g[g] ≤ g_max_g[g]` ∀g |
| `power_cost_peak_limit` | `P_red[d,t] ≤ P_pot[y]` ∀d,t ∈ THP ← **actualizada** (antes usaba Σ P) |

> `peak_power` (restricción anterior `Σ P ≤ p_peak`) fue **reemplazada** por
> `power_balance + grid_limit`. Está comentada en `build_all_constraints`.

### Función objetivo

```
min = lhd_charge_cost        ← Σ_{d,t} P_red[d,t] · costo_red[d,t] · Δt · scaling
    + inversion_cost          ← estaciones + cargadores (sin cambios)
    + gen_investment_cost     ← Σ_g G_g[g] · c_inv_g[g]
    + gen_op_cost             ← Σ_g G_g[g] · c_op_g[g]
    + F_penalty_cost          ← sin cambios
    + power_cost              ← P_pot · 12 · 10 (sin cambios)
```

> `lhd_charge_cost` antes calculaba `Σ P[k,i,d,t] · costo_marginal[i,d,t]`.
> Ahora usa `P_red[d,t] · costo_red[d,t]` — el costo recae sobre lo que
> efectivamente se compra a la red.

---

## Outputs del modelo

Archivos JSON generados por `printer.py` con los esquemas correctos:

| Archivo | Estructura JSON |
|---------|----------------|
| `P_red.json` | `d → {day} → t → {interval} → value` |
| `P_gen.json` | `g → {gen} → d → {day} → t → {interval} → value` |
| `G_g.json`   | `g → {gen} → value` (unidades instaladas) |

---

## Gráfico de despacho (batch plotter)

Añadido a `json_plotter.py` → método `plot_power_dispatch()`.
Se genera automáticamente al correr el batch plotter.

```powershell
python batch_plotter.py --root_dir "output/GX_pruebas" --mode DET
# o directamente:
python json_plotter.py --json_dir "output/GX_pruebas"
```

Salida: `output/GX_pruebas/plots/PowerDispatch_January.png`

El gráfico muestra bandas apiladas por intervalo de tiempo:
- **Naranja** = Solar_PV (P_gen)
- **Gris azulado** = Red eléctrica (P_red)
- Altura total = demanda total de carga (Σ P[k,i,d,t])

Colores definidos en `gen_colors` dentro de `plot_power_dispatch`:
```python
gen_colors = {
    "Solar_PV": "#F4A836",
    "Wind":     "#4FC3F7",
    "Diesel":   "#A5D6A7",
}
```
Tecnologías no listadas usan colores fallback (lila, teal, rojo, amarillo).

---

## Comando de ejecución

```powershell
python setup.py \
  --data_folder "data/Escenarios_Gx/" \
  --solver gurobi \
  --output_folder "output/GX_pruebas/"
```

---

## Pendiente / próximos pasos

- [ ] Almacenamiento BESS (conjuntos H, variables P_bat_h, A_h, H_h)
- [ ] Restricciones balance SOC del BESS
- [ ] Costo de inversión BESS en función objetivo
- [ ] Extender `GenProfiles` con más días representativos
- [ ] Agregar tecnología eólica (hoja `Generators` + perfil en `GenProfiles`)
- [ ] Validar que `G_g * p_max_g` no supere capacidad de subestación
