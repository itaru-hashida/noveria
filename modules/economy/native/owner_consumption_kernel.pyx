# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Owner consumption kernel: числовой срез ``V3.consume_owner_purchases``.

Перенесён ровно один участок — цикл по ``report['owner_consumption']`` из
``Economy.step``. Раньше он писал поля прямо из Python:

  * ``order['consumed_quantity']`` — сколько уже списано по заявке;
  * ``row['consumed'][good]`` — что владелец съел по строке отчёта;
  * ``report['owner_consumed'][good]`` и ``report['consumed'][good]`` —
    суммы месяца;
  * ``row['spent'/'tax'/'freight'/'unfilled'/'unused_budget']`` — итоги строки;
  * ``account['owner_consumption_spent'/'owner_consumed'/'..._tax'/'..._freight']``
    — зеркало на счёте владельца.

Теперь эти поля пишет ядро (``OwnerConsumptionLedger`` — Cython-owned state),
а ``v3.py`` оставляет ровно один вызов границы. Склад по-прежнему меняется
только через существующую stock boundary: ``use_stock`` вызывается
Python-хуком границы, второй копии склада здесь нет.

Ядро сознательно работает с ``object``-арифметикой, а не с ``double``.
``quantity``, остатки заявок и суммы сделок бывают и int, и float; тип виден
дальше в отчёте, а встроенный ``sum`` в Python 3.12 компенсирует погрешность
только при float-старте. Арифметика над ``object`` порождает те же
PyNumber_*, что и интерпретатор, поэтому значения и типы совпадают побитово,
включая int + int -> int.

Порядок операций сохранён буквально:
  * строки ``report['owner_consumption']`` обходятся в переданном порядке;
  * внутри строки — полный обход ``report['orders']`` с проверкой
    ``order['id'] in row['orders']``, а не обход списка id: порядок списаний
    задаёт именно книга заявок;
  * ``filled`` и ``quantity`` считаются до ``use_stock``, как и раньше;
  * ``max(0, filled-consumed_quantity)`` — встроенный ``max``, при равенстве
    он возвращает ``0`` и сохраняет его тип;
  * три накопления идут строго в прежнем порядке: ``row['consumed']``, затем
    ``report['owner_consumed']``, затем ``report['consumed']``;
  * ``spent``/``tax``/``freight`` — встроенный ``sum`` по списку сделок,
    собранному тем же фильтром ``t['buy_order'] in row['orders']``;
  * ``unfilled`` — словарное включение по книге заявок, поэтому порядок ключей
    прежний;
  * ``unused_budget = max(0, row['budget']-row['spent'])`` читает уже
    записанный ``spent``;
  * счёт получает ``copy.deepcopy(row['consumed'])`` — отдельная копия, а не
    ссылка на словарь строки.

RNG ядро не трогает: перемешивание и расход RNG остаются за границей.
"""

import copy


cdef class OwnerConsumptionLedger:
    """Cython-owned state среза: что списано и что накоплено за месяц.

    Значения живут здесь, а словари движка (``order``, ``row``, ``report``,
    ``account``) получают те же значения сразу после расчёта — это
    compatibility view, а не второй ledger: ядро не пересчитывает их повторно
    и не хранит собственной копии склада.

    Объект создаётся на один вызов границы. Как и ленивый индекс склада, он
    не живёт на движке: тесты массово копируют движок через ``deepcopy``, а
    cdef-класс пережил бы такую копию непредсказуемо.
    """

    cdef object engine
    cdef object hooks
    cdef object eps
    # order_id -> filled: сколько по заявке уже списано.
    cdef dict consumed_quantity
    # good -> накоплено за месяц (view: report['owner_consumed']).
    cdef dict owner_consumed
    # good -> накоплено за месяц (view: report['consumed']).
    cdef dict consumed
    # good -> накоплено по текущей строке (view: row['consumed']).
    cdef dict row_consumed

    def __init__(self, engine, hooks, eps):
        self.engine = engine
        self.hooks = hooks
        self.eps = eps
        self.consumed_quantity = {}
        # Копии, а не ссылки: порядок ключей и уже накопленные значения месяца
        # сохраняются, но пишет дальше только ядро.
        self.owner_consumed = dict(engine.report['owner_consumed'])
        self.consumed = dict(engine.report['consumed'])
        self.row_consumed = {}

    cdef object consumed_before(self, dict order):
        """Отметка заявки: из ledger, а до первого касания — из поля заявки.

        Прежний код читал ``order.get('consumed_quantity', 0)``. Отметка могла
        остаться от прошлого месяца или быть выставлена до этого запуска,
        поэтому первое чтение берёт ровно то же значение и дальше владеет им.
        """
        if order['id'] not in self.consumed_quantity:
            self.consumed_quantity[order['id']] = order.get('consumed_quantity', 0)
        return self.consumed_quantity[order['id']]

    cdef object take(self, dict order, dict account):
        """Списать остаток заявки и записать отметку — прежний порядок."""
        cdef object filled = order['quantity']-order['remaining']
        cdef object quantity = max(0, filled-self.consumed_before(order))
        if quantity > self.eps:
            # Списание идёт прежним путём — через stock boundary движка.
            self.hooks.use_stock(account['id'], order['good'], quantity)
            self.consumed_quantity[order['id']] = filled
            order['consumed_quantity'] = filled
        return quantity

    cdef object start_row(self, dict row):
        """Начать строку отчёта: своя накопленная копия."""
        # Копия, а не ссылка: строка отчёта остаётся compatibility view.
        self.row_consumed = dict(row['consumed'])
        return None

    cdef object record(self, dict row, object good, object quantity):
        """Три накопления подряд — ровно в прежнем порядке."""
        cdef object value = self.row_consumed.get(good, 0)+quantity
        cdef object owner_value, total
        self.row_consumed[good] = value
        row['consumed'][good] = value
        owner_value = self.owner_consumed.get(good, 0)+quantity
        self.owner_consumed[good] = owner_value
        self.engine.report['owner_consumed'][good] = owner_value
        total = self.consumed.get(good, 0)+quantity
        self.consumed[good] = total
        self.engine.report['consumed'][good] = total
        return None

    cdef object settle_row(self, dict row, dict account):
        """Итоги строки: суммы по сделкам, unfilled и зеркало на счёте."""
        cdef list orders = self.engine.report['orders']
        cdef list row_orders = row['orders']
        cdef list trades = [t for t in self.engine.report['trades']
                            if t['buy_order'] in row_orders]
        row.update(spent=sum(t['gold']+t['freight'] for t in trades),
            tax=sum(t['tax'] for t in trades), freight=sum(t['freight'] for t in trades),
            unfilled={o['good']: o['remaining'] for o in orders if o['id'] in row_orders})
        row['unused_budget'] = max(0, row['budget']-row['spent'])
        account.update(owner_consumption_spent=row['spent'],
            owner_consumed=copy.deepcopy(row['consumed']),
            owner_consumption_tax=row['tax'],
            owner_consumption_freight=row['freight'])
        return None


def run(object engine, object hooks, object eps):
    """Пройти строки owner consumption, не меняя порядок обхода и RNG."""
    cdef OwnerConsumptionLedger ledger = OwnerConsumptionLedger(engine, hooks, eps)
    cdef list rows = engine.report['owner_consumption']
    cdef list orders = engine.report['orders']
    cdef object row, account, order, good, quantity
    cdef Py_ssize_t index, count

    count = len(rows)
    for index in range(count):
        row = rows[index]
        account = engine.accounts[row['owner']]
        ledger.start_row(row)
        for order in orders:
            if order['id'] not in row['orders']:
                continue
            quantity = ledger.take(order, account)
            if quantity > eps:
                good = order['good']
                ledger.record(row, good, quantity)
        ledger.settle_row(row, account)
    return None
