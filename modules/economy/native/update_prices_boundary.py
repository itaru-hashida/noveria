"""Flat pack/journal/apply для native update_prices и recovery quotes."""
import time

import numpy as np

from modules.economy.support import EPS

from . import load_serial_step


def _indices(values):
    return {value: index for index, value in enumerate(values)}


def _int32(values):
    return np.asarray(values, dtype=np.int32)


def _float64(values):
    return np.asarray(values, dtype=np.float64)


def _uint8(values):
    return np.asarray(values, dtype=np.uint8)


def pack_update_prices(engine):
    """Изолировать объектное состояние Python от числового native-входа."""
    accounts = list(engine.accounts.values())
    enterprises = list(engine.enterprises.values())
    goods = list(engine.goods)
    account_index = _indices(a['id'] for a in accounts)
    enterprise_index = _indices(e['id'] for e in enterprises)
    good_index = _indices(goods)

    subjects = []
    ask_updates = []
    for index, enterprise in enumerate(enterprises):
        subjects.append((index, account_index[enterprise['id']], enterprise['good'],
                         enterprise['price'], enterprise.get('sales_history', []), False))
    for account in accounts:
        if account['kind'] == 'household':
            continue
        firm = engine.enterprises.get(account['id'])
        ask_prices = account.get('ask_prices', {})
        for good, quantity in account['stock'].items():
            if quantity <= EPS or (firm and firm['good'] == good):
                continue
            if good not in ask_prices:
                ask_updates.append((account_index[account['id']], good_index[good],
                                    float(engine.goods[good]['price'])))
            subjects.append((-1, account_index[account['id']], good,
                             ask_prices.get(good, engine.goods[good]['price']),
                             account.get('stock_sales_history', {}).get(good, []), True))

    public_orders = [o for o in engine.meta.get('public_orders', []) if o['side'] == 'buy']
    feedback = [o for o in engine.meta.get('price_feedback', []) if o.get('unfunded_need')]
    market_rows = [row for month in engine.meta.get('market_history', [])
                   for row in month['markets']]
    states = list(dict.fromkeys([a['state_id'] for a in accounts]
                                + [r['state_id'] for r in market_rows]))
    provinces = list(dict.fromkeys([a['province_id'] for a in accounts]
                                   + [r['province_id'] for r in market_rows]))
    state_index = _indices(states)
    province_index = _indices(provinces)

    tile_ids = {a.get('cell_id') or a['tile_id'] for a in accounts}
    tile_ids.update(e['tile_id'] for e in enterprises)
    tile_ids.update(o['tile_id'] for o in public_orders)
    tile_ids.update(o['tile_id'] for o in feedback)
    tiles = sorted(tile_ids)
    tile_index = _indices(tiles)
    distances = np.full((len(tiles), len(tiles)), np.nan, dtype=np.float64)
    for source_index, source in enumerate(tiles):
        for target_index, target in enumerate(tiles):
            distance, _ = engine.path(source, target)
            if distance is not None:
                distances[source_index, target_index] = distance

    subject_enterprise, subject_account = [], []
    subject_good, subject_tile, subject_state, subject_province = [], [], [], []
    subject_price, subject_last, subject_previous = [], [], []
    subject_history_count, subject_is_stock = [], []
    for enterprise, account, good, price, history, is_stock in subjects:
        a = accounts[account]
        subject_enterprise.append(enterprise)
        subject_account.append(account)
        subject_good.append(good_index[good])
        market_tile = enterprises[enterprise]['tile_id'] if enterprise >= 0 else a.get('cell_id') or a['tile_id']
        subject_tile.append(tile_index[market_tile])
        subject_state.append(state_index[a['state_id']])
        subject_province.append(province_index[a['province_id']])
        subject_price.append(price)
        subject_last.append(history[-1]['sold'] if history else 0.0)
        subject_previous.append(history[-2]['sold'] if len(history) > 1 else 0.0)
        subject_history_count.append(len(history))
        subject_is_stock.append(is_stock)

    enterprise_stock = np.zeros((len(enterprises), len(goods)), dtype=np.float64)
    for enterprise in enterprises:
        stock = accounts[account_index[enterprise['id']]]['stock']
        for good, quantity in stock.items():
            enterprise_stock[enterprise_index[enterprise['id']], good_index[good]] = quantity

    recovery_order = sorted(range(len(enterprises)),
        key=lambda i: (engine.depth(enterprises[i]['good']), enterprises[i]['id']))
    # Порядок сложения cost должен совпадать с dict/recipe V3, а не с общим goods index.
    input_offsets = [0]
    input_goods, input_values = [], []
    wear_offsets = [0]
    wear_goods, wear_values = [], []
    for product in goods:
        recipe = engine.goods[product]
        for good, ratio in recipe['inputs'].items():
            input_goods.append(good_index[good])
            input_values.append(ratio)
        input_offsets.append(len(input_goods))
        for good, ratio in engine.equipment_wear(recipe, 1).items():
            wear_goods.append(good_index[good])
            wear_values.append(ratio)
        wear_offsets.append(len(wear_goods))
    resource_ok = []
    for enterprise in enterprises:
        resource = engine.goods[enterprise['good']]['resource']
        resource_ok.append(not resource or any(t['province_id'] == enterprise['province_id']
            and resource in t['resources'] for t in engine.tiles.values()))

    data = dict(
        subject_enterprise=_int32(subject_enterprise),
        subject_account=_int32(subject_account),
        subject_good=_int32(subject_good),
        subject_tile=_int32(subject_tile),
        subject_state=_int32(subject_state),
        subject_province=_int32(subject_province),
        subject_price=_float64(subject_price),
        subject_last_sold=_float64(subject_last),
        subject_previous_sold=_float64(subject_previous),
        subject_history_count=_int32(subject_history_count),
        subject_is_stock=_uint8(subject_is_stock),
        new_prices=np.empty(len(subjects), dtype=np.float64),
        trace_branch=np.zeros(len(subjects), dtype=np.int32),
        trace_limits=np.full(len(subjects), np.nan, dtype=np.float64),
        trace_competitor=np.full(len(subjects), np.nan, dtype=np.float64),
        trace_budget=np.full(len(subjects), np.nan, dtype=np.float64),
        trace_signal_volume=np.zeros(len(subjects), dtype=np.float64),
        enterprise_good=_int32([good_index[e['good']] for e in enterprises]),
        enterprise_tile=_int32([tile_index[e['tile_id']] for e in enterprises]),
        enterprise_price=_float64([e['price'] for e in enterprises]),
        enterprise_stock=enterprise_stock,
        order_good=_int32([good_index[o['good']] for o in public_orders]),
        order_tile=_int32([tile_index[o['tile_id']] for o in public_orders]),
        order_participant=_int32([account_index[o['participant']] for o in public_orders]),
        order_price=_float64([o['price'] for o in public_orders]),
        order_remaining=_float64([o['remaining'] for o in public_orders]),
        order_active=_uint8([o['status'] not in ('cancelled', 'expired') for o in public_orders]),
        feedback_good=_int32([good_index[o['good']] for o in feedback]),
        feedback_tile=_int32([tile_index[o['tile_id']] for o in feedback]),
        feedback_budget=_float64([o['budget_price'] for o in feedback]),
        market_state=_int32([state_index[r['state_id']] for r in market_rows]),
        market_province=_int32([province_index[r['province_id']] for r in market_rows]),
        market_good=_int32([good_index[r['good']] for r in market_rows]),
        market_volume=_float64([r['volume'] for r in market_rows]),
        market_history_len=len(engine.meta.get('market_history', [])),
        distances=distances,
        good_weight=_float64([engine.goods[g]['weight'] for g in goods]),
        transport_rate=float(engine.cfg['transport_rate']),
        price_step=float(engine.rules['price_step']),
        minimum_price=float(engine.rules['minimum_price']),
        maximum_price=float(engine.rules['maximum_price']),
        eps=float(EPS),
        recovery_order=_int32(recovery_order),
        recovery_had_price=_uint8(['production_price' in e for e in enterprises]),
        recovery_production_price=np.full(len(enterprises), np.nan, dtype=np.float64),
        recovery_quote_count=np.zeros(len(goods), dtype=np.int32),
        recovery_quote_enterprise=np.zeros((len(goods), len(enterprises)), dtype=np.int32),
        recovery_quote_price=np.zeros((len(goods), len(enterprises)), dtype=np.float64),
        enterprise_suspended=_uint8([bool(e.get('suspended')) for e in enterprises]),
        enterprise_resource_ok=_uint8(resource_ok),
        recovery_input_offset=_int32(input_offsets),
        recovery_input_good=_int32(input_goods),
        recovery_input_value=_float64(input_values),
        recovery_wear_offset=_int32(wear_offsets),
        recovery_wear_good=_int32(wear_goods),
        recovery_wear_value=_float64(wear_values),
        good_default_price=_float64([engine.goods[g]['price'] for g in goods]),
        good_labor=_float64([engine.goods[g]['labor'] for g in goods]),
        good_consumer=_uint8([g in (engine.cfg['basic_needs'] | engine.cfg['extra_needs'])
                              for g in goods]),
        wage=float(engine.cfg['wage']),
        markup=float(engine.cfg['markup']),
        consumption_tax=float(engine.cfg['consumption_tax']),
    )
    meta = dict(subjects=subjects, ask_updates=ask_updates,
                account_index=account_index, enterprise_index=enterprise_index,
                good_index=good_index)
    return data, meta


def apply_update_prices(engine, data, meta):
    """Применить готовый journal, не повторяя расчёт цены."""
    journal = []
    accounts = list(engine.accounts.values())
    enterprises = list(engine.enterprises.values())
    goods = list(engine.goods)
    for account, good, price in meta['ask_updates']:
        accounts[account].setdefault('ask_prices', {})[goods[good]] = price
        journal.append(('ask_price_init', account, good, price))
    for subject, (enterprise, account, good, _, _, is_stock) in enumerate(meta['subjects']):
        if data['subject_history_count'][subject] == 0:
            continue
        price = float(data['new_prices'][subject])
        if is_stock:
            accounts[account]['ask_prices'][goods[meta['good_index'][good]]] = price
            journal.append(('ask_price_set', subject, price))
        else:
            enterprises[enterprise]['price'] = price
            journal.append(('enterprise_price_set', subject, price))
    return journal


def apply_recovery_quotes(engine, data, meta):
    """Применить production_price и восстановить семантический recovery-кэш."""
    from collections import defaultdict

    enterprises = list(engine.enterprises.values())
    journal = []
    quotes = defaultdict(list)
    for enterprise in data['recovery_order']:
        enterprise = int(enterprise)
        if data['recovery_had_price'][enterprise]:
            enterprises[enterprise].pop('production_price', None)
            journal.append(('production_price_clear', enterprise))
        price = float(data['recovery_production_price'][enterprise])
        if np.isfinite(price):
            enterprises[enterprise]['production_price'] = price
            quotes[enterprises[enterprise]['good']].append((enterprises[enterprise], price))
            journal.append(('production_price_set', enterprise, price))
    engine._recovery_quotes = quotes
    return journal


def _run(engine, include_recovery):
    timings = {}
    started = time.perf_counter()
    data, meta = pack_update_prices(engine)
    timings['pack'] = time.perf_counter()-started
    native = load_serial_step()
    started = time.perf_counter()
    if include_recovery:
        native.preparation(data)
    else:
        native.update_prices(data)
    timings['native'] = time.perf_counter()-started
    started = time.perf_counter()
    journal = apply_update_prices(engine, data, meta)
    if include_recovery:
        journal.extend(apply_recovery_quotes(engine, data, meta))
    timings['apply'] = time.perf_counter()-started
    result = dict(timings=timings, data=data, meta=meta, journal=journal)
    engine.native_update_prices = result
    engine.native_preparation = result
    return journal


def run_update_prices(engine):
    """Изолированная диагностика первого участка."""
    return _run(engine, include_recovery=False)


def run_native_preparation(engine):
    """Один pack/call/apply для update_prices и prepare_recovery_quotes."""
    return _run(engine, include_recovery=True)
