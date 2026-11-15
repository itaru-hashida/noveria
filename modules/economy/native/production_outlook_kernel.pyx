# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Числовое ядро прогноза выпуска: продажи, спрос и план выпуска.

Ядро не владеет состоянием и не заводит второго ledger'а. Книга заявок,
``offers``, ``reference_price``, ``net_price``, ``reachable_demand``,
``expected_stock_sales`` и кэш прогноза остаются в Python-границе
``production_outlook_boundary``: сюда приходят уже посчитанные числа, срез
истории продаж и список сделок месяца. Живые заявки и сделки ядро только
читает.

Арифметика намеренно остаётся объектной (``object``, а не ``double``): цена,
остаток и ёмкость бывают ``int``, и приведение к ``double`` изменило бы тип
результата. Порядок операций и порядок ключей результата повторяют V3
буквально; ``max``/``min`` заменены на сравнения с той же семантикой выбора
первого аргумента при равенстве.

Суммы float повторяют не наивное сложение, а компенсированное суммирование
встроенного ``sum`` в CPython 3.12 (Neumaier): иначе последний бит расходится
и траектория уходит уже на первом месяце.

Единственный Python-хук внутри ядра — ``overhead_hook``: прежний код звал
``enterprise_overhead`` дважды (в ``fixed_unit`` и в ``expected_fixed_cost``),
и оба вызова сохранены в том же порядке.
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


cdef inline tuple _fold(object total, object comp, object value):
    """Шаг суммирования ровно как встроенный ``sum`` в CPython 3.12.

    ``sum`` складывает float компенсированно (Neumaier): к сумме добавляется
    поправка на погрешность каждого шага, а в конце — ``total+comp``. Наивное
    сложение даёт другой последний бит (21.599999999999994 вместо
    ...98), и траектория расходится с первого месяца. Целые складываются
    обычным ``+``, чтобы тип результата остался ``int``.
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


def select_price(object price, object base_net, object cost,
                 object production_price, object eps):
    """Цена прогноза: рентабельную текущую цену повышать не требуется.

    Прежнее выражение V3 — ``e['price'] if base_net+EPS >= cost else
    e.get('production_price', e['price'])``. Запасной вариант
    ``production_price`` считает граница: это чтение словаря.
    """
    if base_net+eps >= cost:
        return price
    return production_price


def sold_quantity(object trades, object seller, object good):
    """Проданное этим продавцом по этому товару за месяц.

    Прежний код — ``sum(t['quantity'] for t in trades if ...)``. Сумма идёт
    компенсированно и по порядку сделок, поэтому последний бит совпадает.
    """
    cdef object total = 0
    cdef object comp = 0.0
    cdef object row
    cdef tuple folded
    for row in trades:
        if row['seller'] == seller and row['good'] == good:
            total, comp = _fold(total, comp, row['quantity'])
    return _finish(total, comp)


def own_average(object observations, object sales_forecast):
    """Средние продажи трёх последних месяцев либо прежний прогноз."""
    cdef object total = 0
    cdef object comp = 0.0
    cdef object row
    if not observations:
        return sales_forecast
    for row in observations:
        total, comp = _fold(total, comp, row['sold'])
    return _finish(total, comp)/len(observations)


def outlook(object observations, object sales_forecast, object sold_now,
            object demand, object expected_sales, object stock_planning,
            object stock, object capacity, object previous, object cost,
            object expected_price, object overhead_hook, object eps,
            object overstock_factor, object decline_rate, object price):
    """Собрать прогноз выпуска: те же числа, тот же порядок ключей.

    ``expected_sales`` приходит уже посчитанным хуком либо ``None``, когда
    ``STOCK_PLANNING_REVISION`` выключена: короткое замыкание сохраняет
    граница, ядро только подставляет ``0.0``.

    ``overhead_hook`` вызывается один раз для ``fixed_unit`` (только при
    положительном выпуске) и второй раз для ``expected_fixed_cost`` — ровно
    как два прежних обращения к ``enterprise_overhead``.
    """
    cdef object own
    cdef object forecast
    cdef object projected_stock
    cdef object target
    cdef object fixed_unit
    cdef object overhead_cost
    cdef object margin
    cdef object limit
    own = own_average(observations, sales_forecast)
    forecast = _max2(own, sold_now+demand)
    if not stock_planning:
        expected_sales = 0.0
    # Начальный склад сначала торгуется в этом месяце; восполняем остаток,
    # который останется после торгов.
    projected_stock = _max0(stock-expected_sales)
    target = _max0(_min2(capacity, forecast-projected_stock))
    # При обычном падении спроса мощность сокращается постепенно. Если склад
    # переполнен, быстрый спад разрешён, чтобы не накапливать лишний товар.
    if (stock_planning >= 2 and target < previous-eps
            and not (stock > forecast*overstock_factor)):
        target = previous-decline_rate*(previous-target)
        target = _max0(_min2(capacity, target))
    if target > eps:
        fixed_unit = overhead_hook()/target
    else:
        fixed_unit = 0.0
    overhead_cost = overhead_hook()
    margin = expected_price-cost-fixed_unit
    if projected_stock >= forecast and forecast > eps:
        limit = 'Готовые запасы'
    elif forecast <= eps:
        limit = 'Нет обеспеченного спроса'
    else:
        limit = 'План'
    if margin < -eps:
        target = 0
        limit = 'Отрицательная ожидаемая маржа'
    return {'sales_forecast': forecast, 'reachable_demand': demand,
            'expected_unit_cost': cost, 'expected_price': expected_price,
            'expected_margin': margin, 'production_limit': limit,
            'planned_output': target, 'input_target_output': target,
            'planned_price': price, 'expected_fixed_cost': overhead_cost,
            'expected_fixed_unit': fixed_unit}
