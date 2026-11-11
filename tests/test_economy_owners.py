import copy
import json
from pathlib import Path

import pytest

from core.db import WorldDB
from core.economy_state import EconomyState
from modules.economy import Economy
from core.lab_analytics import capture
from core.lab_export import AnalysisExporter
from core.lab_store import LabStore


@pytest.fixture
def engine():
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    cfg = json.loads(Path('content/economy.json').read_text(encoding='utf-8'))
    class LegacyOwnerEconomy(Economy):
        PROFIT_DISTRIBUTION_REVISION = 0
    result = LegacyOwnerEconomy(dict(entities=entities,
        links=[dict(source='s', target=i, kind='owns') for i in ('p', 'c')]),
        [tile], meta=dict(id='game', version=3, month=0, config=cfg))
    result.seed = 17
    result.rng.seed(17)
    result.bootstrap()
    result.cfg['investment_interval'] = 1000
    result.meta['month'] = 1
    return result


def test_project_material_assignment_keeps_surplus_offer(engine):
    a = engine.accounts['merchant:s']
    a.update(stock={'planks': 10}, values={'planks': 10})
    loc = engine.location('p')
    a['project'] = dict(good='bread', capacity=5, location=loc, materials={},
        working_cash=120, expected_price=20, expansion=False, created=1)
    sells = engine.sell_orders()
    engine.project_requests(sells)
    assert a['project']['materials']['planks'] == 2
    offers = [row for row in engine.offers(engine.accounts['h'], 'planks', sells)
        if row[1]['participant'] == a['id']]
    assert len(offers) == 1
    offer = offers[0][1]
    assert offer['quantity'] == offer['remaining'] == 8
    assert a['stock_reserved']['planks'] == 8
    buy = engine.order('h', 'planks', 8, 'buy', 10, 'Дополнительные нужды')
    engine.match(buy, {'planks': [offer]})
    assert buy['status'] == 'filled'
    assert a['stock']['planks'] == 2
    assert a['project']['materials']['planks'] == 2
    engine.validate()


def fish_firm(engine):
    return next(e for e in engine.enterprises.values() if e['good'] == 'fish')


def pending_project(engine, account, cash=120):
    account['project'] = dict(good='bread', capacity=5, location=engine.location('p'), materials={},
        working_cash=cash, expected_price=20, expansion=False, created=1, delivery=0)


@pytest.mark.parametrize('kind', ['private', 'merchant', 'treasury', 'municipal'])
def test_dividends_follow_owner_id_for_every_owner_kind(engine, kind):
    e = fish_firm(engine)
    original = next(a for a in engine.accounts.values() if a['kind'] == kind)
    owner = engine.account('specific-owner', kind, 0,
        **{k: original[k] for k in ('state_id', 'province_id', 'city_id', 'tile_id')})
    e.update(owner_id=owner['id'], profit=100)
    engine.accounts[e['id']]['balance'] = 200
    before = engine.gold()
    engine.pay_dividends({})
    assert e['dividends'] == 50
    assert owner['balance'] == 50
    assert engine.gold() == pytest.approx(before)
    assert any(f['sender'] == e['id'] and f['recipient'] == owner['id']
        and f['gold'] == 50 and f['reason'] == 'Дивиденды' for f in engine.report['flows'])
    engine.pay_dividends({})
    assert owner['balance'] == 50


@pytest.mark.parametrize('profit,cash', [(0, 1000), (-10, 1000), (100, 0), (100, 60), (100, 1000)])
def test_dividend_limits_and_idle_working_reserve(engine, profit, cash):
    e = fish_firm(engine)
    e.update(profit=profit, output=0, status='idle')
    a = engine.accounts[e['id']]
    a['balance'] = cash
    engine.pay_dividends({})
    reserve = e['capacity']*engine.goods['fish']['labor']*engine.cfg['wage']
    assert e['working_reserve'] == pytest.approx(reserve)
    assert reserve > 0
    assert e['dividends'] == pytest.approx(min(.5*max(profit, 0), max(0, cash-reserve)))


def test_paid_inputs_and_existing_protection_are_not_counted_twice(engine):
    e = next(e for e in engine.enterprises.values() if e['good'] == 'bread')
    a = engine.accounts[e['id']]
    recipe = engine.goods['bread']
    a.update(balance=1000, protected=25)
    for good, ratio in recipe['inputs'].items():
        a['stock'][good] = e['capacity']*ratio
    wage = e['capacity']*recipe['labor']*engine.cfg['wage']
    assert engine.working_cash_reserve(e, {}) == pytest.approx(wage)
    assert engine.cash_after_commitments(e['id'], 25, wage) == pytest.approx(1000-25-wage)
    a['protected'] = 25+wage
    assert engine.cash_after_commitments(e['id'], 25, wage) == pytest.approx(1000-25-wage)
    a['stock']['flour'] = 0
    assert engine.working_cash_reserve(e, {}) > wage


def test_pending_project_cash_is_excluded_from_dividends_and_consumption(engine):
    e = fish_firm(engine)
    a = engine.accounts[e['id']]
    a.update(balance=200, stock={}, values={}, protected=120)
    e['profit'] = 1000
    pending_project(engine, a)
    expected = 120+2*engine.goods['planks']['price']+engine.goods['tools']['price']
    assert engine.project_cash_reserve(a['id'], {}) == pytest.approx(expected)
    engine.pay_dividends({})
    assert e['dividends'] == pytest.approx(200-expected-e['working_reserve'])
    owner = engine.accounts['merchant:s']
    owner.update(balance=expected, stock={}, values={}, protected=120)
    pending_project(engine, owner)
    engine.owner_requests({})
    row = next(r for r in engine.report['owner_consumption'] if r['owner'] == owner['id'])
    assert row['budget'] == 0
    assert not row['orders']
    assert 'project' in owner


def test_project_reserved_purchase_does_not_duplicate_cash_hold(engine):
    a = engine.accounts['merchant:s']
    a.update(balance=200, stock={}, values={}, protected=120)
    pending_project(engine, a)
    before = engine.cash_after_commitments(a['id'], engine.project_cash_reserve(a['id'], {}))
    engine.order(a['id'], 'planks', 2, 'buy', engine.goods['planks']['price'], 'Строительные материалы')
    after = engine.cash_after_commitments(a['id'], engine.project_cash_reserve(a['id'], {}))
    assert after == pytest.approx(before)


def test_owner_budget_excludes_existing_payment_reserves(engine):
    a = engine.accounts['private:s']
    a.update(balance=1000, protected=100)
    engine.order(a['id'], 'grain', 20, 'buy', 10, 'Сырьё')
    engine.owner_requests({})
    row = next(r for r in engine.report['owner_consumption'] if r['owner'] == a['id'])
    assert row['free_cash'] == 700 and row['budget'] == 35
    assert a['reserved'] == pytest.approx(235)


def test_dividends_enter_consumption_budget_on_next_market(engine):
    e = fish_firm(engine)
    e['price'] = 20
    owner = engine.accounts[e['owner_id']]
    opening = owner['balance']
    first = engine.step()
    row = next(r for r in first['owner_consumption'] if r['owner'] == owner['id'])
    assert row['budget'] == pytest.approx(.05*opening)
    assert e['dividends'] > 0
    dividend_index = next(i for i, f in enumerate(first['flows'])
        if f['reason'] == 'Дивиденды' and f['sender'] == e['id'])
    assert all(i < dividend_index for i, f in enumerate(first['flows']) if f.get('buy_order') in row['orders'])
    closing = owner['balance']
    second = engine.step()
    row = next(r for r in second['owner_consumption'] if r['owner'] == owner['id'])
    assert row['budget'] == pytest.approx(.05*closing)


def test_project_transport_and_locked_inputs_are_protected(engine):
    e = next(e for e in engine.enterprises.values() if e['good'] == 'bread')
    a = engine.accounts[e['id']]
    pending_project(engine, a)
    engine.graph.add_edge('t', 'site', weight=10)
    a['project']['location']['tile_id'] = 'site'
    bill = engine.construction_bill(5)
    expected_delivery = sum(q*engine.goods[g]['weight'] for g, q in bill.items())*10*engine.cfg['transport_rate']
    expected = 120+expected_delivery
    for good, q in bill.items():
        a['stock'][good] = q
        a['project']['materials'][good] = q
    assert engine.project_cash_reserve(a['id'], {}) == pytest.approx(expected)
    # Даже сырьё того же вида, закреплённое проектом, не уменьшает оборотный резерв.
    a['stock']['flour'] = 10
    free = engine.working_cash_reserve(e, {})
    a['project']['materials']['flour'] = 10
    assert engine.working_cash_reserve(e, {}) > free


@pytest.mark.parametrize('buyer,purpose,taxed', [
    ('private:s', 'Личное потребление собственника', True),
    ('merchant:s', 'Личное потребление собственника', True),
    ('h', 'Базовые нужды', True), ('h', 'Дополнительные нужды', True),
    ('private:s', 'Сырьё', False), ('merchant:s', 'Строительные материалы', False),
    ('h', 'Сырьё', False)])
def test_consumption_tax_depends_on_order_purpose(engine, buyer, purpose, taxed):
    e = fish_firm(engine)
    a = engine.accounts[buyer]
    if purpose == 'Строительные материалы':
        a['project'] = dict(materials={})
    initial = engine.gold()
    sell = engine.order(e['id'], 'fish', 2, 'sell', 10, 'Продажа')
    buy = engine.order(buyer, 'fish', 2, 'buy', 10, purpose)
    engine.match(buy, {'fish': [sell]})
    assert engine.report['trades'][-1]['tax'] == (2 if taxed else 0)
    assert engine.report['trades'][-1]['seller_gold'] == (18 if taxed else 20)
    assert engine.gold() == pytest.approx(initial)


def test_partial_owner_purchase_consumes_only_bought_goods_once(engine):
    a = engine.accounts['private:s']
    a.update(balance=600, stock={'fish': 5}, values={'fish': 5})
    e = fish_firm(engine)
    sell = engine.order(e['id'], 'fish', 1, 'sell', 2, 'Продажа')
    engine.owner_requests({'fish': [sell]})
    row = next(r for r in engine.report['owner_consumption'] if r['owner'] == a['id'])
    orders = [o for o in engine.report['orders'] if o['id'] in row['orders']]
    assert row['budget'] == 30
    assert sum(o['reserved'] for o in orders) == pytest.approx(30)
    initial = engine.gold()
    for o in orders:
        engine.match(o, {'fish': [sell]})
        engine.cancel_order(o)
    engine.consume_owner_purchases()
    assert a['stock']['fish'] == 5
    assert row['consumed'] == {'fish': 1}
    assert row['spent'] == 2 and row['unused_budget'] == 28
    assert row['tax'] == .2
    assert engine.report['owner_consumed']['fish'] == 1
    assert engine.report['consumed']['fish'] == 1
    engine.consume_owner_purchases()
    assert a['stock']['fish'] == 5
    assert row['consumed']['fish'] == 1
    assert engine.gold() == pytest.approx(initial)


@pytest.mark.parametrize('scenario', ['missing', 'route', 'price'])
def test_owner_orders_fail_without_spending_or_consuming(engine, scenario):
    e = fish_firm(engine)
    sells = {}
    if scenario != 'missing':
        sell = engine.order(e['id'], 'fish', 1, 'sell', 10, 'Продажа')
        sells = {'fish': [sell]}
        if scenario == 'route':
            engine.graph.add_node('isolated')
            sell['tile_id'] = 'isolated'
    owner = engine.accounts['private:s']
    before = owner['balance']
    engine.owner_requests(sells)
    row = next(r for r in engine.report['owner_consumption'] if r['owner'] == owner['id'])
    for o in engine.report['orders']:
        if o['id'] in row['orders']:
            if scenario == 'price':
                for sell in sells.get(o['good'], []):
                    sell['price'] = 1e6
                    engine.invalidate_market_books()
            engine.match(o, sells)
            engine.cancel_order(o)
    engine.consume_owner_purchases()
    assert row['spent'] == 0 and row['consumed'] == {}
    assert owner['balance'] == before
    assert row['unused_budget'] == row['budget']


@pytest.mark.parametrize('location_field', ['cell_id', 'tile_id'])
def test_owner_moves_cell_and_buys_from_current_location(engine, location_field):
    a = engine.accounts['private:s']
    a[location_field] = 'remote'
    engine.graph.add_edge('t', 'remote', weight=10)
    engine.tiles['remote'] = dict(engine.tiles['t'], id='remote', province_id='other')
    e = fish_firm(engine)
    sell = engine.order(e['id'], 'fish', 1, 'sell', 2, 'Продажа')
    engine.owner_requests({'fish': [sell]})
    buy = next(o for o in engine.report['orders']
        if o['participant'] == a['id'] and o['side'] == 'buy' and o['good'] == 'fish')
    assert buy['tile_id'] == 'remote' and buy['province_id'] == 'other'
    engine.match(buy, {'fish': [sell]})
    assert engine.report['trades'][-1]['path'] == ['t', 'remote']
    assert engine.report['trades'][-1]['freight'] == pytest.approx(.08)
    engine.cancel_order(buy)
    a[location_field] = 't'
    previous = len(engine.report['orders'])
    engine.owner_requests({'fish': [sell]})
    moved = [o for o in engine.report['orders'][previous:] if o['participant'] == a['id']]
    assert moved and all(o['tile_id'] == 't' and o['province_id'] == 'p' for o in moved)


def test_closed_enterprise_returns_capital_without_dividends(engine):
    e = fish_firm(engine)
    e['profit'] = 100
    engine.close_enterprise(e)
    before = engine.accounts[e['owner_id']]['balance']
    engine.pay_dividends({})
    assert engine.accounts[e['owner_id']]['balance'] == before
    assert e['dividends'] == 0


def test_defaults_and_dividend_guard_survive_old_configuration_reload(engine, tmp_path):
    for key in ('dividend_profit_share', 'working_reserve_months', 'owner_consumption_rate'):
        del engine.cfg[key]
    e = fish_firm(engine)
    e['profit'] = 100
    engine.activate_rules()
    assert engine.cfg['owner_consumption_rate'] == .05
    engine.pay_dividends({})
    before = {i: a['balance'] for i, a in engine.accounts.items()}
    # Перезагрузка идёт через checkpoint, а не через таблицы Ladybug.
    world = dict(snapshot=dict(entities=list(engine.entities.values()),
                                 links=[dict(source=owner, target=target, kind='owns')
                                        for target, owner in engine.owners.items()]),
                 tiles=list(engine.tiles.values()), campaign=None)
    state = EconomyState.create(world, accounts=list(engine.accounts.values()),
                                enterprises=list(engine.enterprises.values()),
                                meta=engine.meta)
    state.save_checkpoint(tmp_path/'owners')
    reopened_state = EconomyState.load_checkpoint(tmp_path/'owners', world)
    reopened = reopened_state.engine
    reopened.pay_dividends({})
    assert {i: a['balance'] for i, a in reopened.accounts.items()} == before


def test_short_run_conserves_money_goods_and_exposes_lab_metrics(engine):
    initial_gold, stock = engine.gold(), engine.total_stock()
    for _ in range(4):
        r = engine.step()
        assert all(r['invariants'].values())
        assert engine.gold() == pytest.approx(initial_gold)
        for good, before in stock.items():
            assert engine.total_stock()[good] == pytest.approx(before+r['production'].get(good, 0)
                -r['inputs_used'].get(good, 0)-r['construction_used'].get(good, 0)
                -r['consumed'].get(good, 0)-r['equipment_wear'].get(good, 0))
        stock = engine.total_stock()
        for row in r['owner_consumption']:
            assert row['spent'] <= row['budget']+1e-7
        data = capture(engine, r)
        assert data['overview'][0]['owner_consumption_spent'] >= 0
        assert sum(data['overview'][0]['money_shares'].values()) == pytest.approx(1)
        assert data['overview'][0]['dividends'] == pytest.approx(sum(e['dividends'] for e in engine.enterprises.values()))


def test_owner_consumption_analytics_pass_export_consistency_checks(engine, tmp_path):
    engine.meta['report_detail'] = 'deep'
    meta = dict(id='owners', status='completed', completed=0, months=2, detail='full',
        seed=17, initial_month=engine.meta['month'], engine={'version': 3})
    with LabStore(tmp_path/'analytics.sqlite', create=True) as store:
        store.append_month(0, capture(engine, dict(month=0)), meta)
        for month in (1, 2):
            report = engine.step()
            meta['completed'] = month
            store.append_month(month, capture(engine, report), meta)
    with AnalysisExporter(tmp_path) as exporter:
        exporter.validate()
        assert all(c['status'] != 'failed' for c in exporter.checks)


def test_repeated_command_does_not_repeat_dividends_or_consumption(service):
    service.initialize_economy()
    state = service.economy_state()
    e = next(e for e in state.engine.enterprises.values() if e['good'] == 'fish')
    # Прибыльная реальная продажа вместо условного положительного поля profit.
    e['price'] *= 2
    service.advance_turn(command_id='owners-once')
    accounts = copy.deepcopy(state.engine.accounts)
    report = state.last_report
    assert report['owner_consumption']
    assert any(r['dividends'] > 0 for r in report['dividends'])
    # Повторная команда не повторяет ни дивиденды, ни потребление: экономика в
    # памяти не двигается, а отчёт возвращается сохранённый.
    service.advance_turn(command_id='owners-once')
    assert state.engine.accounts == accounts
    assert state.engine.meta['month'] == 1
    # Ход начинается с 1 и увеличивается до 2: сохранён тот же отчёт, а не новый.
    assert service.db.turn_result('owners-once')['turn'] == 2
