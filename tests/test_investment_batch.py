"""Регрессии независимой инвестиционной оценки и последовательного commit."""
import copy
from collections import defaultdict
from dataclasses import replace
from types import MappingProxyType

import pytest

from core.db import WorldDB
from core.events import EventBus
from core.world_service import WorldService
from loader import load_world
from shadow.investment_batch import (OrderSnapshot, RequiredMarketKeys,
    _build_evaluation_index, _customer_limit, _required_market_keys, build_snapshot, commit,
    evaluate, evaluate_all)


def initialized_engine(service):
    service.initialize_economy()
    return service.economy_state().engine


def prepared_snapshot(engine):
    engine.meta['month'] = 1
    engine.reset_report()
    engine.report.update(investment_decisions=[], investment_candidates=[])
    sells = engine.sell_orders()
    households = engine.shuffled(a for a in engine.accounts.values() if a['kind'] == 'household')
    engine.household_requests(households, engine.cfg['basic_needs'], 'Базовые нужды', sells)
    engine.household_requests(households, engine.cfg['extra_needs'], 'Дополнительные нужды', sells)
    engine.owner_requests(sells)
    engine.public_project_requests(sells)
    return build_snapshot(engine, sells), sells


def engine_state(engine):
    return (copy.deepcopy(engine.accounts), copy.deepcopy(engine.enterprises),
            copy.deepcopy(engine.meta), copy.deepcopy(engine.report), engine.rng.getstate())


def test_customer_limit_uses_each_buy_order_price(service):
    engine = initialized_engine(service)
    snapshot, _sells = prepared_snapshot(engine)
    candidate = snapshot.candidates[0]
    good = candidate.good
    source = dict(candidate.location)['tile_id']
    orders = (
        OrderSnapshot('buyer-cheap', 'tile-cheap', 10.0, 1.0, 100.0, True),
        OrderSnapshot('buyer-expensive', 'tile-expensive', 20.0, 1.0, 100.0, True),
    )
    distances = dict(snapshot.distances)
    distances[source, 'tile-cheap'] = 2.0
    distances[source, 'tile-expensive'] = 2.0
    isolated = replace(snapshot, orders=MappingProxyType({good: orders}),
        distances=MappingProxyType(distances), evaluation_index=None)

    result = _customer_limit(isolated, candidate, source, good)

    freight = 2.0*snapshot.recipes[good].weight*snapshot.transport_rate
    assert result == 20.0-freight
    assert result != 10.0-freight


def test_evaluate_is_pure_and_repeatable(service):
    engine = initialized_engine(service)
    snapshot, _sells = prepared_snapshot(engine)
    candidate = snapshot.candidates[0]
    before_engine = engine_state(engine)
    before_snapshot = repr(snapshot)
    before_rng = engine.rng.getstate()

    first = evaluate(snapshot, candidate)
    second = evaluate(snapshot, candidate)

    assert first == second
    assert repr(snapshot) == before_snapshot
    assert engine_state(engine) == before_engine
    assert engine.rng.getstate() == before_rng


def test_reverse_evaluation_order_preserves_results(service):
    engine = initialized_engine(service)
    snapshot, _sells = prepared_snapshot(engine)

    forward = evaluate_all(snapshot)
    reverse = evaluate_all(snapshot, reversed(snapshot.candidates))

    assert {row.candidate.key: row for row in forward} == {
        row.candidate.key: row for row in reverse}


def test_commit_rejects_second_candidate_for_same_owner(service):
    engine = initialized_engine(service)
    snapshot, sells = prepared_snapshot(engine)
    by_owner = defaultdict(list)
    for evaluation in evaluate_all(snapshot):
        if evaluation.accepted:
            by_owner[evaluation.candidate.investor_id].append(evaluation)
    owner, evaluations = next((owner, rows) for owner, rows in by_owner.items() if len(rows) >= 2)
    state_ids = sorted({a['state_id'] for a in engine.accounts.values() if a.get('state_id')})
    flows = engine.investment_flow_balances(state_ids)

    commit(engine, evaluations[:2], sells, flows)

    assert engine.accounts[owner].get('project')
    decisions = [row for row in engine.report['investment_decisions'] if row['investor'] == owner]
    assert sum(row['outcome'] == 'started' for row in decisions) == 1
    assert any(row['outcome'] == 'rejected' and 'уже имеет проект' in row['reason']
               for row in decisions)


class LoggingMapping:
    def __init__(self, values):
        self.values = values
        self.requested = set()

    def __getitem__(self, key):
        self.requested.add(key)
        return self.values[key]


def test_sparse_index_covers_every_evaluate_all_lookup(service):
    engine = initialized_engine(service)
    snapshot, _sells = prepared_snapshot(engine)
    index = snapshot.evaluation_index
    logged = {name: LoggingMapping(getattr(index, name))
              for name in ('history', 'customer_rows', 'demand_groups', 'offer_curves',
                           'supplier_economics')}
    logged_snapshot = replace(snapshot, evaluation_index=replace(index, **logged))

    evaluate_all(logged_snapshot)

    for name, mapping in logged.items():
        assert mapping.requested
        assert mapping.requested <= set(getattr(index, name))


def test_sparse_index_is_smaller_than_full_goods_tiles_product(service):
    engine = initialized_engine(service)
    snapshot, _sells = prepared_snapshot(engine)
    tiles = {investor.tile_id for investor in snapshot.investors.values()}
    tiles.update(offer.tile_id for rows in snapshot.offers.values() for offer in rows)
    tiles.update(order.tile_id for rows in snapshot.orders.values() for order in rows)
    tiles.update(firm.tile_id for rows in snapshot.suppliers.values() for firm in rows)
    tiles.update(dict(candidate.location)['tile_id'] for candidate in snapshot.candidates)
    full_product_size = len(snapshot.recipes)*len(tiles)

    index = snapshot.evaluation_index
    assert len(index.customer_rows) < full_product_size
    assert len(index.demand_groups) < full_product_size
    assert len(index.offer_curves) < full_product_size


def test_dense_and_sparse_indices_produce_identical_evaluations(service):
    engine = initialized_engine(service)
    sparse_snapshot, _sells = prepared_snapshot(engine)
    source_tiles = {dict(candidate.location)['tile_id'] for candidate in sparse_snapshot.candidates}
    source_tiles.update(firm.tile_id for rows in sparse_snapshot.suppliers.values() for firm in rows)
    buyer_tiles = {investor.tile_id for investor in sparse_snapshot.investors.values()}
    buyer_tiles.update(source_tiles)
    buyer_tiles.update(order.tile_id for rows in sparse_snapshot.orders.values() for order in rows)
    dense_required = RequiredMarketKeys(
        customer=tuple((good, tile) for good in sparse_snapshot.recipes for tile in source_tiles),
        offers=tuple((good, tile) for good in sparse_snapshot.recipes for tile in buyer_tiles),
        history=_required_market_keys(sparse_snapshot).history)
    without_index = replace(sparse_snapshot, evaluation_index=None)
    dense_snapshot = replace(without_index,
        evaluation_index=_build_evaluation_index(without_index, dense_required))

    assert evaluate_all(sparse_snapshot) == evaluate_all(dense_snapshot)


def test_missing_required_market_key_is_an_error(service):
    engine = initialized_engine(service)
    snapshot, _sells = prepared_snapshot(engine)
    candidate = snapshot.candidates[0]
    key = (candidate.good, dict(candidate.location)['tile_id'])
    index = snapshot.evaluation_index
    customer_rows = dict(index.customer_rows)
    del customer_rows[key]
    broken = replace(snapshot, evaluation_index=replace(index,
        customer_rows=MappingProxyType(customer_rows)))

    with pytest.raises(KeyError):
        evaluate(broken, candidate)


def test_twelve_months_snapshot_batch_reproducible_and_valid(tmp_path):
    services = []
    databases = []
    try:
        for tag in ('a', 'b'):
            directory = tmp_path/tag
            directory.mkdir()
            db = WorldDB(directory/'world.lbug')
            load_world(db)
            service = WorldService(db, EventBus())
            service.initialize_economy()
            service.economy_state().engine.meta['report_detail'] = 'none'
            databases.append(db)
            services.append(service)
        engines = [service.economy_state().engine for service in services]
        for expected_month in range(1, 13):
            reports = [engine.step(investment_mode='snapshot_batch') for engine in engines]
            assert all(report['month'] == expected_month for report in reports)
            assert all(report['investment_mode'] == 'snapshot_batch' for report in reports)
            assert engine_state(engines[0]) == engine_state(engines[1])
            for engine, report in zip(engines, reports):
                assert all(report['invariants'].values())
                engine.validate()
                assert all(account['balance'] >= 0 for account in engine.accounts.values())
                assert all(quantity >= 0 for account in engine.accounts.values()
                           for quantity in account['stock'].values())
    finally:
        for db in databases:
            db.close()
