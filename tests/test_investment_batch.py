"""Регрессии независимой инвестиционной оценки и последовательного commit."""
import copy
from collections import defaultdict

from core.db import WorldDB
from core.events import EventBus
from core.world_service import WorldService
from loader import load_world
from shadow.investment_batch import build_snapshot, commit, evaluate, evaluate_all


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
