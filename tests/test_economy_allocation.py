import copy

import pytest

from test_economy_v3 import engine, firm, clear, project


def test_cheaper_stock_only_covers_one_buyer_once(engine):
    clear(engine)
    bakery = firm(engine)
    bakery['price'] = 2.1
    seller = engine.accounts['private:s']
    engine.add_stock(seller['id'], 'bread', 1, 1)
    sell = engine.order(seller['id'], 'bread', 1, 'sell', 1, 'Продажа')
    engine.order('h', 'bread', 1, 'buy', 3, 'Базовые нужды')
    engine.order('merchant:s', 'bread', 1, 'buy', 3, 'Личное потребление собственника')
    before = copy.deepcopy(engine.report)
    assert engine.reachable_demand(bakery, {'bread': [sell]}) == 1
    assert engine.report == before
    assert engine.reachable_demand(bakery, {'bread': [sell]}) == 1


def test_existing_affordable_capacity_prevents_duplicate_bakery(engine):
    clear(engine)
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    engine.order('h', 'bread', 50, 'buy', 3, 'Базовые нужды')
    assert firm(engine)['capacity'] > 50
    for investor in ('private:s', 'merchant:s'):
        assert engine.project_candidate(engine.accounts[investor], {}) is None


def test_unreachable_capacity_cannot_block_local_investment(engine):
    clear(engine)
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    bakery = firm(engine)
    engine.graph.add_node('isolated')
    bakery['tile_id'] = 'isolated'
    engine.order('h', 'bread', 50, 'buy', 3, 'Базовые нужды')
    result = engine.project_candidate(engine.accounts['private:s'], {})
    assert result and result['good'] == 'bread'
    assert result['capacity'] == 50


def test_unfunded_capacity_does_not_block_a_funded_investor(engine):
    clear(engine)
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 0
    engine.accounts[bakery['owner_id']]['balance'] = 0
    engine.order('h', 'bread', 50, 'buy', 3, 'Базовые нужды')
    result = engine.project_candidate(engine.accounts['private:s'], {})
    assert result and result['good'] == 'bread'
    assert result['capacity'] == 50


def test_investors_account_for_projects_announced_in_same_month(engine):
    clear(engine)
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    firm(engine)['status'] = 'closed'
    flour = engine.accounts[firm(engine, 'flour')['id']]
    wood = engine.accounts[firm(engine, 'wood')['id']]
    flour.update(stock={'flour': 40}, values={'flour': 40*1.7})
    wood.update(stock={'wood': 5}, values={'wood': 5*1.1})
    engine.meta['month'] = engine.cfg['investment_interval']
    engine.order('h', 'bread', 50, 'buy', 3, 'Базовые нужды')
    engine.project_requests(engine.sell_orders())
    projects = [a['project'] for a in engine.accounts.values() if a.get('project', {}).get('good') == 'bread']
    assert len(projects) == 1
    # Общий резерв и проверка полной цепочки могут уменьшить мощность ниже
    # конечного спроса; существенен один проект без двойного обещания входов.
    assert 0 < projects[0]['capacity'] <= 50


def test_idle_bakery_sells_excess_inputs_but_keeps_next_plan_and_project(engine):
    clear(engine)
    bakery = firm(engine)
    a = engine.accounts[bakery['id']]
    a.update(stock={'flour': 100, 'planks': 2}, values={'flour': 170, 'planks': 4})
    bakery['sales_history'] = [dict(sold=0, output=0)]*3
    bakery['production_plan'] = dict(output=10)
    a['project'] = project(engine, expansion=True)
    a['project']['materials'] = {'planks': 2}
    offers = engine.sell_orders()
    sale = next(o for o in offers['flour'] if o['participant'] == bakery['id'])
    reserve = 10*engine.cfg['input_buffer']*.8
    assert sale['quantity'] == pytest.approx(100-reserve)
    assert not any(o['participant'] == bakery['id'] for o in offers.get('planks', []))
    buy = engine.order('merchant:s', 'flour', 100, 'buy', 10, 'Сырьё')
    engine.match(buy, {'flour': [sale]})
    assert a['stock']['flour'] == pytest.approx(reserve)
    assert a['project']['materials']['planks'] == 2


def test_three_idle_months_release_all_unneeded_inputs(engine):
    clear(engine)
    bakery = firm(engine)
    a = engine.accounts[bakery['id']]
    a.update(stock={'flour': 100}, values={'flour': 170})
    bakery['sales_history'] = [dict(sold=0, output=0)]*3
    bakery['production_plan'] = dict(output=0)
    offers = engine.sell_orders()
    assert next(o for o in offers['flour'] if o['participant'] == bakery['id'])['quantity'] == 100


def test_production_recovers_after_temporary_demand_shutdown(engine):
    config = {k: copy.deepcopy(engine.cfg[k]) for k in ('basic_needs', 'extra_needs', 'owner_consumption_rate')}
    reports = [copy.deepcopy(engine.step()) for _ in range(6)]
    for key in ('basic_needs', 'extra_needs'):
        engine.cfg[key] = {good: 0 for good in engine.cfg[key]}
    engine.cfg['owner_consumption_rate'] = 0
    reports.extend(copy.deepcopy(engine.step()) for _ in range(12))
    engine.cfg.update(config)
    restored = [copy.deepcopy(engine.step()) for _ in range(12)]
    assert sum(r['production'].get('bread', 0) for r in restored) > 0
    assert sum(r['consumed'].get('bread', 0) for r in restored) > 0
    assert all(all(r['invariants'].values()) for r in reports+restored)


def test_household_can_pay_current_price_without_using_other_reservations(engine):
    clear(engine)
    seller = engine.accounts['private:s']
    buyer = engine.accounts['h']
    buyer.update(balance=10, protected=2)
    engine.add_stock(seller['id'], 'bread', 2, 2)
    sell = engine.order(seller['id'], 'bread', 2, 'sell', 3, 'Продажа')
    other = engine.order('h', 'fish', 1, 'buy', 2, 'Базовые нужды')
    buy = engine.order('h', 'bread', 2, 'buy', 1, 'Базовые нужды')
    buy['adaptive_household_budget'] = True
    engine.match(buy, {'bread': [sell]})
    assert buy['status'] == 'filled'
    assert buy['price'] == 3
    assert buyer['balance'] == pytest.approx(4)
    assert buyer['protected'] == 2
    assert buyer['reserved'] == other['reserved'] == 2


def test_explicit_price_limit_is_not_automatically_raised(engine):
    clear(engine)
    seller = engine.accounts['private:s']
    engine.add_stock(seller['id'], 'bread', 2, 2)
    sell = engine.order(seller['id'], 'bread', 2, 'sell', 3, 'Продажа')
    buy = engine.order('h', 'bread', 2, 'buy', 1, 'Базовые нужды')
    engine.match(buy, {'bread': [sell]})
    assert buy['remaining'] == 2 and buy['price'] == 1
