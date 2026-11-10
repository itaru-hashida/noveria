"""Кэш market_signal: тот же ответ, но без повторного обхода истории.

Ключ — (государство, провинция, товар), запись действительна, пока тот же
объект истории. Тест считает обходы самой истории, поэтому проверяет именно
снятую работу, а не факт наличия атрибута.
"""
import pytest

from test_economy_v3 import engine  # noqa: F401  фикстура движка без БД


class CountingHistory(list):
    """История рынка, считающая обходы: столько раз её пролистал market_signal."""

    def __init__(self, values):
        super().__init__(values)
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return super().__iter__()


def row(state='s', province='p', good='bread', volume=10.0, requested=12.0,
        trade_gold=20.0, freight=2.0, unmet=1.0, limit_value=24.0):
    return dict(state_id=state, province_id=province, good=good, volume=volume,
                requested=requested, trade_gold=trade_gold, freight=freight,
                unmet_solvent=unmet, limit_value=limit_value)


def history(months=2):
    return CountingHistory(dict(month=month, markets=[row(), row(good='fish')])
                           for month in range(1, months + 1))


def test_signal_averages_history(engine):
    signal = engine.market_signal(history(2), 's', 'p', 'bread')
    assert signal['volume'] == 10.0
    assert signal['unmet'] == 1.0
    # Цены считаются как сумма по месяцам, делённая на суммарный объём.
    assert signal['price'] == 2.0
    assert signal['landed_price'] == pytest.approx(2.2)
    assert signal['limit_price'] == 2.0


def test_second_call_does_not_walk_history_again(engine):
    periods = history(3)
    first = engine.market_signal(periods, 's', 'p', 'bread')
    assert periods.scans == 1
    second = engine.market_signal(periods, 's', 'p', 'bread')
    assert second == first
    assert periods.scans == 1


def test_keys_are_independent(engine):
    periods = history(1)
    bread = engine.market_signal(periods, 's', 'p', 'bread')
    fish = engine.market_signal(periods, 's', 'p', 'fish')
    assert bread['volume'] == fish['volume']
    assert len(engine._market_signal_cache) == 2


def test_absent_good_is_cached_too(engine):
    periods = history(1)
    empty = dict(volume=0, unmet=0, price=None, landed_price=None, limit_price=None)
    assert engine.market_signal(periods, 's', 'p', 'tools') == empty
    assert periods.scans == 1
    assert engine.market_signal(periods, 's', 'p', 'tools') == empty
    assert periods.scans == 1


def test_returned_signal_belongs_to_caller(engine):
    periods = history(1)
    signal = engine.market_signal(periods, 's', 'p', 'bread')
    signal['volume'] = -1
    assert engine.market_signal(periods, 's', 'p', 'bread')['volume'] == 10.0


def test_new_history_object_invalidates_entry(engine):
    engine.market_signal(history(1), 's', 'p', 'bread')
    # Месяц пересобирает market_history новым списком: старая запись не годится.
    newer = history(2)
    engine.market_signal(newer, 's', 'p', 'bread')
    assert newer.scans == 1


def test_reset_report_drops_cache(engine):
    periods = history(1)
    engine.market_signal(periods, 's', 'p', 'bread')
    assert '_market_signal_cache' in vars(engine)
    engine.reset_report()
    assert '_market_signal_cache' not in vars(engine)
    engine.market_signal(periods, 's', 'p', 'bread')
    assert periods.scans == 2
