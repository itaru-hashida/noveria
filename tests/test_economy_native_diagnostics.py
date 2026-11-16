"""Focused-тесты diagnostics-ядра: counters event trace, порядок групп.

Проверяется числовая агрегация сделок месяца для ``trade.executed`` событий:
``count``, ``quantity``, ``gold``, ``freight`` по прежнему ключу группировки
и в прежнем порядке (первое появление ключа). Порядок и количество событий
обязаны совпасть с прежним обходом ``trade_groups``.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
гоняет обе реализации на одинаковом состоянии.
"""
import copy
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy.native import diagnostics_boundary as boundary  # noqa: E402


def trade(buyer, seller, good, quantity, gold, freight, purpose='buy',
          buyer_province='p1', seller_province='p2'):
    return dict(buyer=buyer, seller=seller, good=good, quantity=quantity,
                gold=gold, freight=freight, purpose=purpose,
                buyer_province=buyer_province, seller_province=seller_province)


def run_both(trades):
    native = boundary.aggregate_trade_groups(copy.deepcopy(trades))
    ref = boundary.aggregate_trade_groups_python(copy.deepcopy(trades))
    return native, ref


# --- 1. Совпадение native и Python-эталона ------------------------------------

def test_native_matches_python_reference():
    trades = [
        trade('b1', 's1', 'flour', 10, 30.0, 1.0),
        trade('b1', 's1', 'flour', 5, 15.0, 0.5),
        trade('b2', 's1', 'flour', 2, 8.0, 0.2),
        trade('b1', 's2', 'bread', 1, 4, 0),
    ]
    native, ref = run_both(trades)
    assert native == ref
    assert len(native) == 3
    (key1, count1, qty1, gold1, fr1) = native[0]
    assert key1 == ('b1', 's1', 'flour', 'p1', 'p2', 'buy')
    assert (count1, qty1, gold1, fr1) == (2, 15.0, 45.0, 1.5)


def test_group_order_follows_first_appearance():
    trades = [
        trade('b2', 's2', 'bread', 1, 1.0, 0),
        trade('b1', 's1', 'flour', 1, 2.0, 0),
        trade('b2', 's2', 'bread', 1, 3.0, 0),
    ]
    native, ref = run_both(trades)
    assert native == ref
    keys = [row[0] for row in native]
    assert keys == [('b2', 's2', 'bread', 'p1', 'p2', 'buy'),
                    ('b1', 's1', 'flour', 'p1', 'p2', 'buy')]


def test_count_starts_int_and_sums_stay_float():
    trades = [trade('b1', 's1', 'flour', 1, 1, 0)]
    native, ref = run_both(trades)
    (_, count, quantity, gold, freight) = native[0]
    assert type(count) is int
    assert type(quantity) is float
    assert type(gold) is float
    assert type(freight) is float


def test_empty_trades_gives_empty_list():
    native, ref = run_both([])
    assert native == [] and ref == []


def test_missing_fields_use_defaults():
    trades = [dict(buyer='b1', seller='s1', good='flour')]
    native, ref = run_both(trades)
    assert native == ref
    (_, count, quantity, gold, freight) = native[0]
    assert (count, quantity, gold, freight) == (1, 0.0, 0.0, 0.0)


def test_event_count_and_order_match_python_iteration():
    """Один кортеж на группу => столько же log_event вызовов, в том же порядке."""
    trades = [trade('b%d' % i, 's%d' % (i % 3), 'g%d' % (i % 2), i, float(i), 0.1)
              for i in range(1, 20)]
    native, ref = run_both(trades)
    assert native == ref
    assert len(native) == len({(t['buyer'], t['seller'], t['good'],
                                t['buyer_province'], t['seller_province'], t['purpose'])
                               for t in trades})


# --- 2. Fallback без .pyd --------------------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    trades = [trade('b1', 's1', 'flour', 10, 30.0, 1.0),
              trade('b1', 's1', 'flour', 5, 15.0, 0.5)]
    native, _ = run_both(trades)
    monkeypatch.setattr(boundary, 'aggregate_trade_groups',
                        boundary.aggregate_trade_groups_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    ref = boundary.aggregate_trade_groups(copy.deepcopy(trades))
    assert ref == native
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('diagnostics_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_diagnostics_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('aggregate_trade_groups', 'aggregate_trade_groups_python',
                 'load_diagnostics_kernel'):
        assert callable(getattr(boundary, name)), name
