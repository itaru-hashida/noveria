import copy

import pytest

from scripts.research_agents import ObservedEconomy, Probe
from test_economy_v3 import engine, firm, clear


def market(engine, both=False, flour_price=2, wood_price=2):
    clear(engine)
    for account in engine.accounts.values():
        account.update(stock={}, values={})
    bakery = firm(engine)
    bakery.update(price=2.1, capacity=20, sales_forecast=0,
        sales_history=[dict(sold=0, output=0)]*3, production_plan=dict(output=10))
    account = engine.accounts[bakery['id']]
    account['balance'] = 1000
    if not both:
        engine.add_stock(account['id'], 'wood', 2, 2.2)
    goods = dict(flour=(8, flour_price))
    if both:
        goods['wood'] = (1, wood_price)
    for good, (quantity, price) in goods.items():
        engine.add_stock('merchant:s', good, quantity, quantity)
        engine.add_stock('private:s', good, quantity, quantity*price)
        engine.accounts['merchant:s'].setdefault('ask_prices', {})[good] = 1
        engine.accounts['private:s'].setdefault('ask_prices', {})[good] = price
    sells = engine.sell_orders()
    engine.order('h', 'bread', 10, 'buy', 3, 'Базовые нужды')
    engine.input_requests(bakery, sells)
    orders = {o['good']: o for o in engine.report['orders']
        if o['id'] in bakery['production_plan']['input_orders']}
    for good, (quantity, _) in goods.items():
        other = engine.order('h', good, quantity, 'buy', 1, 'Сырьё')
        engine.match(other, sells)
        assert other['status'] == 'filled'
    return bakery, account, orders, sells


def test_generated_input_bid_recovers_profitable_production(engine):
    bakery, account, orders, sells = market(engine)
    order = orders['flour']
    plan = copy.deepcopy(bakery['production_plan'])
    gold = engine.gold()
    engine.match(order, sells)
    assert order['price'] == 2 and order['initial_input_limit'] == 1
    assert order['status'] == 'filled'
    assert order['quantity'] == 8
    assert bakery['production_plan'] == plan
    assert account['protected'] == pytest.approx(.6)
    engine.produce([bakery], sells)
    assert bakery['output'] == 10
    assert bakery['unit_cost'] <= plan['expected_net_price']
    assert engine.gold() == pytest.approx(gold)


@pytest.mark.parametrize('first', ['flour', 'wood'])
def test_two_repriced_inputs_fit_frozen_margin_in_either_order(engine, first):
    bakery, account, orders, sells = market(engine, both=True)
    second = 'wood' if first == 'flour' else 'flour'
    engine.match(orders[first], sells)
    engine.match(orders[second], sells)
    assert all(o['status'] == 'filled' for o in orders.values())
    engine.produce([bakery], sells)
    assert bakery['output'] == 10
    assert bakery['unit_cost'] == pytest.approx(1.86)
    assert bakery['unit_cost'] <= bakery['production_plan']['expected_net_price']


def test_other_input_uses_updated_price_commitment(engine):
    bakery, account, orders, sells = market(engine, both=True, wood_price=3)
    engine.match(orders['flour'], sells)
    assert engine.input_bid_ceiling(orders['wood']) == pytest.approx(2.3)
    engine.match(orders['wood'], sells)
    assert orders['wood']['remaining'] == 1
    assert orders['wood']['price'] == pytest.approx(2.3)
    assert account['stock'].get('wood', 0) == 0


def test_no_increase_beyond_margin_for_unprofitable_input(engine):
    bakery, account, orders, sells = market(engine, flour_price=3)
    engine.match(orders['flour'], sells)
    assert orders['flour']['remaining'] == 8
    assert orders['flour']['price'] == pytest.approx(2.15)
    assert account['stock'].get('flour', 0) == 0


def test_repricing_preserves_payroll_project_and_other_order_budget(engine):
    bakery, account, orders, sells = market(engine)
    other = engine.order(bakery['id'], 'fish', 1, 'buy', 2, 'Пользовательский лимит')
    account['protected'] += 3
    account['balance'] = account['reserved']+account['protected']+.8
    engine.match(orders['flour'], sells)
    assert orders['flour']['price'] == pytest.approx(1.1)
    assert engine.free_cash(bakery['id']) == pytest.approx(0)
    assert other['reserved'] == 2
    assert account['payroll_protected'] == pytest.approx(.6)
    assert account['protected'] == pytest.approx(3.6)
    assert account['reserved']+account['protected'] <= account['balance']+1e-7


@pytest.mark.parametrize('case', ['explicit', 'stale', 'unlinked', 'old_plan'])
def test_explicit_or_unverifiable_order_retains_limit(engine, case):
    bakery, account, orders, sells = market(engine)
    order = orders['flour']
    if case == 'explicit':
        order.pop('adaptive_input_budget')
    elif case == 'stale':
        bakery['production_plan']['month'] -= 1
    elif case == 'unlinked':
        bakery['production_plan']['input_orders'] = []
    else:
        bakery['production_plan'].pop('input_prices')
    engine.match(order, sells)
    assert order['price'] == 1 and order['remaining'] == 8


class LegacyExperiment(ObservedEconomy):
    """Независимый прежний исследовательский расчёт, без нового игрового пересмотра."""
    def reprice_input_order(self, buy, sells):
        pass


def test_game_policy_matches_independent_experiment(engine):
    actual = engine
    expected = copy.deepcopy(engine)
    expected.__class__ = LegacyExperiment
    expected.probe = Probe('input_bid')
    def economic(value):
        if isinstance(value, dict):
            return {k: economic(v) for k, v in value.items() if k != 'initial_input_limit'}
        if isinstance(value, (list, tuple)):
            return [economic(v) for v in value]
        return value
    for month in range(1, 7):
        expected.probe.begin(month)
        actual.step()
        expected.step()
        for name in ('accounts', 'enterprises', 'meta'):
            assert economic(getattr(actual, name)) == economic(getattr(expected, name))
        assert actual.rng.getstate() == expected.rng.getstate()
