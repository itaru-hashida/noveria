import copy
import csv
import io
import json
from pathlib import Path

import pytest

from core.db import WorldDB
from core.economy_state import EconomyState
from core.lab import (compare_experiments, create_experiment, digest, execute_experiment,
                      list_experiments, live_series, read_live, start_experiment)
from core.lab_import import import_csv
from core.lab_store import LabStore
from core.world_service import WorldService
from modules.economy import Economy

ROOT = Path(__file__).resolve().parents[1]




@pytest.fixture(scope='module')
def pair(tmp_path_factory):
    runs = tmp_path_factory.mktemp('lab-pair')
    directories = []
    for detail in ('full', 'summary'):
        directory = create_experiment(detail, months=3, detail=detail, runs=runs)
        execute_experiment(directory)
        directories.append(directory)
    return directories


def test_reproducible_isolated_runs_and_optional_journal(pair):
    assert pair[0] != pair[1]
    # Экономика эксперимента лежит в его Parquet-checkpoint, а граф мира — отдельно.
    from core.economy_state import EconomyState
    states = []
    for directory in pair:
        with WorldDB(directory/'world.lbug', read_only=True) as db:
            graph = dict(snapshot=db.snapshot(), tiles=db.tiles(), campaign=db.campaign())
        states.append(EconomyState.load_checkpoint(directory/'economy', graph, strict=False))
    assert states[0].engine.accounts == states[1].engine.accounts
    assert states[0].engine.enterprises == states[1].engine.enterprises
    assert [r['month'] for r in states[0].reports] == [r['month'] for r in states[1].reports]
    with LabStore(pair[0]/'analytics.sqlite') as full, LabStore(pair[1]/'analytics.sqlite') as summary:
        assert full.metadata()['initial_state_hash'] == summary.metadata()['initial_state_hash']
        assert full.query('orders')
        assert not summary.query('orders')
        assert full.query('enterprises') == summary.query('enterprises')
        assert full.metadata()['status'] == 'completed'
        assert len(full.query('overview')) == 4
    with pytest.raises(FileExistsError):
        execute_experiment(pair[0])


def test_analytics_match_engine_and_order_links(pair):
    from core.economy_state import EconomyState
    with WorldDB(pair[0]/'world.lbug', read_only=True) as db:
        graph = dict(snapshot=db.snapshot(), tiles=db.tiles(), campaign=db.campaign())
    state = EconomyState.load_checkpoint(pair[0]/'economy', graph, strict=False)
    reports = {report['month']: report for report in state.reports}
    with LabStore(pair[0]/'analytics.sqlite') as store:
        for month in (1, 2, 3):
            report = reports[month]
            overview = store.query('overview', start=month, end=month)[0]
            assert overview['output'] == pytest.approx(sum(report['production'].values()))
            assert overview['sold'] == pytest.approx(sum(t['quantity'] for t in report['trades']))
            assert overview['household_gold'] == pytest.approx(report['balances']['household'])
            owner_unmet = sum(o['remaining'] for o in report['orders']
                if o.get('purpose') == 'Личное потребление собственника')
            assert overview['unmet'] == pytest.approx(sum(sum(p['deficits'].values()) for p in report['provinces'])+owner_unmet)
            production = store.series('production', start=month, end=month)[0]
            assert production['output'] == pytest.approx(overview['output'])
            orders = store.query('orders', start=month, end=month, limit=10000)
            original = {o['id']: o for o in report['orders']}
            assert len(orders) == len(original)
            for order in orders:
                assert order['quantity'] == original[order['id']]['quantity']
                assert order['remaining'] == original[order['id']]['remaining']
                if order['side'] == 'buy':
                    executions = store.order_trades(month, order['id'], limit=10000)
                    assert sum(t['quantity'] for t in executions) == pytest.approx(order['filled'])
        current = state.engine.enterprises
        for row in store.query('enterprises', start=3, end=3, limit=10000):
            assert row['owner_id'] == current[row['id']]['owner_id']
            assert row['output'] == current[row['id']]['output']


def test_source_unchanged_and_read_only_live(service, tmp_path):
    service.initialize_economy()
    service.advance_turn(command_id='source-month')
    service.schedule_social('stratum-province-0-0', 'loyalty', 62, command_id='lab-copy-command')
    path = Path(service.db.path)
    # Сохранение экономики — явное: без него экономика остаётся в памяти и
    # checkpoint не создаётся.
    service.save_checkpoint()
    accounts = copy.deepcopy(service.economy_state().engine.accounts)
    snapshot = dict(counts=service.db.counts(), meta=service.db.meta(),
                    commands=service.db.pending_commands())
    service.db.close()
    before = path.read_bytes()
    live = read_live(path)
    assert live['months'] == [1]
    assert live['report']['month'] == 1
    assert live_series(path, 1, 1)[0]['output'] == pytest.approx(sum(live['report']['production'].values()))
    directory = create_experiment('copy', months=2, seed=99, source=path, runs=tmp_path/'runs')
    config = execute_experiment(directory)
    assert config['seed'] == 73129
    assert path.read_bytes() == before
    with WorldDB(path, read_only=True) as db:
        assert dict(counts=db.counts(), meta=db.meta(),
                    commands=db.pending_commands()) == snapshot
        with db.read_transaction():
            with pytest.raises(RuntimeError):
                db.set_turn(999)
    with WorldDB(directory/'world.lbug', read_only=True) as clone:
        assert clone.meta()['turn'] == snapshot['meta']['turn']+2
        assert clone.get_entity('stratum-province-0-0')['properties']['loyalty'] == 62
    with WorldDB(path, read_only=True) as db:
        graph = dict(snapshot=db.snapshot(), tiles=db.tiles(), campaign=db.campaign())
    copied = EconomyState.load_checkpoint(directory/'economy', graph, strict=False)
    # Прогон копии длиннее исходника: счета исходника входят в копию, а новые
    # предприятия за два месяца — это нормальный рост, а не потеря состояния.
    assert set(accounts) <= set(copied.engine.accounts)
    assert copied.engine.meta['month'] > 1
    missing = tmp_path/'missing.lbug'
    with pytest.raises(ValueError):
        read_live(missing)
    assert not missing.exists()


def test_failure_keeps_completed_analytics(tmp_path, monkeypatch):
    original = WorldService.advance_turn
    calls = []
    def fail_second(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('deliberate failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(WorldService, 'advance_turn', fail_second)
    directory = create_experiment('failure', months=3, runs=tmp_path)
    with pytest.raises(ValueError, match='deliberate failure'):
        execute_experiment(directory)
    with LabStore(directory/'analytics.sqlite') as store:
        assert store.metadata()['completed'] == 1
        assert store.metadata()['status'] == 'failed'
        assert [r['month'] for r in store.query('overview')] == [0, 1]


def test_background_worker_and_stop(tmp_path):
    directory = create_experiment('background', months=2, runs=tmp_path)
    (directory/'cancel.request').touch()
    process = start_experiment(directory)
    assert process.wait(timeout=60) == 0
    with LabStore(directory/'analytics.sqlite') as store:
        assert store.metadata()['status'] == 'cancelled'
        assert store.metadata()['completed'] == 0
    assert list_experiments(tmp_path)[0]['status'] == 'cancelled'


def test_reopen_comparison_and_filtered_pagination(pair, tmp_path):
    rows = compare_experiments(pair, start=1, end=3)
    assert len(rows) == 6
    assert rows[0]['output'] == rows[3]['output']
    rows = compare_experiments(pair, 'production', good='bread', province_id='province-0', start=1, end=3)
    assert len(rows) == 6
    with LabStore(tmp_path/'large.sqlite', create=True) as store:
        store.append_month(120, {'orders': [dict(entity_id=f'firm-{i%10}', good='bread', value=i) for i in range(20000)]}, {})
        trace = []
        store.connection.set_trace_callback(trace.append)
        page = store.query('orders', start=120, end=120, entity_id='firm-1', limit=17, offset=17)
        assert len(page) == 17 and page[0]['value'] == 171
        assert 'LIMIT 17 OFFSET 17' in trace[-1]
        store.export_csv(tmp_path/'orders.csv', 'orders', entity_id='firm-1')
        assert len((tmp_path/'orders.csv').read_text(encoding='utf-8-sig').splitlines()) == 2001


def test_import_external_reports(tmp_path):
    samples = [
        ('scenario_monthly.csv', 'overview', dict(active=1, total_gold=100, unmet_population=2, trade_volume=3)),
        ('industry_monthly.csv', 'production', dict(good='bread', sell_quantity=3, buy_unfilled=2, production=4)),
        ('enterprise_monthly.csv', 'enterprises', dict(enterprise_id='firm', capacity_open=5, cash_end=10, output=3, profit=1)),
        ('scenario_provinces.csv', 'geography', dict(province_id='p', population=10,
            deficits='{"bread":1}', consumption='{"bread":2}')),
    ]
    for filename, dataset, values in samples:
        row = dict(scenario='external', month=1, **values)
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
        content = stream.getvalue().encode('utf-8')
        directories = import_csv(content, filename, tmp_path)
        assert directories
        for directory in directories:
            with LabStore(directory/'analytics.sqlite') as store:
                assert store.metadata()['status'] == 'imported'
                assert store.query(dataset, limit=1)
                assert not store.query('orders')
    with pytest.raises(ValueError, match='Неизвестный CSV'):
        import_csv(b'x,y\n1,2\n', 'unknown.csv', tmp_path)
