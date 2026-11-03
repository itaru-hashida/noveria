import copy

import pytest

from scripts.research_food_money import MoneyEconomy, details
from scripts.research_agents import Probe, capture
from test_economy_v3 import engine


def observe(engine):
    engine.__class__ = MoneyEconomy
    engine.probe = Probe('control')
    engine.probe.begin(1)
    engine.step()
    return details(engine, capture(engine))


def test_money_observer_preserves_game_and_rng(engine):
    actual = copy.deepcopy(engine)
    result = observe(actual)
    engine.step()
    for name in ('accounts', 'enterprises', 'meta'):
        assert getattr(actual, name) == getattr(engine, name)
    assert actual.rng.getstate() == engine.rng.getstate()
    assert result['households']


def test_unfunded_and_unfilled_are_distinct_and_reconcile(engine):
    engine.accounts['h']['balance'] = .01
    # Изменяем начальный тестовый фонд, сохраняя бухгалтерский инвариант.
    engine.meta['initial_gold'] = engine.gold()
    result = observe(engine)
    h = result['households'][0]
    assert h['unfunded'] > 0
    assert h['need']-h['consumed'] == pytest.approx(h['unfunded']+h['unfilled'])
    assert h['bread_only_affordable'] <= h['requested']
