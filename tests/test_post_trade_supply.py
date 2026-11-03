import copy

import pytest

from scripts.experiment_post_trade_supply import PostTradeSupply
from test_supply_response import market
from test_economy_v3 import engine


def sold_stock(engine):
    f, sells, buy = market(engine, 'post_trade_raw')
    engine.__class__ = PostTradeSupply
    engine.input_requests(f, sells)
    assert f['production_plan']['output'] == 0
    engine.match(buy, sells)
    assert buy['status'] == 'filled'
    engine._planning_orders = copy.deepcopy(engine.report['orders'])
    for o in engine.report['orders']:
        engine.cancel_order(o)
    return f, sells


def test_raw_replenishes_actual_sales_after_clearing(engine):
    f, sells = sold_stock(engine)
    engine.produce([f], sells)
    assert f['production_plan']['output'] == 4
    assert f['output'] == 4
    assert engine.post_trade_plans[0]['before'] == 0


def test_late_target_respects_project_money_and_shared_natural_limit(engine):
    f, sells = sold_stock(engine)
    a = engine.accounts[f['id']]
    a['project'] = dict(working_cash=10, delivery=2)
    a['balance'] = 12+.084
    engine.cfg['extraction_per_tile'] = .5
    engine.produce([f], sells)
    assert f['production_plan']['output'] == pytest.approx(1)
    assert f['output'] == pytest.approx(.5)
    assert a['balance'] >= 12


def test_exploratory_class_does_nothing_in_control(engine):
    f, sells = sold_stock(engine)
    engine.probe.variant = 'control'
    engine.produce([f], sells)
    assert f['production_plan']['output'] == f['output'] == 0
