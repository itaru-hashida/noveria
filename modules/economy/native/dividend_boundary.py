"""Подготовка условий выплаты и последовательное применение дивидендов."""

import importlib
import sys


class DividendHooks:
    """Доступ Python-границы к счетам, рыночным данным и отчёту."""

    def __init__(self, engine, sells):
        self.engine = engine
        self.sells = sells
        self._owner_kinds = None

    # --- чтение состояния -------------------------------------------------
    def cfg(self, key):
        return self.engine.cfg[key]

    def month(self):
        return self.engine.meta['month']

    def revision(self):
        return self.engine.PROFIT_DISTRIBUTION_REVISION

    def owner_kinds(self):
        # OWNER_KINDS — константа экономического модуля, а не поле движка.
        # Взять её из модуля класса недостаточно: подкласс (лабораторный
        # сценарий, тестовый LegacyOwnerEconomy) живёт в своём модуле и этой
        # константы не содержит. Поэтому ищем по MRO и, если её нет ни у одного
        # класса, падаем обратно на модуль v3, который импортирован всегда.
        if self._owner_kinds is None:
            kinds = None
            for cls in type(self.engine).__mro__:
                module = sys.modules.get(cls.__module__)
                kinds = getattr(module, 'OWNER_KINDS', None)
                if kinds is not None:
                    break
            if kinds is None:
                from .. import v3
                kinds = v3.OWNER_KINDS
            self._owner_kinds = kinds
        return self._owner_kinds

    def accounts(self):
        return self.engine.accounts

    def enterprises(self):
        return self.engine.enterprises.values()

    def population(self, account):
        return self.engine.population(account)

    def account(self, account_id):
        return self.engine.accounts[account_id]

    def owner_kind(self, account_id):
        return self.engine.accounts[account_id]['kind']

    def balance(self, account_id):
        # Чтение идёт через cash_store: колонки остаются источником истины, а
        # словарь счёта — их зеркало. Значение и тип те же, что в dict.
        return self.engine.cash_store.get_balance(account_id)

    def reserved(self, account_id):
        return self.engine.cash_store.get_reserved_cash(account_id)

    def protected(self, account_id):
        # Защита кассы — колонка того же владельца, что balance/reserved.
        return self.engine.cash_store.get_protected_cash(account_id)

    # --- товары, склад и котировки (остаются в Python) ---------------------
    def recipe(self, good):
        return self.engine.goods[good]

    def free_stock(self, account_id, good):
        return self.engine.free_stock(account_id, good)

    def quoted_cost(self, account, good, quantity):
        return self.engine.quoted_cost(account, good, quantity, self.sells)[1]

    def equipment(self, recipe, quantity):
        return self.engine.equipment_requirements(recipe, quantity)

    def project_reserve(self, account_id):
        return self.engine.project_cash_reserve(account_id, self.sells)

    def share(self, revision):
        if revision:
            return self.engine.cfg['business_income_share']
        return self.engine.cfg['dividend_profit_share']

    def reason(self, revision):
        if revision:
            return 'Доход населения от бизнеса'
        return 'Дивиденды'

    # --- side effects: единственные точки мутации -------------------------
    def transfer(self, sender, recipient, amount, reason):
        self.engine.transfer(sender, recipient, amount, reason)

    def book(self, account_id, working, project):
        self.engine.accounts[account_id].update(working_reserve=working,
            project_reserve=project)

    def record(self, enterprise_id, owner_id, owner_kind, profit, dividends,
               profit_limit, available, working, project, revision):
        """Строка отчёта: прежний dict плюс признак вида получателя."""
        row = dict(enterprise=enterprise_id, owner=owner_id, owner_kind=owner_kind,
            profit=profit, dividends=dividends, profit_limit=profit_limit,
            available_cash=available, working_reserve=working,
            project_reserve=project)
        row['recipient_kind'] = 'household' if revision else owner_kind
        self.engine.report['dividends'].append(row)
        self.engine.report['diagnostics'].append(dict(kind='dividend', **row))
        return row


def household_groups_python(accounts):
    """Эталон группировки: порядок словаря счетов сохранён."""
    groups = {}
    for account in accounts.values():
        if account['kind'] == 'household':
            groups.setdefault(account['state_id'], []).append(account)
    return groups


def working_reserve_python(hooks, enterprise):
    """Эталон рабочего резерва: тот же порядок умножений и сложений."""
    recipe = hooks.recipe(enterprise['good'])
    quantity = enterprise['capacity']*hooks.cfg('working_reserve_months')
    needed = quantity*recipe['labor']*hooks.cfg('wage')
    for good, ratio in recipe['inputs'].items():
        missing = max(0, quantity*ratio-hooks.free_stock(enterprise['id'], good))
        needed += missing*hooks.quoted_cost(hooks.account(enterprise['id']), good, missing)
    for good, required in hooks.equipment(recipe, quantity).items():
        missing = max(0, required-hooks.free_stock(enterprise['id'], good))
        needed += missing*hooks.quoted_cost(hooks.account(enterprise['id']), good, missing)
    return needed


def cash_after_commitments_python(balance, reserved, protected, project_reserve,
                                  working_reserve):
    """Эталон свободной кассы: ``max`` в том же порядке."""
    hold = max(protected, project_reserve+working_reserve)
    return max(0, balance-reserved-hold)


def calculate_dividend_python(profit, share, available):
    limit = share*max(0, profit)
    return limit, min(limit, available)


def _pay_dividends(hooks, group_accounts, available_cash, calculate):
    """Подготовить и провести выплаты последовательно по предприятиям."""
    month = hooks.month()
    revision = hooks.revision()
    owners = hooks.owner_kinds()
    groups = group_accounts(hooks.accounts())
    records = []
    for enterprise in hooks.enterprises():
        if enterprise.get('dividend_month') == month:
            continue
        enterprise.update(dividends=0.0, dividend_month=month)
        if enterprise['status'] == 'closed':
            continue
        sender = enterprise['id']
        owner_id = enterprise['owner_id']
        owner_kind = hooks.owner_kind(owner_id)
        if owner_kind not in owners:
            continue
        recipients = groups.get(enterprise['state_id'], []) if revision else []
        if revision:
            total = 0
            for account in recipients:
                total = total+hooks.population(account)
            if not total:
                continue
        else:
            total = 0
        working = working_reserve_python(hooks, enterprise)
        project = hooks.project_reserve(sender)
        available = available_cash(hooks.balance(sender),
            hooks.reserved(sender), hooks.protected(sender), project, working)
        share = hooks.share(revision)
        limit, amount = calculate(enterprise['profit'], share, available)
        if revision:
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


def load_dividend_kernel():
    """Вернуть native-модуль dividend_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.dividend_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


def pay_dividends_python(hooks):
    return _pay_dividends(hooks, household_groups_python,
                          cash_after_commitments_python, calculate_dividend_python)


def run_pay_dividends(hooks):
    return _pay_dividends(hooks, household_groups, cash_after_commitments,
                          calculate_dividend)

_kernel = load_dividend_kernel()
household_groups = getattr(_kernel, 'household_groups', household_groups_python)
working_reserve = working_reserve_python
cash_after_commitments = getattr(
    _kernel, 'cash_after_commitments', cash_after_commitments_python)
calculate_dividend = getattr(_kernel, 'calculate_dividend', calculate_dividend_python)
NATIVE_KERNEL = calculate_dividend is not calculate_dividend_python


def pay_dividends(engine, sells):
    """Единственная точка входа для выплаты дивидендов."""
    hooks = DividendHooks(engine, sells)
    return run_pay_dividends(hooks)
