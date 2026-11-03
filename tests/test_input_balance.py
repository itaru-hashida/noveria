import copy

import pytest

from scripts.experiment_input_balance import BalancedEconomy, imbalance
from scripts.research_agents import Probe
from test_economy_v3 import engine, firm, clear


def market(engine, flour=8, wood=.2):
    clear(engine)
    engine.__class__ = BalancedEconomy
    engine.probe = Probe('balanced')
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    bakery = firm(engine)
    bakery.update(price=2.1, capacity=20, sales_forecast=0,
        sales_history=[dict(sold=0, output=10)]*3)
    engine.accounts[bakery['id']]['balance'] = 1000
    engine.add_stock(bakery['id'], 'wood', .3, .33)
    for good, q, price in [('flour', flour, 1.7), ('wood', wood, 1.1)]:
        engine.add_stock('merchant:s', good, q, q*price)
        engine.accounts['merchant:s'].setdefault('ask_prices', {})[good] = price
    sells = engine.sell_orders()
    engine.order('h', 'bread', 10, 'buy', 3, 'Базовые нужды')
    engine.input_requests(bakery, sells)
    orders = {o['good']: o for o in engine.report['orders'] if o['id'] in bakery['production_plan']['input_orders']}
    return bakery, orders, sells


def test_missing_complement_limits_purchase_without_erasing_demand(engine):
    bakery, orders, sells = market(engine, wood=0)
    plan = copy.deepcopy(bakery['production_plan'])
    gold = engine.gold()
    engine.match(orders['flour'], sells)
    assert engine.accounts[bakery['id']]['stock']['flour'] == pytest.approx(2.4)
    assert orders['flour']['quantity'] == 8
    assert orders['flour']['remaining'] == pytest.approx(5.6)
    assert orders['flour']['reserved'] == pytest.approx(5.6*orders['flour']['price'])
    assert orders['flour']['input_balance_limited']
    assert bakery['production_plan'] == plan
    assert engine.accounts[bakery['id']]['payroll_protected'] == pytest.approx(.6)
    assert engine.gold() == pytest.approx(gold)


@pytest.mark.parametrize('first', ['flour', 'wood'])
def test_balance_works_in_both_purchase_orders(engine, first):
    bakery, orders, sells = market(engine, flour=4)
    for good in (first, 'wood' if first == 'flour' else 'flour'):
        engine.match(orders[good], sells)
    account = engine.accounts[bakery['id']]
    assert account['stock']['flour'] == pytest.approx(4)
    assert account['stock']['wood'] == pytest.approx(.5)
    engine.produce([bakery], sells)
    assert bakery['output'] == pytest.approx(5)
    assert account['reserved']+account['protected'] <= account['balance']+1e-7


@pytest.mark.parametrize('case', ['expensive', 'reserved', 'route'])
def test_unavailable_complement_does_not_justify_overbuying(engine, case):
    bakery, orders, sells = market(engine)
    if case == 'expensive':
        for offer in sells['wood']:
            offer['price'] = 100
    elif case == 'reserved':
        engine.accounts[bakery['id']]['stock_reserved']['wood'] = .3
        sells['wood'] = []
    else:
        engine.graph.add_node('isolated')
        for offer in sells['wood']:
            offer['tile_id'] = 'isolated'
    cap = engine.purchase_quantity(orders['flour'], sells)
    assert cap == pytest.approx(0 if case == 'reserved' else 2.4)


def test_explicit_order_and_control_do_not_change(engine):
    bakery, orders, sells = market(engine, wood=0)
    orders['flour'].pop('adaptive_input_budget')
    assert engine.purchase_quantity(orders['flour'], sells) == 8
    orders['flour']['adaptive_input_budget'] = True
    engine.probe.variant = 'control'
    engine.match(orders['flour'], sells)
    assert orders['flour']['status'] == 'filled'


def test_imbalance_is_fixed_price_unmatched_stock(engine):
    bakery, orders, sells = market(engine, wood=0)
    engine.add_stock(bakery['id'], 'flour', 8, 10000)
    values = imbalance(engine)
    assert values['unpaired_input_value'] == pytest.approx((8-2.4)*1.7)


def test_complement_cannot_spend_salary_or_other_reserved_money(engine):
    bakery, orders, sells = market(engine)
    account = engine.accounts[bakery['id']]
    # Другие резервы не могут оплатить дополняющий вход.
    account['reserved'] -= orders['wood']['reserved']
    orders['wood']['reserved'] = 0
    account['protected'] = account['balance']-account['reserved']
    assert engine.free_cash(bakery['id']) == pytest.approx(0)
    assert engine.purchase_quantity(orders['flour'], sells) == pytest.approx(2.4)


def test_single_input_and_stale_plan_are_unchanged(engine):
    bakery, orders, sells = market(engine)
    flour = firm(engine, 'flour')
    explicit = engine.order(flour['id'], 'grain', 2, 'buy', 1, 'Сырьё')
    explicit['adaptive_input_budget'] = True
    flour['production_plan'] = dict(month=engine.meta['month'], output=2,
        input_ratios={'grain': 1.2}, input_prices={'grain': 1}, wage_unit=.048,
        expected_net_price=1.7, input_orders=[explicit['id']])
    assert engine.purchase_quantity(explicit, sells) == 2
    bakery['production_plan']['month'] -= 1
    assert engine.purchase_quantity(orders['flour'], sells) == orders['flour']['remaining']
