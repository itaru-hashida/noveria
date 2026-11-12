import copy
import json
from pathlib import Path

import pytest

from modules.economy import Economy

pytest.importorskip('modules.economy.native.serial_step')
from modules.economy.native.update_prices_boundary import (
    run_native_households, run_native_market, run_native_preparation, run_native_public_projects,
    run_native_recovery, run_update_prices)


def engine():
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    cfg = json.loads(Path('tests/fixtures/economy.json').read_text(encoding='utf-8'))
    result = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    result.meta['report_detail'] = 'none'
    result.seed = 17
    result.rng.seed(17)
    result.bootstrap()
    return result


def assert_same_state(reference, native):
    assert reference.accounts == native.accounts
    assert reference.enterprises == native.enterprises
    assert reference.meta == native.meta
    assert reference.report == native.report
    assert reference.rng.getstate() == native.rng.getstate()
    reference.validate()
    native.validate()


def run_recovery_pair(reference, public=False):
    native = copy.deepcopy(reference)
    # Прямой replay начинается с той же monthly derived state, что и step().
    for name in ('_planning_orders', '_stock_sales_cache', '_production_outlook_cache'):
        reference.__dict__.pop(name, None)
        native.__dict__.pop(name, None)
    reference.update_prices()
    reference.prepare_recovery_quotes()
    sells = reference.sell_orders()
    households = reference.shuffled(a for a in reference.accounts.values()
                                    if a['kind'] == 'household')
    reference.household_requests(households, reference.cfg['basic_needs'],
                                 'Базовые нужды', sells)
    reference.household_requests(households, reference.cfg['extra_needs'],
                                 'Дополнительные нужды', sells)
    reference.owner_requests(sells)
    reference.recover_enterprises(sells)
    if public:
        reference.public_project_requests(sells)
        run_native_public_projects(native)
    else:
        run_native_recovery(native)
    assert_same_state(reference, native)
    assert reference._stock_sales_cache == native._stock_sales_cache
    for name in ('_market_quote_version', '_market_quote_versions',
                 '_market_quotes_epoch', '_demand_revision'):
        assert getattr(reference, name) == getattr(native, name)
    left = {(None,)+key[1:]: value for key, value in
            reference._production_outlook_cache.items()}
    right = {(None,)+key[1:]: value for key, value in
             native._production_outlook_cache.items()}
    assert left == right
    return sells, native


def test_update_prices_native_journal_and_full_step_match_v3():
    reference = engine()
    # Первый ход создаёт реальные history/public_orders/price_feedback для ветвей цены.
    reference.step()
    native = copy.deepcopy(reference)

    reference.update_prices()
    journal = run_update_prices(native)
    assert journal
    assert_same_state(reference, native)


def test_recovery_quotes_native_journal_matches_v3_and_preserves_recipe_order():
    reference = engine()
    reference.step()
    native = copy.deepcopy(reference)

    reference.update_prices()
    reference.prepare_recovery_quotes()
    journal = run_native_preparation(native)

    assert journal
    assert reference._recovery_quotes == native._recovery_quotes
    assert_same_state(reference, native)
    data = native.native_preparation['data']
    goods = list(native.goods)
    for product, recipe in native.goods.items():
        index = goods.index(product)
        start, stop = data['recovery_input_offset'][index:index+2]
        assert list(data['recovery_input_good'][start:stop]) == [
            goods.index(good) for good in recipe['inputs']]


def test_sell_orders_native_ids_reserves_sells_caches_and_rng_match_v3():
    reference = engine()
    reference.step()
    native = copy.deepcopy(reference)

    reference.update_prices()
    reference.prepare_recovery_quotes()
    expected_sells = reference.sell_orders()
    run_native_market(native)

    assert expected_sells == native.native_market['sells']
    assert_same_state(reference, native)
    for name in ('_market_quote_version', '_market_quote_versions', '_market_quotes_epoch'):
        assert getattr(reference, name) == getattr(native, name)
    for name in ('_market_books', '_quoted_cost_cache', '_reference_price_cache'):
        assert name not in reference.__dict__
        assert name not in native.__dict__


def test_household_requests_native_orders_needs_cash_and_rng_match_v3():
    reference = engine()
    reference.step()
    native = copy.deepcopy(reference)

    reference.update_prices()
    reference.prepare_recovery_quotes()
    sells = reference.sell_orders()
    households = reference.shuffled(a for a in reference.accounts.values()
                                    if a['kind'] == 'household')
    reference.household_requests(households, reference.cfg['basic_needs'],
                                 'Базовые нужды', sells)
    reference.household_requests(households, reference.cfg['extra_needs'],
                                 'Дополнительные нужды', sells)
    run_native_households(native)

    assert sells == native.native_households['sells']
    assert [a['id'] for a in households] == [
        a['id'] for a in native.native_households['households']]
    assert reference._demand_revision == native._demand_revision
    assert_same_state(reference, native)


def test_native_deep_food_diagnostics_match_v3():
    reference, native = engine(), engine()
    reference.meta['report_detail'] = native.meta['report_detail'] = 'deep'
    reference.step()
    native.step(native_mode='cython')
    assert_same_state(reference, native)


def test_two_complete_native_steps_match_v3():
    reference, native = engine(), engine()
    for _ in range(2):
        reference.step()
        native.step(native_mode='cython')
        assert_same_state(reference, native)


def test_recovery_underfunded_owner_partial_transfer_reopen_and_cancellation():
    reference = engine()
    reference.step()
    reference.cfg['owner_consumption_rate'] = 0.0
    for enterprise_id in ('firm-00010', 'firm-00011'):
        e = reference.enterprises[enterprise_id]
        e.update(status='closed', closed=True)
        a = reference.accounts[enterprise_id]
        a.update(balance=0.0, reserved=0.0, protected=0.0, operating_protected=0.0)
        a['stock'][e['good']] = 0.0
        a['stock']['wood'] = 2.0
    reference.accounts['merchant:s']['balance'] = 3.0
    reference.meta['initial_gold'] = reference.gold()

    sells, native = run_recovery_pair(reference)

    transfers = [row for row in reference.report['flows']
                 if row['reason'] == 'Восстановление оборотного капитала']
    assert len(transfers) == 1
    assert transfers[0]['gold'] == 3.0
    assert reference.enterprises['firm-00011']['status'] == 'idle'
    assert reference.enterprises['firm-00010']['status'] == 'closed'
    cancelled = [o for o in reference.report['orders'] if o['status'] == 'cancelled']
    assert {(o['participant'], o['good']) for o in cancelled} == {
        ('firm-00011', 'flax'), ('firm-00011', 'wood')}
    assert sells == native.native_recovery['sells']


def test_recovery_outlook_cache_reuse_and_offer_cancellation_match_v3():
    reference = engine()
    reference.step()
    sells, native = run_recovery_pair(reference)
    native_sells = native.native_recovery['sells']

    # plan() должен попасть в recovery cache, а не увидеть уже отменённые offers.
    for enterprise_id in sorted(reference.enterprises):
        reference.plan(reference.enterprises[enterprise_id], sells=sells)
        native.plan(native.enterprises[enterprise_id], sells=native_sells)
    assert_same_state(reference, native)

    order = next(o for o in reference.report['orders']
                 if o['side'] == 'sell' and o['status'] == 'active'
                 and o['participant'] in reference.enterprises)
    native_order = native.report['orders'][int(order['id'].split('-')[1])]
    reference.cancel_order(order, 'Проверка версии outlook')
    native.cancel_order(native_order, 'Проверка версии outlook')
    e = reference.enterprises[order['participant']]
    native_e = native.enterprises[order['participant']]
    assert reference.production_outlook(e, sells) == native.production_outlook(native_e, native_sells)
    assert reference._market_quote_versions == native._market_quote_versions
    assert_same_state(reference, native)


def test_recovery_equal_landed_prices_use_python_lexical_order():
    reference = engine()
    reference.step()
    sells, native = run_recovery_pair(reference)

    # В этих книгах есть равные landed prices и ID с разным числом разрядов.
    # В частности, строковый порядок Python различает 2 < 268 и 11 < 9.
    for good in reference.goods:
        rows = [o for o in sells.get(good, []) if o['status'] == 'active']
        assert [o['id'] for o in rows] == [
            o['id'] for o in native.native_recovery['sells'][good]]
        by_price = {}
        for order in rows:
            by_price.setdefault(order['price'], set()).add(
                int(order['id'].split('-')[1]))
        # Книги с одинаковой ценой действительно присутствуют в сценарии.
        if any(len(ids) > 1 for ids in by_price.values()):
            break
    else:
        pytest.fail('Нужен сценарий с равными ценами для проверки FIFO')
    assert_same_state(reference, native)


def test_public_project_stock_claims_releases_orders_and_cash_cap_match_v3():
    reference = engine()
    reference.step()
    reference.goods['tools']['durable'] = {'wear_rate': 0.1}
    reference.cfg['institutions']['test_school'] = dict(
        name='Школа', materials={'wood': 20.0}, equipment={'tools': 6.0},
        equipment_wear={}, work_required=10.0, max_workers=5.0)
    reference.cfg['construction_modes']['normal']['support_goods'] = {'bread': 4.0}
    project = reference.start_public_project('c', 'test_school')
    account = reference.accounts['municipal:c']
    account['stock'].update(wood=10.0, tools=4.0, bread=10.0)
    account.update(balance=1.0, reserved=0.0, protected=0.0)
    reference.enterprises['firm-00007']['suspended'] = True
    reference.meta['initial_gold'] = reference.gold()

    sells, native = run_recovery_pair(reference, public=True)

    held = reference.accounts['municipal:c']['public_projects'][0]
    assert held['id'] == project['id']
    assert held['materials']['wood'] == 10.0
    assert held['equipment_held']['tools'] == 4.0
    assert held['support_stock']['bread'] == 10.0
    releases = [e for e in native.native_public_projects['journal']
                if e[0] == 'public_stock_release']
    assert releases
    orders = [o for o in reference.report['orders'] if o.get('project_id') == project['id']]
    assert orders
    assert any(o['purpose'] == 'Содержание ускоренной стройки' for o in orders) is False
    assert all(o['quantity']*o['price'] <= 1.0 for o in orders)
    assert sells == native.native_public_projects['sells']
    assert reference._demand_revision == native._demand_revision
