# Implementacion detallada del modelo de consumo WP2

Este documento describe como implementar y usar el modelo de consumo WP2 en
el proyecto Teniente. El objetivo es que la carga de consumos ya no se
calcule con la logica fisica interna de WP1, sino que se lea desde archivos
JSON precalculados por nodo, manteniendo compatibilidad con la formulacion y
con el resto del pipeline.

## 1. Objetivo funcional

WP2 reemplaza el calculo de consumos por viaje del pipeline clasico por una
tabla externa indexada por nodo. En la practica:

- cada nodo del sistema tiene un tiempo de ciclo predefinido;
- cada nodo tiene una energia por ciclo predefinida;
- el modelo sigue necesitando `n_trips`, `energy_consumption` y
  `diesel_consumption` para armar las restricciones y el costo;
- la estructura de la formulacion no cambia, solo cambia la fuente de datos.

Esto permite desacoplar el consumo de la geometria o de supuestos internos y
usar directamente las rutas y consumos ya validados en los JSON de datos.

## 2. Archivos que participan

Los puntos de integracion principales son:

- [setup.py](../setup.py)
- [src/time_series/timeseries.py](../src/time_series/timeseries.py)
- [src/optimization/functions.py](../src/optimization/functions.py)
- [src/optimization/opt_model.py](../src/optimization/opt_model.py)

Si necesitas seguir el contexto general del ajuste de Teniente, revisa tambien
[CAMBIOS_WP2_PAUSAS_Y_RUNNER.md](../CAMBIOS_WP2_PAUSAS_Y_RUNNER.md).

## 3. Interfaz de ejecucion

La activacion del modelo se controla desde la linea de comandos:

```powershell
--consumption_model {wp1,wp2}
--wp2_consumption_json <archivo.json>
```

### Comportamiento esperado

- `wp1` conserva el calculo fisico original.
- `wp2` lee consumos y tiempos precalculados desde un JSON.
- Si no se pasa `--wp2_consumption_json`, el sistema intenta inferir el JSON
  segun la tecnologia de la flota.
- Si la flota mezcla diesel y electrico, el JSON debe pasarse de forma
  explicita.

### Seleccion automatica del JSON

Cuando `--consumption_model wp2` no recibe archivo explicito:

- si toda la flota es diesel, se usa `diesel_routes_within_time.json`;
- si la flota no contiene diesel, se usa `electric_routes_within_time.json`.

El archivo se resuelve relativo a `data_folder` salvo que se entregue una ruta
absoluta.

## 4. Estructura del JSON WP2

El JSON debe estar indexado por `node_name` y contener, para cada nodo, un
objeto con estos campos:

```json
{
  "NODO_1": {
    "time_per_cycle_s": 480,
    "energy_per_cycle_kwh": 12.5
  },
  "NODO_2": {
    "time_per_cycle_s": 960,
    "energy_per_cycle_kwh": 20.1
  }
}
```

### Reglas de validacion

- el archivo debe existir;
- el archivo debe ser un objeto JSON de primer nivel;
- cada nodo requerido por el sistema debe estar presente;
- `time_per_cycle_s` debe ser numerico, finito y positivo;
- `energy_per_cycle_kwh` debe ser numerico, finito y positivo.

Si falta un nodo o hay una entrada invalida, el pipeline debe fallar antes de
formular la optimizacion.

## 5. Carga de datos en `Timeseries`

La logica central vive en [src/time_series/timeseries.py](../src/time_series/timeseries.py).

### 5.1. Carga del JSON por nodo

La funcion interna `_load_wp2_consumption_by_node` hace lo siguiente:

1. valida que se haya recibido ruta;
2. valida que el archivo exista;
3. lee y parsea el JSON;
4. verifica que el contenido sea un diccionario;
5. recorre todos los nodos requeridos por el sistema;
6. construye un diccionario auxiliar con `travel_hours` y `energy_kwh`;
7. acumula nodos faltantes o entradas invalidas y lanza un error unico si hay
   problemas.

La conversion de tiempo ocurre inmediatamente al leer el JSON:

```python
travel_hours = time_per_cycle_s / 3600.0
```

### 5.2. Construccion de `Trips`

La funcion `get_trips` cambia de comportamiento segun el modelo:

- con `wp1` llama a la rutina historica;
- con `wp2` usa el JSON por nodo y arma una tabla `Trips` con indice
  compuesto `(elhd, node)`.

Para cada combinacion de equipo y nodo:

- `travel_duration` se fija en `1` para conservar compatibilidad con la
  formulacion;
- `n_trips` se calcula con la misma regla de WP1, pero a partir del tiempo
  del JSON ya convertido a horas;
- `energy_consumption` toma el valor `energy_per_cycle_kwh`;
- `diesel_consumption` se calcula solo si el equipo es diesel.

### 5.3. Regla de `n_trips`

La implementacion preserva la regla existente:

```python
if travel_hours <= delta_t:
    n_trips = round(delta_t / travel_hours)
else:
    n_trips = 1
```

Eso asegura que WP2 siga alimentando la formulacion con el mismo tipo de
parametro que esperaba WP1.

### 5.4. Conversion de diesel

Si el `elhd` es diesel, la energia por ciclo se convierte a litros usando el
BSFC del equipo. La logica implementada es:

- leer `get_fuel_consumption(elhd)`;
- si el dato no es numerico o no es positivo, usar `230 g/kWh` como fallback;
- convertir de kWh a gramos;
- convertir de gramos a litros usando densidad `832 g/L`.

La formula efectiva es:

```python
diesel_liters = energy_kwh * bsfc_g_per_kwh / 832.0
```

## 6. Conexion con el modelo de optimizacion

La tabla `Trips` generada por `Timeseries` alimenta los parametros del modelo
en [src/optimization/functions.py](../src/optimization/functions.py).

### Parametros relevantes

La formulacion toma directamente:

- `d_i` desde `get_n_intervals_trip`;
- `n_trips` desde `get_n_trips`;
- `pe_i` desde `get_energy_consumption`;
- `pd_i` desde `get_diesel_consumption`.

Con eso, el resto del modelo sigue funcionando sin distinguir si el origen
del consumo fue WP1 o WP2.

### Eficiencias de bateria

El modelo tambien incorpora las eficiencias de carga y descarga que vienen en
los datos maestros:

- para onboard: `eta_charge_b` y `eta_discharge_b`;
- para swap: `s_eta_charge_b` y `s_eta_discharge_b`.

Aunque esto no pertenece a WP2 estrictamente, es parte del flujo completo de
consumo y balance energetico que usa los valores obtenidos en `Trips`.

## 7. Punto de entrada del solver

En [src/optimization/opt_model.py](../src/optimization/opt_model.py) se pasan
los parametros del solver, incluyendo `TimeLimit` cuando existe. Esto no
modifica WP2, pero forma parte del flujo recomendado para ejecutar escenarios
con datos precalculados:

- `MIPGap`;
- `Threads`;
- `NodefileStart`;
- `SoftMemLimit`;
- `Heuristics`;
- `TimeLimit`.

WP2 se beneficia de este esquema porque el tiempo de preprocesamiento baja y
el peso de la optimizacion queda concentrado en la formulacion.

## 8. Pasos de implementacion recomendados

Si quieres reproducir WP2 en otro escenario o en una rama nueva, sigue este
orden:

1. Crear o validar el JSON por nodo con `time_per_cycle_s` y
   `energy_per_cycle_kwh`.
2. Verificar que el JSON cubra todos los nodos del sistema.
3. Exponer `--consumption_model wp2` y `--wp2_consumption_json` en la CLI.
4. Resolver la ruta del JSON relativa a `data_folder` cuando no sea absoluta.
5. Cargar el JSON dentro de `Timeseries` antes de construir `Trips`.
6. Convertir `time_per_cycle_s` a horas y calcular `n_trips`.
7. Asignar `travel_duration = 1` para no romper la formulacion existente.
8. Calcular `diesel_consumption` a partir de `energy_consumption` solo en
   equipos diesel.
9. Pasar los parametros derivados al modelo de optimizacion.
10. Ejecutar una validacion de cobertura de nodos y de no nulos en los
    campos consumidos por la formulacion.

## 9. Validaciones minimas

Antes de considerar listo el modelo, conviene comprobar al menos lo
siguiente:

- el archivo JSON existe y se puede leer;
- todos los nodos requeridos estan presentes;
- no hay valores nulos en `n_trips`, `energy_consumption` ni
  `diesel_consumption`;
- los escenarios diesel usan el JSON diesel y los electricos usan el JSON
  electrico;
- un escenario mixto obliga a pasar `--wp2_consumption_json`.

Si el pipeline falla en esta etapa, el problema suele estar en el JSON, en el
nombre de un nodo o en la tecnologia detectada para la flota.

## 10. Ejemplo de ejecucion

```powershell
python setup.py --consumption_model wp2 --objective cost --solver gurobi
```

Si quieres fijar el JSON manualmente:

```powershell
python setup.py --consumption_model wp2 --wp2_consumption_json data_teniente_diesel\diesel_routes_within_time.json
```

Para un escenario electrico:

```powershell
python setup.py --consumption_model wp2 --wp2_consumption_json data_teniente_onboard\electric_routes_within_time.json
```

## 11. Resultado esperado

Con WP2 activo, el sistema debe:

- leer consumos y tiempos directamente desde el JSON;
- construir la tabla `Trips` sin recalcular rutas internas;
- mantener compatible la formulacion de optimizacion;
- preservar la conversion a diesel y el resto de balances energeticos;
- fallar temprano si falta informacion de un nodo.

En otras palabras, WP2 cambia la fuente de verdad del consumo, no la
estructura del modelo.