# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Числовое ядро спроса и прогноза продаж: индексы, группы и достижимый объём.

Перенесены расчётные срезы ``V3``:

  * ``_customer_orders`` — индекс покупательских заявок по товару;
  * ``_orders_in_route_component`` — группировка заявок по компоненте маршрутов;
  * ``_customer_quote_index`` — пары ``(заявка, доставка)`` для площадки;
  * ``customer_quotes`` — отсев собственных/мёртвых заявок и предел
    платёжеспособности ``цена-доставка``;
  * ``_customer_demand_rows`` — плоские строки спроса для группировки;
  * ``customer_demand_groups`` — агрегат по ``(participant, tile, order_price)``
    с платёжеспособностью каждой отдельной заявки;
  * ``reachable_demand`` — покрытие групп живой книгой без продавца-кандидата;
  * ``expected_stock_sales`` — продажа старого склада по уже обеспеченным
    заявкам.

Ядро не владеет состоянием: книга офферов, счета, склад, ``report['orders']``
и кэши (``_customer_quotes_cache``, ``_customer_orders_index``,
``_route_order_components``, ``_customer_demand_rows_cache``,
``_stock_sales_cache``) остаются за движком/границей. Сюда приходят готовые
списки, живые заявки, словарь остатков и хуки ``landed``/``offers``/``account``.

Арифметика намеренно объектная (``object``, а не ``double``): ``price``,
``remaining``, ``reserved`` и ``stock`` бывают ``int``, и приведение к
``double`` изменило бы тип результата. Суммы, которые прежний код считал
встроенным ``sum`` (остатки офферов, средние продажи, население), повторяют
компенсированное суммирование CPython 3.12 (Neumaier); накопления прежнего
кода через ``+=`` остаются наивными.
"""


cdef inline object _max0(object value):
    """``max(0, value)``: при равенстве и для отрицательных побеждает ``0``."""
    if value > 0:
        return value
    return 0


cdef inline object _max2(object first, object second):
    """``max(first, second)``: второй побеждает только при строгом «больше»."""
    if second > first:
        return second
    return first


cdef inline object _min2(object first, object second):
    """``min(first, second)``: второй побеждает только при строгом «меньше»."""
    if second < first:
        return second
    return first


cdef inline object _min3(object first, object second, object third):
    """``min(first, second, third)``: побеждает первый минимальный элемент."""
    return _min2(_min2(first, second), third)


cdef inline tuple _fold(object total, object comp, object value):
    """Шаг суммирования ровно как встроенный ``sum`` в CPython 3.12.

    ``sum`` складывает float компенсированно (Neumaier): к сумме добавляется
    поправка на погрешность каждого шага, а в конце — ``total+comp``. Наивное
    сложение даёт другой последний бит, а он меняет порядок сделок и RNG.
    Целые складываются обычным ``+``, чтобы тип результата остался ``int``.
    """
    cdef object new_total
    if type(total) is float and type(value) is float:
        new_total = total+value
        if abs(total) >= abs(value):
            comp = comp+((total-new_total)+value)
        else:
            comp = comp+((value-new_total)+total)
        return new_total, comp
    return total+value, comp


cdef inline object _finish(object total, object comp):
    """Итог компенсированной суммы: ``total+comp`` только при ненулевой поправке."""
    if comp != 0.0:
        return total+comp
    return total


# ---------------------------------------------------------------------------
# Индексы книги заявок
# ---------------------------------------------------------------------------

def customer_orders(object orders, object good):
    """Индекс покупательских заявок по товару в порядке списка заявок.

    Прежнее списковое включение ``V3._customer_orders``: те же живые объекты,
    тот же порядок, никаких копий.
    """
    cdef list result = []
    cdef object order
    for order in orders:
        if order['side'] == 'buy' and order['good'] == good:
            result.append(order)
    return result


def route_order_groups(object orders, object component_hook):
    """Сгруппировать заявки по компоненте маршрутов в порядке первого появления.

    Прежний ``defaultdict(list)`` обходил заявки по порядку, поэтому порядок
    ключей и элементов внутри групп сохранён буквально.
    """
    cdef dict groups = {}
    cdef object order
    cdef object component
    for order in orders:
        component = component_hook(order['tile_id'])
        groups.setdefault(component, []).append(order)
    return groups


def customer_quote_index(object customer_orders, object tile_id, object good,
                         object landed_hook):
    """Пары ``(заявка, доставка)`` для площадки назначения.

    Доставка считается прежним ``landed(tile_id, order_tile, good, 0)``; заявка
    без маршрута в результат не попадает, как и раньше.
    """
    cdef list result = []
    cdef object order
    cdef object quote
    for order in customer_orders:
        quote = landed_hook(tile_id, order['tile_id'], good, 0)
        if quote:
            result.append((order, quote[1]))
    return result


# ---------------------------------------------------------------------------
# Котировки и группы спроса
# ---------------------------------------------------------------------------

def customer_quotes(object rows, object owner_id, object eps):
    """Предел каждой заявки: отсев собственных, мёртвых и без остатка.

    Порядок проверок прежний (собственная заявка, остаток, статус), поэтому
    короткое замыкание и порядок результата не меняются.
    """
    cdef list result = []
    cdef object order
    cdef object freight
    for order, freight in rows:
        if order['participant'] == owner_id:
            continue
        if order['remaining'] <= eps:
            continue
        if order['status'] in ('cancelled', 'expired'):
            continue
        result.append((order, order['price']-freight))
    return result


def demand_rows(object rows, object eps):
    """Плоские строки спроса: только скаляры, снятые с живых заявок.

    Индекс котировок уже отбросил собственные заявки кандидата; здесь остаётся
    прежний отсев мёртвых и без остатка.
    """
    cdef list result = []
    cdef object order
    cdef object freight
    cdef object order_price
    for order, freight in rows:
        if order['remaining'] <= eps:
            continue
        if order['status'] in ('cancelled', 'expired'):
            continue
        order_price = order['price']
        result.append((order['participant'], order['tile_id'], order_price,
                       order_price-freight, freight, order['remaining'],
                       order['reserved']))
    return tuple(result)


def demand_groups(object rows, object price, object eps):
    """Агрегировать одинаковый спрос: те же ключи, тот же порядок групп.

    Платёжеспособность принадлежит отдельной заявке: объём каждой строки
    ограничен её собственным резервом, а не общим резервом группы. Сумма
    ``row[5] += ...`` остаётся наивной, как в прежнем коде.
    """
    cdef dict grouped = {}
    cdef object participant
    cdef object tile_id
    cdef object order_price
    cdef object limit
    cdef object freight
    cdef object remaining
    cdef object reserved
    cdef object row
    cdef object group_key
    for participant, tile_id, order_price, limit, freight, remaining, reserved in rows:
        if limit+eps < price:
            continue
        group_key = (participant, tile_id, order_price)
        row = grouped.get(group_key)
        if row is None:
            row = grouped[group_key] = [participant, tile_id, order_price, limit,
                                        freight, 0.0]
        row[5] += _min2(remaining, reserved/_max2(price+freight, eps))
    return tuple(tuple(row) for row in grouped.values())


# ---------------------------------------------------------------------------
# Достижимый спрос
# ---------------------------------------------------------------------------

def reachable_demand(object owner_id, object price, object groups,
                     object remaining, object account_hook, object offers_hook,
                     object good, object sells, object eps):
    """Достижимый спрос: покрытие групп живой книгой в прежнем порядке.

    ``remaining`` — общий словарь остатков офферов месяца: покрытие одного
    покупателя уменьшает остаток для следующего. Книга отсортирована по
    доставленной цене, поэтому за ценовой границей обход завершается, а не
    продолжается. Свои офферы кандидата не покрывают его собственный спрос.
    """
    cdef object demand = 0.0
    cdef object participant
    cdef object tile_id
    cdef object order_price
    cdef object limit
    cdef object freight
    cdef object volume
    cdef object buyer
    cdef object landed
    cdef object sell_price
    cdef object sell
    cdef object covered
    cdef object row
    for row in groups:
        participant, tile_id, order_price, limit, freight, volume = row
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
            covered = _min2(volume, remaining[sell['id']])
            volume -= covered
            remaining[sell['id']] -= covered
        demand += volume
    return demand


def stock_offers(object offers, object participant, object eps):
    """Сумма собственных живых офферов продавца по товару.

    Прежний код суммировал встроенным ``sum``, поэтому сумма компенсированная
    и того же порядка, что и список офферов.
    """
    cdef object total = 0
    cdef object comp = 0.0
    cdef object offer
    for offer in offers:
        if offer['participant'] != participant:
            continue
        if offer['status'] in ('cancelled', 'expired'):
            continue
        total, comp = _fold(total, comp, offer['remaining'])
    return _finish(total, comp)


def expected_stock_sales(object stock, object offered, object reachable,
                         object history):
    """Продажа старого склада: ``min(stock, offered, max(demand, average))``.

    Средние продажи — прежний встроенный ``sum`` по трём наблюдениям, делённый
    на их число; история пуста — среднее равно нулю. ``min``/``max`` сохраняют
    семантику выбора первого аргумента при равенстве.
    """
    cdef object average_sold = 0.0
    cdef object expected
    cdef object total = 0
    cdef object comp = 0.0
    cdef object row
    if history:
        for row in history:
            total, comp = _fold(total, comp, row.get('sold', 0.0))
        average_sold = _finish(total, comp)/len(history)
    expected = _max2(reachable, average_sold)
    return _min3(stock, offered, expected)
