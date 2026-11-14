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

# Допуск остатка. Переопределяется configure() из support.py, чтобы не
# дублировать константу проекта.
_EPS = 1e-7


def configure(eps):
    """Задать EPS проекта для native-границы и Python-эталона."""
    global _EPS
    _EPS = eps
    if _store is not None:
        _store.set_epsilon(eps)


class OrderStorePython:
    """Эталон границы: колонки полей над теми же живыми объектами заявок."""

    def __init__(self, orders):
        self.orders = orders
        # Номер раскладки: сдвиг слотов увеличивает его, и кэш в книге
        # пересчитывается (см. OfferViewPython._book_slots).
        self.version = 0
        self.remaining = [order['remaining'] for order in orders]
        self.reserved = [order.get('reserved', 0) for order in orders]
        self.status = [order['status'] for order in orders]
        self.price = [order.get('price', 0) for order in orders]
        self.index = {id(order): slot for slot, order in enumerate(orders)}

    def _slot(self, order):
        """Номер слота или -1; слот проверяется тождеством объекта.

        ``id`` переиспользуется после сборки мусора, поэтому одной записи в
        ``index`` мало: слот обязан указывать именно на эту заявку.
        """
        self.sync()
        slot = self.index.get(id(order), -1)
        if slot < 0 or slot >= len(self.orders):
            return -1
        if self.orders[slot] is not order:
            self.index.pop(id(order), None)
            return -1
        return slot

    def sync(self):
        """Выровнять колонки по списку заявок после внешнего append."""
        total, have = len(self.orders), len(self.remaining)
        if total == have:
            return
        if total < have:
            self.remaining = [o['remaining'] for o in self.orders]
            self.reserved = [o.get('reserved', 0) for o in self.orders]
            self.status = [o['status'] for o in self.orders]
            self.price = [o.get('price', 0) for o in self.orders]
            self.index = {id(o): i for i, o in enumerate(self.orders)}
            self.version += 1
            return
        for position in range(have, total):
            order = self.orders[position]
            self.remaining.append(order['remaining'])
            self.reserved.append(order.get('reserved', 0))
            self.status.append(order['status'])
            self.price.append(order.get('price', 0))
            self.index[id(order)] = position
        # Дописывание в хвост не сдвигает существующие номера: версия та же.

    def append(self, order):
        self.sync()
        slot = len(self.orders)
        self.orders.append(order)
        self.remaining.append(order['remaining'])
        self.reserved.append(order.get('reserved', 0))
        self.status.append(order['status'])
        self.price.append(order.get('price', 0))
        self.index[id(order)] = slot
        # Хвостовое дописывание не сдвигает номера: версия не меняется.

    def remove(self, order):
        slot = self._slot(order)
        if slot < 0:
            raise ValueError('Заявка не принадлежит хранилищу')
        self.orders.pop(slot)
        self.remaining.pop(slot)
        self.reserved.pop(slot)
        self.status.pop(slot)
        self.price.pop(slot)
        self.index.pop(id(order), None)
        for position in range(slot, len(self.orders)):
            self.index[id(self.orders[position])] = position
        self.version += 1

    def set_remaining(self, order, value):
        slot = self._slot(order)
        if slot >= 0:
            self.remaining[slot] = value
        order['remaining'] = value

    def set_reserved(self, order, value):
        slot = self._slot(order)
        if slot >= 0:
            self.reserved[slot] = value
        order['reserved'] = value

    def set_status(self, order, value):
        slot = self._slot(order)
        if slot >= 0:
            self.status[slot] = value
        order['status'] = value

    def set_price(self, order, value):
        slot = self._slot(order)
        if slot >= 0:
            self.price[slot] = value
        order['price'] = value

    def is_live(self, order, eps=_EPS):
        slot = self._slot(order)
        if slot < 0:
            return (order['remaining'] > eps
                    and order['status'] not in ('cancelled', 'expired'))
        return (self.remaining[slot] > eps
                and self.status[slot] not in ('cancelled', 'expired'))

    def iter_live(self):
        """Живая заявка вместе с номером её слота.

        Слот стабилен: дописывание в хвост номера не сдвигает, а ``remove``
        увеличивает ``version``, и тогда потребитель берёт слоты заново.
        """
        self.sync()
        return ((order, slot) for slot, order in enumerate(self.orders))

    def slots(self, orders):
        """Номера слотов для книги одним вызовом; -1, если заявки нет.

        Здесь же колонки догоняют словарь: нативные ветки ``update_prices_boundary``
        пишут ``remaining``/``status`` прямо в dict в обход границы, и без сверки
        представление увидело бы устаревшее значение. Сверяются только поля,
        которые читает представление; ``reserved``/``price`` обход не читает.

        Промах не сканирует список: заявка получает -1 и читается через свой
        словарь. Скан был бы квадратичным на снимках planning-книг.
        """
        self.sync()
        result = []
        total = len(self.orders)
        for order in orders:
            slot = self.index.get(id(order), -1)
            if slot < 0 or slot >= total or self.orders[slot] is not order:
                self.index.pop(id(order), None)
                result.append(-1)
                continue
            for field, column in (('remaining', self.remaining),
                                  ('status', self.status)):
                value = order[field]
                current = column[slot]
                # Сравнивается и тип: ``5`` и ``5.0`` равны по значению, но
                # встроенный sum в Python 3.12 компенсирует погрешность только
                # с float-стартом, поэтому подмена типа сдвинула бы отчёт.
                if current is not value and (current != value
                                             or type(current) is not type(value)):
                    column[slot] = value
            result.append(slot)
        return result

    def slot(self, order):
        """Номер слота одной заявки или -1."""
        return self._slot(order)

    def columns(self):
        self.sync()
        return dict(remaining=list(self.remaining), reserved=list(self.reserved),
                    status=list(self.status), price=list(self.price))

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
