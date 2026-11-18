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
    cdef object buy_remaining = prepared[0]
    cdef object sell_remaining = prepared[1]
    cdef object reserved = prepared[2]
    cdef object price = prepared[3]
    cdef object purchase = prepared[4]
    cdef object q = _PY_MIN(buy_remaining, sell_remaining,
                            reserved/price, purchase)
    if q <= eps:
        return 0
    return q
