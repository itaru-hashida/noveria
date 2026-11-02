import copy

import pytest

from modules.geography import topology
from test_economy_v3 import engine, firm, clear, project


def test_empty_unprofitable_stock_recovers_price_and_output(engine):
    bakery = firm(engine)
    for other in engine.enterprises.values():
        other['suspended'] = other is not bakery
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    engine.cfg.update(basic_needs={'bread': .002}, extra_needs={},
        owner_consumption_rate=0, investment_interval=10000)
    bakery.update(price=1, sales_history=[], sales_forecast=0)
    engine.add_stock(bakery['id'], 'flour', 10000, 17000)
    engine.add_stock(bakery['id'], 'wood', 10000, 11000)
    reports = [copy.deepcopy(engine.step()) for _ in range(15)]
    assert sum(r['production'].get('bread', 0) for r in reports) > 0
    assert bakery['status'] != 'closed'
    assert bakery['price'] > 1.53/.9
    assert all(all(r['invariants'].values()) for r in reports)


def test_b2b_and_mixed_demand_use_actual_tax_purposes(engine):
    clear(engine)
    producer = firm(engine, 'tools')
    producer.update(price=4.3, sales_history=[], sales_forecast=0)
    engine.accounts[producer['id']]['stock']['tools'] = 0
    engine.prepare_recovery_quotes()
    engine.order('merchant:s', 'tools', 10, 'buy', 4.3, 'Строительные материалы')
    assert engine.plan(producer) == min(10, producer['capacity'])
    assert producer['expected_margin'] == pytest.approx(.29)
    assert producer['planned_price'] == 4.3
    engine.order('h', 'tools', 10, 'buy', 4.3, 'Дополнительные нужды')
    engine.plan(producer)
    assert producer['expected_price'] == pytest.approx(4.3*.95)


def test_closed_firm_restarts_with_owners_real_money(engine):
    bakery = firm(engine)
    a = engine.accounts[bakery['id']]
    a['stock']['bread'] = 0
    engine.cfg.update(investment_interval=10000, owner_consumption_rate=0)
    clear(engine)
    engine.close_enterprise(bakery)
    gold = engine.gold()
    report = engine.step()
    assert bakery['status'] != 'closed'
    assert bakery['output'] > 0
    assert engine.gold() == pytest.approx(gold)
    assert any(row['action'] == 'reopened' and row['enterprise'] == bakery['id']
        for row in report['lifecycle'])
    assert any(row['reason'] == 'Восстановление оборотного капитала'
        and row['recipient'] == bakery['id'] for row in report['flows'])
    assert all(report['invariants'].values())


def test_closed_upstream_firm_sees_orders_created_by_downstream_planning(engine):
    bakery = firm(engine)
    mill = firm(engine, 'flour')
    engine.accounts[bakery['id']]['stock'].update(bread=0, flour=0)
    engine.accounts[mill['id']]['stock']['flour'] = 0
    clear(engine)
    engine.close_enterprise(mill)
    report = engine.step()
    assert mill['status'] != 'closed'
    assert mill['output'] > 0
    assert any(o['participant'] == bakery['id'] and o['good'] == 'flour'
        and o['side'] == 'buy' for o in report['orders'])
    assert all(report['invariants'].values())


def test_recovery_requires_demand_and_available_owner_funds(engine):
    clear(engine)
    bakery = firm(engine)
    a = engine.accounts[bakery['id']]
    engine.close_enterprise(bakery)
    a['stock']['bread'] = 0
    bakery['sales_history'] = [{'sold': 200}]*3
    engine.recover_enterprises({})
    assert bakery['status'] == 'closed'
    owner = engine.accounts[bakery['owner_id']]
    owner['protected'] = owner['balance']
    engine.order('h', 'bread', 10, 'buy', 3, 'Базовые нужды')
    engine.recover_enterprises({})
    assert bakery['status'] == 'closed'
    assert a['balance'] == 0


def test_resource_investment_serves_another_province(engine):
    clear(engine)
    engine.entities['p2'] = dict(id='p2', kind='Province', name='P2', properties={'capital_id': 'c2'})
    engine.entities['c2'] = dict(id='c2', kind='City', name='C2',
        properties={'province_id': 'p2', 'tile_id': 't2'})
    engine.owners.update(p2='s', c2='s')
    engine.tiles['t2'] = dict(engine.tiles['t'], id='t2', x=1, resources=[], province_id='p2')
    engine.graph = topology(list(engine.tiles.values()))
    engine.paths = {}
    firm(engine, 'ore')['status'] = 'closed'
    engine.account('remote-buyer', 'merchant', 1000,
        state_id='s', province_id='p2', city_id='c2', tile_id='t2')
    engine.order('remote-buyer', 'ore', 10, 'buy', 10, 'Сырьё')
    candidate = engine.project_candidate(engine.accounts['private:s'], {})
    assert candidate and candidate['good'] == 'ore'
    assert candidate['location']['province_id'] == 'p'


@pytest.mark.parametrize('recent_progress', [False, True])
def test_stalled_project_releases_materials_but_progress_extends_deadline(engine, recent_progress):
    clear(engine)
    engine.cfg['investment_interval'] = 10000
    engine.meta['month'] = 15
    a = engine.accounts['private:s']
    a['project'] = project(engine)
    a['project'].update(created=1, last_progress=1,
        progress_quantity=1 if recent_progress else 2, materials={'planks': 2}, expected_price=20)
    a.update(stock={'planks': 2}, values={'planks': 2}, protected=120)
    assert engine.free_stock(a['id'], 'planks') == 0
    engine.project_requests({})
    if recent_progress:
        assert a['project']['last_progress'] == 15
        assert engine.free_stock(a['id'], 'planks') == 0
    else:
        assert 'project' not in a
        assert engine.free_stock(a['id'], 'planks') == 2
        assert a['protected'] == 0


def test_supply_guidance_cannot_fill_orders_with_future_goods(engine):
    clear(engine)
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    producer = firm(engine, 'iron')
    producer['price'] = .1
    engine.prepare_recovery_quotes()
    buyer = engine.accounts['h']
    value, limit, supplied = engine.quoted_cost(buyer, 'iron', 10, {})
    assert not supplied and limit > .1
    before = engine.gold()
    order = engine.order('h', 'iron', 10, 'buy', limit, 'Сырьё')
    engine.match(order, {})
    assert order['remaining'] == 10
    assert not buyer['stock']
    assert engine.gold() == before
    assert not engine.report['trades']


def test_existing_production_budget_survives_own_construction(engine):
    clear(engine)
    producer = firm(engine, 'fish')
    producer.update(capacity=1, sales_history=[], sales_forecast=0)
    a = engine.accounts[producer['id']]
    payroll = engine.goods['fish']['labor']*engine.cfg['wage']
    a.update(balance=payroll, stock={}, values={})
    a['project'] = project(engine, expansion=True)
    a['project'].update(good='fish', materials={})
    engine.order('h', 'fish', 1, 'buy', 2, 'Базовые нужды')
    engine.recover_enterprises({})
    engine.project_requests({})
    assert a['protected'] == pytest.approx(payroll)
    assert not any(o['participant'] == producer['id'] and o['purpose'] == 'Строительные материалы'
        for o in engine.report['orders'])
    engine.input_requests(producer, {})
    engine.produce([producer], {})
    assert producer['output'] == pytest.approx(1)
    assert 'project' in a
