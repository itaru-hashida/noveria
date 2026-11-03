import copy
import gzip
import json
import pickle

import pytest

from core.db import WorldDB
from modules.economy.food_diagnostics import FoodDiagnostics
from scripts.record_food_diagnostics import run, summarize
from test_economy_v3 import engine


def test_diagnostics_do_not_change_decisions_money_or_randomness(engine):
    silent = copy.deepcopy(engine)
    silent.FOOD_DIAGNOSTICS = False
    for _ in range(3):
        report = engine.step(); baseline = silent.step()
        for key in ('accounts', 'enterprises', 'meta'):
            assert getattr(engine, key) == getattr(silent, key)
        assert engine.rng.getstate() == silent.rng.getstate()
        for key in ('trades', 'flows', 'invariants'):
            assert report[key] == baseline[key]
        assert all(t['accounting_unknown'] < 1e-7 for t in report['food_diagnostics']['totals'].values())


def test_budget_blocker_does_not_hide_unknown_root_or_use_late_income(engine):
    engine.transfer('h', 'treasury:s', engine.accounts['h']['balance'], 'Тестовый перевод')
    engine.accounts['h']['stock'] = {}
    report = engine.step(); diag = report['food_diagnostics']
    h = next(h for h in diag['households'] if h['id'] == 'h')
    assert h['cash'] == h['cash_after_trade'] == 0
    for good, total in diag['totals'].items():
        assert total['deficit'] == total['unfunded']
        assert total['root_unknown'] == total['deficit']
        assert total['accounting_unknown'] < 1e-7
    producer_ids = [p['id'] for p in diag['producers']]
    assert len(producer_ids) == len(set(producer_ids))
    assert diag['goods']['bread']['inputs'] == {'flour': .8, 'wood': .1}
    assert set(diag['goods']['flour']['producers']) <= set(producer_ids)
    assert all('candidates' not in str(p) for p in diag['producers'])


def test_missing_observation_stays_visible_as_accounting_gap(engine):
    a = engine.accounts['h']; a.update(stock={})
    trace = FoodDiagnostics(engine, [a], {})
    trace.clearing(engine, {})
    a['needs'] = {g: dict(needed=100000*q, consumed=1, deficit=100000*q-1)
        for g, q in engine.cfg['basic_needs'].items()}
    diag = trace.finish(engine)
    assert all(t['accounting_unknown'] == pytest.approx(1) for t in diag['totals'].values())


def test_compact_report_survives_real_database(engine, tmp_path):
    engine.step()
    with WorldDB(tmp_path/'food.lbug') as db:
        engine.save(db)
        saved = db.economy_records('EconomyMonth')[0]
        assert saved['food_diagnostics'] == engine.report['food_diagnostics']


def test_gzip_export_is_readable_and_refuses_overwrite(engine, tmp_path):
    checkpoint = tmp_path/'engine.pickle'; checkpoint.write_bytes(pickle.dumps(engine))
    output = tmp_path/'food.jsonl.gz'
    stats = run(checkpoint, output, 2)
    assert stats == summarize(output)
    assert stats['months'] == 2 and stats['contexts'] == 1
    with gzip.open(output, 'rt', encoding='utf-8') as source:
        records = [json.loads(line) for line in source]
    assert [r['type'] for r in records] == ['context', 'month', 'month']
    assert all(t['accounting_unknown'] < 1e-7 for t in stats['totals'].values())
    assert output.stat().st_size < sum(len(json.dumps(r)) for r in records)
    with pytest.raises(FileExistsError):run(checkpoint, output, 1)
    assert stats == summarize(output)


def test_month_without_shortage_keeps_only_summary(engine):
    a = engine.accounts['h']
    a['stock'] = {g: engine.population(a)*q for g, q in engine.cfg['basic_needs'].items()}
    trace = FoodDiagnostics(engine, [a], {})
    trace.clearing(engine, {})
    a['needs'] = {g: dict(needed=q, consumed=q, deficit=0) for g, q in a['stock'].items()}
    result = trace.finish(engine)
    assert result['households'] == result['producers'] == []
    assert result['market'] == result['goods'] == {}
    assert all(t['deficit'] == t['accounting_unknown'] == 0 for t in result['totals'].values())


def test_laboratory_keeps_compact_summary_and_optional_details(engine):
    from core.lab_analytics import capture

    report = engine.step()
    full = next(r for r in capture(engine, report, 'full')['diagnostics'] if r['kind'] == 'food_diagnostics')
    summary = next(r for r in capture(engine, report, 'summary')['diagnostics'] if r['kind'] == 'food_diagnostics')
    assert full['evidence'] == report['food_diagnostics']
    assert set(summary['evidence']) == {'schema', 'month', 'rules', 'totals'}
    assert summary['evidence']['totals'] == full['evidence']['totals']
