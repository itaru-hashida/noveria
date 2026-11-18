"""Потребление владельцев: расчёт по заявке и применение в порядке книги."""
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


def calculate_consumption_python(ordered, remaining, consumed):
    filled = ordered-remaining
    return filled, max(0, filled-consumed)


def run_rows(engine, hooks, eps, calculate):
    """Обойти живую книгу и применить списание и итоги отчёта."""
    for row in engine.report['owner_consumption']:
        account = engine.accounts[row['owner']]
        for order in engine.report['orders']:
            if order['id'] not in row['orders']:
                continue
            filled, quantity = calculate(
                order['quantity'], order['remaining'], order.get('consumed_quantity', 0))
            if quantity > eps:
                hooks.use_stock(account['id'], order['good'], quantity)
                engine.order_store.set_metadata(order, 'consumed_quantity', filled)
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
    run_rows(engine, OwnerConsumptionHooks(engine), eps, calculate_consumption_python)


def consume_owner_purchases(engine, eps):
    """Единственный вход из V3.step."""
    run_rows(engine, OwnerConsumptionHooks(engine), eps, calculate_consumption)


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


_kernel = load_owner_consumption_kernel()
calculate_consumption = getattr(_kernel, 'calculate_consumption', calculate_consumption_python)
NATIVE_KERNEL = calculate_consumption is not calculate_consumption_python
