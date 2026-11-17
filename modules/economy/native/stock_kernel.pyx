# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
from .stock_store_boundary import get_stock_store
"""Stock mutation kernel: add_stock/use_stock над словарным складом счёта.

Ядро сознательно работает с ``object``-значениями, а не с ``double``.
Остатки V3 бывают и int, и float, и тип результата виден дальше: встроенный
``sum`` в Python 3.12 компенсирует погрешность только при float-старте,
поэтому int-остаток даёт другие последние биты отчёта. Арифметика над
``object`` в Cython порождает те же PyNumber_*, что и интерпретатор, так что
значения и типы совпадают побитово, включая int + int -> int.

Порядок операций сохранён буквально, в том числе:
  * ``min(quantity, available)`` и ``max(0, ...)`` — встроенные min/max,
    а не тернарный оператор: при равенстве min возвращает quantity;
  * в use_stock стоимость считается как ``values*quantity/available``
    (сначала умножение, затем деление), а не ``values/available*quantity``.
"""

# EPS проекта; задаётся один раз из Python-границы, чтобы не платить
# за передачу аргумента на каждом вызове и не дублировать константу.
cdef double _EPS = 1e-7


def set_epsilon(double eps):
    """Зафиксировать EPS, которым пользуются допуски остатка."""
    global _EPS
    _EPS = eps


cdef inline object _track(dict totals, object good, object before, object after):
    """Обновить ленивый индекс физических запасов, если он уже построен.

    Повторяет V3._update_physical_stock_total: строка индекса существует
    только для уже посчитанного товара, иначе вызов ничего не делает.
    """
    cdef dict row
    if totals is None:
        return None
    row = totals.get(good)
    if row is None:
        return None
    row['quantity'] = row['quantity']+(after-before)
    row['count'] = row['count']+((1 if after > _EPS else 0)
                                 -(1 if before > _EPS else 0))
    return None


cdef inline tuple _fold(object total, object comp, object value):
    """Шаг суммирования ровно как встроенный ``sum`` в CPython 3.12.

    ``sum`` складывает float компенсированно (Neumaier); наивное ``+=`` дало бы
    другой последний бит, а он меняет траекторию. Целые складываются обычным
    ``+``, чтобы тип остался ``int``.
    """
    cdef object new_total
    if type(total) is float and type(value) is float:
        new_total = total+value
        if abs(total) >= abs(value):
            comp = comp+((total-new_total)+value)
        else:
            comp = comp+((value-new_total)+total)
        return new_total, comp
    return total+value, comp


cdef inline object _finish(object total, object comp):
    """Итог компенсированной суммы: ``total+comp`` только при ненулевой поправке."""
    if comp != 0.0:
        return total+comp
    return total


def locked_material(object account_data, object good):
    """V3 locked_material: проект, публичные стройки, оснащение и поддержка.

    Порядок сложений прежний — сначала проект, затем по каждому проекту
    материалы, оснащение и поддержка. Арифметика объектная, поэтому int
    остаётся int, как и в прежнем коде.
    """
    cdef object locked = account_data.get('project', {}).get('materials', {}).get(good, 0)
    cdef object project
    for project in account_data.get('public_projects', ()):
        locked = locked+project.get('materials', {}).get(good, 0)
        locked = locked+project.get('equipment_held', {}).get(good, 0)
        locked = locked+project.get('support_stock', {}).get(good, 0)
    return locked


def free_stock(object account_data, object good):
    """V3 free_stock: ``max(0, stock - reserved - locked_material)``.

    Встроенный ``max`` возвращает первый аргумент при равенстве, поэтому при
    неположительном остатке результат — именно ``0`` (int), как и раньше.
    """
    cdef object value = (account_data['stock'].get(good, 0)
                         -account_data.get('stock_reserved', {}).get(good, 0)
                         -locked_material(account_data, good))
    if value > 0:
        return value
    return 0


def total_stock(object accounts, object goods):
    """V3 total_stock: компенсированная сумма склада по каждому товару."""
    cdef dict totals = {}
    cdef dict comps = {}
    cdef object good, account, total, comp
    for good in goods:
        total = 0
        comp = 0.0
        for account in accounts:
            total, comp = _fold(total, comp, account['stock'].get(good, 0))
        totals[good] = _finish(total, comp)
    return totals


def gold(object accounts):
    """V3 gold: компенсированная сумма балансов в прежнем порядке обхода."""
    cdef object total = 0
    cdef object comp = 0.0
    cdef object account
    for account in accounts:
        total, comp = _fold(total, comp, account['balance'])
    return _finish(total, comp)


def add_stock(owner, account, good, quantity, value=0):
    """V3 add_stock через единый StockStore."""
    cdef dict account_data = owner.accounts[account]
    cdef dict values = account_data['values']
    cdef object stock_store = get_stock_store(owner)
    cdef object before = stock_store.get_stock(account, good)
    cdef object after = stock_store.add_stock(account, good, quantity)
    values[good] = values.get(good, 0)+value
    _track(owner.__dict__.get('_physical_stock_totals'), good, before, after)


def use_stock(owner, account, good, quantity):
    """V3 use_stock: списание по средней стоимости и возврат этой стоимости."""
    cdef dict account_data = owner.accounts[account]
    cdef dict values = account_data['values']
    cdef object stock_store = get_stock_store(owner)
    cdef object available = stock_store.get_stock(account, good)
    cdef object value
    cdef object after
    if quantity < -_EPS or quantity > available+_EPS:
        raise ValueError('Двойное расходование товара')
    # Встроенный min: при равенстве возвращает quantity, сохраняя его тип.
    quantity = min(quantity, available)
    value = values.get(good, 0)*quantity/available if available > 0 else 0
    after = max(0, available-quantity)
    stock_store.set_stock(account, good, after)
    # Перечитывание values.get(good, 0) — как в V3, а не переиспользование
    # ранее прочитанного значения: порядок операций должен совпадать.
    values[good] = max(0, values.get(good, 0)-value)
    _track(owner.__dict__.get('_physical_stock_totals'), good, available, after)
    return value
