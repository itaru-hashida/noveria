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

Постоянный оброк передаётся как готовое число.
"""


from modules.economy.native.offer_view cimport cover_demand_from_books
from modules.economy.native.demand_kernel cimport expected_stock_sales


def forecast_from_books(tuple demand_input, tuple stock_input, object outlook_input):
    """Рассчитать покрытие спроса, продажи склада и выпуск за один вход."""
    cdef object owner, price, groups, eps, demand, sales
    cdef dict books, remaining
    owner, price, groups, books, remaining, eps = demand_input
    demand = cover_demand_from_books(owner, price, groups, books, remaining, eps)
    sales, offered, stock, history, alternate = stock_input
    if offered is not None:
        reachable = demand
        if alternate is not None:
            owner, price, groups, books, remaining, eps = alternate
            reachable = cover_demand_from_books(owner, price, groups, books, remaining, eps)
        sales = expected_stock_sales(stock, offered, reachable, history)
    return outlook(outlook_input._replace(demand=demand, expected_sales=sales), eps), sales


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


cpdef object sold_quantity(object trades, object seller, object good):
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


def outlook(object inputs, object eps):
    """Собрать прогноз выпуска: те же числа, тот же порядок ключей.

    ``expected_sales`` приходит уже посчитанным хуком либо ``None``, когда
    ``STOCK_PLANNING_REVISION`` выключена: короткое замыкание сохраняет
    граница, ядро только подставляет ``0.0``.

    ``overhead_cost`` рассчитан границей.
    """
    cdef object observations = inputs.observations
    cdef object sales_forecast = inputs.sales_forecast
    cdef object trades = inputs.trades
    cdef object seller = inputs.seller
    cdef object good = inputs.good
    cdef object demand = inputs.demand
    cdef object expected_sales = inputs.expected_sales
    cdef object stock_planning = inputs.stock_planning
    cdef object stock = inputs.stock
    cdef object capacity = inputs.capacity
    cdef object previous = inputs.previous
    cdef object cost = inputs.cost
    cdef object expected_price = inputs.expected_price
    cdef object overhead_cost = inputs.overhead
    cdef object overstock_factor = inputs.overstock_factor
    cdef object decline_rate = inputs.decline_rate
    cdef object price = inputs.price
    cdef object own
    cdef object forecast
    cdef object projected_stock
    cdef object target
    cdef object fixed_unit
    cdef object margin
    cdef object limit
    own = own_average(observations, sales_forecast)
    forecast = _max2(own, sold_quantity(trades, seller, good)+demand)
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
        fixed_unit = overhead_cost/target
    else:
        fixed_unit = 0.0
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
