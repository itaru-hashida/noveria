# cython: language_level=3
from .stock_store_boundary import get_stock_store
"""Чистый расчёт одной уже выбранной сделки.

Ядро не выбирает buyer/seller, не считает ``q`` и не пишет владельцев. Сюда
приходит подготовленный tuple и возвращается только готовый результат.

Все записи (CashStore, StockStore, OrderStore, report и hooks движка)
остаются в ``trade_boundary.apply_trade`` под существующим rollback.
"""

import builtins

# Python-``max``/``min`` берутся один раз. Встроенная C-версия вернула бы
# другой тип на границе ``max(0, x)`` при ``x == 0.0`` (целый 0 вместо float),
# и последние разряды отчёта сдвинулись бы.
_PY_MAX = builtins.max
_PY_MIN = builtins.min


def calculate_trade(object prepared, object eps):
    """Pure numeric calculation over prepared data."""
    cdef object free_cash = prepared[6]
    cdef object buy_price = prepared[7]
    cdef object buy_reserved = prepared[8]
    cdef object consumption_tax = prepared[9]
    cdef bint final_consumption = prepared[10]
    cdef object freight = prepared[3]
    cdef object distance = prepared[4]
    cdef object path = prepared[5]
    cdef object sell_price = prepared[15]
    cdef object month = prepared[16]
    cdef object trade_number = prepared[17]
    cdef object quantity = prepared[18]
    cdef object price = prepared[19]
    cdef object release = quantity*buy_price
    cdef object gross
    cdef object tax
    cdef object trade_id
    if quantity*price > free_cash+release+eps:
        release = _PY_MIN(buy_reserved, _PY_MAX(release, quantity*price))
    gross = quantity*sell_price
    tax = gross*consumption_tax if final_consumption else 0.0
    trade_id = f'{month}:trade:{trade_number}'
    return (release, gross, tax, trade_id, quantity*freight, distance, path)
