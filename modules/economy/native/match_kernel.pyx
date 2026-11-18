# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Чистый расчёт количества одной уже выбранной пары.

Python владеет книгой, обходом, RNG, live rechecks, callbacks и writes.
Ядро получает только числовые входы и возвращает количество либо нулевой
результат; оно не принимает owner и не знает о заявках, движке или сделках.
"""

import builtins

_PY_MIN = builtins.min


def calculate_quantity(object prepared, object eps):
    """Pure q/eligibility calculation from explicit numeric inputs."""
    cdef object buy_remaining = prepared.buy_remaining
    cdef object sell_remaining = prepared.sell_remaining
    cdef object reserved = prepared.reserved
    cdef object price = prepared.price
    cdef object purchase = prepared.purchase_quantity
    cdef object q = _PY_MIN(buy_remaining, sell_remaining,
                            reserved/price, purchase)
    if q <= eps:
        return 0
    return q
