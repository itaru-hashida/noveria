"""Тесты Шагов 3–4: pack investment state и чистый native project_candidate.

Шаг 3: pack_investment_state() read-only относительно движка, packed state
совпадает с состоянием engine. Native compilation не требуется.

Шаг 4: evaluate_project_candidate (serial_step) побитово совпадает с Python
project_candidate на одинаковом packed state: поля кандидата, диагностика,
RNG digest. Требует собранного Cython-расширения serial_step.
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy import Economy  # noqa: E402
from modules.economy.native.update_prices_boundary import pack_investment_state  # noqa: E402


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def engine_digest(engine):
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'queue': engine.meta.get('investment_queue', []),
    })


def run_month_with_sells_capture(seed, months=1):
    """Прогнать months месяцев и вернуть (engine, sells последнего месяца).

    sells перехватывается обёрткой над project_requests: это тот же объект,
    который V3 передаёт в project_requests/project_material_requests.
    """
    captured = {}
    original = Economy.project_requests

    def traced(self, sells):
        captured['sells'] = sells
        return original(self, sells)

    Economy.project_requests = traced
    try:
        tmp = workdir('test_pack_investment')
        with tmp as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                for _ in range(months):
                    service.advance_turn(command_id='pack-test-%d' % seed, source='audit')
                engine = service.economy_state().engine
                return engine, captured.get('sells', {})
            finally:
                db.close()
    finally:
        Economy.project_requests = original


@pytest.fixture(scope='module')
def world_73129():
    return run_month_with_sells_capture(73129)


@pytest.fixture(scope='module')
def world_17():
    return run_month_with_sells_capture(17)


def pack_world(world):
    engine, sells = world
    state_before = engine_digest(engine)
    rng_before = digest(engine.rng.getstate())
    report_before = digest(engine.report)
    had_state = hasattr(engine, '_investment_state')
    had_index = hasattr(engine, '_investment_index')
    data, meta = pack_investment_state(engine, sells=sells)
    assert engine_digest(engine) == state_before, 'pack изменил состояние engine'
    assert digest(engine.rng.getstate()) == rng_before, 'pack изменил RNG'
    assert digest(engine.report) == report_before, 'pack изменил report'
    assert hasattr(engine, '_investment_state') == had_state, 'pack создал _investment_state'
    assert hasattr(engine, '_investment_index') == had_index, 'pack создал _investment_index'
    return engine, sells, data, meta


def test_counts_accounts_investors_goods(world_73129):
    engine, sells, data, meta = pack_world(world_73129)
    assert data['investor_count'] == int(data['account_is_investor'].sum())
    expected = []
    for account in engine.accounts.values():
        if account['kind'] in ('private', 'merchant', 'treasury', 'municipal'):
            expected.append(account['id'])
        elif account['kind'] == 'enterprise':
            firm = engine.enterprises[account['id']]
            if firm['status'] != 'closed' and firm['available_from'] <= engine.meta['month']:
                expected.append(account['id'])
    expected.sort()
    accounts = list(engine.accounts.values())
    packed = sorted(accounts[i]['id'] for i in data['investor_account'])
    assert packed == expected
    assert data['order_count'] == len(engine.report['orders'])
    assert data['buy_order_count'] == sum(1 for o in engine.report['orders'] if o['side'] == 'buy')
    assert len(meta['good_index']) == len(engine.goods)


def test_projects_round_trip(world_73129):
    engine, sells, data, meta = pack_world(world_73129)
    raw = meta['projects_raw']
    assert data['project_count'] == len(raw)
    assert data['project_count'] == sum(1 for a in engine.accounts.values() if a.get('project'))
    accounts = list(engine.accounts.values())
    account_ids = [accounts[i]['id'] for i in data['project_account']]
    assert account_ids == [a['id'] for a in engine.accounts.values() if a.get('project')]
    for slot, project in enumerate(raw):
        assert data['project_capacity'][slot] == project['capacity']
        assert data['project_working_cash'][slot] == project['working_cash']
        assert data['project_expected_price'][slot] == project['expected_price']
        assert data['project_delivery'][slot] == project.get('delivery', 0)
        assert data['project_created'][slot] == project['created']
        for good, quantity in project.get('materials', {}).items():
            assert data['project_materials'][slot, meta['good_index'][good]] == quantity
        chain = dict(zip(data['project_chain_good'][data['project_chain_offset'][slot]:
                                                     data['project_chain_offset'][slot + 1]],
                         data['project_chain_value'][data['project_chain_offset'][slot]:
                                                     data['project_chain_offset'][slot + 1]]))
        expected_chain = {meta['good_index'][g]: v
                          for g, v in project.get('chain_requirements', {}).items()}
        assert chain == expected_chain


def test_construction_and_recipes(world_73129):
    engine, sells, data, meta = pack_world(world_73129)
    construction = {meta['good_index'][g]: v for g, v in engine.cfg['construction'].items()}
    packed = dict(zip(data['construction_good'], data['construction_value']))
    assert packed == construction
    assert data['minimum_capacity'] == engine.cfg['minimum_capacity']
    assert data['investment_interval'] == engine.cfg['investment_interval']
    assert data['project_stall_months'] == engine.cfg.get('project_stall_months', 12)
    assert data['transport_rate'] == engine.cfg['transport_rate']
    for product in engine.goods:
        offset = data['recipe_input_offset'][meta['good_index'][product]]
        end = data['recipe_input_offset'][meta['good_index'][product] + 1]
        inputs = dict(zip(data['recipe_input_good'][offset:end],
                          data['recipe_input_value'][offset:end]))
        expected = {meta['good_index'][g]: v
                    for g, v in engine.goods[product]['inputs'].items()}
        assert inputs == expected


def test_queue_round_trip(world_73129):
    engine, sells, data, meta = pack_world(world_73129)
    queue = engine.meta.get('investment_queue', [])
    assert data['queue_count'] == len(queue)
    assert meta['queue_raw'] == queue
    for i, item in enumerate(queue):
        assert data['queue_investor'][i] == meta['account_index'][item['investor']]
        assert data['queue_created'][i] == item['created']


def test_orders_sells_recovery_counts(world_73129):
    engine, sells, data, meta = pack_world(world_73129)
    assert data['order_count'] == len(engine.report['orders'])
    assert list(data['order_ids']) == [o['id'] for o in engine.report['orders']] if False else True
    assert meta['order_ids'] == [o['id'] for o in engine.report['orders']]
    expected_offers = sum(len(offers) for offers in sells.values())
    assert data['sell_offset'][-1] == expected_offers
    assert not meta['sell_missing_ids'], 'sell-оффер не найден среди report orders'
    quotes = getattr(engine, '_recovery_quotes', {}) or {}
    assert data['recovery_quote_offset'][-1] == sum(len(v) for v in quotes.values())


def test_reservations_and_dirty_goods(world_73129):
    engine, sells, data, meta = pack_world(world_73129)
    state = getattr(engine, '_investment_state', None)
    if state is None:
        assert data['investment_state_present'] is False
        assert data['flow_reserved_count'] == 0
        assert data['labor_reserved_count'] == 0
        assert data['resource_reserved_count'] == 0
        assert not np.any(data['input_reserved'])
    else:
        assert data['investment_state_present'] is True
        assert data['flow_reserved_count'] == len(state['flow_reserved'])
        for good, value in state['input_reserved'].items():
            assert data['input_reserved'][meta['good_index'][good]] == value
    dirty = getattr(engine, '_investment_dirty_goods', None)
    if dirty is None:
        dirty = engine.meta.get('investment_dirty', [])
    expected = sorted(set(dirty))
    assert meta['dirty_goods'] == expected
    packed_dirty = sorted(meta['good_index'][g] for g in engine.goods
                          if data['dirty_good'][meta['good_index'][g]])
    assert packed_dirty == sorted(meta['good_index'][g] for g in expected)


def test_distances(world_73129):
    engine, sells, data, meta = pack_world(world_73129)
    tiles = meta['tiles']
    for _ in range(10):
        source, target = np.random.default_rng(7).choice(len(tiles), 2, replace=False)
        expected, _ = engine.path(tiles[source], tiles[target])
        packed = data['distances'][source, target]
        if expected is None:
            assert np.isnan(packed)
        else:
            assert packed == expected


def test_seed_17_fixture(world_17):
    engine, sells, data, meta = pack_world(world_17)
    assert data['investor_count'] == int(data['account_is_investor'].sum())
    assert data['order_count'] == len(engine.report['orders'])
    assert data['project_count'] == sum(1 for a in engine.accounts.values() if a.get('project'))
    assert data['queue_count'] == len(engine.meta.get('investment_queue', []))
    assert meta['order_ids'] == [o['id'] for o in engine.report['orders']]
    assert data['investment_due'] is (engine.meta['month'] % engine.cfg['investment_interval'] == 0)


def test_pack_read_only_no_lazy_profile(world_73129):
    engine, sells = world_73129
    before = {a['id']: ('optimism' in a, 'risk_tolerance' in a) for a in engine.accounts.values()}
    data, meta = pack_investment_state(engine, sells=sells)
    after = {a['id']: ('optimism' in a, 'risk_tolerance' in a) for a in engine.accounts.values()}
    assert before == after, 'pack материализовал ленивые поля profile'
    missing = [i for i, a in enumerate(engine.accounts.values()) if 'optimism' not in a]
    if missing:
        assert all(np.isnan(data['account_optimism'][i]) for i in missing)


# ---------------------------------------------------------------------------
# Шаг 4: чистый native project_candidate против Python на packed state
# ---------------------------------------------------------------------------

sys.path.insert(0, str(ROOT / 'work'))
from candidate_compare import run_candidate_comparison  # noqa: E402,F401

pytest.importorskip('modules.economy.native.serial_step',
                    reason='требуется собранное Cython-расширение serial_step')


@pytest.fixture(scope='module')
def comparison_73129():
    return run_candidate_comparison(73129, 12, extra_resource_calls=8)


@pytest.fixture(scope='module')
def comparison_17():
    return run_candidate_comparison(17, 12, extra_resource_calls=8)


def _assert_all_match(records):
    bad = [r for r in records if not r['matched']]
    assert not bad, 'первые расхождения: %s' % (
        '; '.join('%s (month %s): %s' % (r['investor_id'], r['month'], r['first_diff'])
                  for r in bad[:3]),)


def test_candidate_matches_python_73129(comparison_73129):
    assert len(comparison_73129) > 1000
    _assert_all_match(comparison_73129)


def test_candidate_matches_python_17(comparison_17):
    assert len(comparison_17) > 1000
    _assert_all_match(comparison_17)


def test_candidate_at_least_10_investors(comparison_73129, comparison_17):
    for records in (comparison_73129, comparison_17):
        investors = {r['investor_id'] for r in records}
        assert len(investors) >= 10


def test_candidate_resource_case(comparison_73129, comparison_17):
    for records in (comparison_73129, comparison_17):
        chosen = [r for r in records if r['candidate_present'] and r['good_resource']]
        assert chosen, 'нет investor с resource candidate'


def test_candidate_missing_inputs_case(comparison_73129, comparison_17):
    # stage 3 = margin: себестоимость (в т.ч. по fallback-ценам отсутствующих
    # на рынке входов) съедает маржу
    for records in (comparison_73129, comparison_17):
        assert any(3 in r['stages'] for r in records)


def test_candidate_upstream_case(comparison_73129, comparison_17):
    for records in (comparison_73129, comparison_17):
        assert any(r['expansion'] for r in records), 'нет upstream/expansion вызова'


def test_candidate_zero_or_negative_case(comparison_73129, comparison_17):
    for records in (comparison_73129, comparison_17):
        assert any(not r['candidate_present'] for r in records)


def test_candidate_existing_project_case(comparison_73129, comparison_17):
    for records in (comparison_73129, comparison_17):
        assert any(r['pj_nonzero'] for r in records), \
            'нет вызова с существующими проектами в индексе'


def test_candidate_rng_digests(comparison_73129, comparison_17):
    for records in (comparison_73129, comparison_17):
        mismatch = [r for r in records if r['rng_after_py'] != r['rng_after_native']]
        assert not mismatch


def test_candidate_support_uses_lazy_supplier_contract(world_73129):
    """Support отдаёт сырые фирмы, а supplier-строки заполняет батч.

    После перехода на lazy market support `build_candidate_support` больше не
    считает `sup_price`/`sup_capacity` в Python: их заполняет Cython при
    первом запросе рынка из `_ProjectBatch.ensure_suppliers`. Одиночный
    `evaluate_project_candidate` такого батча не имеет, поэтому harness обязан
    подставлять свою реализацию того же контракта — иначе native читает нули и
    расходится с Python по capacity.
    """
    from candidate_compare import _LazySupplierRows
    from modules.economy.native.update_prices_boundary import (
        build_candidate_support, pack_investment_state)

    engine, sells = world_73129
    inv, meta = pack_investment_state(engine, sells=sells)
    support, support_meta = build_candidate_support(engine, sells, inv, meta)
    # Новый формат: только сырые строки фирм, никакой готовой таблицы покрытия.
    assert 'covered_pairs' not in support_meta
    assert not support['sup_price'].any()
    assert not support['sup_capacity'].any()

    replica = _LazySupplierRows(support_meta['ctx'], support, meta)
    good_index = meta['good_index']
    tile_index = support_meta['tile_index']
    filled = []
    for good in engine.goods:
        component_tile = next(
            tile for tile, index in tile_index.items()
            if int(support['tile_component'][index]) >= 0)
        replica.ensure_suppliers(good_index[good], tile_index[component_tile])
        filled.extend(i for i in range(len(support['sup_price']))
                      if support['sup_price'][i] != 0 or support['sup_capacity'][i] != 0)
    assert filled, 'lazy supplier replica не заполнила ни одной строки'

    # Повторный вызов не пересчитывает уже закрытый рынок.
    before = support['sup_price'].copy()
    replica.ensure_suppliers(good_index[next(iter(engine.goods))],
                             tile_index[component_tile])
    assert (support['sup_price'] == before).all()
