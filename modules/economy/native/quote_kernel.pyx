# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Числовое ядро котировок и себестоимости: книга, суммы и потолок закупки.

Ядро не владеет состоянием и не заводит второго ledger'а: книга заявок,
кэши котировок (``_reference_price_cache``/``_quoted_cost_cache``/
``_unit_cost_cache``), ``_recovery_quotes``, склад и кассу держит движок.
Сюда приходят уже готовые значения и хуки границы ``quote_boundary``:

  * ``offers`` — живое ``OfferView`` книги: ядро только обходит его в прежнем
    порядке и читает ``remaining``/``price``/``participant``;
  * ``signal_hook()`` — прежний ``market_signal``: кэш сигнала и ключ остаются
    Python, поэтому ядро зовёт хук только при пустой книге;
  * ``reference_hook()`` — прежний ``reference_price``: кэш котировок и
    ``market_signal`` остаются состоянием движка;
  * ``recovery_rows`` — прежний срез ``_recovery_quotes[good]``: пары
    ``(firm, price)`` в том же порядке;
  * ``landed_hook(tile, price)`` — прежний ``landed``: маршрут, кэш доставки
    и тариф остаются Python;
  * ``stock_hook(good)`` — прежний ``free_stock``; ядро склад не трогает.

Арифметика намеренно остаётся объектной (``object``, а не ``double``): цена,
остаток, резерв и объём бывают ``int``, и приведение к ``double`` изменило бы
тип результата. Тип виден дальше — в ``margin``, ``expected_profit`` проекта и
в сумме ``unit_cost``, поэтому ``max``/``min`` заменены сравнениями с той же
семантикой выбора первого аргумента при равенстве.

Суммы float (``unit_cost``) повторяют не наивное сложение, а компенсированное
суммирование встроенного ``sum`` в CPython 3.12 (Neumaier): иначе последний
бит расходится и траектория уходит уже на первом месяце. Накопления
``quoted_cost`` (``value``/``ceiling``) наоборот наивные: прежний код складывал
их через ``+=``.
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
    """``min(first, second, third)``: первый минимальный элемент побеждает."""
    return _min2(_min2(first, second), third)


cdef inline tuple _fold(object total, object comp, object value):
    """Шаг суммирования ровно как встроенный ``sum`` в CPython 3.12.

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


def quoted_cost(object offers, object quantity, object reference_hook,
                object recovery_rows, object owner_id, object landed_hook,
                object eps):
    """``V3.quoted_cost``: книга, затем ориентир восстановления выпуска.

    Порядок операций буквальный: ``min(left, remaining)``, накопление
    ``value``, ``max(ceiling, price)``, вычитание из ``left`` и ранний выход
    по ``left <= EPS``. Ориентир восстановления считается только когда книга
    не покрыла объём, и ``landed`` зовётся только для чужих фирм — как в
    прежнем списковом включении с моржовым оператором.
    """
    cdef object left = quantity
    cdef object value = 0.0
    cdef object ceiling = 0.0
    cdef object q
    cdef object price
    cdef object sell
    cdef object quote
    cdef object reference
    cdef object firm
    cdef object firm_price
    cdef object landed
    cdef object quotes
    cdef object best
    for price, sell, quote in offers:
        q = _min2(left, sell['remaining'])
        value += q*price
        ceiling = _max2(ceiling, price)
        left -= q
        if left <= eps:
            break
    if left > eps:
        reference = reference_hook()
        # Ориентир следующего выпуска не является предложением товара.
        quotes = []
        for firm, firm_price in recovery_rows:
            if firm['id'] == owner_id:
                continue
            landed = landed_hook(firm['tile_id'], firm_price)
            if landed:
                quotes.append(landed[0])
        if quotes:
            best = quotes[0]
            for quote in quotes[1:]:
                if quote < best:
                    best = quote
            reference = _max2(reference, best)
        value += left*reference
        ceiling = _max2(ceiling, reference)
    return value, _max2(ceiling, eps), left <= eps


cpdef api object quoted_cost_values(object offers, object quantity, object reference,
                       object recovery_prices, object eps):
    """Котировка по полностью подготовленным числовым recovery-данным."""
    cdef object left = quantity
    cdef object value = 0.0
    cdef object ceiling = 0.0
    cdef object q
    cdef object price
    cdef object best
    cdef object remaining
    for price, remaining in offers:
        q = _min2(left, remaining)
        value += q*price
        ceiling = _max2(ceiling, price)
        left -= q
        if left <= eps:
            break
    if left > eps:
        if recovery_prices:
            best = min(recovery_prices)
            reference = _max2(reference, best)
        value += left*reference
        ceiling = _max2(ceiling, reference)
    return value, _max2(ceiling, eps), left <= eps


def reference_price(object offers, object signal_hook, object default_price):
    """``V3.reference_price``: первая живая цена книги либо ориентир рынка.

    ``bool(offers)`` и ``offers[0]`` — те же два ленивых обращения, что в
    прежнем коде: оба сдвигают ``first_live`` книги ровно на один шаг. Хук
    сигнала зовётся только при пустой книге, как и прежний ``market_signal``.
    """
    if offers:
        return offers[0][0]
    return signal_hook() or default_price


def cost_sum(object items, object hook):
    """``sum(ratio*reference(good) for good, ratio in items)`` компенсированно.

    ``items`` — уже материализованный список пар рецепта в прежнем порядке
    (входы, затем износ). ``hook(good)`` отдаёт цену доставки: ядро её не
    считает и в книгу не ходит.
    """
    cdef object total = 0
    cdef object comp = 0.0
    cdef object good
    cdef object ratio
    for good, ratio in items:
        total, comp = _fold(total, comp, ratio*hook(good))
    return _finish(total, comp)


def prepared_cost_sum(object items):
    """Сумма стоимости по заранее разрешённым котировкам."""
    cdef object total = 0
    cdef object comp = 0.0
    cdef object ratio, price
    for ratio, price in items:
        total, comp = _fold(total, comp, ratio*price)
    return _finish(total, comp)


def volume_input_prices(object items, object quantity, object stock_hook,
                        object quoted_hook, object reference_hook,
                        object account, object eps):
    """Средние доставленные цены входов для объёма плана.

    Прежний цикл шёл по ``recipe['inputs'].items()`` в порядке рецепта и
    складывал ``prices[good]``. Порядок ключей результата сохраняется
    буквально: товар без недостачи не вызывает ``quoted_cost`` вовсе.
    """
    cdef object prices = {}
    cdef object good
    cdef object ratio
    cdef object missing
    cdef object value
    for good, ratio in items:
        missing = _max0(quantity*ratio-stock_hook(good))
        if missing > eps:
            value, unused, unused2 = quoted_hook(account, good, missing)
            prices[good] = value/missing
        else:
            prices[good] = reference_hook(good)
    return prices


def input_bid_ceiling(object inputs, object excluded_good, object ratio,
                      object prices, object orders, object input_order_ids,
                      object expected_net_price, object wage_unit,
                      object fixed_unit):
    """Предельная цена автоматической закупки при зафиксированной марже.

    Прежний код на каждый вход сканировал ``report['orders']`` целиком
    (``o['id'] in plan['input_orders'] and o['good'] == good``). Здесь тот же
    отбор делается одним проходом, а списки цен собираются в порядке заявок:
    ``max([prices[good]]+committed)`` возвращает первый максимальный элемент,
    поэтому порядок и типы сохраняются побитово. Вход самой закупки
    пропускается, как и прежде; ``ratio`` — его норма из ``input_ratios``.
    """
    cdef object committed = {}
    cdef object row
    cdef object bucket
    cdef object prices_list
    cdef object other_cost = wage_unit+fixed_unit
    cdef object good
    cdef object quantity
    for row in orders:
        if row['id'] not in input_order_ids:
            continue
        bucket = committed.get(row['good'])
        if bucket is None:
            bucket = []
            committed[row['good']] = bucket
        bucket.append(row['price'])
    for good, quantity in inputs:
        if good == excluded_good:
            continue
        bucket = committed.get(good)
        if bucket is None:
            prices_list = [prices[good]]
        else:
            prices_list = [prices[good]]+bucket
        other_cost += quantity*max(prices_list)
    return _max0((expected_net_price-other_cost)/ratio)


def reprice_decision(object quote, object ceiling, object budget,
                     object remaining, object price, object eps):
    """Число для ``reprice_input_order``: новая цена и доплата, или ``None``.

    Мутации (резерв кассы, поля заявки, ревизия спроса) остаются за
    Python-хуками границы: ядро считает только ``min(quote, ceiling,
    budget/remaining)`` и доплату по прежнему выражению.
    """
    cdef object new_price = _min3(quote, ceiling, budget/remaining)
    if new_price > price+eps:
        return new_price, remaining*(new_price-price)
    return None
