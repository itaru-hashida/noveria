# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
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


def add_stock(owner, account, good, quantity, value=0):
    """V3 add_stock: остаток и стоимость растут аддитивно, тип не меняется."""
    cdef dict account_data = owner.accounts[account]
    cdef dict stock = account_data['stock']
    cdef dict values = account_data['values']
    cdef object before = stock.get(good, 0)
    cdef object after = before+quantity
    stock[good] = after
    values[good] = values.get(good, 0)+value
    _track(owner.__dict__.get('_physical_stock_totals'), good, before, after)


def use_stock(owner, account, good, quantity):
    """V3 use_stock: списание по средней стоимости и возврат этой стоимости."""
    cdef dict account_data = owner.accounts[account]
    cdef dict stock = account_data['stock']
    cdef dict values = account_data['values']
    cdef object available = stock.get(good, 0)
    cdef object value
    cdef object after
    if quantity < -_EPS or quantity > available+_EPS:
        raise ValueError('Двойное расходование товара')
    # Встроенный min: при равенстве возвращает quantity, сохраняя его тип.
    quantity = min(quantity, available)
    value = values.get(good, 0)*quantity/available if available > 0 else 0
    after = max(0, available-quantity)
    stock[good] = after
    # Перечитывание values.get(good, 0) — как в V3, а не переиспользование
    # ранее прочитанного значения: порядок операций должен совпадать.
    values[good] = max(0, values.get(good, 0)-value)
    _track(owner.__dict__.get('_physical_stock_totals'), good, available, after)
    return value
