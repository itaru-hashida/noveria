"""Общий flat pack/journal/apply для native-фаз V3 до owner_requests."""
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
                                   + [engine.market_province(a) for a in accounts]
                                   + [r['province_id'] for r in market_rows]))
    state_index = _indices(states)
    province_index = _indices(provinces)

    demand_orders = [o for o in engine.report['orders'] if o['side'] == 'buy']
    tile_ids = {a.get('cell_id') or a['tile_id'] for a in accounts}
    tile_ids.update(e['tile_id'] for e in enterprises)
    tile_ids.update(o['tile_id'] for o in public_orders)
    tile_ids.update(o['tile_id'] for o in feedback)
    tile_ids.update(o['tile_id'] for o in demand_orders)
    tile_ids.update(a['project']['location']['tile_id'] for a in accounts if a.get('project'))
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
    household_order = sorted((i for i, a in enumerate(accounts) if a['kind'] == 'household'),
                             key=lambda i: accounts[i]['id'])
    basic_goods = list(engine.cfg['basic_needs'])
    extra_goods = list(engine.cfg['extra_needs'])
    household_capacity = len(household_order)*(len(basic_goods)+len(extra_goods))
    owner_order = sorted((i for i, a in enumerate(accounts) if a['kind'] in ('private', 'merchant')),
                         key=lambda i: accounts[i]['id'])
    owner_capacity = len(owner_order)*3
    # project_cash_reserve читает уже созданные household orders, поэтому в native
    # передаются все заявки, а не только public orders из update_prices.
    construction = list(engine.cfg['construction'])
    construction_offsets = [0]
    construction_goods, construction_values = [], []
    project_capacity, project_working, project_delivery = [], [], []
    project_tiles = []
    for account in accounts:
        project = account.get('project')
        project_capacity.append(project['capacity'] if project else 0.0)
        project_working.append(project['working_cash'] if project else 0.0)
        project_delivery.append(project.get('delivery', 0) if project else 0.0)
        project_tiles.append(tile_index[project['location']['tile_id']] if project else -1)
    for good in construction:
        construction_goods.append(good_index[good])
        construction_values.append(engine.cfg['construction'][good]/engine.cfg['minimum_capacity'])
    construction_offsets.append(len(construction_goods))
    tile_ids.update(o['tile_id'] for o in demand_orders)
    if any(o['tile_id'] not in tile_index for o in demand_orders):
        raise ValueError('Маршрутная матрица не содержит площадку заявки')
    owner_basket = [good_index[g] for g in ('bread', 'fish', 'cloth')]
    public_requests = []
    for account in sorted(accounts, key=lambda a: a['id']):
        for project in account.get('public_projects', []):
            for bucket, requirements, purpose in (
                    ('materials', project['material_bill'], 0),
                    ('equipment_held', project['equipment'], 1)):
                for good, required in requirements.items():
                    public_requests.append((account_index[account['id']], project, bucket,
                                            good_index[good], float(required), purpose))
            support = engine.cfg['construction_modes'][project['mode']]['support_goods']
            for good, per_worker in support.items():
                public_requests.append((account_index[account['id']], project, 'support_stock',
                    good_index[good], float(project['max_workers']*per_worker), 2))
    public_capacity = len(public_requests)
    public_release_capacity = max(1, public_capacity*sell_capacity)
    demand_capacity = len(demand_orders)+household_capacity+owner_capacity+public_capacity
    def demand_field(name, default=0):
        return [o.get(name, default) for o in demand_orders] + [default]*(demand_capacity-len(demand_orders))
    equipment_offsets = [0]
    equipment_goods, equipment_values = [], []
    for product in goods:
        for good, ratio in engine.goods[product].get('equipment', {}).items():
            equipment_goods.append(good_index[good])
            equipment_values.append(ratio)
        equipment_offsets.append(len(equipment_goods))

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
        market_landed_component=_float64([r['trade_gold']+r['freight'] for r in market_rows]),
        market_history_len=len(engine.meta.get('market_history', [])),
        distances=distances,
        good_weight=_float64([engine.goods[g]['weight'] for g in goods]),
        transport_rate=float(engine.cfg['transport_rate']),
        price_step=float(engine.rules['price_step']),
        minimum_price=float(engine.rules['minimum_price']),
        maximum_price=float(engine.rules['maximum_price']),
        eps=float(EPS),
        recovery_order=_int32(recovery_order),
        enterprise_recovery_order=_int32(sorted(range(len(enterprises)),
            key=lambda i: enterprises[i]['id'])),
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
        sell_order_initial_quantity=np.zeros(sell_capacity, dtype=np.float64),
        sell_order_remaining=np.zeros(sell_capacity, dtype=np.float64),
        sell_order_backing=np.zeros(sell_capacity, dtype=np.float64),
        sell_order_price=np.zeros(sell_capacity, dtype=np.float64),
        sell_order_index=np.zeros(sell_capacity, dtype=np.int32),
        sell_execution_seq=np.zeros(sell_capacity, dtype=np.int32),
        sell_order_count=0,
        initial_order_count=len(engine.report['orders']),
        account_state=_int32([state_index[a['state_id']] for a in accounts]),
        account_province=_int32([province_index[engine.market_province(a)] for a in accounts]),
        account_tile=_int32([tile_index[engine.market_tile(a)] for a in accounts]),
        account_balance=_float64([a['balance'] for a in accounts]),
        account_money_reserved=_float64([a.get('reserved', 0) for a in accounts]),
        account_protected=_float64([a.get('protected', 0) for a in accounts]),
        account_population=_float64([engine.population(a) for a in accounts]),
        household_order=_int32(household_order),
        household_basic_good=_int32([good_index[g] for g in basic_goods]),
        household_basic_rate=_float64([engine.cfg['basic_needs'][g] for g in basic_goods]),
        household_basic_rate_by_good=_float64([engine.cfg['basic_needs'].get(g, 0) for g in goods]),
        household_extra_good=_int32([good_index[g] for g in extra_goods]),
        household_extra_rate=_float64([engine.cfg['extra_needs'][g] for g in extra_goods]),
        basic_budget_revision=bool(engine.BASIC_BUDGET_REVISION),
        household_quote_ceiling=np.zeros((len(accounts), len(goods)), dtype=np.float64),
        household_offer_index=np.zeros(sell_capacity, dtype=np.int32),
        household_offer_landed=np.zeros(sell_capacity, dtype=np.float64),
        household_sort_good=np.zeros(len(basic_goods), dtype=np.int32),
        household_request_account=np.zeros(household_capacity, dtype=np.int32),
        household_request_good=np.zeros(household_capacity, dtype=np.int32),
        household_request_needed=np.zeros(household_capacity, dtype=np.float64),
        household_request_missing=np.zeros(household_capacity, dtype=np.float64),
        household_request_price=np.zeros(household_capacity, dtype=np.float64),
        household_request_fraction=np.zeros(household_capacity, dtype=np.float64),
        household_request_basic=np.zeros(household_capacity, dtype=np.uint8),
        household_order_quantity=np.zeros(household_capacity, dtype=np.float64),
        household_order_reserved=np.zeros(household_capacity, dtype=np.float64),
        household_budget_price=np.zeros(household_capacity, dtype=np.float64),
        household_unfunded=np.zeros(household_capacity, dtype=np.uint8),
        household_order_index=np.full(household_capacity, -1, dtype=np.int32),
        household_execution_seq=np.full(household_capacity, -1, dtype=np.int32),
        household_request_count=0,
        household_order_count_created=household_capacity,
        demand_order_good=_int32([good_index[o['good']] for o in demand_orders]
                                 +[0]*(demand_capacity-len(demand_orders))),
        demand_order_participant=_int32([account_index[o['participant']] for o in demand_orders]+[0]*(demand_capacity-len(demand_orders))),
        demand_order_tile=_int32([tile_index[o['tile_id']] for o in demand_orders]+[0]*(demand_capacity-len(demand_orders))),
        demand_order_price=_float64(demand_field('price')),
        demand_order_remaining=_float64(demand_field('remaining')),
        demand_order_reserved=_float64(demand_field('reserved')),
        demand_order_active=_uint8([o['status'] not in ('cancelled', 'expired') for o in demand_orders]+[0]*(demand_capacity-len(demand_orders))),
        demand_order_final=_uint8(demand_field('final_consumption', False)),
        demand_order_construction=_uint8([o['purpose'] == 'Строительные материалы' for o in demand_orders]+[0]*(demand_capacity-len(demand_orders))),
        demand_order_count=len(demand_orders),
        project_capacity=_float64(project_capacity),
        project_working=_float64(project_working),
        project_delivery=_float64(project_delivery),
        project_tile=_int32(project_tiles),
        construction_good=_int32(construction_goods),
        construction_ratio=_float64(construction_values),
        owner_order=_int32(owner_order),
        owner_basket_good=_int32(owner_basket),
        owner_consumption_rate=float(engine.cfg['owner_consumption_rate']),
        owner_family_size=float(engine.OWNER_FAMILY_SIZE),
        owner_food_quality=float(engine.OWNER_FOOD_QUALITY),
        owner_account=np.zeros(len(owner_order), dtype=np.int32),
        owner_project_reserve=np.zeros(len(owner_order), dtype=np.float64),
        owner_free_cash=np.zeros(len(owner_order), dtype=np.float64),
        owner_budget=np.zeros(len(owner_order), dtype=np.float64),
        owner_request_good=np.zeros(owner_capacity, dtype=np.int32),
        owner_order_quantity=np.zeros(owner_capacity, dtype=np.float64),
        owner_order_reserved=np.zeros(owner_capacity, dtype=np.float64),
        owner_order_price=np.zeros(owner_capacity, dtype=np.float64),
        owner_order_index=np.full(owner_capacity, -1, dtype=np.int32),
        owner_execution_seq=np.full(owner_capacity, -1, dtype=np.int32),
        owner_request_count=0,
        owner_order_count_created=0,
        public_request_account=_int32([row[0] for row in public_requests]),
        public_request_good=_int32([row[3] for row in public_requests]),
        public_request_required=_float64([row[4] for row in public_requests]),
        public_request_purpose=_int32([row[5] for row in public_requests]),
        public_request_held=_float64([row[1][row[2]].get(goods[row[3]], 0)
                                      for row in public_requests]),
        public_held=np.zeros(public_capacity, dtype=np.float64),
        public_order_quantity=np.zeros(public_capacity, dtype=np.float64),
        public_order_reserved=np.zeros(public_capacity, dtype=np.float64),
        public_order_price=np.zeros(public_capacity, dtype=np.float64),
        public_order_index=np.full(public_capacity, -1, dtype=np.int32),
        public_release_request=np.zeros(public_release_capacity, dtype=np.int32),
        public_release_sell=np.zeros(public_release_capacity, dtype=np.int32),
        public_release_quantity=np.zeros(public_release_capacity, dtype=np.float64),
        public_release_count=0,
        public_order_count_created=0,
        recovery_equipment_offset=_int32(equipment_offsets),
        recovery_equipment_good=_int32(equipment_goods),
        recovery_equipment_value=_float64(equipment_values),
        enterprise_owner=_int32([account_index[e['owner_id']] for e in enterprises]),
        enterprise_account=_int32([account_index[e['id']] for e in enterprises]),
        enterprise_available_from=_int32([e['available_from'] for e in enterprises]),
        enterprise_sales_forecast=_float64([e['sales_forecast'] for e in enterprises]),
        enterprise_sold_now=_float64([sum(t['quantity'] for t in engine.report['trades']
            if t['seller'] == e['id'] and t['good'] == e['good']) for e in enterprises]),
        enterprise_history_count=_int32([min(3, len(e.get('sales_history', []))) for e in enterprises]),
        enterprise_history_sold=_float64([[h.get('sold', 0) for h in e.get('sales_history', [])[-3:]]+[0]*(3-min(3, len(e.get('sales_history', [])))) for e in enterprises]),
        enterprise_history_output=_float64([[h.get('output', 0) for h in e.get('sales_history', [])[-3:]]+[0]*(3-min(3, len(e.get('sales_history', [])))) for e in enterprises]),
        enterprise_previous_output=_float64([e.get('planned_output', 0) for e in enterprises]),
        enterprise_overhead_rate=float(engine.cfg.get('enterprise_overhead_rate', 0)),
        output_overstock_factor=float(engine.cfg['output_overstock_factor']),
        output_decline_rate=float(engine.cfg['output_decline_rate']),
        stock_planning_revision=int(engine.STOCK_PLANNING_REVISION),
        current_month=int(engine.meta['month']),
        sell_active=np.ones(sell_capacity, dtype=np.uint8),
        sell_remaining=np.zeros(sell_capacity, dtype=np.float64),
        demand_group_participant=np.zeros(demand_capacity, dtype=np.int32),
        demand_group_tile=np.zeros(demand_capacity, dtype=np.int32),
        demand_group_price=np.zeros(demand_capacity, dtype=np.float64),
        demand_group_limit=np.zeros(demand_capacity, dtype=np.float64),
        demand_group_freight=np.zeros(demand_capacity, dtype=np.float64),
        demand_group_volume=np.zeros(demand_capacity, dtype=np.float64),
        recovery_enterprise=np.full(len(enterprises), -1, dtype=np.int32),
        recovery_transfer=np.zeros(len(enterprises), dtype=np.float64),
        recovery_hold=np.zeros(len(enterprises), dtype=np.float64),
        recovery_reopened=np.zeros(len(enterprises), dtype=np.uint8),
        recovery_cancel_sell=np.full((len(enterprises), sell_capacity), -1, dtype=np.int32),
        recovery_cancel_count=np.zeros(len(enterprises), dtype=np.int32),
        recovery_outlook_count=0,
        recovery_debug_target=np.zeros(len(enterprises), dtype=np.float64),
        recovery_debug_demand=np.zeros(len(enterprises), dtype=np.float64),
        recovery_debug_margin=np.zeros(len(enterprises), dtype=np.float64),
        recovery_debug_required=np.zeros(len(enterprises), dtype=np.float64),
        recovery_debug_forecast=np.zeros(len(enterprises), dtype=np.float64),
        recovery_debug_expected=np.zeros(len(enterprises), dtype=np.float64),
        recovery_stock_cache_set=np.zeros(len(enterprises), dtype=np.uint8),
        recovery_market_version=np.zeros((len(enterprises), len(goods)), dtype=np.int32),
        recovery_market_version_work=_int32([
            getattr(engine, '_market_quote_versions', {}).get(g, 0) for g in goods]),
        recovery_expected_cost=np.zeros(len(enterprises), dtype=np.float64),
        recovery_expected_price=np.zeros(len(enterprises), dtype=np.float64),
        recovery_planned_price=np.zeros(len(enterprises), dtype=np.float64),
        recovery_fixed_cost=np.zeros(len(enterprises), dtype=np.float64),
        recovery_fixed_unit=np.zeros(len(enterprises), dtype=np.float64),
        recovery_production_limit=np.zeros(len(enterprises), dtype=np.int32),
        recovery_outlook_valid=np.zeros(len(enterprises), dtype=np.uint8),
        enterprise_history_input_limited=_uint8([[bool(h.get('input_limited')) for h in e.get('sales_history', [])[-3:]]
            +[False]*(3-min(3, len(e.get('sales_history', [])))) for e in enterprises]),
        enterprise_trades_version=_int32([getattr(engine, '_trades_by_seller', {}).get(e['id'], 0)
                                          for e in enterprises]),
    )
    meta = dict(subjects=subjects, ask_updates=ask_updates,
                account_index=account_index, enterprise_index=enterprise_index,
                good_index=good_index, month=engine.meta['month'],
                public_requests=public_requests,
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
            quantity = float(data['sell_order_initial_quantity'][index])
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


def apply_households(engine, data, meta, households, sells):
    """Создать needs и buy orders из готовых household request events."""
    food_trace = None
    if engine.FOOD_DIAGNOSTICS and engine.report_detail() == 'deep':
        from modules.economy.food_diagnostics import FoodDiagnostics
        food_trace = FoodDiagnostics(engine, households, sells)
    accounts = list(engine.accounts.values())
    goods = list(engine.goods)
    journal = []
    for index in range(data['household_request_count']):
        account = int(data['household_request_account'][index])
        good_index = int(data['household_request_good'][index])
        good = goods[good_index]
        needed = float(data['household_request_needed'][index])
        a = accounts[account]
        a.setdefault('needs', {})[good] = dict(needed=needed, consumed=0, deficit=needed)
        journal.append(('set_household_need', int(data['household_execution_seq'][index]),
                        account, good_index, needed))
        order_index = int(data['household_order_index'][index])
        if order_index < 0:
            continue
        quantity = float(data['household_order_quantity'][index])
        price = float(data['household_request_price'][index])
        reserved = float(data['household_order_reserved'][index])
        basic = bool(data['household_request_basic'][index])
        state_id, province_id, tile_id = meta['order_locations'][account]
        order = dict(id=f'{meta["month"]}-{order_index}', participant=a['id'], good=good,
            quantity=quantity, remaining=quantity, price=price, side='buy',
            purpose='Базовые нужды' if basic else 'Дополнительные нужды',
            status='active', reason='', expires=meta['month'], final_consumption=True,
            state_id=state_id, province_id=province_id, tile_id=tile_id,
            reserved=reserved, backing=reserved,
            need_quantity=float(data['household_request_missing'][index]),
            budget_price=float(data['household_budget_price'][index]),
            unfunded_need=bool(data['household_unfunded'][index]),
            adaptive_household_budget=True)
        a['reserved'] = a.get('reserved', 0)+reserved
        engine.report['orders'].append(order)
        engine._demand_revision[good] += 1
        journal.append(('create_household_order', int(data['household_execution_seq'][index]),
                        order['id'], account, good_index, quantity, price, reserved))
    return journal, food_trace


def apply_owners(engine, data, meta, sells):
    """Материализовать личные заявки и диагностику владельцев."""
    accounts = list(engine.accounts.values())
    goods = list(engine.goods)
    journal = []
    for i, account in enumerate(data['owner_order']):
        account = int(account)
        a = accounts[account]
        project = float(data['owner_project_reserve'][i])
        available = float(data['owner_free_cash'][i])
        budget = float(data['owner_budget'][i])
        a.update(owner_free_cash=available, owner_consumption_budget=budget,
                 project_reserve=project)
        row = dict(owner=a['id'], free_cash=available, project_reserve=project,
                   budget=budget, spent=0.0, tax=0.0, freight=0.0, consumed={}, orders=[])
        engine.report['owner_consumption'].append(row)
        journal.append(('owner_budget', i, account, project, available, budget))
        for k in range(3):
            index = i*3+k
            order_index = int(data['owner_order_index'][index])
            if order_index < 0:
                continue
            good = goods[int(data['owner_request_good'][index])]
            quantity = float(data['owner_order_quantity'][index])
            price = float(data['owner_order_price'][index])
            reserved = float(data['owner_order_reserved'][index])
            state_id, province_id, tile_id = meta['order_locations'][account]
            order = dict(id=f'{meta["month"]}-{order_index}', participant=a['id'], good=good,
                quantity=quantity, remaining=quantity, price=price, side='buy',
                purpose='Личное потребление собственника', status='active', reason='',
                expires=meta['month'], final_consumption=True, state_id=state_id,
                province_id=province_id, tile_id=tile_id, reserved=reserved, backing=reserved)
            a['reserved'] = a.get('reserved', 0)+reserved
            engine.report['orders'].append(order)
            engine._demand_revision[good] += 1
            row['orders'].append(order['id'])
            journal.append(('create_owner_order', int(data['owner_execution_seq'][index]),
                            order['id'], account, good, quantity, price, reserved))
    return journal


def apply_recovery(engine, data, meta, sells):
    """Применить готовые переводы, reopen, отмены и операционные резервы."""
    accounts = list(engine.accounts.values())
    enterprises = list(engine.enterprises.values())
    goods = list(engine.goods)
    journal = []
    # Восстанавливаем именно те cache entries, которые V3 создал во время recovery.
    # Все поля и версии книг взяты из journal: повторный production_outlook здесь
    # увидел бы уже отменённые offers и мог бы вернуть другой план.
    outlook_cache = getattr(engine, '_production_outlook_cache', {})
    stock_cache = {}
    limits = ('Готовые запасы', 'Нет обеспеченного спроса', 'План',
              'Отрицательная ожидаемая маржа')
    for index, e in enumerate(enterprises):
        if not data['recovery_outlook_valid'][index]:
            continue
        if data['recovery_stock_cache_set'][index]:
            stock_cache[e['id']] = float(data['recovery_debug_expected'][index])
        history = tuple((float(data['enterprise_history_sold'][index, k]),
                         float(data['enterprise_history_output'][index, k]),
                         bool(data['enterprise_history_input_limited'][index, k]))
                        for k in range(data['enterprise_history_count'][index]))
        outlook_goods = engine.__dict__.setdefault('_outlook_goods', {}).get(e['good'])
        if outlook_goods is None:
            recipe = engine.goods[e['good']]
            outlook_goods = tuple(sorted(set(recipe['inputs'])
                | set(engine.equipment_requirements(recipe, 1)) | {e['good']}))
            engine._outlook_goods[e['good']] = outlook_goods
        quote_versions = (getattr(engine, '_market_quotes_epoch', 0),
            tuple(int(data['recovery_market_version'][index, meta['good_index'][good]])
                  for good in outlook_goods))
        key = (id(sells), e['id'], e['tile_id'], e['good'], e['price'],
            e.get('production_price'), e['capacity'], e.get('planned_output', 0),
            accounts[int(data['enterprise_account'][index])]['stock'].get(e['good'], 0),
            history, engine._demand_revision[e['good']], quote_versions,
            int(data['enterprise_trades_version'][index]))
        target = float(data['recovery_debug_target'][index])
        outlook_cache[key] = dict(
            sales_forecast=float(data['recovery_debug_forecast'][index]),
            reachable_demand=float(data['recovery_debug_demand'][index]),
            expected_unit_cost=float(data['recovery_expected_cost'][index]),
            expected_price=float(data['recovery_expected_price'][index]),
            expected_margin=float(data['recovery_debug_margin'][index]),
            production_limit=limits[int(data['recovery_production_limit'][index])],
            planned_output=target,
            input_target_output=target,
            planned_price=float(data['recovery_planned_price'][index]),
            expected_fixed_cost=float(data['recovery_fixed_cost'][index]),
            expected_fixed_unit=float(data['recovery_fixed_unit'][index]))
    engine._production_outlook_cache = outlook_cache
    engine._stock_sales_cache = stock_cache
    # defaultdict V3 материализует нулевую ревизию для каждого оценённого товара.
    for index, e in enumerate(enterprises):
        if data['recovery_outlook_valid'][index]:
            engine._demand_revision[e['good']]
    for i in range(data['recovery_outlook_count']):
        index = int(data['recovery_enterprise'][i])
        e = enterprises[index]
        firm = accounts[int(data['enterprise_account'][index])]
        owner = accounts[int(data['enterprise_owner'][index])]
        amount = float(data['recovery_transfer'][i])
        if amount > 0:
            owner['balance'] -= amount
            firm['balance'] += amount
            engine.report['flows'].append(dict(sender=owner['id'], recipient=firm['id'],
                gold=amount, reason='Восстановление оборотного капитала'))
            journal.append(('recovery_transfer', i, owner['id'], firm['id'], amount))
        if data['recovery_reopened'][i]:
            e.update(status='idle', closed=None, unviable_months=0)
            for j in range(data['recovery_cancel_count'][i]):
                sell_index = int(data['recovery_cancel_sell'][i, j])
                order_id = f'{meta["month"]}-{int(data["sell_order_index"][sell_index])}'
                order = next(o for o in engine.report['orders'] if o['id'] == order_id)
                good = order['good']
                firm['stock_reserved'][good] = max(0, firm['stock_reserved'].get(good, 0)
                                                   -order['remaining'])
                order.update(status='cancelled', reason='Сырьё возвращено в производство')
                engine.mark_market_quotes_dirty(good)
                journal.append(('recovery_cancel', i, order_id))
            engine.report['lifecycle'].append(dict(enterprise=e['id'], good=e['good'],
                action='reopened', reason='Восстановлен обеспеченный рентабельный спрос'))
            journal.append(('recovery_reopen', i, e['id']))
        hold = float(data['recovery_hold'][i])
        if e['status'] != 'closed':
            firm['operating_protected'] = hold
            firm['protected'] += hold
            journal.append(('recovery_hold', i, e['id'], hold))
    return journal


def apply_public_projects(engine, data, meta, sells):
    """Материализовать закрепление склада, release events и публичные заявки."""
    accounts = list(engine.accounts.values())
    goods = list(engine.goods)
    journal = []
    releases = [[] for _ in meta['public_requests']]
    for i in range(data['public_release_count']):
        releases[int(data['public_release_request'][i])].append(i)
    purposes = ('Материалы городской стройки', 'Оснащение городской стройки',
                'Содержание ускоренной стройки')
    for request, row in enumerate(meta['public_requests']):
        account_index, project, bucket, good_index, _, purpose = row
        account = accounts[account_index]
        good = goods[good_index]
        project[bucket][good] = float(data['public_held'][request])
        for release_index in releases[request]:
            sell_index = int(data['public_release_sell'][release_index])
            order_id = f'{meta["month"]}-{int(data["sell_order_index"][sell_index])}'
            order = next(o for o in engine.report['orders'] if o['id'] == order_id)
            take = float(data['public_release_quantity'][release_index])
            order['quantity'] -= take
            order['remaining'] -= take
            order['backing'] -= take
            account['stock_reserved'][good] = max(0,
                account['stock_reserved'].get(good, 0)-take)
            engine.mark_market_quotes_dirty(good)
            if order['remaining'] <= EPS:
                engine.cancel_order(order, 'Материалы закреплены за городской стройкой')
            journal.append(('public_stock_release', request, order_id, take))
        order_index = int(data['public_order_index'][request])
        if order_index < 0:
            continue
        quantity = float(data['public_order_quantity'][request])
        price = float(data['public_order_price'][request])
        reserved = float(data['public_order_reserved'][request])
        state_id, province_id, tile_id = meta['order_locations'][account_index]
        order = dict(id=f'{meta["month"]}-{order_index}', participant=account['id'],
            good=good, quantity=quantity, remaining=quantity, price=price, side='buy',
            purpose=purposes[purpose], status='active', reason='', expires=meta['month'],
            final_consumption=False, state_id=state_id, province_id=province_id,
            tile_id=tile_id, reserved=reserved, backing=reserved, project_id=project['id'])
        account['reserved'] = account.get('reserved', 0)+reserved
        engine.report['orders'].append(order)
        engine._demand_revision[good] += 1
        journal.append(('create_public_project_order', request, order['id'], quantity,
                        price, reserved))
    return journal


def _run(engine, include_recovery, include_sells=False, include_households=False,
         include_owners=False, include_enterprise_recovery=False, include_public=False):
    timings = {}
    started = time.perf_counter()
    data, meta = pack_update_prices(engine)
    timings['pack'] = time.perf_counter()-started
    native = load_serial_step()
    started = time.perf_counter()
    if include_public:
        native.public_project_preparation(data, engine.rng)
    elif include_enterprise_recovery:
        native.recovery_preparation(data, engine.rng)
    elif include_owners:
        native.owner_preparation(data, engine.rng)
    elif include_households:
        native.household_preparation(data, engine.rng)
    elif include_sells:
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
    households = None
    food_trace = None
    if include_households:
        accounts = list(engine.accounts.values())
        households = [accounts[int(i)] for i in data['household_order']]
        household_journal, food_trace = apply_households(engine, data, meta, households, sells)
        journal.extend(household_journal)
    if include_owners:
        journal.extend(apply_owners(engine, data, meta, sells))
    if include_enterprise_recovery:
        journal.extend(apply_recovery(engine, data, meta, sells))
    if include_public:
        journal.extend(apply_public_projects(engine, data, meta, sells))
    timings['apply'] = time.perf_counter()-started
    result = dict(timings=timings, data=data, meta=meta, journal=journal, sells=sells,
                  households=households, food_trace=food_trace)
    engine.native_update_prices = result
    engine.native_preparation = result
    if include_sells:
        engine.native_market = result
    if include_households:
        engine.native_households = result
    if include_owners:
        engine.native_owners = result
    if include_enterprise_recovery:
        engine.native_recovery = result
    if include_public:
        engine.native_public_projects = result
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


def run_native_households(engine):
    """Общий native boundary до конца basic/extra household requests."""
    return _run(engine, include_recovery=True, include_sells=True, include_households=True)


def run_native_owners(engine):
    """Общий native boundary до конца owner_requests."""
    return _run(engine, include_recovery=True, include_sells=True, include_households=True,
                include_owners=True)


def run_native_recovery(engine):
    """Общий native boundary до конца recover_enterprises."""
    return _run(engine, include_recovery=True, include_sells=True, include_households=True,
                include_owners=True, include_enterprise_recovery=True)


def run_native_public_projects(engine):
    """Общий native boundary до конца public_project_requests."""
    return _run(engine, include_recovery=True, include_sells=True, include_households=True,
                include_owners=True, include_enterprise_recovery=True, include_public=True)
