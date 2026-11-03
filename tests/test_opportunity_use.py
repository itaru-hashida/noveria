import copy

import pytest

from scripts.assess_opportunity_use import OpportunityEconomy, feasible_reserve
from scripts.research_agents import Probe, capture
from scripts.summarize_opportunity_use import idle_summary
from test_economy_v3 import engine, firm, clear


def market(engine, demand=2):
    clear(engine)
    for a in engine.accounts.values():
        a.update(stock={}, values={}, balance=1000)
    for f in engine.enterprises.values():
        f.update(workers=0, output=0)
    first, second = firm(engine), firm(engine, 'flour')
    for f in (first, second):
        f.update(good='bread', capacity=4, price=2.1, planned_output=0)
        engine.add_stock(f['id'], 'flour', 3.2, 3.2*1.7)
        engine.add_stock(f['id'], 'wood', .4, .4*1.1)
    buy = engine.order('h', 'bread', demand, 'buy', 3, 'Базовые нужды')
    engine._planning_orders = [copy.deepcopy(buy)]
    engine.cancel_order(buy)
    return first, second


def test_joint_reserve_does_not_count_shared_demand_twice(engine):
    first, second = market(engine)
    before = copy.deepcopy((engine.accounts, engine.enterprises, engine.meta, engine.rng.getstate()))
    result = feasible_reserve(engine, {})
    locals = {f['id']: f['extra'] for f in result['local']}
    assert locals[first['id']] == locals[second['id']] == 2
    assert result['joint_extra']['bread'] == 2
    assert (engine.accounts, engine.enterprises, engine.meta, engine.rng.getstate()) == before


def test_joint_reserve_respects_shared_labor(engine):
    market(engine)
    engine.cfg['labor_share'] = .5/100000
    result = feasible_reserve(engine, {})
    assert result['joint_extra']['bread'] == pytest.approx(1)


def test_finished_stock_covers_demand_before_extra_production(engine):
    first, _ = market(engine)
    engine.add_stock(first['id'], 'bread', 1, 1)
    result = feasible_reserve(engine, {})
    assert result['stock_coverage']['bread'] == 1
    assert result['joint_extra']['bread'] == 1


@pytest.mark.parametrize('constraint', ['margin', 'cash', 'input', 'buyer_cash', 'route'])
def test_unavailable_or_unprofitable_capacity_is_not_reserve(engine, constraint):
    first, second = market(engine)
    for f in (first, second):
        a = engine.accounts[f['id']]
        if constraint == 'margin':
            f['price'] = 1
        elif constraint == 'cash':
            a['protected'] = a['balance']
        elif constraint == 'input':
            a['stock_reserved']['flour'] = a['stock']['flour']
    if constraint == 'buyer_cash':
        engine.accounts['h']['balance'] = 0
    elif constraint == 'route':
        engine.graph.add_node('isolated')
        engine._planning_orders[0]['tile_id'] = 'isolated'
    assert feasible_reserve(engine, {})['joint_extra']['bread'] == 0


def test_local_reserve_does_not_reuse_buyer_cash_across_orders(engine):
    first, _ = market(engine, demand=1)
    order = copy.deepcopy(engine._planning_orders[0])
    order['id'] += '-other'
    engine._planning_orders.append(order)
    engine.accounts['h']['balance'] = 2.1
    result = feasible_reserve(engine, {})
    local = next(f for f in result['local'] if f['id'] == first['id'])
    assert local['extra'] == pytest.approx(1)
    assert result['joint_extra']['bread'] == pytest.approx(1)


def test_opportunity_observer_preserves_game_and_rng(engine):
    observed = copy.deepcopy(engine)
    observed.__class__ = OpportunityEconomy
    observed.probe = Probe('control')
    for month in range(1, 4):
        observed.probe.begin(month)
        engine.step()
        observed.step()
        capture(observed)
        assert observed.opportunity_reserve
        for name in ('accounts', 'enterprises', 'meta'):
            assert getattr(engine, name) == getattr(observed, name)
        assert engine.rng.getstate() == observed.rng.getstate()


def test_persistent_idle_requires_consecutive_available_months():
    def row(extra, output=0):
        return dict(opportunity_reserve=dict(local=[dict(id='f', good='bread', capacity=10,
            output=output, extra=extra)]))
    result = idle_summary([row(1), row(1), row(0), row(1), row(1)])
    assert result['eligible'] == 1 and result['persistent'] == 0 and result['longest'] == 2
    result = idle_summary([row(1), row(1), row(1)])
    assert result['persistent'] == 1 and result['fraction'] == 1
    assert idle_summary([row(1, 1)]*3)['persistent'] == 0
