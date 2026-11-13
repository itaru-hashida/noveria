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
    construction_goods, construction_values, construction_amounts = [], [], []
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
        construction_amounts.append(engine.cfg['construction'][good])
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
        construction_amount=_float64(construction_amounts),
        minimum_capacity=float(engine.cfg['minimum_capacity']),
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


class _InvestmentPackView:
    """Локальный индекс для read-only pack без записи временных полей engine."""

    def __init__(self, engine, state):
        self.engine = engine
        self._investment_state = state
        self._investment_index = state['index']

    def __getattr__(self, name):
        return getattr(self.engine, name)


def pack_project_requests(engine, sells):
    """Один снимок всего обзора, включая возможные upstream-пары."""
    import copy

    state = engine.build_investment_state()
    view = _InvestmentPackView(engine, state)
    inv, meta = pack_investment_state(view, sells=sells)
    extra = {(firm['province_id'], firm['good'])
             for firm in engine.enterprises.values() if firm['status'] != 'closed'}
    support, names = build_candidate_support(view, sells, inv, meta,
                                            extra_pairs=extra, all_goods=True)
    names.pop('ctx', None)
    for key in ('account', 'good', 'province', 'state', 'tile'):
        names[key + '_names'] = list(sorted(names[key + '_index'],
                                            key=names[key + '_index'].get))
    names['province_city'] = {pid: engine.location(pid).get('city_id')
                              for pid in names['province_index'] if pid is not None}
    # В native не передаётся engine, bound method или Python callback.
    # Проекты и строки решений — объекты выходного журнала.
    accounts = {aid: {key: copy.deepcopy(value) for key, value in account.items()
                     if key in ('id', 'kind', 'state_id', 'province_id', 'project',
                                'optimism', 'risk_tolerance')}
                for aid, account in engine.accounts.items()}
    report_keys = ('investment_decisions', 'investment_candidates', 'events')
    snapshot = dict(
        trace=bool(getattr(engine, '_native_project_trace', False)),
        accounts=accounts, enterprises=engine.enterprises,
        goods=engine.goods, cfg=engine.cfg, tiles=engine.tiles, owners=engine.owners,
        upstream_diagnostics=copy.deepcopy(getattr(engine, '_upstream_candidate_diagnostics', None)),
        state=state, dirty_goods=set(getattr(engine, '_investment_dirty_goods',
                                            engine.meta.get('investment_dirty', []))),
        meta=copy.deepcopy({key: engine.meta[key] for key in
            ('month', 'investment_queue', 'investment_full_reconcile_month', 'report_detail')
            if key in engine.meta}),
        report={key: copy.deepcopy(engine.report[key]) for key in report_keys})
    snapshot['meta'].setdefault('report_detail', engine.report_detail())
    return inv, support, names, snapshot


def apply_project_requests(engine, result, names):
    """Материализовать готовый журнал; оценки и commit уже завершены."""
    result['journal'] = result['decision_buffer'].materialize(names)
    for aid, row in result['accounts'].items():
        target = engine.accounts[aid]
        for key in ('project', 'optimism', 'risk_tolerance'):
            if key in row:
                target[key] = row[key]
    for key in ('investment_queue', 'investment_full_reconcile_month'):
        if key in result['meta']:
            if key == 'investment_queue':
                engine.meta.setdefault(key, [])[:] = result['meta'][key]
            else:
                engine.meta[key] = result['meta'][key]
    engine.report.update(result['report'])
    evaluate = result['report']['investment_review']['mode'] != 'skipped'
    if evaluate or hasattr(engine, '_investment_dirty_goods'):
        engine._investment_dirty_goods = result['dirty_goods']
    if evaluate:
        for key in ('_investment_supply_cache', '_investment_index',
                    '_investment_demand_cache', '_upstream_candidate_cache',
                    '_investment_input_reserved', '_investment_state',
                    '_investment_customer_limits'):
            engine.__dict__.pop(key, None)
    if result['upstream_diagnostics'] is not None:
        engine._upstream_candidate_diagnostics = result['upstream_diagnostics']


def run_native_project_requests(engine, sells):
    """Один pack/call/apply; material requests остаётся отдельной Python-фазой."""
    from . import serial_step

    started = time.perf_counter()
    inv, support, names, snapshot = pack_project_requests(engine, sells)
    packed = time.perf_counter()
    result = serial_step.native_project_requests(inv, support, names, snapshot, engine.rng)
    calculated = time.perf_counter()
    apply_project_requests(engine, result, names)
    applied = time.perf_counter()
    result['timings'] = dict(pack=packed-started, native=calculated-packed,
                             apply=applied-calculated)
    result['native_calls'] = 1
    engine.native_project_requests = result
    investors = [engine.accounts[aid] for aid in result['investors']]
    engine.project_material_requests(sells, result['due'], investors)
    return result['journal']


def pack_investment_state(engine, data=None, meta=None, sells=None):
    """Flat packed state для будущего native project-прохода (Шаг 3).

    Только упаковка: никаких вычислений кандидатов, никаких RNG-вызовов,
    никакой записи в engine. Функция read-only относительно движка:

    - не создаёт `_investment_state`/`_investment_index`, если их нет;
    - не материализует ленивые поля (`profile` не вызывается, optimism/
      risk_tolerance отсутствующих инвесторов остаются NaN);
    - не меняет report, meta, словари счетов и очередь.

    Индексы и маршрутная матрица переиспользуются из pack_update_prices(),
    если переданы data/meta; иначе строятся заново тем же способом.

    Возвращает (inv_data, inv_meta). Все игровые строки (ID, purpose, reason)
    остаются в inv_meta как Python-таблицы; inv_data содержит только числовые
    массивы, пригодные для typed memoryviews.
    """
    import copy as _copy

    accounts = list(engine.accounts.values())
    enterprises = list(engine.enterprises.values())
    goods = list(engine.goods)
    if data is not None and meta is not None:
        account_index = meta['account_index']
        enterprise_index = meta['enterprise_index']
        good_index = meta['good_index']
        distances = data['distances']
        tile_index = None  # восстанавливается ниже по meta, если нужно
    else:
        account_index = _indices(a['id'] for a in accounts)
        enterprise_index = _indices(e['id'] for e in enterprises)
        good_index = _indices(goods)
        distances = None
        tile_index = None

    # --- площадки и маршруты (тот же набор, что в pack_update_prices) ---
    public_orders = [o for o in engine.meta.get('public_orders', []) if o['side'] == 'buy']
    feedback = [o for o in engine.meta.get('price_feedback', []) if o.get('unfunded_need')]
    market_rows = [row for month in engine.meta.get('market_history', [])
                   for row in month['markets']]
    demand_orders = [o for o in engine.report['orders'] if o['side'] == 'buy']
    tile_ids = {a.get('cell_id') or a['tile_id'] for a in accounts}
    tile_ids.update(e['tile_id'] for e in enterprises)
    tile_ids.update(o['tile_id'] for o in public_orders)
    tile_ids.update(o['tile_id'] for o in feedback)
    tile_ids.update(o['tile_id'] for o in demand_orders)
    tile_ids.update(a['project']['location']['tile_id'] for a in accounts if a.get('project'))
    tiles = sorted(tile_ids)
    tile_index = _indices(tiles)
    if distances is None:
        distances = np.full((len(tiles), len(tiles)), np.nan, dtype=np.float64)
        for source_index, source in enumerate(tiles):
            for target_index, target in enumerate(tiles):
                distance, _ = engine.path(source, target)
                if distance is not None:
                    distances[source_index, target_index] = distance

    states = list(dict.fromkeys([a['state_id'] for a in accounts]
                                + [r['state_id'] for r in market_rows]))
    provinces = list(dict.fromkeys([a['province_id'] for a in accounts]
                                   + [engine.market_province(a) for a in accounts]
                                   + [r['province_id'] for r in market_rows]))
    state_index = _indices(states)
    province_index = _indices(provinces)

    # --- ресурсы и плитки (для resource tile RNG и capacity) ---
    resource_names = sorted({recipe['resource'] for recipe in engine.goods.values()
                             if recipe['resource']})
    resource_index = _indices(resource_names)
    tile_resources = []
    tile_resource_offsets = [0]
    for tile in tiles:
        values = engine.tiles[tile]['resources']
        tile_resources.extend(resource_index[r] for r in values if r in resource_index)
        tile_resource_offsets.append(len(tile_resources))
    tile_province = _int32([province_index[engine.tiles[t]['province_id']]
                            if engine.tiles[t]['province_id'] in province_index else -1
                            for t in tiles])

    # --- investors: тот же предикат, что в project_requests ---
    month = engine.meta['month']
    investor_accounts = []
    for index, account in enumerate(accounts):
        if account['kind'] in ('private', 'merchant', 'treasury', 'municipal'):
            investor_accounts.append(index)
            continue
        if account['kind'] == 'enterprise':
            firm = engine.enterprises[account['id']]
            if firm['status'] != 'closed' and firm['available_from'] <= month:
                investor_accounts.append(index)
    investor_accounts.sort(key=lambda i: accounts[i]['id'])

    kind_codes = {'private': 0, 'merchant': 1, 'treasury': 2, 'municipal': 3,
                  'enterprise': 4, 'household': 5}
    n_acc, n_goods = len(accounts), len(goods)
    account_stock = np.zeros((n_acc, n_goods), dtype=np.float64)
    account_stock_reserved = np.zeros((n_acc, n_goods), dtype=np.float64)
    for index, account in enumerate(accounts):
        for good, quantity in account['stock'].items():
            account_stock[index, good_index[good]] = quantity
        for good, quantity in account.get('stock_reserved', {}).items():
            account_stock_reserved[index, good_index[good]] = quantity

    # --- проекты счетов (round-trip: сырые копии в meta) ---
    project_accounts = []
    project_raw = []
    for index, account in enumerate(accounts):
        project = account.get('project')
        if project:
            project_accounts.append(index)
            project_raw.append(_copy.deepcopy(project))
    n_proj = len(project_accounts)
    project_materials = np.zeros((n_proj, n_goods), dtype=np.float64)
    chain_offsets = [0]
    chain_goods, chain_values = [], []
    for slot, project in enumerate(project_raw):
        for good, quantity in project.get('materials', {}).items():
            project_materials[slot, good_index[good]] = quantity
        for good, value in sorted(project.get('chain_requirements', {}).items()):
            chain_goods.append(good_index[good])
            chain_values.append(value)
        chain_offsets.append(len(chain_goods))

    # --- investment_queue (round-trip: сырые копии в meta) ---
    queue_raw = _copy.deepcopy(engine.meta.get('investment_queue', []))
    queue_investor = _int32([account_index.get(item.get('investor'), -1) for item in queue_raw])
    queue_created = _int32([item.get('created', 0) for item in queue_raw])

    # --- рецепты и cfg ---
    recipe_input_offsets = [0]
    recipe_input_goods, recipe_input_values = [], []
    recipe_equipment_offsets = [0]
    recipe_equipment_goods, recipe_equipment_values = [], []
    for product in goods:
        recipe = engine.goods[product]
        for good, ratio in recipe['inputs'].items():
            recipe_input_goods.append(good_index[good])
            recipe_input_values.append(ratio)
        recipe_input_offsets.append(len(recipe_input_goods))
        for good, ratio in recipe.get('equipment', {}).items():
            recipe_equipment_goods.append(good_index[good])
            recipe_equipment_values.append(ratio)
        recipe_equipment_offsets.append(len(recipe_equipment_goods))
    construction_goods = list(engine.cfg['construction'])
    construction_values = [engine.cfg['construction'][g] for g in construction_goods]

    # --- market history (поля, которые читает market_signal) ---
    history_offsets = [0]
    history_state, history_province, history_good = [], [], []
    history_volume, history_requested, history_unmet = [], [], []
    history_trade_gold, history_freight, history_limit_value = [], [], []
    for entry in engine.meta.get('market_history', []):
        for row in entry['markets']:
            history_state.append(state_index[row['state_id']])
            history_province.append(province_index[row['province_id']])
            history_good.append(good_index[row['good']])
            history_volume.append(row['volume'])
            history_requested.append(row['requested'])
            history_unmet.append(row['unmet_solvent'])
            history_trade_gold.append(row['trade_gold'])
            history_freight.append(row['freight'])
            history_limit_value.append(row['limit_value'])
        history_offsets.append(len(history_state))

    # --- report orders (все; buy-подмножество отдельно) ---
    orders = list(engine.report['orders'])
    order_ids = [o['id'] for o in orders]
    order_id_index = {oid: i for i, oid in enumerate(order_ids)}
    order_side_buy = _uint8([o['side'] == 'buy' for o in orders])
    buy_order_indices = _int32([i for i, o in enumerate(orders) if o['side'] == 'buy'])
    order_status_active = _uint8([o['status'] not in ('cancelled', 'expired') for o in orders])
    order_status_cancelled = _uint8([o['status'] == 'cancelled' for o in orders])
    order_status_expired = _uint8([o['status'] == 'expired' for o in orders])
    order_final_consumption = _uint8([bool(o.get('final_consumption')) for o in orders])
    order_construction = _uint8([o.get('purpose') == 'Строительные материалы' for o in orders])

    # --- sells (книга офферов, виденная project_requests) ---
    sells = sells if sells is not None else {}
    sell_offsets = [0]
    sell_order_idx, sell_participant, sell_province, sell_tile = [], [], [], []
    sell_price, sell_quantity, sell_remaining, sell_backing = [], [], [], []
    sell_missing = []
    for good in goods:
        for offer in sells.get(good, []):
            idx = order_id_index.get(offer.get('id'), -1)
            if idx < 0:
                sell_missing.append(offer.get('id'))
            sell_order_idx.append(idx)
            sell_participant.append(account_index.get(offer['participant'], -1))
            sell_province.append(province_index.get(offer['province_id'], -1))
            sell_tile.append(tile_index.get(offer['tile_id'], -1))
            sell_price.append(offer['price'])
            sell_quantity.append(offer['quantity'])
            sell_remaining.append(offer['remaining'])
            sell_backing.append(offer.get('backing', 0))
        sell_offsets.append(len(sell_order_idx))

    # --- recovery quotes ---
    recovery_offsets = [0]
    recovery_firm, recovery_price = [], []
    recovery_quotes = getattr(engine, '_recovery_quotes', {}) or {}
    for good in goods:
        for firm, price in recovery_quotes.get(good, []):
            recovery_firm.append(enterprise_index.get(firm['id'], -1))
            recovery_price.append(price)
        recovery_offsets.append(len(recovery_firm))

    # --- версии и dirty goods ---
    dirty_source = getattr(engine, '_investment_dirty_goods', None)
    if dirty_source is None:
        dirty_source = engine.meta.get('investment_dirty', [])
    dirty_flags = _uint8([g in set(dirty_source) for g in goods])
    due = month % engine.cfg['investment_interval'] == 0

    # --- candidate support state: только если уже существует ---
    state = getattr(engine, '_investment_state', None)
    index_snapshot = getattr(engine, '_investment_index', None)
    input_reserved = np.zeros(n_goods, dtype=np.float64)
    supply_revision = np.zeros(n_goods, dtype=np.int32)
    flow_rows, labor_rows, resource_rows = [], [], []
    flow_balances_raw = {}
    upstream_raw = {}
    if state is not None:
        for good, value in state['input_reserved'].items():
            input_reserved[good_index[good]] = value
        for good, value in state['supply_revision'].items():
            supply_revision[good_index[good]] = value
        for (state_id, good), value in state['flow_reserved'].items():
            flow_rows.append((state_index[state_id], good_index[good], value))
        for (state_id, province_id), value in state['labor_reserved'].items():
            labor_rows.append((state_index[state_id], province_index[province_id], value))
        for (province_id, resource), value in state['resource_reserved'].items():
            resource_rows.append((province_index[province_id], resource_index[resource], value))
        flow_balances_raw = _copy.deepcopy(state['flow_balances'])
        upstream_raw = _copy.deepcopy(state['upstream_candidates'])
    labor_reserved_available, resource_used_available = {}, {}
    markets_pairs = []
    if index_snapshot is not None:
        labor_reserved_available = {k: v for k, v in index_snapshot['labor'].items()}
        resource_used_available = {k: v for k, v in index_snapshot['resource_used'].items()}
        for state_id, pairs in index_snapshot['markets'].items():
            for province_id, good in pairs:
                markets_pairs.append((state_id, province_id, good))
    markets_pairs.sort()
    labor_keys = sorted(labor_reserved_available)
    resource_used_keys = sorted(resource_used_available)

    inv_data = dict(
        # accounts / investors
        account_kind=_uint8([kind_codes.get(a['kind'], 255) for a in accounts]),
        account_state=_int32([state_index[a['state_id']] for a in accounts]),
        account_province=_int32([province_index[a['province_id']] for a in accounts]),
        account_tile=_int32([tile_index[a.get('cell_id') or a['tile_id']] for a in accounts]),
        account_balance=_float64([a['balance'] for a in accounts]),
        account_reserved=_float64([a.get('reserved', 0) for a in accounts]),
        account_protected=_float64([a.get('protected', 0) for a in accounts]),
        account_operating_protected=_float64([a.get('operating_protected', 0) for a in accounts]),
        account_payroll_protected=_float64([a.get('payroll_protected', 0) for a in accounts]),
        # NaN = profile ещё не вызывался (ленивые поля не материализуются)
        account_optimism=_float64([a.get('optimism', np.nan) for a in accounts]),
        account_risk_tolerance=_float64([a.get('risk_tolerance', np.nan) for a in accounts]),
        account_has_project=_uint8([bool(a.get('project')) for a in accounts]),
        account_is_investor=_uint8([i in set(investor_accounts) for i in range(n_acc)]),
        account_stock=account_stock,
        account_stock_reserved=account_stock_reserved,
        investor_account=_int32(investor_accounts),
        investor_count=len(investor_accounts),
        # enterprises
        enterprise_good=_int32([good_index[e['good']] for e in enterprises]),
        enterprise_state=_int32([state_index[e['state_id']] for e in enterprises]),
        enterprise_province=_int32([province_index[e['province_id']] for e in enterprises]),
        enterprise_tile=_int32([tile_index[e['tile_id']] for e in enterprises]),
        enterprise_status_closed=_uint8([e['status'] == 'closed' for e in enterprises]),
        enterprise_suspended=_uint8([bool(e.get('suspended')) for e in enterprises]),
        enterprise_available_from=_int32([e['available_from'] for e in enterprises]),
        enterprise_capacity=_float64([e['capacity'] for e in enterprises]),
        enterprise_price=_float64([e['price'] for e in enterprises]),
        enterprise_account=_int32([account_index[e['id']] for e in enterprises]),
        # projects
        project_account=_int32(project_accounts),
        project_count=n_proj,
        project_good=_int32([good_index[p['good']] for p in project_raw]),
        project_capacity=_float64([p['capacity'] for p in project_raw]),
        project_working_cash=_float64([p['working_cash'] for p in project_raw]),
        project_delivery=_float64([p.get('delivery', 0) for p in project_raw]),
        project_expected_price=_float64([p['expected_price'] for p in project_raw]),
        project_expected_profit=_float64([p['expected_profit'] for p in project_raw]),
        project_expected_investment=_float64([p['expected_investment'] for p in project_raw]),
        project_expected_unit_cost=_float64([p['expected_unit_cost'] for p in project_raw]),
        project_expansion=_uint8([bool(p['expansion']) for p in project_raw]),
        project_created=_int32([p['created'] for p in project_raw]),
        project_last_progress=_int32([p.get('last_progress', p['created']) for p in project_raw]),
        project_progress_quantity=_float64([p.get('progress_quantity', 0) for p in project_raw]),
        project_state=_int32([state_index[p['location']['state_id']] for p in project_raw]),
        project_province=_int32([province_index[p['location']['province_id']] for p in project_raw]),
        project_tile=_int32([tile_index[p['location']['tile_id']] for p in project_raw]),
        project_city_present=_uint8([p['location'].get('city_id') is not None for p in project_raw]),
        project_materials=project_materials,
        project_chain_offset=_int32(chain_offsets),
        project_chain_good=_int32(chain_goods),
        project_chain_value=_float64(chain_values),
        project_has_parent=_uint8([p.get('parent_project_id') is not None for p in project_raw]),
        project_dependency_expires=_int32([p.get('dependency_expires') or -1 for p in project_raw]),
        # investment queue
        queue_investor=queue_investor,
        queue_created=queue_created,
        queue_count=len(queue_raw),
        # goods / recipes / cfg
        good_labor=_float64([engine.goods[g]['labor'] for g in goods]),
        good_weight=_float64([engine.goods[g]['weight'] for g in goods]),
        good_price=_float64([engine.goods[g]['price'] for g in goods]),
        good_resource=_int32([resource_index.get(engine.goods[g]['resource'], -1) for g in goods]),
        good_consumer=_uint8([g in (engine.cfg['basic_needs'] | engine.cfg['extra_needs'])
                              for g in goods]),
        recipe_input_offset=_int32(recipe_input_offsets),
        recipe_input_good=_int32(recipe_input_goods),
        recipe_input_value=_float64(recipe_input_values),
        recipe_equipment_offset=_int32(recipe_equipment_offsets),
        recipe_equipment_good=_int32(recipe_equipment_goods),
        recipe_equipment_value=_float64(recipe_equipment_values),
        construction_good=_int32([good_index[g] for g in construction_goods]),
        construction_value=_float64(construction_values),
        minimum_capacity=float(engine.cfg['minimum_capacity']),
        investment_interval=int(engine.cfg['investment_interval']),
        project_stall_months=int(engine.cfg.get('project_stall_months', 12)),
        transport_rate=float(engine.cfg['transport_rate']),
        wage=float(engine.cfg['wage']),
        enterprise_overhead_rate=float(engine.cfg.get('enterprise_overhead_rate', 0)),
        startup_cash=float(engine.cfg['startup_cash']),
        investment_payback_months=float(engine.cfg['investment_payback_months']),
        extraction_per_tile=float(engine.cfg['extraction_per_tile']),
        labor_share=float(engine.cfg['labor_share']),
        consumption_tax=float(engine.cfg['consumption_tax']),
        eps=float(EPS),
        # tiles / resources
        tile_province=tile_province,
        tile_resource_offset=_int32(tile_resource_offsets),
        tile_resource=_int32(tile_resources),
        distances=distances,
        # market history
        history_offset=_int32(history_offsets),
        history_state=_int32(history_state),
        history_province=_int32(history_province),
        history_good=_int32(history_good),
        history_volume=_float64(history_volume),
        history_requested=_float64(history_requested),
        history_unmet=_float64(history_unmet),
        history_trade_gold=_float64(history_trade_gold),
        history_freight=_float64(history_freight),
        history_limit_value=_float64(history_limit_value),
        history_month_count=len(engine.meta.get('market_history', [])),
        # orders / sells / recovery quotes
        order_count=len(orders),
        order_good=_int32([good_index[o['good']] for o in orders]),
        order_participant=_int32([account_index[o['participant']] for o in orders]),
        order_state=_int32([state_index[o['state_id']] for o in orders]),
        order_province=_int32([province_index[o['province_id']] for o in orders]),
        order_tile=_int32([tile_index[o['tile_id']] for o in orders]),
        order_side_buy=order_side_buy,
        order_status_active=order_status_active,
        order_status_cancelled=order_status_cancelled,
        order_status_expired=order_status_expired,
        order_price=_float64([o['price'] for o in orders]),
        order_quantity=_float64([o['quantity'] for o in orders]),
        order_remaining=_float64([o['remaining'] for o in orders]),
        order_reserved=_float64([o.get('reserved', 0) for o in orders]),
        order_backing=_float64([o.get('backing', 0) for o in orders]),
        order_final_consumption=order_final_consumption,
        order_construction=order_construction,
        buy_order_index=buy_order_indices,
        buy_order_count=len(buy_order_indices),
        sell_offset=_int32(sell_offsets),
        sell_order_index=_int32(sell_order_idx),
        sell_participant=_int32(sell_participant),
        sell_province=_int32(sell_province),
        sell_tile=_int32(sell_tile),
        sell_price=_float64(sell_price),
        sell_quantity=_float64(sell_quantity),
        sell_remaining=_float64(sell_remaining),
        sell_backing=_float64(sell_backing),
        recovery_quote_offset=_int32(recovery_offsets),
        recovery_quote_firm=_int32(recovery_firm),
        recovery_quote_price=_float64(recovery_price),
        # versions / dirty goods / review
        market_quote_version=int(getattr(engine, '_market_quote_version', 0)),
        market_quote_epoch=int(getattr(engine, '_market_quotes_epoch', 0)),
        good_demand_revision=_int32([getattr(engine, '_demand_revision', {}).get(g, 0)
                                     for g in goods]),
        good_quote_version=_int32([getattr(engine, '_market_quote_versions', {}).get(g, 0)
                                   for g in goods]),
        dirty_good=dirty_flags,
        investment_due=bool(due),
        current_month=int(month),
        # candidate support state (пусто, если обзор ещё не строился)
        input_reserved=input_reserved,
        supply_revision=supply_revision,
        flow_reserved_state=_int32([r[0] for r in flow_rows]),
        flow_reserved_good=_int32([r[1] for r in flow_rows]),
        flow_reserved_value=_float64([r[2] for r in flow_rows]),
        flow_reserved_count=len(flow_rows),
        labor_reserved_state=_int32([r[0] for r in labor_rows]),
        labor_reserved_province=_int32([r[1] for r in labor_rows]),
        labor_reserved_value=_float64([r[2] for r in labor_rows]),
        labor_reserved_count=len(labor_rows),
        resource_reserved_province=_int32([r[0] for r in resource_rows]),
        resource_reserved_resource=_int32([r[1] for r in resource_rows]),
        resource_reserved_value=_float64([r[2] for r in resource_rows]),
        resource_reserved_count=len(resource_rows),
        market_pair_state=_int32([state_index[s] for s, _, _ in markets_pairs]),
        market_pair_province=_int32([province_index[p] for _, p, _ in markets_pairs]),
        market_pair_good=_int32([good_index[g] for _, _, g in markets_pairs]),
        market_pair_count=len(markets_pairs),
        labor_available_state=_int32([state_index[s] for s, _ in labor_keys]),
        labor_available_province=_int32([province_index[p] for _, p in labor_keys]),
        labor_available_value=_float64([labor_reserved_available[k] for k in labor_keys]),
        resource_used_province=_int32([province_index[p] for p, _ in resource_used_keys]),
        resource_used_resource=_int32([resource_index[r] for _, r in resource_used_keys]),
        resource_used_value=_float64([resource_used_available[k] for k in resource_used_keys]),
        investment_state_present=state is not None,
        investment_index_present=index_snapshot is not None,
    )

    inv_meta = dict(
        account_index=account_index,
        enterprise_index=enterprise_index,
        good_index=good_index,
        state_index=state_index,
        province_index=province_index,
        tile_index=tile_index,
        resource_index=resource_index,
        kind_codes=kind_codes,
        month=month,
        order_ids=order_ids,
        sell_missing_ids=sell_missing,
        projects_raw=project_raw,
        queue_raw=queue_raw,
        dirty_goods=sorted(set(dirty_source)),
        flow_balances_raw=flow_balances_raw,
        upstream_cache_raw=upstream_raw,
        resource_names=resource_names,
        states=states,
        provinces=provinces,
        tiles=tiles,
    )
    return inv_data, inv_meta


class _CandidateContext:
    """Read-only реплики хелперов V3 для pack candidate support.

    Не вызывает методы engine, пишущие кэши (reference_price/quoted_cost/
    market_book и т.п.): всё нужное реплицируется по сырым данным.
    Детерминированные кэши маршрутов (path/route_component) допустимы —
    V3 заполнил бы их в том же месяце теми же значениями.
    """

    def __init__(self, engine, sells):
        self.engine = engine
        self.sells = sells
        self.eps = EPS
        self.rate = engine.cfg['transport_rate']
        self.tax = engine.cfg['consumption_tax']
        self.wage = engine.cfg['wage']
        self.min_capacity = engine.cfg['minimum_capacity']
        self.labor_share = engine.cfg['labor_share']
        self.history = engine.meta.get('market_history', [])
        self.orders = engine.report['orders']
        self._books = {}
        self._signals = {}
        self._ref = {}
        self._quotes = {}
        self._dq_rows = {}

    def landed(self, seller_tile, buyer_tile, good, price):
        distance, _ = self.engine.path(seller_tile, buyer_tile)
        if distance is None:
            return None
        weight = self.engine.goods[good]['weight']
        freight = distance*weight*self.rate
        return price+freight, freight

    def book(self, buyer_tile, good):
        key = (buyer_tile, good)
        entry = self._books.get(key)
        if entry is not None:
            return entry
        component = self.engine.route_component(buyer_tile)
        options = []
        for sell in self.sells.get(good, []):
            if self.engine.route_component(sell['tile_id']) != component:
                continue
            quote = self.landed(sell['tile_id'], buyer_tile, good, sell['price'])
            if quote:
                options.append((quote[0], sell))
        options.sort(key=lambda item: (item[0], item[1]['id']))
        self._books[key] = options
        return options

    def live_offers(self, buyer_tile, good, exclude_id):
        for price, sell in self.book(buyer_tile, good):
            if (sell['participant'] != exclude_id and sell['remaining'] > EPS
                    and sell['status'] not in ('cancelled', 'expired')):
                yield price, sell

    def ref_price(self, state_id, province_id, buyer_tile, good, exclude_id):
        key = (state_id, province_id, buyer_tile, good, exclude_id)
        entry = self._ref.get(key)
        if entry is not None:
            return entry
        for price, _ in self.live_offers(buyer_tile, good, exclude_id):
            self._ref[key] = price
            return price
        signal = self.signal(state_id, province_id, good)
        result = signal['landed_price'] or self.engine.goods[good]['price']
        self._ref[key] = result
        return result

    def signal(self, state_id, province_id, good):
        key = (state_id, province_id, good)
        entry = self._signals.get(key)
        if entry is not None:
            return entry
        rows = [r for month in self.history for r in month['markets']
                if r['state_id'] == state_id and r['province_id'] == province_id
                and r['good'] == good]
        if rows:
            volume = sum(r['volume'] for r in rows)
            requested = sum(r['requested'] for r in rows)
            signal = dict(volume=volume/len(self.history),
                          unmet=sum(r['unmet_solvent'] for r in rows)/len(self.history),
                          price=sum(r['trade_gold'] for r in rows)/volume if volume else None,
                          landed_price=sum(r['trade_gold']+r['freight'] for r in rows)/volume if volume else None,
                          limit_price=sum(r['limit_value'] for r in rows)/requested if requested else None)
        else:
            signal = dict(volume=0, unmet=0, price=None, landed_price=None, limit_price=None)
        self._signals[key] = signal
        return signal

    def customer_quotes(self, tile_id, good, exclude_id):
        key = (tile_id, good)
        entry = self._quotes.get(key)
        if entry is None:
            rows = []
            component = self.engine.route_component(tile_id)
            for order in self.orders:
                if order['side'] != 'buy' or order['good'] != good:
                    continue
                if self.engine.route_component(order['tile_id']) != component:
                    continue
                quote = self.landed(tile_id, order['tile_id'], good, 0)
                if quote:
                    rows.append((order, quote[1]))
            entry = [(order, order['price']-freight) for order, freight in rows
                     if order['remaining'] > EPS
                     and order['status'] not in ('cancelled', 'expired')]
            self._quotes[key] = entry
        return [(order, limit) for order, limit in entry if order['participant'] != exclude_id]

    def net_price(self, entity_id, tile_id, good, price):
        consumer = good in (self.engine.cfg['basic_needs'] | self.engine.cfg['extra_needs'])
        quantity, final_quantity = 0.0, 0.0
        for order, limit in self.customer_quotes(tile_id, good, entity_id):
            if limit+EPS < price:
                continue
            landed = price+(order['price']-limit)
            volume = min(order['remaining'], order['reserved']/max(landed, EPS))
            quantity += volume
            if order.get('final_consumption'):
                final_quantity += volume
        share = (final_quantity/quantity if quantity > EPS else float(consumer))
        return price*(1-self.tax*share)

    def unit_cost(self, entity_id, tile_id, state_id, province_id, good):
        recipe = self.engine.goods[good]
        raw = sum(ratio*self.ref_price(state_id, province_id, tile_id, input_good, entity_id)
                  for input_good, ratio in recipe['inputs'].items())
        wear = sum(quantity*self.ref_price(state_id, province_id, tile_id, equip_good, entity_id)
                   for equip_good, quantity in _equipment_wear(self.engine, recipe).items())
        return recipe['labor']*self.wage+raw+wear

    def quoted_cost(self, buyer_id, buyer_tile, state_id, province_id, good, quantity):
        left, value, ceiling = quantity, 0.0, 0.0
        for price, sell in self.live_offers(buyer_tile, good, buyer_id):
            q = min(left, sell['remaining'])
            value += q*price
            ceiling = max(ceiling, price)
            left -= q
            if left <= EPS:
                break
        if left > EPS:
            reference = self.ref_price(state_id, province_id, buyer_tile, good, buyer_id)
            quotes = []
            for firm, price in (getattr(self.engine, '_recovery_quotes', {}) or {}).get(good, []):
                if firm['id'] == buyer_id:
                    continue
                quote = self.landed(firm['tile_id'], buyer_tile, good, price)
                if quote:
                    quotes.append(quote[0])
            if quotes:
                reference = max(reference, min(quotes))
            value += left*reference
            ceiling = max(ceiling, reference)
        return value, max(ceiling, EPS), left <= EPS

    def demand_rows(self, tile_id, good):
        key = (tile_id, good)
        entry = self._dq_rows.get(key)
        if entry is None:
            entry = []
            component = self.engine.route_component(tile_id)
            for order in self.orders:
                if order['side'] != 'buy' or order['good'] != good:
                    continue
                if self.engine.route_component(order['tile_id']) != component:
                    continue
                quote = self.landed(tile_id, order['tile_id'], good, 0)
                if not quote or order['remaining'] <= EPS \
                        or order['status'] in ('cancelled', 'expired'):
                    continue
                freight = quote[1]
                entry.append((order['participant'], order['tile_id'], order['price'],
                              order['price']-freight, freight,
                              order['remaining'], order['reserved']))
            self._dq_rows[key] = entry
        return entry

    def project_cash_reserve(self, account_id):
        account = self.engine.accounts[account_id]
        project = account.get('project')
        if not project:
            return 0.0
        market_tile = account.get('cell_id') or account['tile_id']
        distance, _ = self.engine.path(market_tile, project['location']['tile_id'])
        bill = {g: q*project['capacity']/self.min_capacity
                for g, q in self.engine.cfg['construction'].items()}
        delivery = (sum(q*self.engine.goods[g]['weight'] for g, q in bill.items())
                    * distance*self.rate) if distance is not None else project.get('delivery', 0)
        needed = project['working_cash']+delivery
        for good, quantity in bill.items():
            ordered = sum(o['remaining'] for o in self.orders
                          if o['participant'] == account_id and o['side'] == 'buy'
                          and o['purpose'] == 'Строительные материалы' and o['good'] == good
                          and o['status'] not in ('cancelled', 'expired') and o['reserved'] > EPS)
            missing = max(0, quantity-account['stock'].get(good, 0)-ordered)
            needed += missing*self.quoted_cost(
                account_id, market_tile, account['state_id'],
                self.engine.market_province(account), good, missing)[1]
        return needed

    def cash_after_commitments(self, account_id, project_reserve):
        account = self.engine.accounts[account_id]
        hold = max(account.get('protected', 0), project_reserve)
        return max(0, account['balance']-account.get('reserved', 0)-hold)

    def free_cash(self, account_id):
        account = self.engine.accounts[account_id]
        return max(0.0, account['balance']-account.get('reserved', 0)
                   -account.get('protected', 0))

    def labor_available(self, state_id, province_id):
        return sum(self.engine.population(a)*self.labor_share
                   for a in self.engine.accounts.values()
                   if a['kind'] == 'household' and a['state_id'] == state_id
                   and a['province_id'] == province_id)

    def suppliers(self, good):
        """Базовые поставщики investment_demand по товару (порядок V3)."""
        result = []
        for firm in self.engine.enterprises.values():
            if firm['good'] != good or firm['status'] == 'closed' or firm.get('suspended'):
                continue
            cost = self.unit_cost(firm['id'], firm['tile_id'], firm['state_id'],
                                  firm['province_id'], firm['good'])
            price = (firm['price'] if self.net_price(firm['id'], firm['tile_id'],
                                                     firm['good'], firm['price'])+EPS >= cost
                     else firm.get('production_price', firm['price']))
            if self.net_price(firm['id'], firm['tile_id'], firm['good'], price)+EPS < cost:
                continue
            owner_id = firm['owner_id']
            funds = (self.free_cash(firm['id'])
                     + self.cash_after_commitments(owner_id,
                                                   self.project_cash_reserve(owner_id)))
            if funds <= EPS and not getattr(self, "include_unfunded", False):
                continue
            recipe = self.engine.goods[firm['good']]
            labor = self.labor_available(firm['state_id'], firm['province_id'])
            capacity = min(firm['capacity'], labor/recipe['labor'])
            if recipe['resource']:
                limit = self.engine.resource_limits.get(
                    (firm['province_id'], recipe['resource']), 0)
                capacity = min(capacity, limit)
            result.append((firm, price, capacity))
        return result


def _equipment_wear(engine, recipe):
    return {good: ratio*engine.goods[good].get('durable', {}).get('wear_rate', 0)
            for good, ratio in recipe.get('equipment', {}).items()}


def build_candidate_support(engine, sells, inv_data, inv_meta, extra_pairs=(), all_goods=False):
    """Построить статические flat-таблицы для native project_candidate.

    Read-only: использует _CandidateContext (реплики хелперов) и сырые
    данные engine. Индексы счетов/товаров/государств/провинций берутся из
    inv-pack (pack_investment_state); tile-пространство расширяется
    ресурсными и провинциальными плитками, недостающие строки/столбцы
    distances достраиваются через engine.path.

    extra_pairs: дополнительные (province_id, good) пары для derived-рынков
    (например, синтетический derived_demand): покрываются их локации.
    """
    ctx = _CandidateContext(engine, sells)
    ctx.include_unfunded = all_goods
    account_index = inv_meta['account_index']
    good_index = inv_meta['good_index']
    state_index = inv_meta['state_index']
    province_index = inv_meta['province_index']
    tile_index = dict(inv_meta['tile_index'])
    resource_index = inv_meta['resource_index']
    goods = list(engine.goods)
    accounts = list(engine.accounts.values())

    # --- расширение tile-пространства ---
    index = getattr(engine, '_investment_index', None)
    extra_tiles = set()
    if index is not None:
        for tiles in index['resource_tiles'].values():
            extra_tiles.update(t['id'] for t in tiles)
    for province_id in province_index:
        if province_id is not None:
            extra_tiles.add(engine.location(province_id)['tile_id'])
    extra_tiles -= set(tile_index)
    extra_sorted = sorted(extra_tiles)
    for tile_id in extra_sorted:
        tile_index[tile_id] = len(tile_index)
    all_tiles = sorted(tile_index, key=tile_index.get)
    n_tiles = len(all_tiles)
    old_n = inv_data['distances'].shape[0]
    if n_tiles > old_n:
        distances = np.full((n_tiles, n_tiles), np.nan, dtype=np.float64)
        distances[:old_n, :old_n] = inv_data['distances']
        for tile_id in extra_sorted:
            i = tile_index[tile_id]
            for j, other in enumerate(all_tiles):
                if i == j:
                    distances[i, j] = 0.0
                    continue
                if j < old_n and i < old_n:
                    continue
                # path() не побитово симметричен: каждое направление считается
                # своим вызовом, как это делает V3
                d, _ = engine.path(tile_id, other)
                if d is not None:
                    distances[i, j] = d
                d, _ = engine.path(other, tile_id)
                if d is not None:
                    distances[j, i] = d
    else:
        distances = inv_data['distances']
    tile_ids_by_index = {i: t for t, i in tile_index.items()}

    # --- market signal по всем ключам истории ---
    sig_keys = sorted({(r['state_id'], r['province_id'], r['good'])
                       for m in engine.meta.get('market_history', []) for r in m['markets']})
    sig = [ctx.signal(s, p, g) for s, p, g in sig_keys]

    # --- локации провинций (индекс = province_index) ---
    loc_tile = [0]*len(province_index)
    loc_state = [0]*len(province_index)
    for province_id, province_idx in province_index.items():
        if province_id is None:
            loc_tile[province_idx] = -1
            loc_state[province_idx] = -1
            continue
        loc = engine.location(province_id)
        loc_tile[province_idx] = tile_index[loc['tile_id']]
        loc_state[province_idx] = state_index[loc['state_id']]

    # --- resource tiles: (state, province, resource) -> tile idx по строковому ID ---
    rt_keys, rt_flat, rt_offset = [], [], [0]
    if index is not None:
        for key in sorted(index['resource_tiles']):
            state_id, province_id, resource = key
            rt_keys.append((state_index[state_id], province_index[province_id],
                            resource_index[resource]))
            rt_flat.extend(sorted((tile_index[t['id']] for t in index['resource_tiles'][key]),
                                  key=lambda i: tile_ids_by_index[i]))
            rt_offset.append(len(rt_flat))

    # --- какие (tile, good) нужны ---
    investor_indices = [i for i, a in enumerate(accounts)
                        if a['kind'] in ('private', 'merchant', 'treasury', 'municipal')
                        or (a['kind'] == 'enterprise'
                            and engine.enterprises[a['id']]['status'] != 'closed'
                            and engine.enterprises[a['id']]['available_from'] <= engine.meta['month'])]
    book_keys, ref_keys, cq_keys, dq_keys, ld_keys = set(), set(), set(), set(), set()
    construction_goods = list(engine.cfg['construction'])
    market_goods = set()
    for i in investor_indices:
        account = accounts[i]
        market_tile = account.get('cell_id') or account['tile_id']
        state_id = account['state_id']
        province_id = engine.market_province(account)
        markets = index['markets'].get(state_id, ()) if index is not None else ()
        for pid, good in markets:
            market_goods.add(good)
            loc = engine.location(pid)
            recipe = engine.goods[good]
            tiles = ([loc['tile_id']] if not recipe['resource']
                     else [t['id'] for t in index['resource_tiles'].get(
                         (state_id, pid, recipe['resource']), [])])
            for tile_id in tiles:
                cq_keys.add((tile_id, good))
                dq_keys.add((tile_id, good))
                ld_keys.add((tile_id, good))
                ref_keys.add((state_id, pid, tile_id, good, account['id']))
                for input_good in recipe['inputs']:
                    ref_keys.add((state_id, pid, tile_id, input_good, account['id']))
                for equip_good in recipe.get('equipment', {}):
                    ref_keys.add((state_id, pid, tile_id, equip_good, account['id']))
        for good in construction_goods:
            book_keys.add((market_tile, good))
            ref_keys.add((state_id, province_id, market_tile, good, account['id']))
        for good in list(market_goods):
            for equip_good in engine.goods[good].get('equipment', {}):
                book_keys.add((market_tile, equip_good))
                ref_keys.add((state_id, province_id, market_tile, equip_good, account['id']))
    # derived-рынки (extra_pairs): покрыть локации, которых нет в markets
    covered_pairs = set()
    if index is not None:
        for pairs in index['markets'].values():
            covered_pairs.update(pairs)
    for province_id, good in extra_pairs:
        if (province_id, good) in covered_pairs:
            continue
        state_id = engine.owners.get(province_id)
        recipe = engine.goods[good]
        market_goods.add(good)
        if recipe['resource'] and index is not None:
            tiles = [t['id'] for t in index['resource_tiles'].get(
                (state_id, province_id, recipe['resource']), [])]
        else:
            tiles = [engine.location(province_id)['tile_id']]
        for tile_id in tiles:
            cq_keys.add((tile_id, good))
            dq_keys.add((tile_id, good))
            ld_keys.add((tile_id, good))
            for account in accounts:
                ref_keys.add((state_id, province_id, tile_id, good, account['id']))
                for input_good in recipe['inputs']:
                    ref_keys.add((state_id, province_id, tile_id, input_good,
                                  account['id']))
                for equip_good in recipe.get('equipment', {}):
                    ref_keys.add((state_id, province_id, tile_id, equip_good,
                                  account['id']))
    covered_pairs.update(extra_pairs)
    if all_goods:
        market_goods.update(goods)
    # книги для offers-walk в investment_demand: все счета x кандидатные товары
    for account in accounts:
        market_tile = account.get('cell_id') or account['tile_id']
        for good in market_goods:
            book_keys.add((market_tile, good))
    # unit_cost фирм для suppliers
    for firm in engine.enterprises.values():
        recipe = engine.goods[firm['good']]
        for input_good in recipe['inputs']:
            ref_keys.add((firm['state_id'], firm['province_id'], firm['tile_id'],
                          input_good, firm['id']))
        for equip_good in recipe.get('equipment', {}):
            ref_keys.add((firm['state_id'], firm['province_id'], firm['tile_id'],
                          equip_good, firm['id']))
        cq_keys.add((firm['tile_id'], firm['good']))

    # --- expansion-кандидаты: локация фирмы может не входить в markets ---
    for account in accounts:
        firm = engine.enterprises.get(account['id'])
        if firm is None or firm['status'] == 'closed':
            continue
        tile_id = account.get('cell_id') or account['tile_id']
        state_id = account['state_id']
        province_id = engine.market_province(account)
        recipe = engine.goods[firm['good']]
        cq_keys.add((firm['tile_id'], firm['good']))
        dq_keys.add((firm['tile_id'], firm['good']))
        ld_keys.add((firm['tile_id'], firm['good']))
        ref_keys.add((state_id, province_id, tile_id, firm['good'], account['id']))
        for input_good in recipe['inputs']:
            ref_keys.add((state_id, province_id, tile_id, input_good, account['id']))
        for equip_good in recipe.get('equipment', {}):
            ref_keys.add((state_id, province_id, tile_id, equip_good, account['id']))
            book_keys.add((tile_id, equip_good))
    # книги offers-walk investment_demand: площадка покупателя x товары строк спроса
    dq_goods = {good for _, good in dq_keys}
    for account in accounts:
        market_tile = account.get('cell_id') or account['tile_id']
        for good in dq_goods:
            book_keys.add((market_tile, good))

    # --- flatten книг ---
    book_keys = sorted(book_keys, key=lambda k: (tile_index[k[0]], good_index[k[1]]))
    bk_offset = [0]
    bk_price, bk_order, bk_participant, bk_remaining, bk_active, bk_sellpos = [], [], [], [], [], []
    gs_offset = [0]
    gs_order, gs_remaining = [], []
    order_id_index = {o['id']: i for i, o in enumerate(engine.report['orders'])}
    good_sell_pos = {}
    for good in goods:
        good_start = len(gs_order)
        for offer in sells.get(good, []):
            good_sell_pos[offer['id']] = len(gs_order) - good_start
            gs_order.append(order_id_index.get(offer['id'], -1))
            gs_remaining.append(offer['remaining'])
        gs_offset.append(len(gs_order))
    for tile_id, good in book_keys:
        for price, sell in ctx.book(tile_id, good):
            bk_price.append(price)
            bk_order.append(order_id_index.get(sell['id'], -1))
            bk_participant.append(account_index.get(sell['participant'], -1))
            bk_remaining.append(sell['remaining'])
            bk_active.append(sell['status'] not in ('cancelled', 'expired'))
            bk_sellpos.append(good_sell_pos.get(sell['id'], -1))
        bk_offset.append(len(bk_price))

    # --- reference prices ---
    ref_keys = sorted(ref_keys, key=lambda k: (tile_index[k[2]], good_index[k[3]],
                                               account_index.get(k[4], -1)))
    rf_value = [ctx.ref_price(s, p, t, g, x) for s, p, t, g, x in ref_keys]

    # --- customer quotes / demand rows / leaders ---
    cq_keys = sorted(cq_keys, key=lambda k: (tile_index[k[0]], good_index[k[1]]))
    cq_offset = [0]
    cq_participant, cq_price, cq_limit, cq_remaining, cq_reserved, cq_final = [], [], [], [], [], []
    for tile_id, good in cq_keys:
        for order, limit in ctx.customer_quotes(tile_id, good, None):
            cq_participant.append(account_index.get(order['participant'], -1))
            cq_price.append(order['price'])
            cq_limit.append(limit)
            cq_remaining.append(order['remaining'])
            cq_reserved.append(order['reserved'])
            cq_final.append(bool(order.get('final_consumption')))
        cq_offset.append(len(cq_participant))

    dq_keys = sorted(dq_keys, key=lambda k: (tile_index[k[0]], good_index[k[1]]))
    dq_offset = [0]
    dq_participant, dq_order_tile, dq_order_price, dq_limit, dq_freight, dq_remaining, dq_reserved = \
        [], [], [], [], [], [], []
    for tile_id, good in dq_keys:
        for participant, order_tile, order_price, limit, freight, remaining, reserved \
                in ctx.demand_rows(tile_id, good):
            dq_participant.append(account_index.get(participant, -1))
            dq_order_tile.append(tile_index.get(order_tile, -1))
            dq_order_price.append(order_price)
            dq_limit.append(limit)
            dq_freight.append(freight)
            dq_remaining.append(remaining)
            dq_reserved.append(reserved)
        dq_offset.append(len(dq_participant))

    ld_keys = sorted(ld_keys, key=lambda k: (tile_index[k[0]], good_index[k[1]]))
    ld_limit0, ld_part0, ld_limit1, ld_part1 = [], [], [], []
    for tile_id, good in ld_keys:
        by_participant = {}
        for order, limit in ctx.customer_quotes(tile_id, good, None):
            by_participant[order['participant']] = max(
                limit, by_participant.get(order['participant'], -np.inf))
        leaders = sorted(((limit, participant) for participant, limit in by_participant.items()),
                         reverse=True)[:2]
        for slot in range(2):
            if slot < len(leaders):
                limit, participant = leaders[slot]
            else:
                limit, participant = -np.inf, None
            (ld_limit0 if slot == 0 else ld_limit1).append(limit)
            (ld_part0 if slot == 0 else ld_part1).append(
                account_index.get(participant, -1) if participant is not None else -1)

    # --- suppliers по товарам (для investment_demand) ---
    sup_offset = [0]
    sup_tile, sup_price, sup_capacity = [], [], []
    sup_account, sup_owner = [], []
    for good in goods:
        for firm, price, capacity in ctx.suppliers(good):
            sup_account.append(account_index[firm['id']])
            sup_owner.append(account_index[firm['owner_id']])
            sup_tile.append(tile_index[firm['tile_id']])
            sup_price.append(price)
            sup_capacity.append(capacity)
        sup_offset.append(len(sup_tile))

    support = dict(
        sig_state=_int32([state_index[s] for s, _, _ in sig_keys]),
        sig_province=_int32([province_index[p] for _, p, _ in sig_keys]),
        sig_good=_int32([good_index[g] for _, _, g in sig_keys]),
        sig_count=len(sig_keys),
        sig_volume=_float64([s['volume'] for s in sig]),
        sig_unmet=_float64([s['unmet'] for s in sig]),
        sig_price=_float64([np.nan if s['price'] is None else s['price'] for s in sig]),
        sig_landed=_float64([np.nan if s['landed_price'] is None else s['landed_price'] for s in sig]),
        sig_limit=_float64([np.nan if s['limit_price'] is None else s['limit_price'] for s in sig]),
        loc_tile=_int32(loc_tile),
        loc_state=_int32(loc_state),
        rt_state=_int32([k[0] for k in rt_keys]),
        rt_province=_int32([k[1] for k in rt_keys]),
        rt_resource=_int32([k[2] for k in rt_keys]),
        rt_count=len(rt_keys),
        rt_offset=_int32(rt_offset),
        rt_tile=_int32(rt_flat),
        bk_tile=_int32([tile_index[t] for t, _ in book_keys]),
        bk_good=_int32([good_index[g] for _, g in book_keys]),
        bk_count=len(book_keys),
        bk_offset=_int32(bk_offset),
        bk_price=_float64(bk_price),
        bk_order=_int32(bk_order),
        bk_participant=_int32(bk_participant),
        bk_remaining=_float64(bk_remaining),
        bk_active=_uint8(bk_active),
        bk_sellpos=_int32(bk_sellpos),
        gs_offset=_int32(gs_offset),
        gs_order=_int32(gs_order),
        gs_remaining=_float64(gs_remaining),
        rf_tile=_int32([tile_index[k[2]] for k in ref_keys]),
        rf_good=_int32([good_index[k[3]] for k in ref_keys]),
        rf_exclude=_int32([account_index.get(k[4], -1) for k in ref_keys]),
        rf_count=len(ref_keys),
        rf_value=_float64(rf_value),
        cq_tile=_int32([tile_index[t] for t, _ in cq_keys]),
        cq_good=_int32([good_index[g] for _, g in cq_keys]),
        cq_count=len(cq_keys),
        cq_offset=_int32(cq_offset),
        cq_participant=_int32(cq_participant),
        cq_price=_float64(cq_price),
        cq_limit=_float64(cq_limit),
        cq_remaining=_float64(cq_remaining),
        cq_reserved=_float64(cq_reserved),
        cq_final=_uint8(cq_final),
        dq_tile=_int32([tile_index[t] for t, _ in dq_keys]),
        dq_good=_int32([good_index[g] for _, g in dq_keys]),
        dq_count=len(dq_keys),
        dq_offset=_int32(dq_offset),
        dq_participant=_int32(dq_participant),
        dq_order_tile=_int32(dq_order_tile),
        dq_order_price=_float64(dq_order_price),
        dq_limit=_float64(dq_limit),
        dq_freight=_float64(dq_freight),
        dq_remaining=_float64(dq_remaining),
        dq_reserved=_float64(dq_reserved),
        ld_tile=_int32([tile_index[t] for t, _ in ld_keys]),
        ld_good=_int32([good_index[g] for _, g in ld_keys]),
        ld_count=len(ld_keys),
        ld_limit0=_float64(ld_limit0),
        ld_part0=_int32(ld_part0),
        ld_limit1=_float64(ld_limit1),
        ld_part1=_int32(ld_part1),
        sup_account=_int32(sup_account),
        sup_owner=_int32(sup_owner),
        sup_offset=_int32(sup_offset),
        sup_tile=_int32(sup_tile),
        sup_price=_float64(sup_price),
        sup_capacity=_float64(sup_capacity),
        good_wear_rate=_float64([engine.goods[g].get('durable', {}).get('wear_rate', 0)
                                 for g in goods]),
        distances=distances,
    )
    support_meta = dict(ctx=ctx, account_index=account_index, good_index=good_index,
                        state_index=state_index, province_index=province_index,
                        tile_index=tile_index, resource_index=resource_index,
                        investor_indices=investor_indices, month=engine.meta['month'],
                        covered_pairs=covered_pairs)
    return support, support_meta


def build_candidate_dynamic(engine, support_meta, investor_account_index, allowed_goods=None):
    """Динамический срез состояния для одного инвестора (read-only).

    allowed_goods: None (полный обзор) или множество товаров (targeted).
    """
    accounts = list(engine.accounts.values())
    account = accounts[investor_account_index]
    state_index = support_meta['state_index']
    province_index = support_meta['province_index']
    good_index = support_meta['good_index']
    resource_index = support_meta['resource_index']
    tile_index = support_meta['tile_index']
    index = getattr(engine, '_investment_index', None)
    state = getattr(engine, '_investment_state', None)

    markets = set(index['markets'].get(account['state_id'], ())) if index is not None else set()
    markets = sorted(markets)

    expansion = engine.enterprises.get(account['id'])
    month = engine.meta['month']

    lr_keys = sorted(state['labor_reserved']) if state is not None else []
    rr_keys = sorted(state['resource_reserved']) if state is not None else []
    la_keys = sorted(index['labor']) if index is not None else []
    ru_keys = sorted(index['resource_used']) if index is not None else []

    # проекты из живого индекса, сгруппированные по товарам
    n_goods = len(good_index)
    pj_by_good = [[] for _ in range(n_goods)]
    if index is not None:
        for good, entries in index['projects'].items():
            for location, price, capacity in entries:
                pj_by_good[good_index[good]].append(
                    (tile_index[location['tile_id']], price, capacity))
    pj_offset = [0]
    pj_tile, pj_price, pj_capacity = [], [], []
    for entries in pj_by_good:
        for tile, price, capacity in entries:
            pj_tile.append(tile)
            pj_price.append(price)
            pj_capacity.append(capacity)
        pj_offset.append(len(pj_tile))

    dynamic = dict(
        investor_account=int(investor_account_index),
        mk_province=_int32([province_index[pid] for pid, _ in markets]),
        mk_good=_int32([good_index[good] for _, good in markets]),
        mk_count=len(markets),
        al_mask=_uint8([allowed_goods is None or good in allowed_goods
                        for good in good_index]),
        al_present=allowed_goods is not None,
        lr_state=_int32([state_index[s] for s, _ in lr_keys]),
        lr_province=_int32([province_index[p] for _, p in lr_keys]),
        lr_value=_float64([state['labor_reserved'][k] for k in lr_keys]) if state is not None else _float64([]),
        lr_count=len(lr_keys),
        rr_province=_int32([province_index[p] for p, _ in rr_keys]),
        rr_resource=_int32([resource_index[r] for _, r in rr_keys]),
        rr_value=_float64([state['resource_reserved'][k] for k in rr_keys]) if state is not None else _float64([]),
        rr_count=len(rr_keys),
        la_state=_int32([state_index[s] for s, _ in la_keys]),
        la_province=_int32([province_index[p] for _, p in la_keys]),
        la_value=_float64([index['labor'][k] for k in la_keys]) if index is not None else _float64([]),
        la_count=len(la_keys),
        ru_province=_int32([province_index[p] for p, _ in ru_keys]),
        ru_resource=_int32([resource_index[r] for _, r in ru_keys]),
        ru_value=_float64([index['resource_used'][k] for k in ru_keys]) if index is not None else _float64([]),
        ru_count=len(ru_keys),
        pj_offset=_int32(pj_offset),
        pj_tile=_int32(pj_tile),
        pj_price=_float64(pj_price),
        pj_capacity=_float64(pj_capacity),
        exp_present=expansion is not None,
        exp_good=good_index[expansion['good']] if expansion is not None else -1,
        exp_province=province_index[expansion['province_id']] if expansion is not None else -1,
        exp_state=state_index[expansion['state_id']] if expansion is not None else -1,
        exp_tile=tile_index[expansion['tile_id']] if expansion is not None else -1,
        exp_price=float(expansion['price']) if expansion is not None else np.nan,
        exp_city_present=bool(expansion.get('city_id')) if expansion is not None else False,
        month=int(month),
    )
    return dynamic
