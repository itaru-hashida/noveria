import copy
import sys

import pytest

from test_economy_v3 import engine, clear


def prepare(engine):
    clear(engine)
    engine.accounts['h'].update(balance=20, stock={})
    engine.cfg['basic_needs'] = {'bread': .0001, 'fish': .0001}
    engine.add_stock('merchant:s', 'bread', 100, 100)
    engine.add_stock('merchant:s', 'fish', 100, 900)
    return {g: [engine.order('merchant:s', g, 100, 'sell', price, 'Продажа запасов')]
        for g, price in [('bread', 1), ('fish', 9)]}


def test_cheaper_full_need_first_without_removing_fish(engine):
    sells = prepare(engine)
    engine.household_requests([engine.accounts['h']], engine.cfg['basic_needs'], 'Базовые нужды', sells)
    buys = [o for o in engine.report['orders'] if o['side'] == 'buy']
    assert [o['good'] for o in buys] == ['bread', 'fish']
    assert buys[0]['quantity'] == pytest.approx(10)
    assert buys[1]['quantity'] == pytest.approx(10/9)
    assert engine.accounts['h']['needs']['fish']['needed'] == 10
    assert sum(o['reserved'] for o in buys) == pytest.approx(20)


def test_total_bill_not_unit_price_and_delivery_counts(engine):
    sells = prepare(engine)
    engine.cfg['basic_needs'] = {'bread': .001, 'fish': .00001}
    engine.graph.add_edge('remote', 't', weight=100)
    engine.paths.clear()
    sells['fish'][0]['tile_id'] = 'remote'
    engine.household_requests([engine.accounts['h']], engine.cfg['basic_needs'], 'Базовые нужды', sells)
    buys = [o for o in engine.report['orders'] if o['side'] == 'buy']
    assert buys[0]['good'] == 'fish'  # 1 * 9.8 дешевле 100 * 1.
    assert buys[0]['price'] == pytest.approx(9.8)
    assert buys[1]['quantity'] == pytest.approx(10.2)
