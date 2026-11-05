import copy
import json
from pathlib import Path

import pytest

from core.db import WorldDB
from core.lab_analytics import capture
from modules.economy import Economy


@pytest.fixture
def engine():
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    cfg = json.loads(Path('tests/fixtures/economy.json').read_text(encoding='utf-8'))
    result = Economy(dict(entities=entities, links=links), [tile], meta=dict(id='game', version=1, month=0, config=cfg))
    result.seed = 17
    result.rng.seed(17)
    result.bootstrap()
    return result


def firm(engine, good='bread'):
    return next(e for e in engine.enterprises.values() if e['good'] == good)


def clear(engine):
    engine.reset_report()
    for a in engine.accounts.values():
        a.update(reserved=0, protected=0, stock_reserved={})


def test_match_diagnosis_rechecks_price_after_cheap_offer_exhausted(engine):
    clear(engine)
    seller = firm(engine)['id']
    engine.add_stock('merchant:s', 'bread', 2, 2)
    cheap = engine.order(seller, 'bread', 1, 'sell', 5, 'Продажа')
    expensive = engine.order('merchant:s', 'bread', 2, 'sell', 20, 'Продажа')
    buy = engine.order('h', 'bread', 3, 'buy', 10, 'Базовые нужды')
    engine.match(buy, {'bread': [cheap, expensive]})
    assert buy['quantity']-buy['remaining'] == 1
    assert buy['reason'] == 'landed_price_above_limit'
    rows = buy['match_observation']['candidates']
    assert rows[0]['remaining_before'] == 1 and rows[0]['remaining_after'] == 0
    assert rows[1]['remaining_after'] == 2 and rows[1]['landed_price'] == 20
    trade = engine.report['trades'][0]
    assert all(f['trade_id'] == trade['trade_id'] for f in engine.report['flows'])


def test_match_diagnosis_distinguishes_exhaustion_route_and_unoffered_stock(engine):
    clear(engine)
    seller = firm(engine)['id']
    sell = engine.order(seller, 'bread', 1, 'sell', 5, 'Продажа')
    first = engine.order('h', 'bread', 1, 'buy', 5, 'Базовые нужды')
    engine.match(first, {'bread': [sell]})
    buy = engine.order('h', 'bread', 1, 'buy', 5, 'Базовые нужды')
    engine.match(buy, {'bread': [sell]})
    assert buy['reason'] == 'offer_exhausted'
    assert buy['match_observation']['candidates'][0]['route_available'] is True
    engine.graph.add_node('isolated_seller')
    engine.accounts['merchant:s']['tile_id'] = 'isolated_seller'
    engine.add_stock('merchant:s', 'bread', 1, 1)
    unreachable = engine.order('merchant:s', 'bread', 1, 'sell', 5, 'Продажа')
    buy = engine.order('h', 'bread', 1, 'buy', 5, 'Базовые нужды')
    engine.match(buy, {'bread': [sell, unreachable]})
    assert buy['reason'] == 'offer_exhausted'
    clear(engine)
    engine.graph.add_node('isolated')
    engine.accounts['h']['tile_id'] = 'isolated'
    sell = engine.order(seller, 'bread', 1, 'sell', 5, 'Продажа')
    buy = engine.order('h', 'bread', 1, 'buy', 5, 'Базовые нужды')
    engine.match(buy, {'bread': [sell]})
    assert buy['reason'] == 'no_route'
    engine.cancel_order(sell)
    engine.accounts['merchant:s']['stock']['bread'] = 0
    buy = engine.order('h', 'bread', 1, 'buy', 5, 'Базовые нужды')
    engine.match(buy, {'bread': [sell]})
    assert buy['reason'] == 'stock_not_offered'
    for a in engine.accounts.values():
        a['stock']['bread'] = 0
    buy = engine.order('h', 'bread', 1, 'buy', 5, 'Базовые нужды')
    engine.match(buy, {})
    assert buy['reason'] == 'no_physical_stock'


def test_observations_preserve_economic_results_and_survive_real_db(engine, tmp_path):
    observed = copy.deepcopy(engine)
    silent = copy.deepcopy(engine)
    silent.observe_match = lambda *args: {'reason_code': 'unobserved'}
    silent.finish_match_observation = lambda *args: None
    for _ in range(2):
        observed_report, silent_report = observed.step(), silent.step()
        assert observed.accounts == silent.accounts
        assert observed.enterprises == silent.enterprises
        observed_meta, silent_meta = copy.deepcopy(observed.meta), copy.deepcopy(silent.meta)
        for meta in (observed_meta, silent_meta):
            for key in ('public_orders', 'price_feedback'):
                for order in meta.get(key, []):
                    order.pop('match_observation', None)
                    order.pop('reason', None)
        assert observed_meta == silent_meta
        assert observed_report['trades'] == silent_report['trades']
        assert observed_report['flows'] == silent_report['flows']
    with WorldDB(tmp_path/'observations.lbug') as db:
        observed.save(db)
        stored = db.economy_records('EconomyMonth')[0]
        buys = [o for o in stored['orders'] if o['side'] == 'buy']
        assert buys and all('match_observation' in o for o in buys)
        captured = capture(observed, observed_report)
        assert any('match_observation' in o for o in captured['orders'])
        assert any('production_observation' in e for e in captured['enterprises'])


@pytest.mark.parametrize('missing_options,expected', [(True, 'executable_not_matched'),
    (False, 'insufficient_order_reserve')])
def test_match_observation_detects_missed_pair_or_broken_backing(engine, monkeypatch, missing_options, expected):
    clear(engine)
    sell = engine.order(firm(engine)['id'], 'bread', 1, 'sell', 5, 'Продажа')
    buy = engine.order('h', 'bread', 1, 'buy', 5, 'Базовые нужды')
    if missing_options:
        monkeypatch.setattr(engine, 'offers', lambda *args: [])
    else:
        buy['reserved'] = 0
        engine.accounts['h']['reserved'] = 0
    engine.match(buy, {'bread': [sell]})
    assert not engine.report['trades']
    assert buy['reason'] == expected




def test_reserve_cancel_partial_and_no_double_spend(engine):
    clear(engine)
    engine.accounts['h']['balance'] = 100
    buy = engine.order('h', 'bread', 10, 'buy', 10, 'Базовые нужды')
    assert buy['reserved'] == 100
    assert engine.order('h', 'fish', 10, 'buy', 1, 'Базовые нужды') is None
    with pytest.raises(ValueError, match='зарезервированы'):
        engine.transfer('h', 'private:s', 1, 'Проверка')
    e = firm(engine)
    sell = engine.order(e['id'], 'bread', 2, 'sell', 5, 'Продажа')
    gold = engine.gold()
    engine.match(buy, {'bread': [sell]})
    assert buy['reserved'] == 80
    assert engine.free_cash('h') == 10
    engine.cancel_order(buy)
    assert engine.free_cash('h') == 90
    assert engine.gold() == pytest.approx(gold)


@pytest.mark.parametrize('buyer,purpose,taxed', [('h', 'Базовые нужды', True),
    ('private:s', 'Сырьё', False), ('merchant:s', 'Строительные материалы', False)])
def test_tax_splits_gross_payment_by_purpose(engine, buyer, purpose, taxed):
    clear(engine)
    e = firm(engine)
    initial = {i: a['balance'] for i, a in engine.accounts.items()}
    sell = engine.order(e['id'], 'bread', 10, 'sell', 10, 'Продажа')
    if purpose == 'Строительные материалы':
        engine.accounts[buyer]['project'] = {'materials': {}}
    buy = engine.order(buyer, 'bread', 10, 'buy', 10, purpose)
    engine.match(buy, {'bread': [sell]})
    assert engine.accounts[buyer]['balance'] == initial[buyer]-100
    assert engine.accounts[e['id']]['balance'] == initial[e['id']]+(90 if taxed else 100)
    assert engine.accounts['treasury:s']['balance'] == initial['treasury:s']+(10 if taxed else 0)
    assert engine.gold() == pytest.approx(sum(initial.values()))


def test_delivery_reserved_and_not_taxed(engine):
    clear(engine)
    e = firm(engine)
    engine.graph.add_edge('t', 'remote', weight=100)
    engine.accounts['h']['tile_id'] = 'remote'
    sell = engine.order(e['id'], 'bread', 1, 'sell', 10, 'Продажа')
    freight = 100*engine.goods['bread']['weight']*engine.cfg['transport_rate']
    initial = engine.accounts['h']['balance']
    buy = engine.order('h', 'bread', 1, 'buy', 10+freight, 'Базовые нужды')
    engine.match(buy, {'bread': [sell]})
    assert engine.accounts['h']['balance'] == pytest.approx(initial-10-freight)
    assert engine.report['trades'][0]['tax'] == 1
    assert engine.accounts['carrier:s']['balance'] == pytest.approx(freight)


def test_demand_snapshot_does_not_use_late_income(engine):
    clear(engine)
    engine.accounts['h']['balance'] = 10
    buy = engine.order('h', 'bread', 100, 'buy', 2, 'Базовые нужды')
    engine.record_market()
    assert buy['quantity'] == 5
    engine.cancel_order(buy)
    engine.transfer('private:s', 'h', 100, 'Поздний доход')
    assert engine.meta['market_history'][-1]['markets'][0]['unmet_solvent'] == 5


def test_reserves_survive_real_database_reload(engine, tmp_path):
    clear(engine)
    engine.order('h', 'bread', 20, 'buy', 3, 'Базовые нужды')
    with WorldDB(tmp_path/'reserves.lbug') as db:
        db.store_economy('EconomyAccount', list(engine.accounts.values()))
        db.store_economy('EconomyMeta', [engine.meta])
        reopened = Economy.load(db)
        assert reopened.accounts['h']['reserved'] == 60
        assert reopened.free_cash('h') == engine.free_cash('h')


def test_no_profit_distribution_or_idle_capacity_charge(engine):
    clear(engine)
    e = firm(engine)
    e.update(revenue=100, cost_of_sales=20, tax=10)
    balances = {i: a['balance'] for i, a in engine.accounts.items()}
    engine.cfg.update(public_spending=0, carrier_distribution=0)
    engine.settle(list(engine.enterprises.values()), [engine.accounts['h']])
    assert e['profit'] == 70
    assert {i: a['balance'] for i, a in engine.accounts.items()} == balances


def test_closed_stock_sells_and_money_returns_to_owner(engine):
    clear(engine)
    e = firm(engine)
    initial = engine.accounts[e['owner_id']]['balance']
    cash = engine.accounts[e['id']]['balance']
    engine.close_enterprise(e)
    assert engine.accounts[e['owner_id']]['balance'] == initial+cash
    assert engine.accounts[e['id']]['balance'] == 0
    assert engine.order(e['id'], 'flour', 1, 'buy', 1, 'Сырьё') is None
    stock = engine.accounts[e['id']]['stock']['flour']
    sells = engine.sell_orders()
    assert any(s['participant'] == e['id'] for s in sells['flour'])
    buy = engine.order('h', 'flour', min(1, stock), 'buy', 10, 'Базовые нужды')
    engine.match(buy, {'flour': [s for s in sells['flour'] if s['participant'] == e['id']]})
    engine.return_capital(e)
    assert engine.accounts[e['id']]['balance'] == 0
    assert engine.accounts[e['owner_id']]['balance'] > initial+cash
    assert e['capacity'] > 0 and e['status'] == 'closed'


def test_accessible_orders_restore_plan_and_negative_margin_blocks_output(engine):
    clear(engine)
    e = firm(engine, 'fish')
    engine.accounts[e['id']]['stock']['fish'] = 0
    e.update(price=2, sales_history=[dict(sold=.001)]*3)
    engine.order('h', 'fish', 5, 'buy', 2, 'Базовые нужды')
    assert engine.plan(e) == 5
    e['price'] = .001
    assert engine.plan(e) == 0
    assert e['production_limit'] == 'Отрицательная ожидаемая маржа'


def test_large_stock_alone_does_not_lower_price(engine):
    e = firm(engine)
    e['sales_history'] = [dict(sold=1, offered=100000)]
    price = e['price']
    engine.update_prices()
    assert e['price'] == price


def test_current_stock_sales_are_visible_before_replenishing_output(engine):
    clear(engine)
    e = firm(engine, 'fish')
    e.update(price=2, sales_history=[dict(sold=0)]*3)
    a = engine.accounts[e['id']]
    a['stock']['fish'] = 5
    a['values']['fish'] = .5
    sell = engine.order(e['id'], 'fish', 5, 'sell', 2, 'Продажа')
    buy = engine.order('h', 'fish', 5, 'buy', 2, 'Базовые нужды')
    engine.match(buy, {'fish': [sell]})
    assert engine.plan(e) == 5


def test_paid_inputs_require_only_wages_in_cash(engine):
    clear(engine)
    e = firm(engine)
    a = engine.accounts[e['id']]
    a['stock']['bread'] = 0
    e.update(price=10, sales_history=[dict(sold=5)]*3)
    a['balance'] = 5*engine.goods['bread']['labor']*engine.cfg['wage']
    engine.input_requests(e, {})
    assert a['payroll_protected'] == pytest.approx(a['balance'])
    assert not any(o['participant'] == e['id'] for o in engine.report['orders'])


def test_falling_buyer_budget_is_a_price_signal(engine):
    e = firm(engine)
    e['price'] = 10
    e['sales_history'] = [dict(sold=10), dict(sold=5)]
    engine.meta['price_feedback'] = [dict(good='bread', tile_id='t', unfunded_need=True, budget_price=5)]
    engine.update_prices()
    assert e['price'] == pytest.approx(9.5)


def test_profitable_expansion_is_selected_from_funded_orders(engine):
    clear(engine)
    e = firm(engine)
    e.update(price=20, capacity=5, planned_output=5)
    engine.accounts[e['id']]['stock']['bread'] = 0
    engine.order('h', 'bread', 100, 'buy', 20, 'Базовые нужды')
    choice = engine.project_candidate(engine.accounts[e['id']], {})
    assert choice is not None and choice['expansion']
    assert choice['expected_profit'] > 0
    assert choice['expected_investment'] <= engine.free_cash(e['id'])
    engine.accounts[e['id']]['stock']['bread'] = 1000
    assert engine.project_candidate(engine.accounts[e['id']], {}) is None


def test_closed_raw_material_uses_regular_price_adjustment(engine):
    e = firm(engine)
    engine.close_enterprise(e)
    a = engine.accounts[e['id']]
    a['ask_prices'] = {'flour': 10}
    a['stock_sales_history'] = {'flour': [dict(sold=0)]}
    engine.order('h', 'flour', 1, 'buy', 1, 'Базовые нужды')
    engine.meta['public_orders'] = copy.deepcopy(engine.report['orders'])
    engine.update_prices()
    assert a['ask_prices']['flour'] == pytest.approx(9.5)


def test_losing_equal_price_competition_can_lower_price(engine):
    e, competitor = firm(engine), firm(engine, 'flour')
    e.update(price=10, sales_history=[dict(sold=0)])
    competitor.update(good='bread', price=10)
    engine.accounts[competitor['id']]['stock']['bread'] = 1
    engine.meta['market_history'] = [dict(month=0, markets=[dict(state_id='s', province_id='p', good='bread',
        volume=10, trade_gold=100, freight=0, requested=10, unmet_solvent=0, limit_value=100)])]
    engine.update_prices()
    assert e['price'] == pytest.approx(9.5)


def project(engine, expansion=False):
    e = firm(engine)
    loc = {k: e[k] for k in ('state_id', 'province_id', 'city_id', 'tile_id')}
    return dict(good='bread', capacity=5, location=loc, materials={'planks': 1},
        working_cash=0 if expansion else 120, expected_price=20, expected_profit=10,
        expected_investment=130, expected_unit_cost=2, expansion=expansion, created=1, delivery=0)


def test_partial_project_and_locked_materials_survive_reload(engine, tmp_path):
    clear(engine)
    a = engine.accounts['merchant:s']
    a['project'] = project(engine)
    a['stock'] = {'planks': 1}
    a['values'] = {'planks': 2}
    assert engine.free_stock(a['id'], 'planks') == 0
    assert engine.order(a['id'], 'planks', 1, 'sell', 2, 'Продажа') is None
    engine.finish_projects()
    assert 'project' in a
    with WorldDB(tmp_path/'project.lbug') as db:
        db.store_economy('EconomyAccount', list(engine.accounts.values()))
        db.store_economy('EconomyMeta', [engine.meta])
        reopened = Economy.load(db)
        assert reopened.accounts[a['id']]['project'] == a['project']
    p = a['project']
    for g, q in engine.construction_bill(5).items():
        missing = q-a['stock'].get(g, 0)
        engine.add_stock(a['id'], g, missing, missing)
        p['materials'][g] = q
    count = len(engine.enterprises)
    engine.finish_projects()
    assert 'project' not in a
    assert len(engine.enterprises) == count+1
    new = list(engine.enterprises.values())[-1]
    assert new['output'] == 0 and new['available_from'] == engine.meta['month']+1


def test_expansion_uses_own_materials_and_cash_without_new_account(engine):
    clear(engine)
    e = firm(engine)
    a = engine.accounts[e['id']]
    a['project'] = project(engine, expansion=True)
    for g, q in engine.construction_bill(5).items():
        engine.add_stock(a['id'], g, q, q)
        a['project']['materials'][g] = q
    count, capacity, gold = len(engine.accounts), e['capacity'], engine.gold()
    engine.finish_projects()
    assert e['capacity'] == capacity+5
    assert len(engine.accounts) == count
    assert engine.gold() == gold
    assert engine.report['construction_used'] == engine.construction_bill(5)


def test_one_pass_wages_and_conservation(engine):
    before = engine.gold()
    initial = engine.total_stock()
    r = engine.step()
    assert engine.gold() == pytest.approx(before)
    assert all(r['invariants'].values())
    assert all(a['reserved'] == 0 for a in engine.accounts.values())
    assert sum(f['gold'] for f in r['flows'] if f['reason'] == 'Заработная плата') == pytest.approx(
        sum(e['output']*engine.goods[e['good']]['labor']*engine.cfg['wage'] for e in engine.enterprises.values()))
    for g, q in engine.total_stock().items():
        assert q == pytest.approx(initial[g]+r['production'].get(g, 0)-r['consumed'].get(g, 0)-r['inputs_used'].get(g, 0)-r['construction_used'].get(g, 0))
    assert all(t['quantity'] <= next(o['quantity'] for o in r['orders'] if o['id'] == t['sell_order']) for t in r['trades'])
    data = capture(engine, r)
    assert 'wages' in data['overview'][0]
    assert any(d['kind'] == 'sector_flow' for d in data['diagnostics'])


def test_zero_cash_and_profitable_idle_firm_do_not_force_closure(engine):
    clear(engine)
    e = firm(engine)
    engine.cfg.update(public_spending=0, carrier_distribution=0)
    engine.accounts[e['id']]['balance'] = 0
    e['expected_margin'] = 1
    for _ in range(15):
        engine.settle([e], [engine.accounts['h']])
    assert e['status'] != 'closed'
    engine.cfg['enterprise_overhead_rate'] = .1
    engine.accounts[e['id']]['balance'] = 100
    for _ in range(engine.cfg['loss_months_to_close']):
        engine.settle([e], [engine.accounts['h']])
    assert e['status'] == 'closed'


def test_frozen_plan_replenishes_expected_sales_every_month_without_transfers(engine):
    e = firm(engine, 'fish')
    engine.enterprises = {e['id']: e}
    ids = {'h', e['id'], e['owner_id'], 'treasury:s', 'carrier:s'}
    engine.accounts = {i: a for i, a in engine.accounts.items() if i in ids}
    engine.cfg.update(basic_needs={'fish': .00001}, extra_needs={}, investment_interval=1000,
        consumption_tax=.1, wage=1.125, owner_consumption_rate=0)
    e.update(capacity=1, price=1, sales_forecast=1, sales_history=[dict(sold=1, offered=1)])
    for a in engine.accounts.values():
        a.update(balance=10, stock={}, values={})
    engine.accounts['treasury:s']['balance'] = 4.9
    engine.accounts['carrier:s']['balance'] = 0
    engine.accounts[e['id']]['stock'] = {'fish': 1}
    engine.accounts[e['id']]['values'] = {'fish': .9}
    engine.meta.update(initial_gold=engine.gold(), market_history=[])
    before = {i: a['balance'] for i, a in engine.accounts.items()}
    gold = engine.gold()
    for month in range(10):
        r = engine.step()
        # План учитывает ожидаемую продажу до торгов и затем остаётся фиксированным.
        assert r['production'].get('fish', 0) == 1
        assert r['consumed'].get('fish', 0) == 1
        assert engine.accounts[e['id']]['stock']['fish'] == 1
        assert e['production_plan']['output'] == e['planned_output'] == 1
        assert engine.gold() == pytest.approx(gold)
        assert engine.accounts[e['id']]['balance'] == pytest.approx(before[e['id']])
        assert e['profit'] == pytest.approx(0)


def sawmill_case(engine):
    clear(engine)
    e = firm(engine, 'planks')
    e.update(price=2.3, capacity=5, sales_history=[dict(sold=0)]*3)
    engine.accounts[e['id']].update(stock={'planks': 0, 'wood': 0}, values={'planks': 0, 'wood': 0})
    for a in engine.accounts.values():
        a.pop('project', None)
        a['stock']['planks']=0
        a['values']['planks']=0
    investor = engine.accounts['private:s']
    investor.update(stock={}, values={})
    investor['project'] = project(engine)
    investor['project']['materials'] = {}
    sells = engine.sell_orders()
    return e, investor, sells


def test_construction_before_procurement_creates_backed_raw_orders(engine):
    e, investor, sells = sawmill_case(engine)
    engine.project_requests(sells)
    construction = next(o for o in engine.report['orders'] if o['participant']==investor['id']
        and o['good']=='planks' and o['side']=='buy')
    engine.input_requests(e, sells)
    raw = next(o for o in engine.report['orders'] if o['participant']==e['id'] and o['good']=='wood')
    assert construction['backing'] > 0 and raw['backing'] > 0
    assert e['expected_margin'] > 0 and e['production_plan']['output'] > 0
    assert engine.report['orders'].index(construction) < engine.report['orders'].index(raw)
    frozen = e['production_plan']['output']
    for buy in list(engine.report['orders']):
        if buy['side']=='buy':
            engine.match(buy,sells)
    for o in engine.report['orders']:
        engine.cancel_order(o)
    engine.produce([e],sells)
    assert e['planned_output']==frozen and e['output'] <= frozen+1e-7


def test_late_construction_cannot_increase_closed_procurement_plan(engine):
    e, investor, sells = sawmill_case(engine)
    engine.input_requests(e,sells)
    assert e['production_plan']['output']==0
    # Моделируем заявку, поступившую после закрытия единственного торгового прохода.
    engine.project_requests(sells)
    assert any(o['good']=='planks' and o['side']=='buy' for o in engine.report['orders'])
    frozen=copy.deepcopy(e['production_plan'])
    assert engine.plan(copy.deepcopy(e),sells=sells)>0
    engine.produce([e],sells)
    assert e['production_plan']==frozen
    assert e['planned_output']==e['output']==0
    # В новом месяце та же реальная проектная заявка уже видна до закупок.
    clear(engine)
    engine.meta['month'] += 1
    sells=engine.sell_orders()
    engine.project_requests(sells)
    engine.input_requests(e,sells)
    assert e['production_plan']['output']>0


def test_frozen_plan_uses_financed_target_and_excludes_project_locked_inputs(engine):
    e, investor, sells = sawmill_case(engine)
    a=engine.accounts[e['id']]
    a['balance']=1
    engine.project_requests(sells)
    engine.input_requests(e,sells)
    target=e['production_plan']['output']
    assert 0 < target < e['production_plan']['requested_output']
    assert a['reserved']+a['protected'] <= a['balance']+1e-7
    engine.accounts[e['id']]['project']=project(engine,expansion=True)
    engine.accounts[e['id']]['project']['materials']={'wood':100}
    engine.add_stock(e['id'],'wood',100,100)
    for o in engine.report['orders']:
        engine.cancel_order(o)
    engine.produce([e],sells)
    assert e['planned_output']==target and e['output']==0
    assert a['stock']['wood']==100


def test_building_competes_for_materials_without_double_reservation(engine):
    clear(engine)
    producer=firm(engine,'planks')
    producer_id=producer['id']
    for a in engine.accounts.values():
        a['stock']['planks']=0
        a['values']['planks']=0
    engine.accounts[producer_id].update(stock={'planks':3},values={'planks':3})
    investor=engine.accounts['private:s']
    investor.update(stock={}, values={})
    investor['project']=project(engine)
    investor['project']['materials']={}
    sells=engine.sell_orders()
    engine.project_requests(sells)
    construction=next(o for o in engine.report['orders'] if o['participant']==investor['id'] and o['good']=='planks' and o['side']=='buy')
    other=engine.order('h','planks',3,'buy',100,'Дополнительные нужды')
    engine.match(other,sells)
    engine.match(construction,sells)
    assert other['remaining']==0 and construction['remaining']>0
    assert investor['reserved']==pytest.approx(sum(o['reserved'] for o in engine.report['orders']
        if o['participant']==investor['id'] and o['side']=='buy'))
    assert engine.free_cash(investor['id'])>=0
    assert engine.accounts[producer_id]['stock']['planks']==0
    assert investor['project']['materials'].get('planks',0)==0


def test_construction_does_not_spend_protected_payroll_or_reserve_it_twice(engine):
    clear(engine)
    engine.meta['month']=1
    investor=engine.accounts['private:s']
    investor.update(balance=325,stock={},values={},payroll_protected=200,protected=200)
    investor['project']=project(engine)
    investor['project']['materials']={}
    sells=engine.sell_orders()
    engine.project_requests(sells)
    assert investor['protected']==320
    buys=[o for o in engine.report['orders'] if o['participant']==investor['id'] and o['side']=='buy']
    assert sum(o['reserved'] for o in buys)==pytest.approx(5)
    assert investor['reserved']==pytest.approx(5)
    assert engine.free_cash(investor['id'])==pytest.approx(0)
    assert engine.cash_after_commitments(investor['id'],engine.project_cash_reserve(investor['id'],sells))==pytest.approx(0)


def test_step_places_construction_before_all_raw_procurement_and_plans_once(engine,monkeypatch):
    engine.accounts['private:s'].update(stock={},values={})
    bread=firm(engine)
    engine.accounts[bread['id']].update(stock={},values={})
    engine.accounts['private:s']['project']=project(engine)
    engine.accounts['private:s']['project']['materials']={}
    calls=[]
    original=engine.plan
    def observed(e,*args,**kwargs):
        calls.append(e['id'])
        return original(e,*args,**kwargs)
    monkeypatch.setattr(engine,'plan',observed)
    report=engine.step()
    construction=[i for i,o in enumerate(report['orders']) if o['purpose']=='Строительные материалы']
    raw=[i for i,o in enumerate(report['orders']) if o['purpose']=='Сырьё']
    assert construction and raw and max(construction)<min(raw)
    assert len(calls)==len(set(calls))
    for e in engine.enterprises.values():
        if e.get('production_observation'):
            assert e['planned_output']==e['production_plan']['output']
            assert e['output'] <= e['planned_output']+1e-7
    assert all(report['invariants'].values())


def test_old_save_without_frozen_plans_reloads_and_repeated_runs_match(engine,tmp_path):
    for e in engine.enterprises.values():
        e.pop('production_plan',None)
    engine.meta['rng_state']=engine.rng.getstate()
    with WorldDB(tmp_path/'old-planning.lbug') as db:
        for entity in engine.entities.values():
            db.create_entity(entity['id'],entity['kind'],entity['name'],entity['properties'])
        for target,owner in engine.owners.items():
            db.put_link(owner,target,'owns')
        db.store_tiles(list(engine.tiles.values()))
        engine.save(db,include_month=False)
        reopened=Economy.load(db)
        for _ in range(3):
            expected=copy.deepcopy(engine.step())
            assert reopened.step()==expected
            assert reopened.accounts==engine.accounts
            assert reopened.enterprises==engine.enterprises
            assert all(e.get('production_plan',{}).get('month')==reopened.meta['month']
                for e in reopened.enterprises.values() if e['status']!='closed'
                and e['available_from']<=reopened.meta['month'])
            assert all(e['output']==0 for e in reopened.enterprises.values()
                if e['available_from']>reopened.meta['month'])
