"""Fase 2 del plan GCG: a que bloque va cada restriccion del monolitico.

DESVIACION RESPECTO DEL PLAN, y por que
---------------------------------------
El plan clasificaba parseando el nombre de la restriccion leido del MPS, con
una expresion regular sobre corchetes y comas. Eso NO funciona con lo que
Pyomo escribe. Su writer con symbolic_solver_labels produce:

    c_u_state_unique_elhd(LH518B_1_1_15_1_0)_

o sea: prefijo de direccion (c_u_/c_l_/c_e_), parentesis en vez de corchetes,
**guion bajo como separador de indices**, y guion bajo final. El indice es
irrecuperable: el nombre del equipo (LH518B_1) ya contiene un guion bajo, y el
intervalo 1.0 se escribe 1_0. No hay forma de saber donde termina uno y
empieza el otro.

Asi que la clasificacion se hace sobre el modelo de PYOMO, donde el indice es
una tupla de verdad (('LH518B_1', 1, 15, 1.0)), y export_mps.py traduce cada
restriccion a su etiqueta exacta del MPS via el symbol map del writer. GCG
despues solo compara cadenas, sin parsear nada.

Bloques: (equipo, anio, dia) -- LHD-dia-anio, como pide el plan. Con 4 eLHD,
4 dias representativos y 10 anios son 160 bloques.
"""

# prefijo -> posiciones de (i, y, d) dentro de la tupla de indice.
# Verificado contra ConstraintRules.build_all_constraints (functions.py).
BLOCK_RULES = {
    # indice (i, y, d, t)
    "state_unique_elhd":                      (0, 1, 2),
    "between_shifts_elhd":                    (0, 1, 2),
    "det_stop_all":                           (0, 1, 2),
    "battery_soc":                            (0, 1, 2),
    "battery_lower":                          (0, 1, 2),   # contiene b_bar[y] -> enlace
    "battery_upper":                          (0, 1, 2),   # contiene b_bar[y] -> enlace
    # indice (i, y, d)
    "battery_boundary":                       (0, 1, 2),
    # indice (i, j, y, d, t) -- OJO: el nodo j ocupa la posicion 1
    "interval_extraction_M":                  (0, 2, 3),
    # indice (k, i, y, d, t) -- ZCHARGE_DAYS_TIME_INDEX
    "station_existence_constraint":           (1, 2, 3),   # contiene X[k,y] -> enlace
    "charge_state":                           (1, 2, 3),
    "min_charge_duration":                    (1, 2, 3),
    "max_power":                              (1, 2, 3),
    "charge_only_meal_or_between_shifts_det": (1, 2, 3),
    "no_charge_maintenance_det":              (1, 2, 3),
}

# Todo lo que acopla varios equipos, varios anios, o decide inversion.
MASTER_PREFIXES = {
    # inversion y stock
    "max_n_chargers", "link_station_stock", "apertura_solo_primer_anio",
    "link_charger_stock", "ssee_discreta",
    # capacidad compartida: suman Z_charge / potencia sobre TODOS los equipos
    # de la nave en cada (k, y, d, t)
    "charger_limit", "max_installed_capacity",
    # balance de potencia del sistema en cada (y, d, t)
    "power_balance", "grid_limit", "power_cost_peak_limit",
    # generacion renovable y almacenamiento estacionario
    "gen_limit", "gen_max_units", "max_storage_units",
    "bess_power_upper", "bess_power_lower", "bess_soc_balance",
    "bess_soc_init", "bess_soc_upper", "bess_soc_lower", "bess_soc_cyclic",
    # degradacion de bateria (anual) y su envolvente de McCormick
    "s_def", "d_y_fade", "n_ciclos_link", "b_y_link",
    "mccormick_lb1", "mccormick_lb2", "mccormick_ub1", "mccormick_ub2",
    "mccormick_energy",
    # produccion: acopla todos los equipos del (y, d)
    "daily_production", "production",
    # rompe-simetria entre DOS equipos: no es separable por bloque
    "battery_soc_break_symmetry",
}

MASTER = "__master__"


def clasificar_pyomo(model):
    """Recorre las restricciones ACTIVAS del modelo Pyomo y devuelve

        (asignacion, desconocidos)

    donde `asignacion` es {ConstraintData: clave_de_bloque | MASTER} y
    `clave_de_bloque` es la tupla (equipo, anio, dia). `desconocidos` es
    {prefijo: cuantas}, y si no esta vacio el llamador DEBE abortar: una
    descomposicion incompleta deja que GCG la complete por su cuenta, que es
    justo lo que se quiere evitar.
    """
    import pyomo.environ as pyo

    asignacion, desconocidos = {}, {}
    for cd in model.component_data_objects(pyo.Constraint, active=True):
        prefijo = cd.parent_component().local_name
        if prefijo in MASTER_PREFIXES:
            asignacion[cd] = MASTER
            continue
        posiciones = BLOCK_RULES.get(prefijo)
        if posiciones is None:
            desconocidos[prefijo] = desconocidos.get(prefijo, 0) + 1
            continue
        idx = cd.index()
        if not isinstance(idx, tuple):
            idx = (idx,)
        asignacion[cd] = tuple(idx[p] for p in posiciones)
    return asignacion, desconocidos
