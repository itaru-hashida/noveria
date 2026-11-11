"""Неизменяемый снимок рынка: одна область shared_memory, только массивы.

Раскладка:

    agent_*    (n_agents,)          личное состояние агента
    good_*     (n_goods, ...)       рецепты и константы контента
    offer_*    (n_offers,)          публичное предложение, сгруппировано по товару
    ask_*      (n_asks,)            публичный спрос, сгруппировано по товару
    distance   (n_tiles, n_tiles)   дистанции между торговыми площадками

Площадка — `cell_id or tile_id`, как `Economy.market_tile`. Матрица считается
по площадкам, а не по плиткам: 78 площадок против 1200 плиток сейчас и
~41 000 плиток на целевом мире, где матрица по плиткам не влезает в память.
"""
import numpy as np
from collections import defaultdict
from dataclasses import dataclass

from modules.economy.support import EPS

LAYOUT_ALIGN = 8


@dataclass(frozen=True, slots=True)
class Layout:
    """Описание снимка: имена, типы, формы, смещения. Единственное, что
    передаётся в worker вместе с именем области."""
    entries: tuple

    def shape_of(self, name):
        for item_name, _dtype, shape, _offset in self.entries:
            if item_name == name:
                return shape
        raise KeyError(name)


def market_tile(account):
    return account.get('cell_id') or account['tile_id']


def _group_index(values, n_groups):
    """CSR-указатели: границы каждой группы в отсортированном по группе массиве."""
    counts = np.zeros(n_groups+1, dtype=np.int64)
    for value in values:
        counts[value+1] += 1
    return np.cumsum(counts).astype(np.int32)


def _distance_matrix(engine, tiles):
    """Матрица расстояний и достижимости между торговыми площадками.

    Нужны только расстояние и факт достижимости, а не денежный фрахт. Поэтому
    берётся `path`: `landed` для той же пары умножал расстояние на вес товара и
    тариф и собирал кортеж из четырёх полей, а здесь на 78 площадках 6006 пар.
    `path` отдаёт те же `distance`/`None` из своего кэша, поэтому матрица
    остаётся бит-в-бит прежней.
    """
    n_tiles = len(tiles)
    distance = np.zeros((n_tiles, n_tiles), dtype=np.float64)
    reachable = np.ones((n_tiles, n_tiles), dtype=np.uint8)
    for seller_row, seller_tile in enumerate(tiles):
        for buyer_row, buyer_tile in enumerate(tiles):
            if seller_row == buyer_row:
                continue
            route, _ = engine.path(seller_tile, buyer_tile)
            if route is None:
                reachable[seller_row, buyer_row] = 0
                distance[seller_row, buyer_row] = 0.0
            else:
                distance[seller_row, buyer_row] = route
    return distance, reachable


def build(engine, budget, repeat=1, sells=None):
    """Собрать массивы. `repeat` — синтетическое размножение агентов для замера роста.

    Размножение нужно только для масштабного теста: реальных миров на 5000 фирм
    пока нет, а стоимость на агента и намерение надо измерить до того, как
    строить генератор. Каждая копия — отдельное предприятие со своим складом и
    счётом; публичный рынок при этом не размножается, поэтому это замер
    стоимости расчёта, а не экономики.
    """
    goods = list(engine.goods)
    good_index = {name: index for index, name in enumerate(goods)}
    n_goods = len(goods)

    firms = sorted((e for e in engine.enterprises.values() if e['status'] != 'closed'),
                   key=lambda e: e['id'])
    if repeat > 1:
        base_firms = firms
        firms = []
        for copy in range(repeat):
            for firm in base_firms:
                clone = dict(firm)
                clone['id'] = '%s~%d' % (firm['id'], copy) if copy else firm['id']
                firms.append(clone)
    accounts = [engine.accounts[firm['id'].split('~')[0]] for firm in firms]
    # Площадка и провинция счёта не меняются внутри сборки, поэтому считаются
    # по одному разу на счёт и переиспользуются ниже. `market_province` сам
    # вызывает `market_tile`, так что прежний код делал это дважды на агента.
    account_tile = {account_id: market_tile(account)
                    for account_id, account in engine.accounts.items()}
    account_province = {account_id: engine.market_province(account)
                        for account_id, account in engine.accounts.items()}
    tiles = sorted(set(account_tile.values()))
    tile_index = {tile: index for index, tile in enumerate(tiles)}
    provinces = sorted({account_province[account['id']] for account in accounts})
    province_index = {province: index for index, province in enumerate(provinces)}
    n_agents, n_tiles, n_provinces = len(firms), len(tiles), len(provinces)

    agent_good = np.array([good_index[f['good']] for f in firms], dtype=np.int32)
    agent_tile = np.array([tile_index[account_tile[a['id']]] for a in accounts], dtype=np.int32)
    agent_province = np.array([province_index[account_province[a['id']]] for a in accounts],
                              dtype=np.int32)
    agent_capacity = np.array([f['capacity'] for f in firms], dtype=np.float64)
    agent_price = np.array([f['price'] for f in firms], dtype=np.float64)
    agent_balance = np.array([a['balance'] for a in accounts], dtype=np.float64)
    agent_budget = np.array([budget.get(f['id'], 0.0) for f in firms], dtype=np.float64)
    agent_stock = np.zeros((n_agents, n_goods), dtype=np.float64)
    agent_available_stock = np.zeros_like(agent_stock)
    agent_history = np.zeros((n_agents, 3, 3), dtype=np.float64)
    histories = [[h for h in f.get('sales_history', [])[-3:]
                  if h.get('offered', 0) > EPS or h.get('sold', 0) > EPS
                  or h.get('output', 0) > EPS or h.get('input_limited')] for f in firms]
    for row, (firm, account) in enumerate(zip(firms, accounts)):
        for name, quantity in account['stock'].items():
            agent_stock[row, good_index[name]] = quantity
            agent_available_stock[row, good_index[name]] = engine.free_stock(account['id'], name)
        for offset, item in enumerate(histories[row]):
            agent_history[row, offset] = (item.get('sold', 0.0), item.get('output', 0.0),
                                          float(bool(item.get('input_limited', False))))

    agent_forecast = np.array([f.get('agent_sales_forecast', f.get('sales_forecast', 0.0))
                               for f in firms], dtype=np.float64)
    agent_history_count = np.array([len(h) for h in histories], dtype=np.int32)
    agent_initialized = np.array(['agent_sales_forecast' in f for f in firms], dtype=np.uint8)
    # Сигнал — среднее за месяцы, а не сумма переносимых остатков заявок.
    history = engine.meta.get('market_history', [])[-3:]
    public_demand = np.zeros(n_goods, dtype=np.float64)
    public_value = np.zeros(n_goods, dtype=np.float64)
    public_volume = np.zeros(n_goods, dtype=np.float64)
    for month in history:
        for item in month['markets']:
            g = good_index[item['good']]
            public_demand[g] += item['volume']+item['unmet_solvent']
            public_volume[g] += item['volume']
            public_value[g] += item['trade_gold']+item.get('freight', 0.0)
    public_demand /= max(1, len(history))
    counts = np.bincount(agent_good, minlength=n_goods)
    public_share = public_demand/np.maximum(1, counts)
    # Число продавцов, которые реально конкурируют за спрос: выставили оффер
    # в этом месяце. Делить на всех действующих фирм нельзя — фирма без
    # товара на складе спрос не отнимает, и доля выходит заниженной
    # (проверено: спрос падал до 0,04 от реального).
    offering = np.bincount(agent_good[agent_available_stock[:, :].sum(axis=1) > EPS],
                           minlength=n_goods)
    good_sellers = np.maximum(1, np.maximum(offering, 1)).astype(np.int32)
    # Сколько фирм закупают каждый товар как вход: на столько делится уровень
    # книги при расчёте предела заявки. Без этого агент считает, что весь
    # дешёвый уровень достанется ему одному, ставит заниженный предел и
    # получает отказ по цене доставки.
    input_buyers = np.ones(n_goods, dtype=np.int32)
    public_price = np.divide(public_value, public_volume,
                             out=np.zeros(n_goods), where=public_volume > EPS)

    good_inputs = np.zeros((n_goods, n_goods), dtype=np.float64)
    good_equip = np.zeros((n_goods, n_goods), dtype=np.float64)
    good_labor = np.zeros(n_goods, dtype=np.float64)
    good_price = np.zeros(n_goods, dtype=np.float64)
    good_weight = np.zeros(n_goods, dtype=np.float64)
    good_taxed = np.zeros(n_goods, dtype=np.uint8)
    consumer = engine.cfg['basic_needs'] | engine.cfg['extra_needs']
    buyer_counts = np.zeros(n_goods, dtype=np.int32)
    for name, recipe in engine.goods.items():
        column = good_index[name]
        good_labor[column] = recipe['labor']
        good_price[column] = recipe['price']
        good_weight[column] = recipe.get('weight', 0.0)
        good_taxed[column] = int(name in consumer)
        for input_name, ratio in recipe.get('inputs', {}).items():
            good_inputs[column, good_index[input_name]] = ratio
            buyer_counts[good_index[input_name]] += 1
        for input_name, ratio in recipe.get('equipment', {}).items():
            good_equip[column, good_index[input_name]] = ratio
            buyer_counts[good_index[input_name]] += 1
    input_buyers = np.maximum(1, buyer_counts).astype(np.int32)

    # Публичное предложение — ВСЁ, что видно в книге, включая офферы фирм.
    # Раньше офферы фирм исключались как «дубль своих намерений», и снимок
    # оставался пустым: все 19 офферов зерна принадлежали фирмам. Планировщик
    # брал котировку из истории рынка, а исполнитель — из живой книги, из-за
    # чего предел цены расходился с реальной доставленной ценой и заявка
    # отклонялась как landed_price_above_limit. Продавцы известны и нужны:
    # это публичная книга месяца, а не чужие внутренние планы.
    offers = []
    for order in engine.report['orders']:
        if order['side'] != 'sell' or order['remaining'] <= EPS:
            continue
        if order['status'] in ('cancelled', 'expired'):
            continue
        if order['tile_id'] not in tile_index:
            continue
        offers.append((good_index[order['good']], tile_index[order['tile_id']],
                       order['price'], order['remaining'], order['participant']))
    offers.sort(key=lambda row: row[0])
    offer_good = np.array([row[0] for row in offers], dtype=np.int32)
    offer_tile = np.array([row[1] for row in offers], dtype=np.int32)
    offer_price = np.array([row[2] for row in offers], dtype=np.float64)
    offer_remaining = np.array([row[3] for row in offers], dtype=np.float64)
    offer_seller = [row[4] for row in offers]

    asks = []
    for order in engine.report['orders']:
        if order['side'] != 'buy' or order['remaining'] <= EPS:
            continue
        if order['status'] in ('cancelled', 'expired'):
            continue
        if order['tile_id'] not in tile_index:
            continue
        asks.append((good_index[order['good']], tile_index[order['tile_id']],
                     order['price'], order['remaining'], order.get('reserved', 0.0),
                     order['participant'], bool(order.get('final_consumption'))))
    asks.sort(key=lambda row: row[0])
    ask_good = np.array([row[0] for row in asks], dtype=np.int32)
    ask_tile = np.array([row[1] for row in asks], dtype=np.int32)
    ask_price = np.array([row[2] for row in asks], dtype=np.float64)
    ask_remaining = np.array([row[3] for row in asks], dtype=np.float64)
    ask_reserved = np.array([row[4] for row in asks], dtype=np.float64)
    ask_buyer = [row[5] for row in asks]
    ask_final = [row[6] for row in asks]

    distance, reachable = _distance_matrix(engine, tiles)

    # Котировки включают опубликованные склады фирм. Исключение этих офферов
    # из книги исполнителя не должно заменять их цены ценами из контента.
    public_sells = sells
    if public_sells is None:
        public_sells = defaultdict(list)
        for order in engine.report['orders']:
            if order['side'] == 'sell':
                public_sells[order['good']].append(order)
    agent_input_price = np.zeros((n_agents, n_goods), dtype=np.float64)
    for row, (firm, account) in enumerate(zip(firms, accounts)):
        recipe = engine.goods[firm['good']]
        for g in recipe['inputs'].keys() | recipe.get('equipment', {}).keys():
            agent_input_price[row, good_index[g]] = engine.reference_price(account, g, public_sells)

    # Домохозяйства по провинциям одним проходом по счетам. Раньше обе
    # структуры собирались отдельными comprehension на каждую провинцию, то есть
    # все счета перебирались 2×N_provinces раз, и каждый `market_province`
    # заново звал `market_tile`. Теперь `market_province` считается один раз на
    # счёт, а `population` — один раз на домохозяйство.
    # Порядок домохозяйств внутри провинции — порядок `engine.accounts`,
    # сложение населения — `sum` по возрастающему списку людей, как и раньше:
    # оба совпадают с прежним результатом бит-в-бит.
    communities = {}
    for account_id, account in engine.accounts.items():
        if account['kind'] != 'household':
            continue
        province = engine.market_province(account)
        if province not in province_index:
            continue
        communities.setdefault(province, []).append(
            (account_id, engine.population(account)))
    province_population = {province: sum(people for _, people in rows)
                           for province, rows in communities.items()}
    households_by_province = dict(communities)

    arrays = {
        'agent_good': agent_good, 'agent_tile': agent_tile,
        'agent_province': agent_province, 'agent_capacity': agent_capacity,
        'agent_price': agent_price, 'agent_balance': agent_balance,
        'agent_budget': agent_budget, 'agent_stock': agent_stock,
        'agent_available_stock': agent_available_stock, 'agent_input_price': agent_input_price,
        'agent_history': agent_history, 'agent_history_count': agent_history_count,
        'agent_initialized': agent_initialized, 'agent_forecast': agent_forecast, 'public_share': public_share,
        'good_sellers': good_sellers, 'input_buyers': input_buyers,
        'public_price': public_price,
        'good_inputs': good_inputs, 'good_equip': good_equip, 'good_labor': good_labor,
        'good_wear': np.array([engine.goods[g].get('durable', {}).get('wear_rate', 0.0) for g in goods]),
        'good_price': good_price, 'good_weight': good_weight, 'good_taxed': good_taxed,
        'offer_good': offer_good, 'offer_tile': offer_tile, 'offer_price': offer_price,
        'offer_remaining': offer_remaining,
        'offer_ptr': _group_index(offer_good, n_goods),
        'ask_good': ask_good, 'ask_tile': ask_tile, 'ask_price': ask_price,
        'ask_remaining': ask_remaining, 'ask_reserved': ask_reserved,
        'ask_ptr': _group_index(ask_good, n_goods),
        'distance': distance, 'reachable': reachable,
    }
    meta = dict(month=engine.meta['month'], goods=goods, good_index=good_index,
                tiles=tiles, tile_index=tile_index, provinces=provinces,
                province_index=province_index, n_agents=n_agents, n_goods=n_goods,
                n_tiles=n_tiles, n_provinces=n_provinces,
                firms=[f['id'] for f in firms],
                firm_good=[f['good'] for f in firms],
                firm_tile=[tiles[agent_tile[row]] for row in range(n_agents)],
                firm_province=[provinces[agent_province[row]] for row in range(n_agents)],
                firm_owner=[f['owner_id'] for f in firms],
                # Числовые индексы площадок агентов: исполнителю нужны они, а не
                # строковые id тайлов, чтобы доставка считалась по матрице.
                agent_tile_index=[int(agent_tile[row]) for row in range(n_agents)],
                agent_good_index=[int(agent_good[row]) for row in range(n_agents)],
                # Публичные книги: продавцы и покупатели по каждой записи.
                offer_rows=[dict(good=goods[row[0]], good_index=row[0], tile=tiles[row[1]],
                                 tile_index=row[1], price=row[2],
                                 remaining=row[3], seller=row[4]) for row in offers],
                ask_rows=[dict(good=goods[row[0]], good_index=row[0], tile=tiles[row[1]],
                               tile_index=row[1], price=row[2],
                               remaining=row[3], reserved=row[4], buyer=row[5],
                               final=row[6]) for row in asks],
                # Склады всех счетов: исполнитель обязан списывать товар у того,
                # у кого он куплен, иначе покупка у казны создаёт товар из воздуха.
                all_stock={account_id: dict(account['stock'])
                           for account_id, account in engine.accounts.items()},
                # Труд и ресурсы: физические ограничения выпуска.
                province_population=province_population,
                households_by_province=households_by_province,
                resource_limits=[(province, resource, quantity)
                                 for (province, resource), quantity
                                 in engine.resource_limits.items()
                                 if province in province_index],
                good_resource=[engine.goods[name].get('resource') or ''
                               for name in goods],
                agent_province_name=[provinces[agent_province[row]]
                                     for row in range(n_agents)],
                # обязан сходиться, иначе деньги теряются.
                balances={account_id: account['balance']
                          for account_id, account in engine.accounts.items()},
                tile_of_account=account_tile)
    return arrays, meta


def describe(arrays):
    """Layout: плоский список (имя, dtype, shape, offset) с выравниванием."""
    entries, offsets = [], {}
    cursor = 0
    for name, array in arrays.items():
        cursor = (cursor+LAYOUT_ALIGN-1)//LAYOUT_ALIGN*LAYOUT_ALIGN
        offsets[name] = cursor
        entries.append((name, array.dtype.str, tuple(array.shape), cursor))
        cursor += array.nbytes
    return Layout(tuple(entries)), cursor
