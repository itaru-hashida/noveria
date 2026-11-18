# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Числовое ядро планирования закупок предприятий.

Перенесены расчётные срезы ``V3``:

  * штатный обход ``plan_enterprises`` — порядок по убыванию глубины товара,
    условия восстановления и планирования;
  * ``input_requests`` целиком: начальные цены входов, снятие прошлого
    ``operating_protected``, вызов плана, ``cash_needed``, 40 итераций бисекции
    по доступной кассе, средние цены входов, потребности в сырье и
    оборудовании, резерв зарплат и поля ``production_plan``.

Ядро не владеет состоянием: заявки, деньги, склад и отчёт двигают прежние
stores через хуки границы ``plan_boundary``. Сюда приходят готовый рецепт,
поток заявок ``sells`` и объект хуков с узким набором методов:

  * ``reference_price(good)`` — прежний ``reference_price`` (книга и кэш);
  * ``stock(good)`` — прежний ``free_stock`` (склад минус резервы);
  * подготовленные ``quote_contexts`` — прежний ``quoted_cost(...)`` по книге
    и recovery-контексту; ядру нужен только его потолок ``[1]``;
  * ``plan()`` — прежний ``plan(e, sells=sells)`` (прогноз выпуска);
  * ``volume_prices(target)`` — прежний ``volume_input_prices``;
  * ``reset_protected()``/``set_payroll(value)`` — прежние поля счёта;
  * ``commit_plan(plan, payroll)`` — прежние ``planned_output``,
    ``input_target_output``, ``production_limit``, ``production_plan`` и
    резерв зарплат в том же порядке;
  * ``order(good, quantity, price, purpose)`` — прежний ``self.order(...)``:
    деньги, склад и заявки двигает только он;
  * ``begin_orders()``/``order_ids(since)`` — прежний отрезок
    ``report['orders'][first_order:]``.

Арифметика намеренно объектная (``object``, а не ``double``): ``capacity``,
``labor``, ``wage`` и ``cash`` бывают ``int``, а ``max``/``min`` возвращают
один из аргументов вместе с его типом. Порядок операций повторён буквально:
``(quantity*labor)*wage``, затем входы и оборудование в порядке рецепта;
накопление ``needed`` — наивное ``+=``, как в прежнем коде.

RNG ядро не трогает: перемешивание делает Python, устойчивую сортировку по
глубине товара — ядро. Прежний ``sorted`` был устойчивым, поэтому пары
``(-depth, index)`` воспроизводят тот же порядок без сравнения словарей.
"""

from modules.economy.native.quote_kernel cimport quoted_cost_values


cdef inline object _max0(object value):
    """``max(0, value)``: при равенстве и для отрицательных побеждает ``0``."""
    if value > 0:
        return value
    return 0


cdef inline object _cash_needed(object overhead, object labor, object wage, object quantity,
                                object input_items, object equipment_rates,
                                object stock_values, object quote_contexts,
                                object eps):
    """Прежний ``cash_needed(quantity)``: оброк, труд, сырьё и оборудование.

    ``needed`` накапливается наивным ``+=``, как в прежнем коде, а не
    компенсированным ``sum``. Недостача каждого входа — ``max(0, ...)`` с
    ``0`` первым аргументом; ``quoted_cost`` зовётся и при нулевой недостаче,
    потому что прежний код звал его без проверки.
    """
    cdef object needed = overhead+quantity*labor*wage
    cdef object good
    cdef object ratio
    cdef object wear
    cdef object missing
    cdef object required
    cdef object context
    for good, ratio in input_items:
        missing = _max0(quantity*ratio-stock_values[good])
        context = quote_contexts[good]
        needed += missing*quoted_cost_values(
            context[0], missing, context[1], context[2], eps)[1]
    for good, ratio, wear in equipment_rates:
        required = ratio*quantity
        missing = _max0(required-stock_values[good])
        context = quote_contexts[good]
        needed += missing*quoted_cost_values(
            context[0], missing, context[1], context[2], eps)[1]
    return needed


def plan_target(object overhead, object labor, object wage, object target, object cash,
                object input_items, object equipment_rates, object stock_values,
                object quote_contexts, object eps):
    """Итоговый выпуск: бисекция по кассе, ровно 40 итераций.

    Прежний код сначала считал ``cash_needed(target)`` и только при нехватке
    кассы сужал отрезок. Границы и порядок присваиваний те же: при
    ``cash_needed(middle) <= cash`` растёт ``low``, иначе падает ``high``, а
    результат — ``low``, то есть наибольшее проверенное доступное значение.
    """
    if _cash_needed(overhead, labor, wage, target, input_items, equipment_rates,
                    stock_values, quote_contexts, eps) > cash:
        low = 0.0
        high = target
        for _ in range(40):
            middle = (low+high)/2
            if _cash_needed(overhead, labor, wage, middle, input_items,
                            equipment_rates, stock_values, quote_contexts, eps) <= cash:
                low = middle
            else:
                high = middle
        target = low
    return target


def calculate_target(object snapshot, object cash, object requested, object eps):
    """Рассчитать выпуск по snapshot без engine и Python hooks."""
    return plan_target(snapshot['overhead'], snapshot['labor'], snapshot['wage'],
                      requested, cash, snapshot['input_items'],
                      snapshot['equipment_rates'], snapshot['stock_values'],
                      snapshot['quote_contexts'], eps)


def input_requests(object snapshot, object eps):
    """Прежний ``V3.input_requests``: числа в ядре, мутации через хуки.

    Порядок шагов сохранён буквально: цены входов (прежний код присваивал
    ``input_prices`` дважды, но вызовы ``reference_price`` оставались и
    прогревали кэш), снятие старого ``operating_protected``, план, касса,
    бисекция, средние цены входов, ``planned_output``/``input_target_output``,
    причина предела, ``production_plan``, резерв зарплат, затем заявки на
    сырьё и оборудование и список их ``id``.
    """
    cdef dict input_ratios = snapshot['input_ratios']
    cdef list input_items = snapshot['input_items']
    cdef list equipment_rates = snapshot['equipment_rates']
    cdef dict stock_values = snapshot['stock_values']
    cdef dict quote_contexts = snapshot['quote_contexts']
    cdef object overhead = snapshot['overhead']
    cdef object labor = snapshot['labor']
    cdef object wage = snapshot['wage']
    cdef object good
    cdef object ratio
    cdef object wear
    cdef object target
    cdef object requested
    cdef object cash
    cdef object first_order
    cdef object quantity
    cdef object price
    cdef object order
    cdef object context
    cdef object input_prices
    cdef list orders = []
    cdef dict equipment_required = {}
    cdef dict equipment_wear = {}
    cdef dict plan

    target = snapshot['target']
    cash = snapshot['cash']
    requested = snapshot['requested']
    first_order = snapshot['first_order']
    input_prices = snapshot['input_prices']
    for good, ratio, wear in equipment_rates:
        equipment_required[good] = ratio*target
        equipment_wear[good] = ratio*target*wear
    plan = dict(month=snapshot['month'], output=target,
                sale_price=snapshot['sale_price'],
                expected_net_price=snapshot['expected_net_price'], input_prices=input_prices,
                input_ratios=dict(input_ratios), wage_unit=labor*wage,
                equipment_required=equipment_required,
                equipment_wear=equipment_wear,
                fixed_unit=snapshot['fixed_unit'],
                requested_output=requested, free_cash=cash,
                known_order_count=first_order,
                reachable_demand=snapshot['reachable_demand'],
                sales_forecast=snapshot['sales_forecast'],
                expected_margin=snapshot['expected_margin'],
                reason=snapshot['production_limit'])
    for good, ratio in input_items:
        quantity = _max0(target*ratio-stock_values[good])
        context = quote_contexts[good]
        price = quoted_cost_values(
            context[0], quantity, context[1], context[2], eps)[1]
        orders.append((good, quantity, price, 'Сырьё', True))
    for good, ratio, wear in equipment_rates:
        quantity = _max0(ratio*target-stock_values[good])
        context = quote_contexts[good]
        price = quoted_cost_values(
            context[0], quantity, context[1], context[2], eps)[1]
        orders.append((good, quantity, price, 'Оборудование', False))
    return plan, orders
