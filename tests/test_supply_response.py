import copy

import pytest

from scripts.research_supply_response import SupplyEconomy, expected_stock_sales, diagnostic
from scripts.research_agents import Probe, capture
from test_economy_v3 import engine, firm, clear


def market(engine, variant='control', stock=4, bid=1.1):
    clear(engine)
    engine.__class__ = SupplyEconomy
    engine.probe = Probe(variant)
    engine.probe.begin(1)
    engine.__dict__.pop('_planning_orders', None)
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    f = firm(engine, 'wood')
    f.update(price=1.1, capacity=6, sales_history=[dict(sold=4, output=4)]*3)
    engine.add_stock(f['id'], 'wood', stock, stock*.084)
    sells = engine.sell_orders()
    order = engine.order('h', 'wood', 4, 'buy', bid, 'Фиксированный спрос')
    return f, sells, order


def test_existing_stock_sold_this_month_is_not_next_month_supply(engine):
    f, sells, _ = market(engine, 'lead_time')
    assert expected_stock_sales(engine, f, sells) == 4
    result = engine.production_outlook(f, sells)
    assert result['planned_output'] == 4
    engine.probe.variant = 'control'
    assert engine.production_outlook(f, sells)['planned_output'] == 0


def test_no_replenishment_for_unaffordable_or_unoffered_stock(engine):
    f, sells, _ = market(engine, 'lead_time', bid=.5)
    assert expected_stock_sales(engine, f, sells) == 0
    assert engine.production_outlook(f, sells)['planned_output'] == 0
    for o in engine.report['orders']:
        if o['side'] == 'buy': o['price'] = 2
    assert expected_stock_sales(engine, f, {}) == 0


def test_cheaper_competitor_stock_reduces_expected_sales(engine):
    f, sells, _ = market(engine, 'lead_time')
    engine.add_stock('merchant:s', 'wood', 4, 1)
    sells['wood'].append(engine.order('merchant:s', 'wood', 4, 'sell', .5, 'Продажа'))
    assert expected_stock_sales(engine, f, sells) == 0


def test_expected_sales_use_backing_instead_of_nominal_order_size(engine):
    f, sells, buy = market(engine, 'lead_time')
    engine.accounts['h']['reserved'] -= buy['reserved']-1.1
    buy['reserved'] = 1.1
    assert expected_stock_sales(engine, f, sells) == pytest.approx(1)
    assert engine.production_outlook(f, sells)['planned_output'] == pytest.approx(1)


def test_replenishment_does_not_exceed_capacity_or_turn_unsold_stock_into_demand(engine):
    f, sells, _ = market(engine, 'lead_time', stock=12)
    assert engine.production_outlook(f, sells)['planned_output'] == 0
    engine.accounts[f['id']]['stock']['wood'] = 0
    assert engine.production_outlook(f, sells)['planned_output'] == 4
    f['capacity'] = 2
    assert engine.production_outlook(f, sells)['planned_output'] == 2


@pytest.mark.parametrize('stock', [0, 2, 4])
def test_constant_funded_demand_reproduces_half_supply(engine, stock):
    control, corrected = copy.deepcopy(engine), copy.deepcopy(engine)
    def run(e, variant):
        f, _, _ = market(e, variant, stock=stock)
        outputs, sales = [], []
        for month in range(1, 13):
            clear(e)
            e.meta['month'] = month
            e.probe.begin(month)
            f.update(output=0, workers=0)
            sells = e.sell_orders()
            buy = e.order('h', 'wood', 4, 'buy', 1.1, 'Фиксированный спрос')
            e.input_requests(f, sells)
            e.match(buy, sells)
            sales.append(4-buy['remaining'])
            for o in e.report['orders']:e.cancel_order(o)
            e.produce([f], sells)
            outputs.append(f['output'])
            e.use_stock('h', 'wood', e.accounts['h']['stock'].get('wood', 0))
        return outputs, sales
    old_output, old_sales = run(control, 'control')
    new_output, new_sales = run(corrected, 'lead_time')
    assert old_output == [4-stock, stock]*6
    assert old_sales == [stock, 4-stock]*6
    assert sum(old_sales)/12 == 2
    assert new_output == [4]*12
    assert new_sales == [stock]+[4]*11


def test_supply_observer_preserves_control_and_rng(engine):
    engine.STOCK_PLANNING_REVISION = 0
    observed = copy.deepcopy(engine)
    observed.__class__ = SupplyEconomy
    observed.probe = Probe('control')
    for month in range(1, 4):
        observed.probe.begin(month)
        engine.step(); observed.step()
        capture(observed); diagnostic(observed)
        for key in ('accounts', 'enterprises', 'meta'):
            assert getattr(engine, key) == getattr(observed, key)
        assert engine.rng.getstate() == observed.rng.getstate()


def test_higher_target_still_respects_salary_and_project_money(engine):
    f, sells, _ = market(engine, 'lead_time')
    a = engine.accounts[f['id']]
    a['protected'] = a['balance']-.1
    engine.input_requests(f, sells)
    assert f['production_plan']['output'] == pytest.approx(.1/.084)
    assert a['payroll_protected'] == pytest.approx(.1)
    assert a['protected']+a['reserved'] <= a['balance']+1e-7


def test_negative_margin_is_not_overridden(engine):
    f, sells, _ = market(engine, 'lead_time')
    f['price'] = .001
    f.pop('production_price', None)
    result = engine.production_outlook(f, sells)
    assert result['expected_margin'] < 0
    assert result['planned_output'] == 0


def test_raw_only_does_not_change_processor_plans(engine):
    clear(engine)
    engine.__class__ = SupplyEconomy
    engine.probe = Probe('control')
    bakery = firm(engine, 'bread')
    bakery.update(sales_history=[dict(sold=4, output=4)]*3, price=2.1)
    sells = engine.sell_orders()
    engine.order('h', 'bread', 4, 'buy', 3, 'Фиксированный спрос')
    baseline = engine.production_outlook(bakery, sells)
    engine.probe.variant = 'raw_lead_time'
    assert engine.production_outlook(bakery, sells) == baseline
