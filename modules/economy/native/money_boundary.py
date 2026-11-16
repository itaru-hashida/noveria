"""Граница денежных и трудовых расчётов: эталон, fallback и один вход.

В Cython уходят оставшиеся расчётные срезы:

  * ``project_cash_reserve`` — смета проекта, доставка, учёт купленного и
    обеспеченных заявок, недостача по котировке;
  * ``reserve_expedited_construction_labor`` — труд провинции и резерв
    ускоренных бригад;
  * ``distribute`` — сумма населения, отсечение по живой кассе и доли;
  * распределение доходов carrier/treasury в ``settle``.

``working_cash_reserve`` и числовая часть ``cash_after_commitments`` повторно
не переносятся: они уже живут в ``dividend_kernel``, и граница зовёт их через
``dividend_boundary`` (те же хуки, та же арифметика).

Мутации идут прежними путями: ``transfer`` двигает деньги через
``cash_store``, ``distribute`` — он же; заявки и склад не трогаются. Ядро
возвращает только числа, строки отчёта и события остаются Python.

Что остаётся Python по построению: ``path`` (маршрут и его кэш),
``construction_bill``, ``quoted_cost`` (книга, ``landed``, кэш),
``free_stock``/``stock`` (склад и резервы), ``population``, ``free_cash``,
``transfer``/``distribute`` и RNG перемешивания счетов.

Модуль не импортирует ``modules.economy.support``: EPS приходит через
``configure()``, иначе получается циклический импорт
(support -> boundary -> support). ``dividend_boundary`` такого импорта не
делает, поэтому цикл не возникает.

Эталонные ``*_python`` функции повторяют прежние тела буквально и служат
образцом для focused-тестов на побитовое совпадение, а также fallback, если
расширение не собрано. Имена привязываются один раз при импорте.
"""

import importlib

from . import dividend_boundary as _dividend

# EPS проекта. Задаётся из support.py, чтобы не дублировать константу.
_EPS = 1e-7


def configure(eps):
    """Задать EPS проекта для native-ядра и эталонных Python-функций."""
    global _EPS
    _EPS = eps


class MoneyHooks(object):
    """Живой доступ ядра к движку: чтение чисел плюс прежние переводы.

    Хуки намеренно узкие и не считают числа заново: ядро передаёт готовые
    значения, а граница фиксирует их там, где это делал прежний код.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    # --- чтение чисел ------------------------------------------------------
    def population(self, account):
        return self.engine.population(account)

    def balance(self, account_id):
        # Чтение идёт через cash_store: колонки остаются источником истины.
        return self.engine.cash_store.get_balance(account_id)

    def free_cash(self, account_id):
        return self.engine.free_cash(account_id)

    # --- деньги: единственные точки мутации -------------------------------
    def transfer(self, sender, recipient, amount, reason):
        self.engine.transfer(sender, recipient, amount, reason)

    def distribute(self, sender, recipients, amount, reason):
        self.engine.distribute(sender, recipients, amount, reason)


# ---------------------------------------------------------------------------
# Резерв денег на проект
# ---------------------------------------------------------------------------

def _bill_rows(engine, bill):
    """Смета в порядке словаря: товары, количества и веса для ядра."""
    goods = []
    quantities = []
    weights = []
    for good, quantity in bill.items():
        goods.append(good)
        quantities.append(quantity)
        weights.append(engine.goods[good]['weight'])
    return goods, quantities, weights


def project_cash_reserve(engine, account, sells):
    """Единственный вход из ``V3.project_cash_reserve``: смета и ядро."""
    a = engine.accounts[account]
    project = a.get('project')
    if not project:
        return 0.0
    distance, _ = engine.path(engine.market_tile(a), project['location']['tile_id'])
    goods, quantities, weights = _bill_rows(
        engine, engine.construction_bill(project['capacity']))
    return run_project_reserve(
        project['working_cash'], goods, quantities, weights, distance,
        engine.cfg['transport_rate'], project.get('delivery', 0),
        lambda good: a['stock'].get(good, 0), engine.report['orders'], account,
        'Строительные материалы',
        lambda good, missing: engine.quoted_cost(a, good, missing, sells), _EPS)


def project_cash_reserve_python(engine, account, sells):
    """Эталон ``V3.project_cash_reserve``: буквальный прежний код."""
    a = engine.accounts[account]
    project = a.get('project')
    if not project:
        return 0.0
    distance, _ = engine.path(engine.market_tile(a), project['location']['tile_id'])
    bill = engine.construction_bill(project['capacity'])
    delivery = (sum(q*engine.goods[g]['weight'] for g, q in bill.items())
        * distance*engine.cfg['transport_rate']) if distance is not None else project.get('delivery', 0)
    needed = project['working_cash']+delivery
    for good, quantity in bill.items():
        # Купленные материалы и обеспеченные заявки не требуют повторного резерва денег.
        ordered = sum(o['remaining'] for o in engine.report['orders']
            if o['participant'] == account and o['side'] == 'buy'
            and o['purpose'] == 'Строительные материалы' and o['good'] == good
            and o['status'] not in ('cancelled', 'expired') and o['reserved'] > _EPS)
        missing = max(0, quantity-a['stock'].get(good, 0)-ordered)
        needed += missing*engine.quoted_cost(a, good, missing, sells)[1]
    return needed


# ---------------------------------------------------------------------------
# Рабочий резерв и свободная касса: переиспользование dividend_kernel
# ---------------------------------------------------------------------------

def working_cash_reserve(engine, e, sells):
    """Прежний ``V3.working_cash_reserve`` поверх ядра дивидендов.

    Повторно не переносится: ``dividend_kernel.working_reserve`` уже считает
    ровно это (``quantity*labor*wage``, затем входы и оборудование), поэтому
    граница лишь отдаёт ему те же хуки, что и ``pay_dividends``.
    """
    return _dividend.working_reserve(_dividend.DividendHooks(engine, sells), e)


def working_cash_reserve_python(engine, e, sells):
    """Эталон ``V3.working_cash_reserve``: буквальный прежний код."""
    recipe = engine.goods[e['good']]
    quantity = e['capacity']*engine.cfg['working_reserve_months']
    needed = quantity*recipe['labor']*engine.cfg['wage']
    for good, ratio in recipe['inputs'].items():
        missing = max(0, quantity*ratio-engine.free_stock(e['id'], good))
        needed += missing*engine.quoted_cost(engine.accounts[e['id']], good, missing, sells)[1]
    for good, required in engine.equipment_requirements(recipe, quantity).items():
        missing = max(0, required-engine.free_stock(e['id'], good))
        needed += missing*engine.quoted_cost(engine.accounts[e['id']], good, missing, sells)[1]
    return needed


def cash_after_commitments(engine, account, project_reserve, working_reserve=0):
    """Прежний ``V3.cash_after_commitments`` поверх ядра дивидендов.

    Повторно не переносится: ``dividend_kernel.cash_after_commitments`` уже
    держит ``max(protected, project+working)`` и ``max(0, balance-reserved-hold)``.
    """
    a = engine.accounts[account]
    return _dividend.cash_after_commitments(
        engine.cash_store.get_balance(account),
        engine.cash_store.get_reserved_cash(account),
        a.get('protected', 0), project_reserve, working_reserve)


def cash_after_commitments_python(engine, account, project_reserve,
                                  working_reserve=0):
    """Эталон ``V3.cash_after_commitments``: буквальный прежний код."""
    a = engine.accounts[account]
    hold = max(a.get('protected', 0), project_reserve+working_reserve)
    return max(0, a['balance']-a.get('reserved', 0)-hold)


# ---------------------------------------------------------------------------
# Труд ускоренных строек
# ---------------------------------------------------------------------------

def labor_rows(engine):
    """Труд по (государство, провинция) в прежнем порядке обхода.

    Прежний код для каждой ускоренной стройки заново обходил все счета и
    суммировал труд совпавшей провинции. Здесь тот же обход выполнен один раз,
    а суммы по ключу идут в порядке появления ключа, поэтому результат
    совпадает побитово с суммой по тому же списку счетов.
    """
    rows = []
    for account in engine.accounts.values():
        if account['kind'] == 'household':
            rows.append(((account['state_id'], account['province_id']),
                         engine.population(account)))
    return rows


def reserve_expedited_construction_labor(engine):
    """Единственный вход: труд, ядро и запись двух полей движка."""
    accounts = sorted(engine.accounts.values(), key=lambda a: a['id'])
    labor = run_labor(labor_rows(engine), engine.cfg['labor_share'])
    reservations, workers_by_project = run_expedited(
        accounts, labor, engine.cfg['wage'], engine.cfg['construction_modes'],
        engine.free_cash, _EPS)
    engine._expedited_construction_reservations = reservations
    engine._expedited_construction_workers = workers_by_project


def reserve_expedited_construction_labor_python(engine):
    """Эталон ``V3.reserve_expedited_construction_labor``: буквальный код."""
    from collections import defaultdict
    reservations, workers_by_project = defaultdict(float), {}
    for account in sorted(engine.accounts.values(), key=lambda a: a['id']):
        for project in account.get('public_projects', []):
            if project['mode'] != 'accelerated':
                continue
            ready = (all(project['materials'].get(g, 0)+_EPS >= q
                         for g, q in project['material_bill'].items())
                and all(project['equipment_held'].get(g, 0)+_EPS >= q
                        for g, q in project['equipment'].items()))
            if not ready:
                continue
            mode = engine.cfg['construction_modes']['accelerated']
            loc = project['location']
            key = (loc['state_id'], loc['province_id'])
            available = sum(engine.population(a)*engine.cfg['labor_share']
                for a in engine.accounts.values()
                if a['kind'] == 'household'
                and (a['state_id'], a['province_id']) == key)-reservations[key]
            support_limit = min((project['support_stock'].get(g, 0)/per_worker
                for g, per_worker in mode['support_goods'].items()
                if per_worker > _EPS), default=float('inf'))
            cost_per_worker = engine.cfg['wage']*mode['wage_multiplier']+mode['lodging_allowance']
            workers = min(project['max_workers'],
                project['work_required']-project['work_done'],
                max(0, available), support_limit,
                engine.free_cash(account['id'])/cost_per_worker)
            if workers > _EPS:
                reservations[key] += workers
                workers_by_project[project['id']] = workers
    engine._expedited_construction_reservations = reservations
    engine._expedited_construction_workers = workers_by_project


# ---------------------------------------------------------------------------
# Распределение доходов и settle
# ---------------------------------------------------------------------------

def distribute(engine, sender, recipients, amount, reason):
    """Единственный вход из ``EconomyServices.distribute``: ядро и transfer."""
    return run_distribute(MoneyHooks(engine), sender, recipients, amount, reason)


def distribute_python(engine, sender, recipients, amount, reason):
    """Эталон ``EconomyServices.distribute``: буквальный прежний код."""
    total = sum(engine.population(a) for a in recipients)
    if total:
        amount = min(amount, engine.accounts[sender]['balance'])
        for a in recipients:
            engine.transfer(sender, a['id'], amount*engine.population(a)/total, reason)


def settle(engine, enterprises, households):
    """Единственный вход для второго цикла ``V3.settle``: carrier/treasury.

    Первый цикл (оброк, expenses, profit, месяцы убыточности) уже выполняет
    ``enterprise_settlement_boundary`` и здесь не повторяется. Перемешивание
    счетов остаётся в Python: это RNG месяца.
    """
    accounts = engine.shuffled(engine.accounts.values())
    return run_settle(MoneyHooks(engine), accounts, households,
                      {'carrier': engine.cfg['carrier_distribution'],
                       'treasury': engine.cfg['public_spending']}, _EPS)


def settle_python(engine, enterprises, households):
    """Эталон второго цикла ``V3.settle``: буквальный прежний код."""
    for a in engine.shuffled(engine.accounts.values()):
        rate = {'carrier': engine.cfg['carrier_distribution'],
                'treasury': engine.cfg['public_spending']}.get(a['kind'], 0)
        if rate:
            recipients = [h for h in households if h['state_id'] == a['state_id']]
            engine.distribute(a['id'], recipients,
                min(a['balance']*rate, engine.free_cash(a['id'])),
                'Оплата общественных услуг' if a['kind'] == 'treasury'
                else 'Оплата транспортных услуг населению')


def load_money_kernel():
    """Вернуть native-модуль money_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.money_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


# --- Python-эквиваленты ядра с теми же сигнатурами -------------------------

def _project_reserve_python(working_cash, goods, quantities, weights, distance,
                            transport_rate, delivery_fallback, stock_hook, orders,
                            participant, purpose, quote_hook, eps):
    """Python-эквивалент ``project_cash_reserve``."""
    if distance is not None:
        delivery = (sum(quantities[index]*weights[index]
                        for index in range(len(goods)))
                    * distance*transport_rate)
    else:
        delivery = delivery_fallback
    needed = working_cash+delivery
    for index, good in enumerate(goods):
        ordered = sum(o['remaining'] for o in orders
            if o['participant'] == participant and o['side'] == 'buy'
            and o['purpose'] == purpose and o['good'] == good
            and o['status'] not in ('cancelled', 'expired') and o['reserved'] > eps)
        missing = max(0, quantities[index]-stock_hook(good)-ordered)
        needed += missing*quote_hook(good, missing)[1]
    return needed


def _labor_python(rows, labor_share):
    """Python-эквивалент ``household_labor``: прежний встроенный ``sum``."""
    grouped = {}
    order = []
    for key, population in rows:
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(population*labor_share)
    return {key: sum(grouped[key]) for key in order}


def _expedited_python(accounts, labor, wage, modes, free_cash_hook, eps):
    """Python-эквивалент ``expedited_labor``."""
    reservations, workers_by_project = {}, {}

    def ready(project):
        return (all(project['materials'].get(g, 0)+eps >= q
                    for g, q in project['material_bill'].items())
                and all(project['equipment_held'].get(g, 0)+eps >= q
                        for g, q in project['equipment'].items()))

    for account in accounts:
        for project in account.get('public_projects', []):
            if project['mode'] != 'accelerated':
                continue
            if not ready(project):
                continue
            mode = modes['accelerated']
            loc = project['location']
            key = (loc['state_id'], loc['province_id'])
            available = labor.get(key, 0)-reservations.get(key, 0.0)
            support_limit = min((project['support_stock'].get(g, 0)/per_worker
                for g, per_worker in mode['support_goods'].items()
                if per_worker > eps), default=float('inf'))
            cost_per_worker = wage*mode['wage_multiplier']+mode['lodging_allowance']
            workers = min(project['max_workers'],
                project['work_required']-project['work_done'],
                max(0, available), support_limit,
                free_cash_hook(account['id'])/cost_per_worker)
            if workers > eps:
                reservations[key] = reservations.get(key, 0.0)+workers
                workers_by_project[project['id']] = workers
    return reservations, workers_by_project


def _distribute_python(hooks, sender, recipients, amount, reason):
    """Python-эквивалент ``distribute``."""
    total = sum(hooks.population(a) for a in recipients)
    if total:
        amount = min(amount, hooks.balance(sender))
        for a in recipients:
            hooks.transfer(sender, a['id'], amount*hooks.population(a)/total, reason)
    return None


def _settle_python(hooks, accounts, households, rates, eps):
    """Python-эквивалент ``settle_distributions``."""
    for account in accounts:
        rate = rates.get(account['kind'], 0)
        if rate:
            recipients = [h for h in households if h['state_id'] == account['state_id']]
            amount = min(hooks.balance(account['id'])*rate,
                         hooks.free_cash(account['id']))
            reason = ('Оплата общественных услуг' if account['kind'] == 'treasury'
                      else 'Оплата транспортных услуг населению')
            hooks.distribute(account['id'], recipients, amount, reason)
    return None


NATIVE_KERNEL = False

_kernel = load_money_kernel()
if _kernel is not None:
    run_project_reserve = _kernel.project_cash_reserve
    run_labor = _kernel.household_labor
    run_expedited = _kernel.expedited_labor
    run_distribute = _kernel.distribute
    run_settle = _kernel.settle_distributions
    NATIVE_KERNEL = True
else:
    run_project_reserve = _project_reserve_python
    run_labor = _labor_python
    run_expedited = _expedited_python
    run_distribute = _distribute_python
    run_settle = _settle_python
