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
побитово. Порядок ключей ``e.update(...)`` и порядок присваиваний сохранены
буквально: dict помнит его, и сравнение отчётов построчно не разойдётся.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец
для focused-теста на побитовое совпадение и как fallback, если расширение
не собрано. Имена привязываются один раз при импорте, чтобы не платить за
диспетчер на каждом шаге.
"""

import importlib

# EPS проекта. Импорт из support безопасен: support не импортирует эту
# границу, поэтому цикла support -> boundary -> support не возникает.
from ..support import EPS


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

    def return_capital(self, e):
        self.engine.return_capital(e)

    def close_enterprise(self, e):
        self.engine.close_enterprise(e)


def run_settle_python(engine, enterprises, hooks, eps):
    """Эталон числового цикла settle — буквальный прежний код V3."""
    month = engine.meta['month']
    loss_limit = engine.cfg['loss_months_to_close']
    for e in enterprises:
        due = hooks.overhead(e)
        paid = min(due, hooks.free_cash(e['id']))
        if paid > eps:
            hooks.pay_overhead(e, paid)
        e.update(fixed_cost_due=due, fixed_cost_paid=paid)
        e['expenses'] = e['cost_of_sales']+due
        e['profit'] = e['revenue']-e['tax']-e['expenses']
        e['loss_months'] = e['loss_months']+1 if e['profit'] < -eps else 0
        e['profitable_months'] = e['profitable_months']+1 if e['profit'] > eps else 0
        if e['status'] == 'closed':
            hooks.return_capital(e)
            continue
        unviable = e['loss_months'] > 0 and e['available_from'] <= month
        e['unviable_months'] = e.get('unviable_months', 0)+1 if unviable else 0
        if e['unviable_months'] >= loss_limit:
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
    run_settle = _kernel.run_settle
    NATIVE_KERNEL = True
else:
    run_settle = run_settle_python


def settle(engine, enterprises):
    """Единственная точка входа из ``Economy.settle``: ядро поверх хуков.

    ``enterprises`` — уже готовый список из ``v3.settle``: порядок и RNG
    остаются там. Второй цикл (carrier/treasury distribute) границу не зовёт.
    """
    run_settle(engine, enterprises, EnterpriseSettlementHooks(engine), EPS)
