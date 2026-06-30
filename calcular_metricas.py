"""
Calcula metricas resumen (infraestructura, extraccion, energia y costos) a partir
de los JSON exportados por el Printer en una carpeta de output (parameters.json +
N_chargers.json, N_bays.json, N_batteries.json, X.json, P_red.json, M.json, P_pot.json).

Uso:
    python calcular_metricas.py output/carpeta_1 [output/carpeta_2 ...]

Si se pasan varias carpetas (ej. los 3 macrobloques), tambien imprime el total
combinado.
"""
import argparse
import json
import os


def load_json(folder, name):
    path = os.path.join(folder, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def flatten(d):
    """Recorre un dict anidado de variables exportadas y devuelve la lista
    plana de valores hoja (numeros)."""
    vals = []

    def rec(node):
        if isinstance(node, dict):
            for v in node.values():
                rec(v)
        else:
            vals.append(node)

    rec(d)
    return vals


def unwrap_single(d):
    """ Los parametros con un solo eje quedan exportados como {'_1': {...}}.
    Esta funcion 'pela' esas capas de un solo elemento con clave '_N'. """
    while isinstance(d, dict) and len(d) == 1:
        only_key = next(iter(d))
        if only_key.startswith('_'):
            d = d[only_key]
        else:
            break
    return d


def get_station_keys(varjson):
    """ Devuelve la lista de nombres de estacion presentes en un JSON indexado
    por 'k' (X, N_chargers, N_batteries) o por el primer eje (N_bays). """
    if varjson is None:
        return []
    if 'k' in varjson:
        return list(varjson['k'].keys())
    # N_bays se exporta con clave '_1' en vez de 'k'
    inner = unwrap_single(varjson)
    return list(inner.keys()) if isinstance(inner, dict) else []


def get_station_value(varjson, station, default=0):
    if varjson is None:
        return default
    if 'k' in varjson:
        return varjson['k'].get(station, default)
    inner = unwrap_single(varjson)
    return inner.get(station, default) if isinstance(inner, dict) else default


def compute_metrics(folder):
    params = load_json(folder, 'parameters')
    if params is None:
        raise FileNotFoundError(f"No se encontro parameters.json en {folder}")

    delta_t = params['delta_t']
    scaling = params.get('scaling_factor_op_cost', 1.0)
    station_cost_k = unwrap_single(params['station_cost_k'])
    c_bays_k = unwrap_single(params['c_bays_k'])
    c_crane_k = unwrap_single(params['c_crane_k'])
    c_charger_space_k = unwrap_single(params['c_charger_space_k'])
    c_battery_space_k = unwrap_single(params['c_battery_space_k'])
    charger_cost = params['charger_cost']
    battery_cost = params['battery_cost']
    costo_elec_by_day = unwrap_single(params['costo_electricidad'])

    nch = load_json(folder, 'N_chargers')
    nbat = load_json(folder, 'N_batteries')
    nbays = load_json(folder, 'N_bays')
    x = load_json(folder, 'X')
    pred = load_json(folder, 'P_red')
    m = load_json(folder, 'M')
    ppot = load_json(folder, 'P_pot')

    n_chargers = sum(flatten(nch)) if nch else 0
    n_batteries = sum(flatten(nbat)) if nbat else 0
    n_bays = sum(flatten(nbays)) if nbays else 0
    extraccion_total = sum(flatten(m)) if m else 0.0

    energia_kwh = sum(flatten(pred)) * delta_t if pred else 0.0

    costo_carga = 0.0
    if pred:
        for d_key, d_block in pred['d'].items():
            t_map = d_block['t']
            ce_t_map = unwrap_single(costo_elec_by_day[d_key])
            for t_key, p_val in t_map.items():
                costo_carga += p_val * ce_t_map[t_key] * delta_t
        costo_carga *= scaling

    costo_potencia = sum(flatten(ppot)) * 12 * 10 if ppot else 0.0

    costo_inversion = 0.0
    for station in get_station_keys(x):
        sck = station_cost_k.get(station, 0)
        cbk = c_bays_k.get(station, 0)
        cck = c_crane_k.get(station, 0)
        ccsk = c_charger_space_k.get(station, 0)
        cbsk = c_battery_space_k.get(station, 0)

        x_v = get_station_value(x, station)
        n_bays_v = get_station_value(nbays, station)
        n_ch_v = get_station_value(nch, station)
        n_bat_v = get_station_value(nbat, station)

        costo_inversion += (sck * x_v
                             + cbk * n_bays_v
                             + cck * n_bays_v
                             + (charger_cost + ccsk) * n_ch_v
                             + (battery_cost + cbsk) * n_bat_v)

    costo_total = costo_carga + costo_potencia + costo_inversion

    return {
        'cargadores': n_chargers,
        'bahias': n_bays,
        'baterias': n_batteries,
        'extraccion_total': extraccion_total,
        'energia_kwh': energia_kwh,
        'costo_carga': costo_carga,
        'costo_potencia': costo_potencia,
        'costo_inversion': costo_inversion,
        'costo_total': costo_total,
    }


def print_metrics(label, metrics):
    print(f"\n=== {label} ===")
    print(f"  Cantidad de cargadores : {metrics['cargadores']:.0f}")
    print(f"  Cantidad de bahias     : {metrics['bahias']:.0f}")
    print(f"  Cantidad de baterias   : {metrics['baterias']:.0f}")
    print(f"  Extraccion total       : {metrics['extraccion_total']:,.2f}")
    print(f"  Consumo energia [kWh]  : {metrics['energia_kwh']:,.2f}")
    print(f"  Costo carga baterias   : {metrics['costo_carga']:,.2f} USD")
    print(f"  Costo por potencia     : {metrics['costo_potencia']:,.2f} USD")
    print(f"  Costo de inversion     : {metrics['costo_inversion']:,.2f} USD")
    print(f"  Costo total            : {metrics['costo_total']:,.2f} USD")


def main():
    parser = argparse.ArgumentParser(description='Calcula metricas resumen de una o varias carpetas de output')
    parser.add_argument('folders', nargs='+', help='Carpetas de output a procesar')
    args = parser.parse_args()

    total = None
    for folder in args.folders:
        metrics = compute_metrics(folder)
        print_metrics(folder, metrics)
        if total is None:
            total = dict(metrics)
        else:
            for k in total:
                total[k] += metrics[k]

    if len(args.folders) > 1:
        print_metrics('TOTAL COMBINADO', total)


if __name__ == '__main__':
    main()
