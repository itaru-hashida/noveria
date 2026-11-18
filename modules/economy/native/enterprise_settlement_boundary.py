"""Граница settlement-цикла предприятий: одна привязка реализации на импорт.

В Cython уходит только числовой цикл ``Economy.settle`` по предприятиям:
оброк (``fixed_cost_due``/``fixed_cost_paid``), расходы, прибыль и месяцы
убыточности/прибыльности. Деньги двигает прежний ``Economy.transfer`` через
хук ``pay_overhead``, закрытие и возврат капитала — прежние методы движка,
поэтому второй cash ledger не заводится, ``balance`` напрямую не меняется.

Что осталось в Python и почему:

  * список ``enterprises`` и второй цикл (carrier/treasury distribute)
    остаются в ``v3.settle``: порядок и RNG не переезжают;
  * расчёт оброка (``enterprise_overhead``) — хук ``overhead``: это политика
    движка, ядро лишь получает готовое ``due``;
  * перевод оброка, возврат капитала и закрытие предприятия — хуки: отчёты,
    события и заявки остаются за движком;
  * выбор предприятий и порядок обхода — вне среза вовсе.

Арифметика намеренно ``object``: ``cost_of_sales``/``revenue``/``tax`` бывают
и int, и float, и тип результата виден дальше в отчёте. Cython порождает те
же ``PyNumber_*``, что и интерпретатор, поэтому значения и типы совпадают
побитово. После оплаты Python готовит неизменяемый tuple входов, ядро
возвращает tuple результата, а запись владельца выполняется хуками. Порядок
ключей ``e.update(...)`` и порядок присваиваний сохранены буквально: dict
помнит его, и сравнение отчётов построчно не разойдётся.

Мульти-store money/lifecycle операции намеренно не входят в этот срез:
``pay_overhead`` по-прежнему вызывает ``Economy.transfer`` до числовых
записей, а ``return_capital`` и ``close_enterprise`` по-прежнему вызывают
движок после них. Эти хуки владеют существующими cash/order/report stores;
ядро не создаёт и не обновляет их копии.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец
для focused-теста на побитовое совпадение и как fallback, если расширение
не собрано. Имена привязываются один раз при импорте, чтобы не платить за
диспетчер на каждом шаге.
"""

import copy
import importlib

# EPS проекта. Импорт из support безопасен: support не импортирует эту
# границу, поэтому цикла support -> boundary -> support не возникает.
from ..support import EPS

_MISSING = object()


class EnterpriseSettlementHooks(object):
    """Side effects settlement-цикла: единственные точки мутаций вне чисел.

    Хуки намеренно узкие и повторяют прежний порядок записи. Ни один из них
    не считает числа: ядро передаёт уже посчитанные значения, а граница лишь
    фиксирует их там, где это делал прежний код.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    # --- чтение, которое ядру нужно для чисел -----------------------------
    def overhead(self, e):
        """Оброк месяца: прежняя политика движка, в том числе 0.0 у closed."""
        engine = self.engine
        return engine.enterprise_overhead(e) if e['status'] != 'closed' else 0.0

    def free_cash(self, account_id):
        return self.engine.free_cash(account_id)

    # --- деньги и lifecycle: прежние методы движка -------------------------
    def pay_overhead(self, e, paid):
        self.engine.transfer(e['id'], 'treasury:'+e['state_id'], paid,
            'Оброк за землю и мастерскую')

    # --- prepared inputs / owner writes -----------------------------------
    def prepare_financial_inputs(self, e, due, paid):
        return prepare_financial_inputs(e, due, paid)

    def prepare_unviable_inputs(self, e, month, loss_limit):
        return prepare_unviable_inputs(e, month, loss_limit)

    def apply_financial_results(self, e, result):
        apply_financial_results(e, result)

    def apply_unviable_months(self, e, result):
        apply_unviable_months(e, result)

    def return_capital(self, e):
        self.engine.return_capital(e)

    def close_enterprise(self, e):
        self.engine.close_enterprise(e)


class _SettlementTransaction(object):
    """Checkpoint only the owners touched by enterprise settlement.

    This deliberately does not use ``export_state``: that API replaces the
    live account/enterprise maps and does not include the runtime store
    columns or the report object.  Settlement has a small, explicit owner map
    instead, so rollback writes back through CashStore and OrderStore.
    """

    def __init__(self, engine, enterprises):
        self.engine = engine
        self.enterprises = [(e, copy.deepcopy(e)) for e in enterprises]
        self.account_ids = {e['id'] for e in enterprises}
        self.account_ids.update('treasury:'+e['state_id'] for e in enterprises)
        self.account_ids.update(e.get('owner_id') for e in enterprises
                                if e.get('owner_id') is not None)
        self.accounts = {}
        if hasattr(engine, 'accounts'):
            for account_id in self.account_ids:
                if account_id in engine.accounts:
                    account = engine.accounts[account_id]
                    cash = engine.cash_store
                    self.accounts[account_id] = {
                        'balance': cash.get_balance(account_id),
                        'reserved': cash.get_reserved_cash(account_id),
                        'protected': cash.get_protected_cash(account_id),
                        'project': (copy.deepcopy(account['project'])
                                    if 'project' in account else _MISSING),
                    }
        report = getattr(engine, 'report', None)
        self.report = report
        self.flow_count = len(report.get('flows', ())) if report else 0
        self.lifecycle_count = len(report.get('lifecycle', ())) if report else 0
        self.orders = {}
        if report:
            for order in report.get('orders', ()):
                if order.get('participant') in self.account_ids:
                    self.orders[id(order)] = (order, copy.deepcopy(order))
        self.demand_revision = copy.deepcopy(
            getattr(engine, '_demand_revision', None))
        self.has_dirty = hasattr(engine, '_investment_dirty_goods')
        self.dirty = (copy.deepcopy(engine._investment_dirty_goods)
                      if self.has_dirty else None)

    def rollback(self):
        engine = self.engine
        cash = getattr(engine, 'cash_store', None)
        for account_id, fields in self.accounts.items():
            account = engine.accounts[account_id]
            for field, previous in fields.items():
                if previous is _MISSING:
                    account.pop(field, None)
                elif field == 'balance':
                    cash.set_balance(account_id, previous)
                elif field == 'reserved':
                    cash.set_reserved_cash(account_id, previous)
                elif field == 'protected':
                    cash.set_protected_cash(account_id, previous)
                elif field == 'project':
                    if previous is _MISSING:
                        account.pop(field, None)
                    else:
                        account[field] = copy.deepcopy(previous)
        for e, previous in self.enterprises:
            e.clear()
            e.update(previous)
        for order, previous in self.orders.values():
            order.clear()
            order.update(previous)
            if hasattr(engine, 'order_store'):
                engine.order_store.set_reserved(order, order.get('reserved', 0))
                engine.order_store.set_status(order, order.get('status'))
        if self.report is not None:
            del self.report['flows'][self.flow_count:]
            del self.report['lifecycle'][self.lifecycle_count:]
        if self.demand_revision is not None:
            engine._demand_revision.clear()
            engine._demand_revision.update(self.demand_revision)
        if self.has_dirty:
            engine._investment_dirty_goods = self.dirty
        else:
            engine.__dict__.pop('_investment_dirty_goods', None)


def prepare_financial_inputs(e, due, paid):
    """Read owner state once for the pure financial-result calculation.

    The tuple is deliberately data-only. In particular, no live enterprise
    dict is handed to Cython for it to mutate.
    """
    return (e['cost_of_sales'], due, paid, e['revenue'], e['tax'],
        e['loss_months'], e['profitable_months'], e['status'] == 'closed')


def prepare_unviable_inputs(e, month, loss_limit):
    """Preserve old short-circuit reads for the active lifecycle counter."""
    if e['loss_months'] <= 0:
        return (0, False, loss_limit)
    if e['available_from'] > month:
        return (0, False, loss_limit)
    return (e.get('unviable_months', 0), True, loss_limit)


def calculate_financial_results_python(prepared, eps):
    """Pure Python reference for the Cython financial-result calculation."""
    (cost_of_sales, due, paid, revenue, tax, loss_months,
     profitable_months, closed) = prepared
    expenses = cost_of_sales+due
    profit = revenue-tax-expenses
    loss_months = loss_months+1 if profit < -eps else 0
    profitable_months = profitable_months+1 if profit > eps else 0
    return (due, paid, expenses, profit, loss_months, profitable_months,
        closed)


def calculate_unviable_months_python(prepared):
    previous_unviable, unviable, loss_limit = prepared
    unviable_months = previous_unviable+1 if unviable else 0
    return (unviable_months, unviable_months >= loss_limit)


def apply_financial_results(e, result):
    """Apply the ordered fields written before the lifecycle branch."""
    due, paid, expenses, profit, loss_months, profitable_months = result[:6]
    e.update(fixed_cost_due=due, fixed_cost_paid=paid)
    e['expenses'] = expenses
    e['profit'] = profit
    e['loss_months'] = loss_months
    e['profitable_months'] = profitable_months


def apply_unviable_months(e, result):
    """Apply the active-enterprise counter after the closed branch point."""
    e['unviable_months'] = result[0]


def _run_settle_python(engine, enterprises, hooks, eps):
    """Эталон числового цикла settle — буквальный прежний код V3."""
    month = engine.meta['month']
    loss_limit = engine.cfg['loss_months_to_close']
    for e in enterprises:
        due = hooks.overhead(e)
        paid = min(due, hooks.free_cash(e['id']))
        if paid > eps:
            hooks.pay_overhead(e, paid)
        prepared = hooks.prepare_financial_inputs(e, due, paid)
        result = calculate_financial_results_python(prepared, eps)
        hooks.apply_financial_results(e, result)
        if result[6]:
            hooks.return_capital(e)
            continue
        unviable = hooks.prepare_unviable_inputs(e, month, loss_limit)
        unviable_result = calculate_unviable_months_python(unviable)
        hooks.apply_unviable_months(e, unviable_result)
        if unviable_result[1]:
            hooks.close_enterprise(e)
    return None


def _run_settle(engine, enterprises, hooks, eps):
    """Run the old settlement traversal in Python around pure calculations.

    The list order, owner reads/writes, money calls, and lifecycle short
    circuits are deliberately visible here.  The native extension is used
    only for the two data-only calculations below.
    """
    month = engine.meta['month']
    loss_limit = engine.cfg['loss_months_to_close']
    for e in enterprises:
        due = hooks.overhead(e)
        paid = min(due, hooks.free_cash(e['id']))
        if paid > eps:
            hooks.pay_overhead(e, paid)
        prepared = hooks.prepare_financial_inputs(e, due, paid)
        result = calculate_financial_results(prepared, eps)
        hooks.apply_financial_results(e, result)
        if result[6]:
            hooks.return_capital(e)
            continue
        unviable = hooks.prepare_unviable_inputs(e, month, loss_limit)
        unviable_result = calculate_unviable_months(unviable)
        hooks.apply_unviable_months(e, unviable_result)
        if unviable_result[1]:
            hooks.close_enterprise(e)
    return None


def load_enterprise_settlement_kernel():
    """Вернуть native-модуль enterprise_settlement_kernel или None.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.enterprise_settlement_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_enterprise_settlement_kernel()
if _kernel is not None:
    NATIVE_KERNEL = True

calculate_financial_results = (getattr(_kernel, 'calculate_financial_results',
    calculate_financial_results_python) if _kernel is not None
    else calculate_financial_results_python)
calculate_unviable_months = (getattr(_kernel, 'calculate_unviable_months',
    calculate_unviable_months_python) if _kernel is not None
    else calculate_unviable_months_python)


def _atomic_run(worker, engine, enterprises, hooks, eps):
    transaction = _SettlementTransaction(engine, enterprises)
    try:
        return worker(engine, enterprises, hooks, eps)
    except Exception:
        transaction.rollback()
        raise


def run_settle_python(engine, enterprises, hooks, eps):
    """Reference settlement traversal with the same atomic owner boundary."""
    return _atomic_run(_run_settle_python, engine, enterprises, hooks, eps)


def run_settle(engine, enterprises, hooks, eps):
    """Native numeric traversal with atomic owner application."""
    return _atomic_run(_run_settle, engine, enterprises, hooks, eps)


def settle(engine, enterprises):
    """Выполнить только расчёты с предприятиями."""
    run_settle(engine, enterprises, EnterpriseSettlementHooks(engine), EPS)


def settle_month(engine, enterprises, households):
    """Применить расчёты с предприятиями и распределение доходов вместе."""
    from . import money_boundary

    transaction = _SettlementTransaction(engine, enterprises)
    cash = engine.cash_store
    balances = {account: cash.get_balance(account) for account in engine.accounts
                if account not in transaction.accounts}
    rng_state = engine.rng.getstate()
    try:
        _run_settle(engine, enterprises, EnterpriseSettlementHooks(engine), EPS)
        money_boundary.settle(engine, enterprises, households)
    except Exception:
        transaction.rollback()
        for account, balance in balances.items():
            if cash.get_balance(account) != balance:
                cash.set_balance(account, balance)
        engine.rng.setstate(rng_state)
        raise
