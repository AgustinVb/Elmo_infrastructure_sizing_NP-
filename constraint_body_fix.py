with open('src/optimization/functions.py', 'r', encoding='utf-8') as f:
    content = f.read()

body = [
    # daily_production
    ('total_target = sum(model.m_j[j, d] for j in model.nodes_set)',
     'total_target = sum(model.m_j[j, y] for j in model.nodes_set)'),
    ('for (i2, j2, d2, t2) in model.Y\n            if d2 == d',
     'for (i2, j2, y2, d2, t2) in model.Y\n            if y2 == y and d2 == d'),
    ('model.Y[i2, j2, d2, t2] * model.g_i[i2] * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]',
     'model.Y[i2, j2, y2, d2, t2] * model.g_i[i2] * self.time_series.get_n_trips(j2, i2) * model.filling_factor[i2]'),
    # production
    ('y_pairs = [(i2, t2) for (i2, j2, d2, t2) in model.Y if j2 == j and d2 == d]',
     'y_pairs = [(i2, t2) for (i2, j2, y2, d2, t2) in model.Y if j2 == j and y2 == y and d2 == d]'),
    ('target = pyo_value(model.m_j[j, d])',
     'target = pyo_value(model.m_j[j, y])'),
    ('visits = sum(model.Y[i2, j, d, t2] for i2, t2 in y_pairs)',
     'visits = sum(model.Y[i2, j, y, d, t2] for i2, t2 in y_pairs)'),
    # daily_extraction_M
    ('for (i2, j2, d2, t2) in model.Y_INDEX\n            if i2 == i and d2 == d and j2 == j',
     'for (i2, j2, y2, d2, t2) in model.Y_INDEX\n            if i2 == i and y2 == y and d2 == d and j2 == j'),
    ('return model.M[i, j, d] == term',
     'return model.M[i, j, y, d] == term'),
    # bays_limit_swap
    ('return sum(model.Z_swap[k, i, d, t] for i in valid_i) <= model.N_bays[k]',
     'return sum(model.Z_swap[k, i, y, d, t] for i in valid_i) <= model.N_bays[k]'),
    # station_existence
    ('return model.Z_swap[k,i, d, t] <= model.X[k]',
     'return model.Z_swap[k, i, y, d, t] <= model.X[k]'),
    # charger_limit_swap
    ('return sum(model.Sv[k, d, t, a] for a in model.time_intervals_set) <= model.N_chargers[k]',
     'return sum(model.Sv[k, y, d, t, a] for a in model.time_intervals_set) <= model.N_chargers[k]'),
    # max_installed_capacity_swap
    ('return sum(model.Sv[k, d, t, a]*model.p_charger for a in model.time_intervals_set)  <= model.p_max_k[k]',
     'return sum(model.Sv[k, y, d, t, a]*model.p_charger for a in model.time_intervals_set) <= model.p_max_k[k]'),
    # peak_power_swap
    ('return sum(model.Sv[k, d, t, a]*model.p_charger for k in model.stations_set for a in model.time_intervals_set) <= model.p_peak',
     'return sum(model.Sv[k, y, d, t, a]*model.p_charger for k in model.stations_set for a in model.time_intervals_set) <= model.p_peak'),
    # power_peak_limit
    ('return model.P_red[d, t] <= model.P_pot',
     'return model.P_red[y, d, t] <= model.P_pot[y]'),
    # total_swaps
    ('return model.W[k, d, t] == sum(model.Z_swap[k, i ,d, t] for i in valid_i_list)',
     'return model.W[k, y, d, t] == sum(model.Z_swap[k, i, y, d, t] for i in valid_i_list)'),
    # max_swaps
    ('return sum(model.Z_swap[k, i ,d, t] for k in valid_k_list) <= 1',
     'return sum(model.Z_swap[k, i, y, d, t] for k in valid_k_list) <= 1'),
    # swap_precedence
    ('model.Z_swap[k, i_low, d, tau]',
     'model.Z_swap[k, i_low, y, d, tau]'),
    ('model.Z_swap[k, i_high, d, tau]',
     'model.Z_swap[k, i_high, y, d, tau]'),
    # inventory_discharged
    ('return model.X_dch[k, d, t] == model.X_dch[k, d, t-1] - model.X_ini[k, d, t-1] + model.W[k, d, t]',
     'return model.X_dch[k, y, d, t] == model.X_dch[k, y, d, t-1] - model.X_ini[k, y, d, t-1] + model.W[k, y, d, t]'),
    # inventory_charged
    ('return model.S[k, d, t+1] == model.S[k, d, t] + model.Sv[k, d, t, t - (model.t_charge - 1)] - model.W[k, d, t+1]',
     'return model.S[k, y, d, t+1] == model.S[k, y, d, t] + model.Sv[k, y, d, t, t - (model.t_charge - 1)] - model.W[k, y, d, t+1]'),
    ('return model.S[k, d, t+1] == model.S[k, d, t] - model.W[k, d, t+1]',
     'return model.S[k, y, d, t+1] == model.S[k, y, d, t] - model.W[k, y, d, t+1]'),
    # charging_duration_rule
    ('return model.Sv[k, d, t+1, a] == 0',
     'return model.Sv[k, y, d, t+1, a] == 0'),
    ('return model.Sv[k, d, t+1, a] == model.Sv[k, d, t, a]',
     'return model.Sv[k, y, d, t+1, a] == model.Sv[k, y, d, t, a]'),
    ('return model.Sv[k, d, t+1, a] == model.X_ini[k, d, t]',
     'return model.Sv[k, y, d, t+1, a] == model.X_ini[k, y, d, t]'),
    # CI_S, CI_X_dch, CB_general
    ('return model.S[k, d, t0] == model.S[k, d, tf]',
     'return model.S[k, y, d, t0] == model.S[k, y, d, tf]'),
    ('return model.X_dch[k, d, t0] == model.X_dch[k, d, tf]',
     'return model.X_dch[k, y, d, t0] == model.X_dch[k, y, d, tf]'),
    ('return model.S[k, d, t0] + model.X_dch[k, d, t0] == model.N_batteries[k]',
     'return model.S[k, y, d, t0] + model.X_dch[k, y, d, t0] == model.N_batteries[k]'),
    # meal - Nodes_assigned_at_interval
    ("nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((d, t, i), [])",
     "nodes = self.time_series.mapper['Nodes_assigned_at_interval'].get((y, d, t, i), [])"),
    # maint_stop_all
    ('return model.Z[i, d, t] == 1',
     'return model.Z[i, y, d, t] == 1'),
    # n_swaps_limit
    ('return sum(model.Z_swap[k, i ,d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) <= 5',
     'return sum(model.Z_swap[k, i, y, d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) <= 5'),
    ('return sum(model.Z_swap[k, i ,d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) >= 3',
     'return sum(model.Z_swap[k, i, y, d, t] for (k, i2) in model.ZSWAP_INDEX if i2 == i for t in model.time_intervals_set) >= 3'),
    # power_balance
    ('model.Sv[k, d, t, a] * model.p_charger',
     'model.Sv[k, y, d, t, a] * model.p_charger'),
    ('gen = (sum(model.P_gen[g, d, t] for g in model.gen_set)',
     'gen = (sum(model.P_gen[g, y, d, t] for g in model.gen_set)'),
    ('bess = (sum(model.P_bat[h, d, t] for h in model.storage_set)',
     'bess = (sum(model.P_bat[h, y, d, t] for h in model.storage_set)'),
    ('return model.P_red[d, t] + gen + bess == demand',
     'return model.P_red[y, d, t] + gen + bess == demand'),
    # grid_limit
    ('return model.P_red[d, t] <= model.p_peak',
     'return model.P_red[y, d, t] <= model.p_peak'),
    # gen_limit
    ('return (model.P_gen[g, d, t] + model.Curt_g[g, d, t]',
     'return (model.P_gen[g, y, d, t] + model.Curt_g[g, y, d, t]'),
    ('== model.G_g[g] * model.p_max_g[g] * model.alpha_g[g, d, t])',
     '== model.G_g[g] * model.p_max_g[g] * model.alpha_g[g, y, d, t])'),
    # bess
    ('return model.P_bat[h, d, t] <= model.p_max_h[h] * model.H_h[h]',
     'return model.P_bat[h, y, d, t] <= model.p_max_h[h] * model.H_h[h]'),
    ('return model.P_bat[h, d, t] >= -model.p_max_h[h] * model.H_h[h]',
     'return model.P_bat[h, y, d, t] >= -model.p_max_h[h] * model.H_h[h]'),
    ('prev = model.A_h[h, d, 0] if t == t0 else model.A_h[h, d, t - 1]',
     'prev = model.A_h[h, y, d, 0] if t == t0 else model.A_h[h, y, d, t - 1]'),
    ('return model.A_h[h, d, t] == prev - (model.P_bat[h, d, t] / model.eta_h[h]) * model.delta_t',
     'return model.A_h[h, y, d, t] == prev - (model.P_bat[h, y, d, t] / model.eta_h[h]) * model.delta_t'),
    ('return model.A_h[h, d, 0] == 0',
     'return model.A_h[h, y, d, 0] == 0'),
    ('return model.A_h[h, d, t] <= model.a_max_h[h] * model.H_h[h]',
     'return model.A_h[h, y, d, t] <= model.a_max_h[h] * model.H_h[h]'),
    ('return model.A_h[h, d, t] >= model.a_min_h[h] * model.H_h[h]',
     'return model.A_h[h, y, d, t] >= model.a_min_h[h] * model.H_h[h]'),
    ('return model.A_h[h, d, t_ini] == model.A_h[h, d, t_fin]',
     'return model.A_h[h, y, d, t_ini] == model.A_h[h, y, d, t_fin]'),
    # meal Y references
    ('return sum(model.Y[i, j, d, t] for j in nodes) == 0',
     'return sum(model.Y[i, j, y, d, t] for j in nodes) == 0'),
]

ok = miss = 0
for old, new in body:
    c = content.count(old)
    if c >= 1:
        content = content.replace(old, new)
        ok += 1
    else:
        print(f'MISS: {repr(old[:70])}')
        miss += 1

print(f'Body: {ok} OK, {miss} miss')
with open('src/optimization/functions.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('done')
