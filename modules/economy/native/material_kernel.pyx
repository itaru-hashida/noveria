# cython: language_level=3
"""Числовые расчёты сметы и резерва строительных материалов."""

import builtins

_PY_MIN = builtins.min
_PY_MAX = builtins.max

def construction_bill(object construction, object minimum_capacity, object capacity):
    """Смета проекта: та же формула ``q*capacity/minimum_capacity``.

    Порядок обхода — порядок ``cfg['construction']``, поэтому ключи сметы
    идут в том же порядке, что и у прежнего comprehension. Значения остаются
    ``object``: произведение точное, а деление даёт float ровно там же, где
    давало его прежнее выражение.
    """
    cdef dict bill = {}
    cdef object good
    cdef object quantity
    for good, quantity in construction.items():
        bill[good] = quantity*capacity/minimum_capacity
    return bill

def assignable_stock(object needed, object stock, object held):
    return _PY_MIN(needed, _PY_MAX(0, stock-held))


def assign_from_free_stock(object needed, object free, object held):
    return held+_PY_MIN(needed, free)
