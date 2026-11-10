"""Проба рыночной картины: нейтральность и корректность счётчиков.

Проверяется, что диагностика не меняет расчёт и что попадания/промахи
считаются по факту, а не по догадке.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'audit'))

from market_probe import MarketProbe  # noqa: E402
from test_economy_v3 import engine, firm  # noqa: E402


def fingerprint(engine):
    """Цены, счета и RNG: то, что проба не имеет права сдвинуть."""
    return (repr(engine.gold()),
            tuple(sorted((a['id'], repr(a['balance'])) for a in engine.accounts.values())),
            tuple(sorted((e['id'], repr(e['price']), repr(e['output']))
                         for e in engine.enterprises.values())),
            repr(engine.rng.getstate()))


def test_probe_counts_calls_and_restores_engine(engine):
    original = engine.__class__.landed
    with MarketProbe() as probe:
        assert engine.__class__.landed is not original
        sells = engine.sell_orders()
        account = engine.accounts['h']
        engine.reference_price(account, 'bread', sells)
    assert engine.__class__.landed is original
    assert probe.calls['landed'] > 0
    assert probe.calls['reference_price'] > 0
    assert probe.seconds['landed'] >= 0


def test_probe_reports_phase_of_call(engine):
    with MarketProbe() as probe:
        sells = engine.sell_orders()
        engine.offers(engine.accounts['h'], 'bread', sells)
    assert probe.by_phase
    assert sum(sum(m.values()) for m in probe.by_phase.values()) == sum(probe.calls.values())


def test_probe_hit_and_miss_are_measured_not_guessed(engine):
    with MarketProbe() as probe:
        sells = engine.sell_orders()
        buyer = engine.accounts['h']
        # Первый вызов строит книгу, второй берёт её из кэша.
        engine._market_book(buyer, 'bread', sells)
        engine._market_book(buyer, 'bread', sells)
        # Явный сброс кэша обязан дать промах, а не попадание.
        engine.invalidate_market_books()
        engine._market_book(buyer, 'bread', sells)
    assert probe.calls['_market_book'] == 3
    assert probe.misses['_market_book'] == 2
    assert probe.hits['_market_book'] == 1


def test_probe_does_not_change_prices_or_rng():
    """Два одинаковых движка: с пробой и без неё обязаны совпасть."""
    # `engine` здесь — pytest-фикстура; её исходная функция собирает новый
    # движок при каждом вызове, поэтому два экземпляра независимы.
    factor = engine.__wrapped__
    plain, probed = factor(), factor()
    plain.seed = probed.seed = 17
    plain.rng.seed(17)
    probed.rng.seed(17)
    plain.step()
    with MarketProbe():
        probed.step()
    assert fingerprint(plain) == fingerprint(probed)
