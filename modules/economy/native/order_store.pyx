# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Единая граница мутаций полей заявок: словарь заявки остаётся источником истины.

Задача границы — не хранить заявки, а быть единственным местом, где меняются
``remaining``/``reserved``/``status``/``price``. Хранилище держит **тот же**
список ``report['orders']`` и те же объекты-словари: ни копии, ни второй
независимой модели заявки здесь нет. Поэтому книга офферов (``OfferView``),
которая читает живые словари заявок во время обхода, видит мутацию сразу:
изменение поля — это присваивание в тот же самый dict.

Все значения принимаются как ``object`` и записываются без приведения типа.
Это важно: ``remaining``/``reserved`` бывают и ``int``, и ``float``, а
арифметика вызывающего кода уже посчитала точное значение. Любое приведение
(например, ``double value``) поменяло бы тип и последний разряд, а порядок
сделок и RNG зависят от точного результата.

Порядок операций сохраняется вызывающим кодом: граница не переставляет
присваивания и не объединяет их, поэтому последовательность мутаций остаётся
буквально прежней.
"""

import copy


cdef class OrderStore:
    """Живой список заявок и операции над его полями.

    ``orders`` — исходный список движка (``report['orders']``), а не копия.
    ``append``/``remove`` меняют именно его, поэтому всё, что уже держит
    ссылку на список или на заявку, видит изменение без перепривязки.
    """

    cdef public object orders

    def __cinit__(self, object orders):
        self.orders = orders

    # --- состав списка -----------------------------------------------------
    def append(self, object order):
        """Добавить живую заявку в список движка тем же объектом."""
        self.orders.append(order)

    def remove(self, object order):
        """Убрать заявку из списка движка (сам dict не уничтожается)."""
        self.orders.remove(order)

    # --- поля заявки -------------------------------------------------------
    def set_remaining(self, object order, object value):
        order['remaining'] = value

    def set_reserved(self, object order, object value):
        order['reserved'] = value

    def set_status(self, object order, object value):
        order['status'] = value

    def set_price(self, object order, object value):
        order['price'] = value

    def __deepcopy__(self, memo):
        """Копия движка: новая граница вокруг скопированного списка.

        Через ``memo`` список заявок остаётся тем же объектом, что и
        ``report['orders']`` копии, поэтому и копия сохраняет одно хранилище.
        """
        return OrderStore(copy.deepcopy(self.orders, memo))

    def __reduce__(self):
        """Pickle/deepcopy без memo: восстановить границу над списком."""
        return (OrderStore, (self.orders,))

