import copy
import csv
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tracemalloc
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from core.lab_export import (ARCHIVE_NAME, SCHEMAS, AnalysisExporter, ExportValidationError,
                             auto_export, export_analysis)
from core.lab_store import LabStore

ROOT = Path(__file__).resolve().parents[1]


def monthly_data(month):
    measured = month > 0
    balances = (78, 30, 2) if measured else (100, 10, 0)
    accounts = [dict(id=identity, kind=kind, state_id='s', province_id=province, balance=balance)
        for identity, kind, province, balance in zip(('h', 'e', 'carrier', 'closed'),
            ('household', 'enterprise', 'carrier', 'enterprise'), ('p', 'p', None, 'p'), (*balances, 0))]
    production, geography, enterprises, stocks = [], [], [], []
    for good, identity, stock in [('bread', 'e', 12 if measured else 10), ('flour', 'closed', 3)]:
        bread = good == 'bread'
        output, sold, revenue, profit = ((4, 2, 20, 12) if measured and bread else (0, 0, 0, 0))
        needed = 3 if bread else 0
        consumed = 2 if bread else 0
        unmet = 1 if bread else 0
        base = dict(state_id='s', province_id='p', good=good)
        production.append(dict(base, producers=int(bread), active=int(bread), closed=int(not bread),
            opened=0, closures=0, capacity=10 if bread else 0, output=output, sold=sold, stock=stock,
            finished_stock=stock, revenue=revenue, profit=profit, expenses=revenue-profit,
            production_cost=8 if measured and bread else 0, needed=needed if measured else None,
            consumed=consumed if measured else None, unmet=unmet if measured else None))
        geography.append(dict(base, population=100, household_gold=balances[0], enterprise_gold=balances[1],
            treasury_gold=0, investor_gold=0, production=output, needed=needed if measured else None,
            consumed=consumed if measured else None, unmet=unmet if measured else None,
            coverage=2/3 if measured and bread else None, imports=0, exports=0, enterprises=1))
        enterprises.append(dict(base, id=identity, entity_id=identity, owner_id='owner',
            status='active' if bread else 'closed', opened=0, closed=None if bread else 0,
            reason='existing business' if bread else 'closed for losses', output=output,
            capacity=10 if bread else 0, sold=sold, bought=0, revenue=revenue, profit=profit,
            expenses=revenue-profit, balance=balances[1] if bread else 0,
            stocks={good: stock}, stock_values={good: stock*2}, price=11 if bread else None))
        stocks.append(dict(base, entity_id=identity, quantity=stock, value=stock*2))
    orders, trades, flows = [], [], []
    if measured:
        orders = [dict(id='b', participant='h', state_id='s', province_id='p', good='bread', side='buy',
            quantity=3, remaining=1, filled=2, price=15, creation_index=1, status='partial'),
            dict(id='a', participant='e', state_id='s', province_id='p', good='bread', side='sell',
            quantity=14, remaining=12, filled=2, price=10, creation_index=0, status='partial')]
        trades = [dict(buyer='h', seller='e', buy_order='b', sell_order='a', buyer_state='s', seller_state='s',
            buyer_province='p', seller_province='p', good='bread', quantity=2, price=10, gold=20,
            freight=2, execution_index=0, distance=1, path=['tile-a', 'tile-b'])]
        flows = [dict(sender='h', recipient='e', gold=20, reason='goods', execution_index=0),
                 dict(sender='h', recipient='carrier', gold=2, reason='freight', execution_index=1)]
    return dict(overview=[dict(active=1, closed=1, opened=0, closures=0, output=4 if measured else 0,
        sold=2 if measured else 0, stock=15 if measured else 13, revenue=20 if measured else 0,
        profit=12 if measured else 0, unmet=1 if measured else None, consumed=2 if measured else None)],
        production=production, geography=geography, enterprises=enterprises, accounts=accounts, stocks=stocks,
        orders=orders, trades=trades, flows=flows, diagnostics=[dict(kind='shortage', good='bread', evidence={'unmet': 1})] if measured else [],
        lifecycle=[])


@pytest.fixture
def source(tmp_path):
    directory = tmp_path/'fixture'
    directory.mkdir()
    meta = dict(id='fixture', name='fixture', seed=17, months=1, completed=1, initial_month=0,
                status='completed', detail='full', initial_state_hash='known-world-hash',
                parameters={'known': 2}, engine={'version': 1})
    with LabStore(directory/'analytics.sqlite', create=True) as store:
        store.append_month(0, monthly_data(0), meta)
        store.append_month(1, monthly_data(1), meta)
    return directory


def csv_rows(archive, filename):
    return list(csv.DictReader(io.StringIO(archive.read(filename).decode('utf-8-sig'))))


def file_hash(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def test_owner_metrics_export_and_legacy_missing_values(source):
    with AnalysisExporter(source) as exporter:
        legacy = next(r for r in exporter.monthly() if r['record_kind'] == 'world' and r['month'] == 1)
        assert legacy['dividends_gold'] is None
        assert legacy['owner_consumption_spent_gold'] is None
        assert legacy['money_shares_json'] is None
    with LabStore(source/'analytics.sqlite', create=True) as store:
        data = monthly_data(1)
        data['overview'][0].update(dividends=5, owner_consumption_budget=3,
            owner_consumption_spent=2, owner_consumption_unspent=1, owner_consumption_tax=.2,
            owner_consumption_freight=.1, owner_consumed={'bread': .5},
            dividends_private=5, dividends_merchant=0, dividends_treasury=0, dividends_municipal=0,
            money_shares={'household': .5, 'enterprise': .5}, working_reserve=10, project_reserve=4)
        data['enterprises'][0].update(dividends=5, working_reserve=10, project_reserve=4)
        store.connection.execute('DELETE FROM rows WHERE month=1')
        store.append_month(1, data, store.metadata())
    with AnalysisExporter(source) as exporter:
        row = next(r for r in exporter.monthly() if r['record_kind'] == 'world' and r['month'] == 1)
        assert row['dividends_gold'] == 5
        assert row['owner_consumption_spent_gold'] == 2
        assert row['owner_consumed_by_good_json'] == {'bread': .5}
        assert row['dividends_by_owner_kind_json']['private'] == 5
        assert row['money_shares_json']['household'] == .5
        firm = next(r for r in exporter.enterprises() if r['month'] == 1 and r['enterprise_id'] == 'e')
        assert firm['dividends_gold'] == 5 and firm['working_reserve_gold'] == 10


def test_standard_archive_schema_units_and_read_only(source):
    before = file_hash(source/'analytics.sqlite')
    archive_path = export_analysis(source)
    assert before == file_hash(source/'analytics.sqlite')
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {'manifest.json', 'monthly.csv', 'industries.csv', 'enterprises.csv',
            'provinces.csv', 'market_summary.csv', 'money_summary.csv', 'diagnostics.json', 'changes.md'}
        assert archive.testzip() is None
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['git_commit'] is None
        assert manifest['seed'] == 17 and manifest['initial_world_hash'] == 'known-world-hash'
        assert manifest['format_version'] == 1 and manifest['validation'] == 'passed'
        monthly = csv_rows(archive, 'monthly.csv')
        assert all(r['output_quantity'] == '' for r in monthly if r['record_kind'] == 'world')
        world = next(r for r in monthly if r['month'] == '1' and r['record_kind'] == 'world')
        assert json.loads(world['balances_by_kind_json'])['household'] == 78
        industries = csv_rows(archive, 'industries.csv')
        bread = next(r for r in industries if r['month'] == '1' and r['good'] == 'bread')
        assert float(bread['average_sale_price']) == 10 and float(bread['average_landed_price']) == 11
        assert float(bread['unmet_quantity']) == 1
        provinces = csv_rows(archive, 'provinces.csv')
        assert sum(float(r['household_balance_gold']) for r in provinces if r['month'] == '1' and r['household_balance_gold']) == 78
        assert all(r['population'] == '' for r in provinces if r['record_kind'] == 'good')
        closed = next(r for r in csv_rows(archive, 'enterprises.csv') if r['enterprise_id'] == 'closed')
        assert closed['closure_reason'] == 'closed for losses'
        assert json.loads(archive.read('diagnostics.json'))['engine_invariants'] is None
        for filename in manifest['csv_columns']:
            assert tuple(next(csv.reader(io.StringIO(archive.read(filename).decode('utf-8-sig'))))) == SCHEMAS[filename]


def test_full_archive_preserves_ids_links_order_and_flows(source):
    with zipfile.ZipFile(export_analysis(source, full=True)) as archive:
        orders = csv_rows(archive, 'market_orders.csv')
        trades = csv_rows(archive, 'market_trades.csv')
        flows = csv_rows(archive, 'money_flows.csv')
        assert {r['order_id'] for r in orders} == {'a', 'b'}
        assert trades[0]['buy_order'] == 'b' and trades[0]['sell_order'] == 'a'
        assert trades[0]['source_id'] == '' and trades[0]['trade_id'] == 'fixture:trade:1:0'
        assert [r['execution_index'] for r in flows] == ['0', '1']
        assert flows[0]['sender_kind'] == 'household' and flows[0]['recipient_kind'] == 'enterprise'
        assert json.loads(trades[0]['path_json']) == ['tile-a', 'tile-b']
        market = next(r for r in csv_rows(archive, 'market_summary.csv') if r['month'] == '1' and r['good'] == 'bread' and r['side'] == 'buy')
        assert float(market['requested_quantity']) == 3 and float(market['unfilled_quantity']) == 1
        assert float(market['trade_quantity']) == 2 and float(market['freight_gold']) == 2


def test_summary_missing_journals_are_not_zero(source):
    with LabStore(source/'analytics.sqlite', create=True) as store:
        meta = store.metadata()
        meta['detail'] = 'summary'
        store.save_metadata(meta)
        store.connection.execute("DELETE FROM rows WHERE dataset IN ('orders','trades','flows')")
        store.connection.commit()
    with zipfile.ZipFile(export_analysis(source, full=True)) as archive:
        money = next(r for r in csv_rows(archive, 'money_summary.csv') if r['month'] == '1' and r['participant_kind'] == 'household')
        assert money['inflow_gold'] == money['outflow_gold'] == money['net_flow_gold'] == ''
        bread = next(r for r in csv_rows(archive, 'industries.csv') if r['month'] == '1' and r['good'] == 'bread')
        assert bread['average_sale_price'] == ''
        assert not csv_rows(archive, 'market_orders.csv')
        market = next(r for r in csv_rows(archive, 'market_summary.csv') if r['month'] == '1' and r['good'] == 'bread' and r['side'] == 'sell')
        assert market['requested_quantity'] == '' and float(market['trade_quantity']) == 2


def test_inconsistent_source_rejected_and_previous_zip_preserved(source):
    destination = export_analysis(source)
    before = file_hash(destination)
    with LabStore(source/'analytics.sqlite', create=True) as store:
        store.connection.execute("UPDATE rows SET data=json_set(data,'$.filled',99) WHERE dataset='orders' AND ordinal=0")
        store.connection.commit()
    with pytest.raises(ExportValidationError, match='order_quantities'):
        export_analysis(source)
    assert file_hash(destination) == before
    assert not list(source.glob('.noveria-export-*'))
    assert auto_export(source) is None
    with LabStore(source/'analytics.sqlite') as store:
        assert store.metadata()['status'] == 'completed'
    assert (source/'NOVERIA_ANALYSIS.error.json').is_file()


def test_atomic_zip_failure_and_known_changes(source, monkeypatch):
    path = export_analysis(source)
    before = file_hash(path)
    original = AnalysisExporter.write_diagnostics
    monkeypatch.setattr(AnalysisExporter, 'write_diagnostics', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        export_analysis(source)
    assert file_hash(path) == before
    assert not list(source.glob('.noveria-export-*'))
    monkeypatch.setattr(AnalysisExporter, 'write_diagnostics', original)
    with LabStore(source/'analytics.sqlite', create=True) as store:
        meta = store.metadata()
        meta.update(git_commit='recorded-commit', rule_changes=['Known metadata change'])
        store.save_metadata(meta)
    with zipfile.ZipFile(export_analysis(source)) as archive:
        assert 'Known metadata change' in archive.read('changes.md').decode('utf-8')
        assert json.loads(archive.read('manifest.json'))['git_commit'] == 'recorded-commit'


def test_streaming_does_not_use_lab_query_or_fetchall(source, monkeypatch):
    monkeypatch.setattr(LabStore, 'query', lambda *args, **kwargs: pytest.fail('Paged collector should not be called'))
    class StreamingConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            result = super().execute(*args, **kwargs)
            assert result.arraysize <= 1
            return result
    import core.lab_export as module
    original = module.sqlite3.connect
    def connect(*args, **kwargs):
        return original(*args, **dict(kwargs, factory=StreamingConnection))
    monkeypatch.setattr(module.sqlite3, 'connect', connect)
    with AnalysisExporter(source) as exporter:
        assert exporter.db.execute('PRAGMA temp_store').fetchone()[0] == 1
        assert not isinstance(exporter.raw_rows('orders'), list)
    export_analysis(source, full=True)


def test_cli_and_interface_export_existing_data(source, monkeypatch):
    before = file_hash(source/'analytics.sqlite')
    result = subprocess.run([sys.executable, str(ROOT/'scripts/export_lab.py'), str(source), '--full'],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    monkeypatch.setenv('NOVERIA_LAB_RUNS', str(source.parent))
    app = AppTest.from_file(ROOT/'lab.py', default_timeout=30).run()
    assert not app.exception
    next(b for b in app.button if b.label == 'Создать NOVERIA_ANALYSIS.zip').click().run()
    assert not app.exception
    with zipfile.ZipFile(source/ARCHIVE_NAME) as archive:
        assert json.loads(archive.read('manifest.json'))['mode'] == 'standard'
    assert file_hash(source/'analytics.sqlite') == before


def test_completion_hook_without_running_simulation(tmp_path, monkeypatch):
    import core.lab as lab
    directory = tmp_path/'fake-worker'
    directory.mkdir()
    meta = dict(id='fake', name='fixture', months=1, completed=0, status='created', detail='full', initial_month=0)
    with LabStore(directory/'analytics.sqlite', create=True) as store:
        store.save_metadata(meta)
    engine = SimpleNamespace(meta={'month': 0})

    class FakeState:
        def __init__(self, *args, **kwargs):
            self.engine = engine
            self.last_report = {'month': 0}
            self.keep_reports = False
            self.reports = []

        @classmethod
        def load_checkpoint(cls, *args, **kwargs):
            return cls()

        def save_checkpoint(self, *args, **kwargs):
            return args[0] if args else None

        def attach(self, *args, **kwargs):
            return self

    class FakeDB:
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(lab, 'prepare_world', lambda *args: None)
    monkeypatch.setattr(lab, 'WorldDB', FakeDB)
    monkeypatch.setattr(lab, 'EconomyState', FakeState)
    monkeypatch.setattr(lab, 'WorldService', lambda *args: SimpleNamespace(
        advance_turn=lambda **kwargs: engine.meta.update(month=1),
        attach_economy=lambda state: state,
        world_graph=lambda: {'snapshot': {'entities': [], 'links': []}, 'tiles': []}))
    monkeypatch.setattr(lab, 'capture', lambda engine, report, detail: copy.deepcopy(monthly_data(report['month'])))
    monkeypatch.setattr(lab, 'temporal_anomalies', lambda *args: None)
    assert lab.execute_experiment(directory)['status'] == 'completed'
    assert (directory/ARCHIVE_NAME).is_file()


def test_partial_missing_values_and_historical_git(source, monkeypatch):
    with LabStore(source/'analytics.sqlite', create=True) as store:
        meta = store.metadata()
        meta.update(git_commit='a'*40, control_git_commit='b'*40)
        store.save_metadata(meta)
        store.connection.execute("UPDATE rows SET data=json_set(data,'$.price',NULL) WHERE dataset='orders' AND ordinal=1")
        store.connection.commit()
    import core.lab_export as module
    calls = []
    def git(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(stdout='verified diff of recorded commits')
    monkeypatch.setattr(module.subprocess, 'run', git)
    with zipfile.ZipFile(export_analysis(source)) as archive:
        assert 'verified diff of recorded commits' in archive.read('changes.md').decode('utf-8')
        industry = next(r for r in csv_rows(archive, 'industries.csv') if r['month'] == '1' and r['good'] == 'bread')
        assert industry['average_ask_price'] == ''
        assert any('отсутствуют' in s for s in json.loads(archive.read('diagnostics.json'))['warnings'])
    assert any('diff' in call and 'b'*40 in call and 'a'*40 in call for call in calls)


def test_large_raw_table_uses_bounded_python_memory(source):
    with LabStore(source/'analytics.sqlite', create=True) as store:
        def inserted():
            for number in range(5000):
                row = dict(id=f'zero-{number}', participant='e', good='bread', state_id='s', province_id='p',
                           side='sell', quantity=0, remaining=0, filled=0, price=10, padding='x'*2048)
                yield ('orders', 1, number+2, 's', 'p', 'bread', 'e', None, json.dumps(row))
        store.connection.executemany('INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?)', inserted())
        store.connection.commit()
    tracemalloc.start()
    try:
        destination = export_analysis(source, full=True)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4*1024**2
    with zipfile.ZipFile(destination) as archive:
        assert json.loads(archive.read('manifest.json'))['csv_row_counts']['market_orders.csv'] == 5002
