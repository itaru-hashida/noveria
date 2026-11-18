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
    cdef object free_cash = prepared.free_cash
    cdef object buy_price = prepared.buy_price
    cdef object buy_reserved = prepared.buy_reserved
    cdef object consumption_tax = prepared.consumption_tax
    cdef bint final_consumption = prepared.final_consumption
    cdef object quantity = prepared.quantity
    cdef object price = prepared.landed_price
    cdef object sell_price = prepared.sell_price
    cdef object freight = prepared.freight
    cdef object release = quantity*buy_price
    cdef object gross
    cdef object tax
    if quantity*price > free_cash+release+eps:
        release = _PY_MIN(buy_reserved, _PY_MAX(release, quantity*price))
    gross = quantity*sell_price
    tax = gross*consumption_tax if final_consumption else 0.0
    return (release, gross, tax, quantity*freight)
