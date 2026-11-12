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

    # Sell intents используют тот же плоский state. Порядок аккаунтов задаёт native
    # shuffle; внутри аккаунта entries уже отсортированы по названию товара.
    account_stock = np.zeros((len(accounts), len(goods)), dtype=np.float64)
    account_reserved = np.zeros((len(accounts), len(goods)), dtype=np.float64)
    account_locked = np.zeros((len(accounts), len(goods)), dtype=np.float64)
    sell_offsets = [0]
    sell_goods, sell_ask_prices, sell_ask_present = [], [], []
    sell_subjects = []
    subject_by_pair = {(account, good_index[good]): index
                       for index, (_, account, good, _, _, is_stock) in enumerate(subjects) if is_stock}
    sell_input_ratios, sell_equipment_ratios = [], []
    for account_index_value, account in enumerate(accounts):
        for good, quantity in account['stock'].items():
            good_index_value = good_index[good]
            account_stock[account_index_value, good_index_value] = quantity
        for good, quantity in account.get('stock_reserved', {}).items():
            account_reserved[account_index_value, good_index[good]] = quantity
        enterprise = engine.enterprises.get(account['id'])
        recipe = engine.goods[enterprise['good']] if enterprise else None
        for good in sorted(account['stock']):
            good_index_value = good_index[good]
            account_locked[account_index_value, good_index_value] = engine.locked_material(account['id'], good)
            sell_goods.append(good_index_value)
            is_output = enterprise is not None and good == enterprise['good']
            sell_ask_prices.append(account.get('ask_prices', {}).get(good, engine.goods[good]['price']))
            sell_ask_present.append(not is_output and good in account.get('ask_prices', {}))
            sell_subjects.append(subject_by_pair.get((account_index_value, good_index_value), -1))
            sell_input_ratios.append(recipe['inputs'].get(good, 0) if recipe else 0)
            sell_equipment_ratios.append(recipe.get('equipment', {}).get(good, 0) if recipe else 0)
        sell_offsets.append(len(sell_goods))
    seller_order = sorted(range(len(accounts)), key=lambda i: accounts[i]['id'])
    sell_capacity = len(sell_goods)

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
        account_enterprise=_int32([enterprise_index.get(a['id'], -1) for a in accounts]),
        account_household=_uint8([a['kind'] == 'household' for a in accounts]),
        account_stock=account_stock,
        account_stock_reserved=account_reserved,
        account_locked=account_locked,
        enterprise_closed=_uint8([e['status'] == 'closed' for e in enterprises]),
        enterprise_capacity=_float64([e['capacity'] for e in enterprises]),
        enterprise_planned=_float64([e.get('production_plan', {}).get('output', 0)
                                     for e in enterprises]),
        enterprise_recent=_float64([max((h.get('output', 0) for h in e.get('sales_history', [])[-3:]),
            default=e['capacity']*engine.rules['initial_sales_fraction']) for e in enterprises]),
        input_buffer=float(engine.cfg['input_buffer']),
        seller_order=_int32(seller_order),
        sell_offset=_int32(sell_offsets),
        sell_good=_int32(sell_goods),
        sell_ask_price=_float64(sell_ask_prices),
        sell_ask_present=_uint8(sell_ask_present),
        sell_subject=_int32(sell_subjects),
        sell_input_ratio=_float64(sell_input_ratios),
        sell_equipment_ratio=_float64(sell_equipment_ratios),
        sell_order_account=np.zeros(sell_capacity, dtype=np.int32),
        sell_order_entry=np.zeros(sell_capacity, dtype=np.int32),
        sell_order_good=np.zeros(sell_capacity, dtype=np.int32),
        sell_order_quantity=np.zeros(sell_capacity, dtype=np.float64),
        sell_order_price=np.zeros(sell_capacity, dtype=np.float64),
        sell_order_index=np.zeros(sell_capacity, dtype=np.int32),
        sell_execution_seq=np.zeros(sell_capacity, dtype=np.int32),
        sell_order_count=0,
        initial_order_count=len(engine.report['orders']),
    )
    meta = dict(subjects=subjects, ask_updates=ask_updates,
                account_index=account_index, enterprise_index=enterprise_index,
                good_index=good_index, month=engine.meta['month'],
                order_locations=[(a['state_id'], engine.market_province(a), engine.market_tile(a))
                                 for a in accounts])
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


def apply_sell_orders(engine, data, meta):
    """Материализовать готовые sell intents, не повторяя расчёты order()."""
    from collections import defaultdict

    accounts = list(engine.accounts.values())
    goods = list(engine.goods)
    count = data['sell_order_count']
    by_entry = {int(data['sell_order_entry'][i]): i for i in range(count)}
    journal = [('invalidate_market_books', -1)]
    sells = defaultdict(list)
    engine.invalidate_market_books()
    for account in data['seller_order']:
        account = int(account)
        a = accounts[account]
        enterprise = int(data['account_enterprise'][account])
        if data['account_household'][account] or (
                enterprise >= 0 and data['enterprise_suspended'][enterprise]):
            continue
        for entry in range(data['sell_offset'][account], data['sell_offset'][account+1]):
            good_index = int(data['sell_good'][entry])
            good = goods[good_index]
            is_output = enterprise >= 0 and good_index == data['enterprise_good'][enterprise]
            prices = a.setdefault('ask_prices', {})
            if not is_output:
                price = float(data['sell_ask_price'][entry])
                if not data['sell_ask_present'][entry]:
                    prices[good] = price
                    journal.append(('sell_ask_price_init', account, good_index, price))
            if entry not in by_entry:
                continue
            index = by_entry[entry]
            quantity = float(data['sell_order_quantity'][index])
            price = float(data['sell_order_price'][index])
            state_id, province_id, tile_id = meta['order_locations'][account]
            order = dict(id=f'{meta["month"]}-{int(data["sell_order_index"][index])}',
                participant=a['id'], good=good, quantity=quantity, remaining=quantity,
                price=price, side='sell', purpose='Продажа запасов', status='active',
                reason='', expires=meta['month'], final_consumption=False,
                state_id=state_id, province_id=province_id, tile_id=tile_id,
                reserved=0, backing=quantity)
            a.setdefault('stock_reserved', {})[good] = \
                a.get('stock_reserved', {}).get(good, 0)+quantity
            engine.report['orders'].append(order)
            engine.mark_market_quotes_dirty(good)
            sells[good].append(order)
            journal.append(('create_sell_order', int(data['sell_execution_seq'][index]),
                            order['id'], account, good_index, quantity, price))
    return journal, sells


def _run(engine, include_recovery, include_sells=False):
    timings = {}
    started = time.perf_counter()
    data, meta = pack_update_prices(engine)
    timings['pack'] = time.perf_counter()-started
    native = load_serial_step()
    started = time.perf_counter()
    if include_sells:
        native.market_preparation(data, engine.rng)
    elif include_recovery:
        native.preparation(data)
    else:
        native.update_prices(data)
    timings['native'] = time.perf_counter()-started
    started = time.perf_counter()
    journal = apply_update_prices(engine, data, meta)
    if include_recovery:
        journal.extend(apply_recovery_quotes(engine, data, meta))
    sells = None
    if include_sells:
        sell_journal, sells = apply_sell_orders(engine, data, meta)
        journal.extend(sell_journal)
    timings['apply'] = time.perf_counter()-started
    result = dict(timings=timings, data=data, meta=meta, journal=journal, sells=sells)
    engine.native_update_prices = result
    engine.native_preparation = result
    if include_sells:
        engine.native_market = result
    return journal


def run_update_prices(engine):
    """Изолированная диагностика первого участка."""
    return _run(engine, include_recovery=False)


def run_native_preparation(engine):
    """Один pack/call/apply для update_prices и prepare_recovery_quotes."""
    return _run(engine, include_recovery=True)


def run_native_market(engine):
    """Один pack/call/apply для цен, recovery и sell_orders."""
    return _run(engine, include_recovery=True, include_sells=True)
