"""Граница мутаций заявок: одна привязка реализации на импорт.

Модуль не хранит состояние движка: ``OrderStore`` получает живой список
``report['orders']`` и меняет поля тех же словарей. Копий заявок нет ни здесь,
ни в extension, поэтому ``OfferView`` видит мутацию сразу.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста и как fallback, если расширение не собрано. Имена привязываются
один раз при импорте, чтобы не платить за диспетчер на каждой мутации.

Модуль не импортирует ``modules.economy.support``: у границы нет EPS и других
констант проекта, поэтому циклического импорта нет.
"""

import copy
import importlib


class OrderStorePython:
    """Эталон границы: те же операции над теми же живыми объектами заявок."""

    def __init__(self, orders):
        self.orders = orders

    def append(self, order):
        self.orders.append(order)

    def remove(self, order):
        self.orders.remove(order)

    def set_remaining(self, order, value):
        order['remaining'] = value

    def set_reserved(self, order, value):
        order['reserved'] = value

    def set_status(self, order, value):
        order['status'] = value

    def set_price(self, order, value):
        order['price'] = value

    def __reduce__(self):
        """deepcopy движка: новая граница вокруг скопированного списка."""
        return (type(self), (self.orders,))

    def __deepcopy__(self, memo):
        """Копия движка: граница над тем же скопированным списком orders."""
        return type(self)(copy.deepcopy(self.orders, memo))


def load_order_store():
    """Вернуть native-модуль order_store или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.order_store', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_ORDER_STORE = False

_store = load_order_store()
if _store is not None:
    OrderStore = _store.OrderStore
    NATIVE_ORDER_STORE = True
else:
    OrderStore = OrderStorePython
