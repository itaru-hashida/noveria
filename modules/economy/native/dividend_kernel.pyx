# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Числовой срез ``pay_dividends``: резерв, доступная касса, предел и доли.

Ядро считает только числа и порядок обхода. Деньги оно не двигает: каждая
выплата уходит через ``hooks.transfer``, а тот вызывает прежний
``Economy.transfer``, поэтому единственный денежный ledger остаётся
``cash_boundary``/``cash_store``. Новых счетов, колонок и кэшей здесь нет.

Арифметика — ``object``-арифметика, без ``double`` и без округления. Это
требование точности: ``capacity``, ``labor``, ``wage``, ``profit`` и
``available`` бывают и ``int``, и ``float``, а ``min``/``max`` возвращают один
из аргументов вместе с его типом. Любое приведение к ``double`` сдвинуло бы
последний разряд и вместе с ним порядок сделок и RNG.

Порядок операций сохранён дословно, потому что он наблюдаем:

  * ``needed = quantity*labor*wage`` — слева направо, как в прежнем коде;
  * входы и оборудование обходятся в порядке словарей рецепта;
  * ``hold = max(protected, project+working)``, затем
    ``max(0, balance-reserved-hold)``;
  * ``limit = share*max(0, profit)``, ``amount = min(limit, available)``;
  * доли — ``amount*population(a)/total``: умножение, затем деление, без
    предварительного приведения;
  * short-circuit сохранён: закрытое предприятие не читает вид владельца, а
    нулевое население получателей не доходит до расчёта резерва.

Python-хуками остаются ``transfer``, отчёт, события, RNG, dirty-флаги и любые
внешние side effects. Ядро возвращает только числовые записи, а строки отчёта
строит граница.
"""


def household_groups(accounts):
    """Сгруппировать домохозяйства по государству в порядке словаря счетов.

    Прежний код собирал этот словарь полным перебором ``accounts.values()``.
    Порядок вставки сохранён, поэтому список получателей, а значит и порядок
    начисления, совпадает с прежним.
    """
    cdef dict groups = {}
    cdef object account
    for account in accounts.values():
        if account['kind'] == 'household':
            groups.setdefault(account['state_id'], []).append(account)
    return groups


def working_reserve(hooks, enterprise):
    """Рабочий резерв предприятия: тот же порядок сложения, что и раньше.

    ``quantity*labor*wage`` — одно умножение слева направо, затем по каждому
    входу и по каждому требованию оборудования прибавляется
    ``missing*quoted_cost``. Котировку, склад и состав оборудования считает
    движок через хуки: это чтение книги офферов, а не числовая часть.
    """
    cdef object recipe = hooks.recipe(enterprise['good'])
    cdef object quantity = enterprise['capacity']*hooks.cfg('working_reserve_months')
    cdef object needed = quantity*recipe['labor']*hooks.cfg('wage')
    cdef object good
    cdef object ratio
    cdef object required
    cdef object missing
    cdef object account = hooks.account(enterprise['id'])
    cdef dict inputs = recipe['inputs']
    for good, ratio in inputs.items():
        missing = max(0, quantity*ratio-hooks.free_stock(enterprise['id'], good))
        needed = needed+missing*hooks.quoted_cost(account, good, missing)
    cdef dict equipment = hooks.equipment(recipe, quantity)
    for good, required in equipment.items():
        missing = max(0, required-hooks.free_stock(enterprise['id'], good))
        needed = needed+missing*hooks.quoted_cost(account, good, missing)
    return needed


def cash_after_commitments(balance, reserved, protected, project_reserve, working_reserve):
    """Свободная касса после обязательств: два ``max`` в прежнем порядке.

    ``protected`` уже содержит часть обязательств, поэтому повторно она не
    вычитается: берётся больший из ``protected`` и суммы резервов.
    """
    cdef object hold = max(protected, project_reserve+working_reserve)
    return max(0, balance-reserved-hold)


def pay_dividends(hooks):
    """Обойти предприятия и вернуть числовые записи в прежнем порядке.

    Ядро ничего не платит само: выплата — это ``hooks.transfer``, а строки
    отчёта строит ``hooks.record``. Запись возвращается для каждого
    предприятия, дошедшего до выплаты, включая нулевую: прежний код писал
    строку отчёта и при ``amount == 0``.
    """
    cdef object month = hooks.month()
    cdef object revision = hooks.revision()
    cdef object owners = hooks.owner_kinds()
    cdef dict groups = household_groups(hooks.accounts())
    cdef list records = []
    cdef object enterprise
    cdef object sender
    cdef object owner_id
    cdef object owner_kind
    cdef object recipients
    cdef object account
    cdef object population
    cdef object total
    cdef object working
    cdef object project
    cdef object available
    cdef object share
    cdef object limit
    cdef object amount
    cdef object paid
    cdef object reason
    for enterprise in hooks.enterprises():
        if enterprise.get('dividend_month') == month:
            continue
        enterprise.update(dividends=0.0, dividend_month=month)
        # Short-circuit как в прежнем коде: закрытое предприятие вид владельца
        # не читает вовсе.
        if enterprise['status'] == 'closed':
            continue
        sender = enterprise['id']
        owner_id = enterprise['owner_id']
        owner_kind = hooks.owner_kind(owner_id)
        if owner_kind not in owners:
            continue
        recipients = groups.get(enterprise['state_id'], []) if revision else []
        if revision:
            # Тот же порядок и те же значения, что в прежнем
            # sum(self.population(a) for a in recipients).
            total = 0
            for account in recipients:
                total = total+hooks.population(account)
            if not total:
                continue
        else:
            total = 0
        working = working_reserve(hooks, enterprise)
        project = hooks.project_reserve(sender)
        available = cash_after_commitments(hooks.balance(sender),
            hooks.reserved(sender), hooks.protected(sender), project, working)
        share = hooks.share(revision)
        limit = share*max(0, enterprise['profit'])
        amount = min(limit, available)
        if revision:
            # Прежний distribute: отсечение по живому балансу, затем доли.
            paid = min(amount, hooks.balance(sender))
        else:
            paid = amount
        reason = hooks.reason(revision)
        if revision:
            for account in recipients:
                hooks.transfer(sender, account['id'],
                    paid*hooks.population(account)/total, reason)
        else:
            hooks.transfer(sender, owner_id, paid, reason)
        hooks.book(sender, working, project)
        enterprise.update(dividends=amount, working_reserve=working,
            project_reserve=project, dividend_available_cash=available)
        records.append(hooks.record(sender, owner_id, owner_kind, enterprise['profit'],
            amount, limit, available, working, project, revision))
    return records
