# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Числовое ядро оставшихся денежных и трудовых расчётов.

Перенесены расчётные срезы ``V3``/``EconomyServices``:

  * ``project_cash_reserve`` — смета проекта, доставка, учёт уже купленного и
    обеспеченных заявок, недостача по котировке;
  * ``reserve_expedited_construction_labor`` — труд провинции, резерв
    ускоренных бригад и предел поддержки;
  * ``distribute`` — сумма населения, отсечение по живой кассе и доли
    получателей;
  * распределение доходов carrier/treasury в ``settle`` — ставка, получатели,
    сумма и причина.

Ядро не владеет состоянием и не заводит второго ledger'а: деньги двигает
прежний ``transfer`` через ``cash_store``, заявки и склад не трогаются вовсе.
Сюда приходят готовые числа и хуки границы ``money_boundary``:

  * ``stock_hook(good)`` — склад счёта проекта (без резервов);
  * ``quote_hook(good, missing)`` — прежний ``quoted_cost`` целиком: ядру
    нужен только потолок ``[1]``, а книга и кэш остаются за котировками;
  * ``population_hook(account)`` / ``balance_hook(id)`` / ``free_cash_hook(id)``;
  * ``transfer_hook(sender, recipient, amount, reason)`` — прежний ``transfer``;
  * ``distribute_hook(sender, recipients, amount, reason)`` — прежний
    ``distribute`` (он же ниже вызывает ``transfer_hook``).

``working_cash_reserve`` и числовая часть ``cash_after_commitments`` живут в
уже существующем ``dividend_kernel`` и здесь повторно не переносятся: граница
зовёт их напрямую.

Арифметика намеренно объектная (``object``, а не ``double``): ``capacity``,
``quantity``, ``balance`` и ``labor`` бывают ``int``, а ``min``/``max``
возвращают один из аргументов вместе с его типом. Суммы, которые прежний код
считал встроенным ``sum`` (вес сметы, остатки заявок, население, труд
провинции), повторяют компенсированное суммирование CPython 3.12 (Neumaier);
накопления прежнего кода через ``+=`` остаются наивными.

RNG ядро не трогает: перемешивание счетов в ``settle`` остаётся в Python.
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
    сложение даёт другой последний бит, а он меняет доли получателей, порядок
    сделок и RNG. Целые складываются обычным ``+``, чтобы тип остался ``int``.
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
# Резерв денег на проект
# ---------------------------------------------------------------------------

def project_cash_reserve(object working_cash, object goods, object quantities,
                         object weights, object distance, object transport_rate,
                         object delivery_fallback, object stock_hook,
                         object orders, object participant, object purpose,
                         object quote_hook, object eps):
    """Прежний ``V3.project_cash_reserve``: доставка, заявки и недостача.

    ``goods``/``quantities``/``weights`` — смета в порядке словаря: ядро не
    строит её заново, а обходит готовый список. Вес доставки суммируется
    встроенным ``sum`` (компенсированно) и умножается на ``distance``, затем на
    ``transport_rate`` — прежний порядок ``distance*rate`` справа налево
    сохранён буквально. Если маршрута нет, берётся прежняя ``delivery``
    проекта, даже когда она равна нулю.
    """
    cdef Py_ssize_t index, count = len(goods)
    cdef object total = 0
    cdef object comp = 0.0
    cdef object delivery, needed, ordered, ocomp, missing
    cdef object good, quantity, order
    if distance is not None:
        for index in range(count):
            total, comp = _fold(total, comp, quantities[index]*weights[index])
        delivery = _finish(total, comp)*distance*transport_rate
    else:
        delivery = delivery_fallback
    needed = working_cash+delivery
    for index in range(count):
        good = goods[index]
        quantity = quantities[index]
        # Купленные материалы и обеспеченные заявки не требуют повторного
        # резерва денег: отбор прежний, по участнику, стороне, назначению,
        # товару, статусу и положительному резерву.
        ordered = 0
        ocomp = 0.0
        for order in orders:
            if (order['participant'] == participant and order['side'] == 'buy'
                    and order['purpose'] == purpose and order['good'] == good
                    and order['status'] not in ('cancelled', 'expired')
                    and order['reserved'] > eps):
                ordered, ocomp = _fold(ordered, ocomp, order['remaining'])
        ordered = _finish(ordered, ocomp)
        missing = _max0(quantity-stock_hook(good)-ordered)
        needed += missing*quote_hook(good, missing)[1]
    return needed


# ---------------------------------------------------------------------------
# Труд ускоренных строек
# ---------------------------------------------------------------------------

def household_labor(object rows, object labor_share):
    """Труд по (государство, провинция): прежний встроенный ``sum``.

    ``rows`` — пары ``(key, population)`` в порядке ``accounts.values()``.
    Каждый ключ суммируется компенсированно, потому что прежний код считал
    ``sum(population(a)*labor_share for a in ...)`` по тем же слагаемым и в том
    же порядке.
    """
    cdef dict totals = {}
    cdef dict comps = {}
    cdef dict result = {}
    cdef object key, population, total, comp
    for key, population in rows:
        total = totals.get(key, 0)
        comp = comps.get(key, 0.0)
        total, comp = _fold(total, comp, population*labor_share)
        totals[key] = total
        comps[key] = comp
    for key in totals:
        result[key] = _finish(totals[key], comps[key])
    return result


def expedited_labor(object accounts, object labor, object wage, object modes,
                    object free_cash_hook, object eps):
    """Прежний ``V3.reserve_expedited_construction_labor``.

    Порядок: счета по ``id``, затем их ``public_projects`` в порядке вставки.
    Готовность — прежние два ``all()`` с тем же short-circuit; ``workers`` —
    ``min`` пяти аргументов, где при равенстве побеждает первый. Остаток труда
    перечитывается на каждой стройке, потому что предыдущая его уменьшает.

    Режим берётся из ``modes`` только после проверки ``mode == 'accelerated'``:
    прежний код читал ``cfg['construction_modes']['accelerated']`` лениво, и
    конфигурация без этого ключа не падала, пока ускоренных строек нет.
    """
    cdef dict reservations = {}
    cdef dict workers_by_project = {}
    cdef object account, project, location, key, available
    cdef object mode, support_limit, per_worker, cost_per_worker, workers
    cdef object good, quantity, candidate
    for account in accounts:
        for project in account.get('public_projects', []):
            if project['mode'] != 'accelerated':
                continue
            if not _ready(project, eps):
                continue
            mode = modes['accelerated']
            location = project['location']
            key = (location['state_id'], location['province_id'])
            # Прежний ``defaultdict(float)``: отсутствующий ключ даёт 0.0.
            available = labor.get(key, 0)-reservations.get(key, 0.0)
            support_limit = None
            for good, per_worker in mode['support_goods'].items():
                if per_worker > eps:
                    candidate = project['support_stock'].get(good, 0)/per_worker
                    if support_limit is None or candidate < support_limit:
                        support_limit = candidate
            if support_limit is None:
                support_limit = float('inf')
            cost_per_worker = wage*mode['wage_multiplier']+mode['lodging_allowance']
            workers = project['max_workers']
            candidate = project['work_required']-project['work_done']
            if candidate < workers:
                workers = candidate
            candidate = _max0(available)
            if candidate < workers:
                workers = candidate
            if support_limit < workers:
                workers = support_limit
            candidate = free_cash_hook(account['id'])/cost_per_worker
            if candidate < workers:
                workers = candidate
            if workers > eps:
                reservations[key] = reservations.get(key, 0.0)+workers
                workers_by_project[project['id']] = workers
    return reservations, workers_by_project


cdef inline bint _ready(object project, object eps):
    """Готовность стройки: прежние два ``all()`` с тем же short-circuit."""
    cdef object good, quantity
    for good, quantity in project['material_bill'].items():
        if not project['materials'].get(good, 0)+eps >= quantity:
            return False
    for good, quantity in project['equipment'].items():
        if not project['equipment_held'].get(good, 0)+eps >= quantity:
            return False
    return True


# ---------------------------------------------------------------------------
# Распределение доходов
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Оброк предприятия и финансирование кандидата
# ---------------------------------------------------------------------------

def enterprise_overhead(object capacity, object labor, object wage, object rate):
    """Прежний ``V3.enterprise_overhead``: capacity*labor*wage*rate.

    Порядок сомножителей буквальный: слева направо, без перегруппировки. Тип
    результата не приводится, поэтому int-сомножители дают int, как и раньше.
    """
    return capacity*labor*wage*rate


def project_finances(object capacity, object available, object startup,
                     object material, object delivery, object unit,
                     object fixed_unit, object equipment_capital, object margin,
                     object expansion, object eps):
    """Прежний ``v3.project_finances``: мощность, оборот, вложения, прибыль.

    ``min``/``max`` — встроенные: при равенстве побеждает первый аргумент, и
    тип сохраняется. Порядок сложений ``material+delivery+unit+equipment_capital``
    и ``(unit+fixed_unit)`` прежний, поэтому последний бит тот же.
    """
    cdef object budget, need, working, investment
    budget = _max0(available-startup)
    need = _max2(material+delivery+unit+equipment_capital, eps)
    capacity = _min2(capacity, budget/need)
    working = capacity*equipment_capital
    if not expansion:
        working = working+_max2(startup, capacity*(unit+fixed_unit))
    investment = capacity*(material+delivery)+working
    return capacity, working, investment, capacity*margin


def distribute(object hooks, object sender, object recipients, object amount,
               object reason):
    """Прежний ``EconomyServices.distribute``: доли по населению.

    Сумма населения — встроенный ``sum`` (компенсированный). Нулевая сумма
    означает, что переводов нет вовсе: прежний код не отсекал ``amount`` по
    балансу и не звал ``transfer``. Иначе ``amount`` отсекается по живой кассе
    и каждый получатель получает ``amount*population/total``: умножение, затем
    деление, без предварительного приведения.
    """
    cdef object total = 0
    cdef object comp = 0.0
    cdef object account, share
    for account in recipients:
        total, comp = _fold(total, comp, hooks.population(account))
    total = _finish(total, comp)
    if total:
        amount = _min2(amount, hooks.balance(sender))
        for account in recipients:
            share = amount*hooks.population(account)/total
            hooks.transfer(sender, account['id'], share, reason)
    return None


def settle_distributions(object hooks, object accounts, object households,
                         object rates, object eps):
    """Прежний второй цикл ``V3.settle``: carrier/treasury.

    ``accounts`` уже перемешан Python-стороной: RNG и порядок остаются там.
    Ставка берётся по виду счёта, получатели — прежним фильтром по государству
    в порядке списка домохозяйств, сумма — ``min(balance*rate, free_cash)``.
    Перевод выполняет ``hooks.distribute`` сразу же, поэтому чередование
    расчёта и перевода сохранено, а второй ledger не появляется.
    """
    cdef object account, rate, recipients, amount, reason, household
    for account in accounts:
        rate = rates.get(account['kind'], 0)
        if rate:
            recipients = []
            for household in households:
                if household['state_id'] == account['state_id']:
                    recipients.append(household)
            amount = _min2(hooks.balance(account['id'])*rate,
                           hooks.free_cash(account['id']))
            reason = ('Оплата общественных услуг' if account['kind'] == 'treasury'
                      else 'Оплата транспортных услуг населению')
            hooks.distribute(account['id'], recipients, amount, reason)
    return None
