"""Граница спроса и прогноза продаж: эталон, fallback и один native call site.

Перенесены расчётные срезы ``V3``:

  * ``_customer_orders`` — индекс покупательских заявок по товару;
  * ``_orders_in_route_component`` — группировка заявок по компоненте маршрутов;
  * ``_customer_quote_index`` — пары ``(заявка, доставка)`` для площадки;
  * ``customer_quotes`` — предел платёжеспособности каждой заявки;
  * ``_customer_demand_rows`` — плоские строки агрегата спроса;
  * ``customer_demand_groups`` — агрегат одинакового спроса;
  * ``reachable_demand`` — покрытие групп живой книгой;
  * ``expected_stock_sales`` — продажа старого склада.

Кэши остаются кэшами движка: имена, ключи и hit/miss-семантика прежние.

  * ``_customer_order_index`` — индекс покупательских заявок по товару;
  * ``_route_order_components`` — группы заявок по компоненте маршрутов;
  * ``_customer_quotes_cache`` — индекс пар ``(заявка, доставка)``;
  * ``_customer_demand_rows_cache`` — плоские строки спроса; ключ включает
    ``_demand_revision``, поэтому отмена покупательской заявки их убивает;
  * ``_stock_sales_cache`` — продажа старого склада по предприятию.

Python-хуками остаются ``landed`` (маршрут, тариф и ``_landed_cache``),
``offers`` (книга рынка), ``accounts`` (чтение счёта), ``route_component``
(компонента маршрута) и живой ``report['orders']``. RNG, порядок заявок и
мутации не переносятся: ядро ничего не меняет, кроме кэшей границы.

Эталонные ``*_python`` функции повторяют прежние тела ``V3`` буквально и
служат образцом для focused-тестов на побитовое совпадение, а также fallback,
если расширение не собрано. Имена привязываются один раз при импорте.
"""

import importlib

from ..support import EPS


class DemandHooks(object):
    """Живой доступ ядра к движку: только чтение плюс готовые вызовы.

    Хуки намеренно узкие: всё, что говорит с книгой офферов, маршрутами,
    складом и счетами, остаётся в Python. Ядро получает уже посчитанные числа
    и само не читает ни ``landed``-кэш, ни ``_demand_revision``.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    def landed(self, seller_tile, buyer_tile, good, price):
        return self.engine.landed(seller_tile, buyer_tile, good, price)

    def offers(self, buyer, good, sells):
        return self.engine.offers(buyer, good, sells)

    def account(self, participant):
        return self.engine.accounts[participant]

    def component(self, tile_id):
        return self.engine.route_component(tile_id)


# ---------------------------------------------------------------------------
# Индексы книги заявок
# ---------------------------------------------------------------------------

def _customer_orders(engine, orders, good):
    """Индекс покупательских заявок по товару; кэш движка прежний."""
    cache = getattr(engine, '_customer_order_index', {})
    key = (id(orders), len(orders), good)
    entry = cache.get(key)
    result = entry[1] if entry and entry[0] is orders else None
    if result is None:
        result = run_orders(orders, good)
        # Держим сам список: Python не сможет выдать его id новому
        # временному списку до конца отчётного месяца.
        cache[key] = (orders, result)
        engine._customer_order_index = cache
    return result


def _customer_orders_python(engine, orders, good):
    """Эталон ``_customer_orders``: буквальный прежний код с кэшем."""
    cache = getattr(engine, '_customer_order_index', {})
    key = (id(orders), len(orders), good)
    entry = cache.get(key)
    result = entry[1] if entry and entry[0] is orders else None
    if result is None:
        result = [order for order in orders if order['side'] == 'buy'
                  and order['good'] == good]
        cache[key] = (orders, result)
        engine._customer_order_index = cache
    return result


def _orders_in_route_component(engine, customer_orders, destination_tile):
    """Заявки физически достижимой части карты; кэш движка прежний."""
    component_count = engine._route_component_count
    destination_component = engine.route_component(destination_tile)
    if component_count == 1:
        return customer_orders
    cache = getattr(engine, '_route_order_components', {})
    key = (id(customer_orders), len(customer_orders))
    entry = cache.get(key)
    groups = entry[1] if entry is not None and entry[0] is customer_orders else None
    if groups is None:
        groups = run_route_groups(customer_orders, engine.route_component)
        cache[key] = (customer_orders, groups)
        engine._route_order_components = cache
    return groups.get(destination_component, ())


def _orders_in_route_component_python(engine, customer_orders, destination_tile):
    """Эталон группировки по компоненте: те же ключи и тот же порядок."""
    destination_component = engine.route_component(destination_tile)
    if engine._route_component_count == 1:
        return customer_orders
    cache = getattr(engine, '_route_order_components', {})
    key = (id(customer_orders), len(customer_orders))
    entry = cache.get(key)
    groups = entry[1] if entry is not None and entry[0] is customer_orders else None
    if groups is None:
        groups = {}
        for order in customer_orders:
            groups.setdefault(engine.route_component(order['tile_id']), []).append(order)
        cache[key] = (customer_orders, groups)
        engine._route_order_components = cache
    return groups.get(destination_component, ())


def _customer_quote_index(engine, tile_id, good, orders):
    """Доставка от площадки к покупателям; кэш движка прежний."""
    cache = getattr(engine, '_customer_quotes_cache', {})
    # Вес товара и тариф — константы контента (см. freight_factors) и в ключе
    # не нужны: их изменение означает перезагрузку движка, а не новый месяц.
    key = (id(orders), len(orders), tile_id, good)
    entry = cache.get(key)
    result = entry[1] if entry and entry[0] is orders else None
    if result is None:
        customer_orders = _customer_orders(engine, orders, good)
        in_component = _orders_in_route_component(engine, customer_orders, tile_id)
        result = run_quote_index(in_component, tile_id, good, engine.landed)
        cache[key] = (orders, result)
        engine._customer_quotes_cache = cache
    return result


def _customer_quote_index_python(engine, tile_id, good, orders):
    """Эталон ``_customer_quote_index``: буквальный прежний код с кэшем."""
    cache = getattr(engine, '_customer_quotes_cache', {})
    key = (id(orders), len(orders), tile_id, good)
    entry = cache.get(key)
    result = entry[1] if entry and entry[0] is orders else None
    if result is None:
        result = []
        customer_orders = _customer_orders_python(engine, orders, good)
        for order in _orders_in_route_component_python(engine, customer_orders, tile_id):
            quote = engine.landed(tile_id, order['tile_id'], good, 0)
            if quote:
                result.append((order, quote[1]))
        cache[key] = (orders, result)
        engine._customer_quotes_cache = cache
    return result


# ---------------------------------------------------------------------------
# Котировки покупателей
# ---------------------------------------------------------------------------

def _planning_orders(engine, orders):
    """Прежний выбор списка заявок: планировочный снимок либо живой отчёт."""
    if orders is not None:
        return orders
    return getattr(engine, '_planning_orders', engine.report.get('orders', []))


def customer_quotes(engine, e, orders=None):
    """Единственный вход из ``V3.customer_quotes``: индекс и ядро."""
    orders = _planning_orders(engine, orders)
    return run_quotes(_customer_quote_index(engine, e['tile_id'], e['good'], orders),
                      e['id'], EPS)


def customer_quotes_python(engine, e, orders=None):
    """Эталон ``V3.customer_quotes``: буквальный прежний код."""
    orders = _planning_orders(engine, orders)
    result = []
    for o, freight in _customer_quote_index_python(engine, e['tile_id'], e['good'], orders):
        if o['participant'] == e['id'] or o['remaining'] <= EPS \
                or o['status'] in ('cancelled', 'expired'):
            continue
        result.append((o, o['price']-freight))
    return result


# ---------------------------------------------------------------------------
# Плоские строки и группы спроса
# ---------------------------------------------------------------------------

def _customer_demand_rows(engine, tile_id, good, orders):
    """Плоские строки спроса на смену книги; ключ с ``_demand_revision``.

    Ревизия здесь именно ``_demand_revision``, а не версия книги: версию
    поднимают сделки и изменения предложения, а отмена покупательской заявки
    её не трогает — она меняет только ``reserved`` и ``status`` и растит
    ``_demand_revision``. На версии книги отменённая заявка оставалась в
    записи со старым резервом, и спрос считался по ней ещё раз.
    """
    key = (id(orders), len(orders), tile_id, good, engine._demand_revision[good])
    cache = getattr(engine, '_customer_demand_rows_cache', {})
    entry = cache.get(key)
    if entry is not None and entry[0] is orders:
        return entry[1]
    result = run_rows(_customer_quote_index(engine, tile_id, good, orders), EPS)
    cache[key] = (orders, result)
    engine._customer_demand_rows_cache = cache
    return result


def _customer_demand_rows_python(engine, tile_id, good, orders):
    """Эталон ``_customer_demand_rows``: буквальный прежний код с кэшем."""
    key = (id(orders), len(orders), tile_id, good, engine._demand_revision[good])
    cache = getattr(engine, '_customer_demand_rows_cache', {})
    entry = cache.get(key)
    if entry is not None and entry[0] is orders:
        return entry[1]
    rows = []
    for order, freight in _customer_quote_index_python(engine, tile_id, good, orders):
        if order['remaining'] <= EPS or order['status'] in ('cancelled', 'expired'):
            continue
        rows.append((order['participant'], order['tile_id'], order['price'],
                     order['price']-freight, freight, order['remaining'],
                     order['reserved']))
    result = tuple(rows)
    cache[key] = (orders, result)
    engine._customer_demand_rows_cache = cache
    return result


def customer_demand_groups(engine, e, orders=None):
    """Единственный вход из ``V3.customer_demand_groups``: строки и ядро."""
    orders = _planning_orders(engine, orders)
    return run_groups(_customer_demand_rows(engine, e['tile_id'], e['good'], orders),
                      e['price'], EPS)


def customer_demand_groups_python(engine, e, orders=None):
    """Эталон ``V3.customer_demand_groups``: буквальный прежний код."""
    orders = _planning_orders(engine, orders)
    rows = _customer_demand_rows_python(engine, e['tile_id'], e['good'], orders)
    price = e['price']
    grouped = {}
    for participant, tile_id, order_price, limit, freight, remaining, reserved in rows:
        if limit+EPS < price:
            continue
        group_key = (participant, tile_id, order_price)
        row = grouped.get(group_key)
        if row is None:
            row = grouped[group_key] = [participant, tile_id, order_price, limit,
                                        freight, 0.0]
        # Solvency belongs to an individual order.  Summing quantity and
        # money separately can let one order's cash fund another order's
        # unfunded quantity: min(sum(q), sum(cash) / price) !=
        # sum(min(q, cash / price)).
        row[5] += min(remaining, reserved/max(price+freight, EPS))
    return tuple(tuple(row) for row in grouped.values())


# ---------------------------------------------------------------------------
# Достижимый спрос и продажа старого склада
# ---------------------------------------------------------------------------

def reachable_demand(engine, e, sells):
    """Единственный вход из ``V3.reachable_demand``: остатки книги и ядро."""
    groups = customer_demand_groups(engine, e)
    remaining = {sell['id']: sell['remaining'] for sell in sells.get(e['good'], [])}
    return run_reachable(e['id'], e['price'], groups, remaining,
                         engine.accounts.__getitem__, engine.offers,
                         e['good'], sells, EPS)


def reachable_demand_python(engine, e, sells):
    """Эталон ``V3.reachable_demand``: буквальный прежний код."""
    demand = 0.0
    # Конкуренты важны лишь для товара кандидата: копия всех книг рынка
    # создавалась тысячи раз при планировании выпуска.
    remaining = {sell['id']: sell['remaining'] for sell in sells.get(e['good'], [])}
    for participant, tile_id, order_price, limit, freight, volume in \
            customer_demand_groups_python(engine, e):
        if participant == e['id']:
            continue
        if limit+EPS < e['price']:
            continue
        buyer = engine.accounts[participant]
        landed = e['price']+freight
        for price, sell, _ in engine.offers(buyer, e['good'], sells):
            if price > order_price+EPS or price >= landed-EPS:
                break
            if sell['participant'] == e['id']:
                continue
            covered = min(volume, remaining[sell['id']])
            volume -= covered
            remaining[sell['id']] -= covered
        demand += volume
    return demand


def expected_stock_sales(engine, e, sells, reachable=None):
    """Единственный вход из ``V3.expected_stock_sales``: кэш движка и ядро."""
    offered = run_offered(sells.get(e['good'], []), e['id'], EPS)
    if offered <= EPS:
        return 0.0
    cache = getattr(engine, '_stock_sales_cache', {})
    cached = cache.get(e['id'])
    if cached is not None:
        return cached
    if reachable is None:
        reachable = reachable_demand(engine, e, sells)
    history = e.get('sales_history', [])[-3:]
    stock = engine.accounts[e['id']]['stock'].get(e['good'], 0)
    result = run_stock_sales(stock, offered, reachable, history)
    cache[e['id']] = result
    engine._stock_sales_cache = cache
    return result


def expected_stock_sales_python(engine, e, sells, reachable=None):
    """Эталон ``V3.expected_stock_sales``: буквальный прежний код с кэшем."""
    offered = sum(o['remaining'] for o in sells.get(e['good'], [])
                  if o['participant'] == e['id']
                  and o['status'] not in ('cancelled', 'expired'))
    if offered <= EPS:
        return 0.0
    cache = getattr(engine, '_stock_sales_cache', {})
    cached = cache.get(e['id'])
    if cached is not None:
        return cached
    if reachable is None:
        reachable = reachable_demand_python(engine, e, sells)
    history = e.get('sales_history', [])[-3:]
    average_sold = (sum(h.get('sold', 0.0) for h in history) / len(history)
                    if history else 0.0)
    expected = max(reachable, average_sold)
    result = min(engine.accounts[e['id']]['stock'].get(e['good'], 0), offered, expected)
    cache[e['id']] = result
    engine._stock_sales_cache = cache
    return result


def load_demand_kernel():
    """Вернуть native-модуль demand_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.demand_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


# --- Python-эквиваленты ядра с теми же сигнатурами -------------------------

def _orders_python(orders, good):
    """Python-эквивалент ``customer_orders``."""
    return [order for order in orders
            if order['side'] == 'buy' and order['good'] == good]


def _quote_index_python(customer_orders, tile_id, good, landed_hook):
    """Python-эквивалент ``customer_quote_index``."""
    result = []
    for order in customer_orders:
        quote = landed_hook(tile_id, order['tile_id'], good, 0)
        if quote:
            result.append((order, quote[1]))
    return result


def _quotes_python(rows, owner_id, eps):
    """Python-эквивалент ``customer_quotes``: те же проверки и порядок."""
    result = []
    for order, freight in rows:
        if (order['participant'] == owner_id or order['remaining'] <= eps
                or order['status'] in ('cancelled', 'expired')):
            continue
        result.append((order, order['price']-freight))
    return result


def _rows_python(rows, eps):
    """Python-эквивалент ``demand_rows``."""
    result = []
    for order, freight in rows:
        if order['remaining'] <= eps or order['status'] in ('cancelled', 'expired'):
            continue
        result.append((order['participant'], order['tile_id'], order['price'],
                       order['price']-freight, freight, order['remaining'],
                       order['reserved']))
    return tuple(result)


def _groups_python(rows, price, eps):
    """Python-эквивалент ``demand_groups``."""
    grouped = {}
    for participant, tile_id, order_price, limit, freight, remaining, reserved in rows:
        if limit+eps < price:
            continue
        group_key = (participant, tile_id, order_price)
        row = grouped.get(group_key)
        if row is None:
            row = grouped[group_key] = [participant, tile_id, order_price, limit,
                                        freight, 0.0]
        row[5] += min(remaining, reserved/max(price+freight, eps))
    return tuple(tuple(row) for row in grouped.values())


def _reachable_python(owner_id, price, groups, remaining, account_hook,
                      offers_hook, good, sells, eps):
    """Python-эквивалент ``reachable_demand``."""
    demand = 0.0
    for participant, tile_id, order_price, limit, freight, volume in groups:
        if participant == owner_id:
            continue
        if limit+eps < price:
            continue
        buyer = account_hook(participant)
        landed = price+freight
        for sell_price, sell, _ in offers_hook(buyer, good, sells):
            if sell_price > order_price+eps or sell_price >= landed-eps:
                break
            if sell['participant'] == owner_id:
                continue
            covered = min(volume, remaining[sell['id']])
            volume -= covered
            remaining[sell['id']] -= covered
        demand += volume
    return demand


def _offered_python(offers, participant, eps):
    """Python-эквивалент ``stock_offers``: прежний встроенный ``sum``."""
    return sum(o['remaining'] for o in offers
               if o['participant'] == participant
               and o['status'] not in ('cancelled', 'expired'))


def _stock_sales_python(stock, offered, reachable, history):
    """Python-эквивалент ``expected_stock_sales``."""
    average_sold = (sum(h.get('sold', 0.0) for h in history)/len(history)
                    if history else 0.0)
    expected = max(reachable, average_sold)
    return min(stock, offered, expected)


def _route_groups_python(customer_orders, component_hook):
    """Python-эквивалент ``route_order_groups``."""
    groups = {}
    for order in customer_orders:
        groups.setdefault(component_hook(order['tile_id']), []).append(order)
    return groups


NATIVE_KERNEL = False

_kernel = load_demand_kernel()
if _kernel is not None:
    run_orders = _kernel.customer_orders
    run_route_groups = _kernel.route_order_groups
    run_quote_index = _kernel.customer_quote_index
    run_quotes = _kernel.customer_quotes
    run_rows = _kernel.demand_rows
    run_groups = _kernel.demand_groups
    run_reachable = _kernel.reachable_demand
    run_offered = _kernel.stock_offers
    run_stock_sales = _kernel.expected_stock_sales
    NATIVE_KERNEL = True
else:
    run_orders = _orders_python
    run_route_groups = _route_groups_python
    run_quote_index = _quote_index_python
    run_quotes = _quotes_python
    run_rows = _rows_python
    run_groups = _groups_python
    run_reachable = _reachable_python
    run_offered = _offered_python
    run_stock_sales = _stock_sales_python
