"""Граница owner consumption: один вызов из V3.step, эталон и fallback.

В Cython уходит числовой срез ``consume_owner_purchases``: обход строк
``report['owner_consumption']``, обход книги заявок по каждой строке, расчёт
``filled``/``quantity``, три накопления отчёта и итоги строки со зеркалом на
счёте владельца.

Что остаётся Python-хуком: списание со склада. Ядро вызывает
``hooks.use_stock``, а хук идёт прежним ``engine.use_stock``, который
делегирует в stock boundary. Второй копии склада здесь нет. ``copy.deepcopy``
для зеркала ``owner_consumed`` ядро вызывает там же, где это делал прежний код.

Порядок обхода сохраняется буквально: строки идут в переданном порядке
(``report['owner_consumption']`` заполняет ``owner_requests``), внутри строки
книга заявок обходится целиком и фильтруется по ``row['orders']``. RNG
граница не трогает.

Эталонная Python-ветка (``run_rows_python``) повторяет прежнее тело
``Economy.consume_owner_purchases`` и служит образцом для focused-теста на
побитовое совпадение с ядром, а также fallback, если расширение не собрано.
Сигнатура у обеих веток одна: ``(engine, hooks, eps)``.
"""
import copy
import importlib


class OwnerConsumptionHooks(object):
    """Побочный эффект owner consumption, который обязан остаться в Python.

    Хук здесь один — склад. Он не считает норму выкупа, а только списывает уже
    посчитанное количество через существующую stock boundary движка.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    def use_stock(self, account, good, quantity):
        """Списать выкупленное у владельца — прежним ``engine.use_stock``."""
        self.engine.use_stock(account, good, quantity)


def run_rows_python(engine, hooks, eps):
    """Эталон числового цикла — буквальный прежний код V3."""
    for row in engine.report['owner_consumption']:
        account = engine.accounts[row['owner']]
        for order in engine.report['orders']:
            if order['id'] not in row['orders']:
                continue
            filled = order['quantity']-order['remaining']
            quantity = max(0, filled-order.get('consumed_quantity', 0))
            if quantity > eps:
                hooks.use_stock(account['id'], order['good'], quantity)
                order['consumed_quantity'] = filled
                good = order['good']
                row['consumed'][good] = row['consumed'].get(good, 0)+quantity
                engine.report['owner_consumed'][good] = engine.report['owner_consumed'].get(good, 0)+quantity
                engine.report['consumed'][good] = engine.report['consumed'].get(good, 0)+quantity
        trades = [t for t in engine.report['trades'] if t['buy_order'] in row['orders']]
        row.update(spent=sum(t['gold']+t['freight'] for t in trades),
            tax=sum(t['tax'] for t in trades), freight=sum(t['freight'] for t in trades),
            unfilled={o['good']: o['remaining'] for o in engine.report['orders'] if o['id'] in row['orders']})
        row['unused_budget'] = max(0, row['budget']-row['spent'])
        account.update(owner_consumption_spent=row['spent'], owner_consumed=copy.deepcopy(row['consumed']),
            owner_consumption_tax=row['tax'], owner_consumption_freight=row['freight'])


def consume_owner_purchases_python(engine, eps):
    """Эталонная Python-ветка целиком: тот же цикл и тот же хук склада."""
    run_rows_python(engine, OwnerConsumptionHooks(engine), eps)


def consume_owner_purchases(engine, eps):
    """Единственный вход из V3.step: native-цикл или эталонный fallback."""
    run_rows(engine, OwnerConsumptionHooks(engine), eps)


def load_owner_consumption_kernel():
    """Вернуть native-модуль owner_consumption_kernel или None, если не собран.

    Отсутствие собранного расширения даёт ImportError, а не
    ModuleNotFoundError: пакет ``modules.economy.native`` существует, а
    подмодуля нет. Обе ситуации означают одно — ядро не собрано.
    """
    try:
        return importlib.import_module('.owner_consumption_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_owner_consumption_kernel()
if _kernel is not None:
    run_rows = _kernel.run
    NATIVE_KERNEL = True
else:
    run_rows = run_rows_python
