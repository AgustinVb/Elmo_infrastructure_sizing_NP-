"""Particion de la mina en macrobloques y reparto de los recursos compartidos.

Un macrobloque es UNA nave de carga junto con los equipos asignados a ella
(hoja StationAssignment) y los puntos de extraccion que esos equipos atienden
(hoja NodeAssignment). Como la asignacion equipo-nave es estatica, la
particion queda determinada por los datos de entrada y no requiere ninguna
decision del modelo -- mismo criterio que usa
`run_macrobloques_decomposicion.py` en la rama battery_swapping, que es el
algoritmo con el que se resuelve DCH hoy.

Una vez fijada esa particion, el subproblema de un año NO queda separado del
todo: quedan tres acoples entre macrobloques, que este modulo reparte en
cuotas calculadas directamente de los datos (sin resolver ningun MIP, igual
que el reparto de la meta de produccion del script de un año):

1. La meta de produccion diaria de la mina (`daily_production` exige que la
   extraccion total del dia cubra la suma de los m_j de TODOS los nodos). Si
   se restringe `nodes_set` a los nodos del macrobloque sin mas, cada
   macrobloque queda obligado a cubrir por si solo la suma de sus propios
   nodos, lo que elimina la compensacion entre macrobloques que el modelo
   completo si permite (uno produce de mas y otro de menos). Se reparte con
   el mismo water-filling de un paso del script de un año: cada macrobloque
   arranca en su produccion minima alcanzable y el deficit restante se
   reparte en proporcion a su holgura (ver `split_daily_targets`).

2. El balance de potencia y los limites electricos globales (`power_balance`,
   `grid_limit` con p_peak, `power_cost_peak_limit` con P_pot). Se reparten
   dandole a cada macrobloque una cuota `share` de la potencia disponible
   (ver `compute_power_shares`).

3. La generacion renovable y el almacenamiento (G_g, H), que son decisiones
   unicas de toda la mina. En los años y > y1 ya vienen fijadas desde y1, asi
   que lo unico que hay que repartir es su APORTE de potencia, con la misma
   cuota `share`.

Como las cuotas suman 1, la solucion recombinada satisface los limites
globales originales: sum_k P_red_k <= sum_k share_k * p_peak = p_peak, y lo
mismo para el aporte de generacion y de almacenamiento. Es decir, el reparto
RESTRINGE el problema del año -- por eso solo se usa en la fase forward,
donde basta con obtener una solucion factible (cota superior), y nunca en la
backward, donde un problema restringido daria un corte invalido (sobreestima
el costo del año, y el corte dejaria de ser una cota inferior del costo
futuro).
"""


def build_macroblocks(mine_system, time_series, years):
    """Particiona la mina en macrobloques: {estacion: {"lhds": [...],
    "nodes": [...]}}.

    Usa los mismos mappers que ya construye OptRules.__init__
    (Stations_per_elhd / elhd_per_station y la asignacion de nodos), asi que
    no vuelve a leer los datos de entrada.

    Un equipo asignado a mas de una nave rompe la particion (el macrobloque
    dejaria de ser independiente): se aborta en vez de repartirlo en silencio,
    porque el resultado ya no seria comparable con el modelo completo.
    """
    lhds = mine_system.get_system_lhds()
    stations = mine_system.get_system_stations()
    time_series.get_station_assignment(lhds)
    time_series.get_elhd_at_station(stations)

    stations_per_elhd = time_series.mapper.get('Stations_per_elhd', {})
    elhd_per_station = time_series.mapper.get('elhd_per_station', {})

    shared = {e: sts for e, sts in stations_per_elhd.items() if len(sts) > 1}
    if shared:
        raise ValueError(
            "La descomposicion por macrobloque requiere que cada equipo este "
            "asignado a UNA sola nave (la particion debe ser exacta). Equipos "
            f"con mas de una nave: {sorted(shared)}."
        )

    macroblocks = {}
    for station in stations:
        station_lhds = sorted(elhd_per_station.get(station, []))
        if not station_lhds:
            # Nave sin equipos asignados: no se construye (X exogeno ya la
            # deja fuera, ver driver.infer_exogenous_stations).
            continue
        macroblocks[station] = {
            "lhds": station_lhds,
            "nodes": sorted(nodes_of_lhds(time_series, station_lhds, years)),
        }

    if not macroblocks:
        raise ValueError(
            "No se pudo construir ningun macrobloque: ninguna nave tiene "
            "equipos asignados en StationAssignment."
        )

    _check_partition_is_exact(mine_system, time_series, macroblocks, years)
    return macroblocks


def nodes_of_lhds(time_series, lhd_names, years):
    """Nodos de extraccion que atienden esos equipos, en cualquier año del
    horizonte (la asignacion nodo-equipo si puede cambiar por año, ver
    Timeseries._nodes_for_elhd_year)."""
    nodes = set()
    for lhd in lhd_names:
        for year in years:
            nodes.update(time_series._nodes_for_elhd_year(lhd, year))
    return nodes


def _check_partition_is_exact(mine_system, time_series, macroblocks, years):
    """Un nodo atendido por equipos de dos naves distintas tambien rompe la
    particion: su meta de produccion quedaria contada en dos macrobloques a
    la vez (y el corte del año dejaria de reconstruirse por suma)."""
    owner = {}
    for station, block in macroblocks.items():
        for node in block["nodes"]:
            if node in owner and owner[node] != station:
                raise ValueError(
                    f"El punto de extraccion '{node}' es atendido por equipos "
                    f"de dos naves distintas ('{owner[node]}' y '{station}'), "
                    "asi que la particion en macrobloques no es exacta y su "
                    "meta de produccion quedaria contada dos veces."
                )
            owner[node] = station


def compute_power_shares(mine_system, time_series, macroblocks, years):
    """Cuota `share_k` de la potencia disponible (p_peak, P_pot, aporte de
    G_g y de H) que recibe cada macrobloque, proporcional a la DEMANDA
    ENERGETICA de los equipos asignados a esa nave.

    La demanda se estima con los mismos datos con que el modelo arma el
    consumo de traccion: para cada nodo, las visitas que hacen falta para
    cumplir su meta, multiplicadas por la energia de cada visita,

        visitas(j,y) = m_j,y / (g_i * n_trips(j,i) * filling_i)
        energia(j,i) = pe_i(j,i) * d_i(j,i) * n_trips(j,i)

    de donde el producto se simplifica a m_j,y * pe_i * d_i / (g_i * filling_i).
    Se promedia sobre los años modelados para que la cuota no dependa de un
    año particular: el reparto es fijo durante toda la corrida, porque si
    cambiara entre iteraciones los cortes acumulados dejarian de referirse al
    mismo problema.

    Las cuotas suman exactamente 1, que es lo que garantiza que la solucion
    recombinada respete los limites globales del modelo original.
    """
    demand = {}
    for station, block in macroblocks.items():
        demand[station] = sum(
            _macroblock_energy_demand(mine_system, time_series, block, y)
            for y in years
        )

    total = sum(demand.values())
    if total <= 0:
        # Sin informacion de consumo utilizable: reparto uniforme, para no
        # dejar la corrida sin cuotas.
        n = len(macroblocks)
        return {station: 1.0 / n for station in macroblocks}

    shares = {station: d / total for station, d in demand.items()}
    # Reparte el residuo de punto flotante en la nave mas grande, para que la
    # suma sea 1.0 exacto: si sumara menos de 1 la solucion recombinada
    # dejaria capacidad sin usar, y si sumara mas podria violar el limite
    # global de potencia.
    biggest = max(shares, key=shares.get)
    shares[biggest] += 1.0 - sum(shares.values())
    return shares


def _macroblock_energy_demand(mine_system, time_series, block, year):
    total = 0.0
    for lhd in block["lhds"]:
        for node in time_series._nodes_for_elhd_year(lhd, year):
            try:
                g_i = float(mine_system.elhd.get_load_capacity(lhd))
                filling = float(mine_system.elhd.get_filling_factor(lhd))
                pe = float(time_series.get_energy_consumption(node, lhd))
                d_i = float(time_series.get_n_intervals_trip(node, lhd))
                target = float(time_series.get_extraction_goal(node, year))
            except Exception:
                continue
            if g_i <= 0 or filling <= 0:
                continue
            total += target * pe * d_i / (g_i * filling)
    return total


def split_daily_targets(mine_system, time_series, macroblocks, years):
    """Reparte la meta de produccion diaria entre macrobloques:
    {(estacion, year): target}.

    Mismo water-filling de un paso que `compute_master_daily_targets` del
    script de un año: para cada macrobloque se calcula el rango alcanzable
    [cap_min, cap_max] con la misma expresion de piso/techo que impone
    ConstraintRules.production (cotas exactas, salidas de los datos), se le
    asigna primero su cap_min y el deficit restante hasta la meta global se
    reparte en proporcion a la holgura cap_max - cap_min.

    El indice no lleva dia porque `daily_production` compara contra la misma
    suma de m_j,y todos los dias del año (la meta no varia por dia
    representativo en este modelo).
    """
    targets = {}

    for y in years:
        cap_min, cap_max = {}, {}
        for station, block in macroblocks.items():
            lo = hi = 0.0
            for node in block["nodes"]:
                node_lo, node_hi = _node_capacity_bounds(
                    mine_system, time_series, block["lhds"], node, y
                )
                lo += node_lo
                hi += node_hi
            cap_min[station], cap_max[station] = lo, hi

        total_target = sum(
            float(time_series.get_extraction_goal(node, y))
            for block in macroblocks.values() for node in block["nodes"]
        )
        headroom = sum(cap_max[s] - cap_min[s] for s in macroblocks)
        remaining = total_target - sum(cap_min.values())
        if remaining > headroom:
            # Ni el maximo combinado alcanza la meta: el modelo completo
            # tambien seria infactible, no es un artefacto del reparto.
            remaining = headroom

        for station in macroblocks:
            share = ((cap_max[station] - cap_min[station]) / headroom
                     if headroom > 0 else 0.0)
            targets[(station, y)] = cap_min[station] + max(0.0, remaining) * share

    return targets


def _node_capacity_bounds(mine_system, time_series, lhd_names, node, year):
    """[cap_min, cap_max] de produccion alcanzable en `node` el año `year`
    con los equipos de este macrobloque -- misma formula floor/ceil que
    ConstraintRules.production usa para acotar las visitas."""
    import math

    rep_lhd = None
    for lhd in lhd_names:
        try:
            if time_series.get_n_trips(node, lhd):
                rep_lhd = lhd
                break
        except Exception:
            continue
    if rep_lhd is None:
        return 0.0, 0.0

    prod_per_assign = (
        float(mine_system.elhd.get_load_capacity(rep_lhd))
        * float(time_series.get_n_trips(node, rep_lhd))
        * float(mine_system.elhd.get_filling_factor(rep_lhd))
    )
    if prod_per_assign <= 0:
        return 0.0, 0.0

    target = float(time_series.get_extraction_goal(node, year))
    return (math.floor(target / prod_per_assign) * prod_per_assign,
            math.ceil(target / prod_per_assign) * prod_per_assign)


def coordinate_b_bar(model_params, d_hat, replace):
    """Capacidad de bateria con la que opera la flota el año, dado el estado
    heredado `d_hat` y la decision de reemplazo `replace` (0/1) que se toma
    UNA sola vez para toda la flota, fuera del reparto por macrobloque.

    Sale de b_y_link: b_bar_y <= D_hat + rho_rep * b_max * R_y. La capacidad
    mas alta permitida es siempre la mejor para el año (mas capacidad nunca
    encarece la operacion), asi que la restriccion queda activa y b_bar toma
    directamente su cota superior, acotada ademas por la capacidad nominal.

    model_params: dict con b_max, rho_rep y b_upper (cota superior fisica de
    b_bar, normalmente B_U).
    """
    b_max = model_params["b_max"]
    rho = model_params["rho_rep"]
    b_upper = model_params.get("b_upper", b_max)
    return min(b_upper, d_hat + rho * b_max * (1 if replace else 0))


def aggregate_degradation(b_bar, s_total, n_elhd, gamma_coef):
    """Estado de degradacion de la FLOTA a partir de la energia cargada por
    todos los macrobloques del año.

    Con b_bar fijado por la coordinacion, las dos ecuaciones de degradacion
    dejan de ser bilineales y se evaluan directo:

        N_ciclos = S_total / (n_elhd * b_bar)      (n_ciclos_link)
        D        = b_bar - gamma_coef * N_ciclos   (d_y_fade)

    Devuelve (D, N_ciclos).
    """
    if b_bar <= 0 or n_elhd <= 0:
        return b_bar, 0.0
    n_ciclos = s_total / (n_elhd * b_bar)
    return b_bar - gamma_coef * n_ciclos, n_ciclos
