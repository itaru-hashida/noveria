"""Focused-тесты report/history-ядра: побитовое совпадение с V3.

Проверяются числовые writers истории продаж (``sales_history`` предприятий,
``stock_sales_history`` счетов) и числовые агрегаты отчёта месяца
(``account_flows``, ``sector_flows``, ``balances``, ``money_shares``,
``stocks``, ``active/open_enterprises``). Сравниваются значения, типы,
порядок строк и обрезка истории до трёх записей.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
гоняет обе реализации на одинаковом состоянии.
"""
import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))
sys.path.insert(0, str(ROOT / 'tests'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy.native import report_history_boundary as boundary  # noqa: E402

EPS = 1e-9

# Exact-отпечатки траектории 3 месяца, снятые на Python-коде V3 до переноса.
GOLDEN = {
    17: {
        'engine': '077b775c565b045cb020ca88996723603c290d5b5c20cdcf3b5edd3b9f078f1b',
        'report': '89e82f08689800986581fa2ad62fee18faa04c5a5195f1cf59439740efeb2a55',
        'rng': '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377',
    },
    73129: {
        'engine': 'd8446744ebd07232f7995c4035f5f1dae298d0373132d54e972f7411ef76f531',
        'report': '32ea25765f5057144b8c0b976e323b23e3ef34948615b5f15df7b61cd0e545bc',
        'rng': 'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b',
    },
}


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


class FakeEngine:
    """Минимальный носитель: report, enterprises, accounts, meta, gold."""

    def __init__(self, orders=(), trades=(), enterprises=(), accounts=()):
        self.report = {'orders': list(orders), 'trades': list(trades), 'flows': []}
        self.enterprises = {e['id']: e for e in enterprises}
        self.accounts = {a['id']: a for a in accounts}
        self.meta = {'month': 7}

    def gold(self):
        return sum(a.get('balance', 0) for a in self.accounts.values())

    def total_stock(self):
        return {g: sum(a.get('stock', {}).get(g, 0) for a in self.accounts.values())
                for a in self.accounts.values() for g in a.get('stock', {})}


def make_enterprise(eid, good='flour', **overrides):
    enterprise = {'id': eid, 'good': good, 'output': 5,
                  'input_limited': False, 'reachable_demand': 0.0}
    enterprise.update(overrides)
    return enterprise


def make_account(aid, kind='firm', balance=0.0, stock=None):
    return {'id': aid, 'kind': kind, 'balance': balance,
            'stock': stock or {}}


def sell_order(oid, participant, good, quantity, remaining):
    return {'id': oid, 'side': 'sell', 'participant': participant,
            'good': good, 'quantity': quantity, 'remaining': remaining}


# --- 1. sales_history ---------------------------------------------------------

def test_sales_history_native_matches_python_reference():
    engine_native = FakeEngine(
        orders=[sell_order('o1', 'e1', 'flour', 10, 4),
                sell_order('o2', 'e1', 'flour', 6, 6),
                sell_order('o3', 'e2', 'bread', 8, 1)],
        enterprises=[make_enterprise('e1'), make_enterprise('e2', good='bread'),
                     make_enterprise('e3')])
    engine_ref = copy.deepcopy(engine_native)
    boundary.update_sales_history(engine_native, EPS)
    boundary.update_sales_history_python(engine_ref, EPS)
    assert engine_native.enterprises == engine_ref.enterprises
    row = engine_native.enterprises['e1']['sales_history'][-1]
    assert row['month'] == 7
    assert row['sold'] == (10-4)+(6-6)
    assert row['offered'] == 10+6
    assert row['output'] == 5
    assert row['additional_demand'] is False


def test_sales_history_keeps_only_last_three_rows():
    history = [dict(month=m, sold=m, offered=m, output=m,
                    input_limited=False, additional_demand=False) for m in (4, 5, 6)]
    engine_native = FakeEngine(enterprises=[make_enterprise('e1', sales_history=history)])
    engine_ref = copy.deepcopy(engine_native)
    boundary.update_sales_history(engine_native, EPS)
    boundary.update_sales_history_python(engine_ref, EPS)
    rows = engine_native.enterprises['e1']['sales_history']
    assert [r['month'] for r in rows] == [5, 6, 7]
    assert engine_native.enterprises == engine_ref.enterprises


def test_sales_history_creates_key_for_new_enterprise():
    engine_native = FakeEngine(enterprises=[make_enterprise('e1')])
    boundary.update_sales_history(engine_native, EPS)
    assert 'sales_history' in engine_native.enterprises['e1']


def test_sales_history_types_sold_offered():
    orders = [sell_order('o1', 'e1', 'flour', 3, 1)]
    engine_native = FakeEngine(orders=orders, enterprises=[make_enterprise('e1')])
    boundary.update_sales_history(engine_native, EPS)
    row = engine_native.enterprises['e1']['sales_history'][-1]
    assert type(row['sold']) is int
    assert type(row['offered']) is int


# --- 2. stock_sales_history ----------------------------------------------------

def test_stock_sales_history_native_matches_python_reference():
    trades = [dict(seller='a1', good='wood', quantity=4),
              dict(seller='a1', good='wood', quantity=3),
              dict(seller='a2', good='stone', quantity=2)]
    accounts = [make_account('a1', stock={'wood': 9}),
                make_account('a2', stock={'stone': 1, 'wood': 0}),
                make_account('a3', stock={})]
    engine_native = FakeEngine(trades=trades, accounts=accounts)
    engine_ref = copy.deepcopy(engine_native)
    boundary.update_stock_sales_history(engine_native)
    boundary.update_stock_sales_history_python(engine_ref)
    assert engine_native.accounts == engine_ref.accounts
    assert engine_native.accounts['a1']['stock_sales_history']['wood'][-1]['sold'] == 7
    assert engine_native.accounts['a2']['stock_sales_history']['stone'][-1]['sold'] == 2


def test_stock_sales_history_empty_stock_creates_no_history_key():
    engine_native = FakeEngine(accounts=[make_account('a1', stock={})])
    engine_ref = copy.deepcopy(engine_native)
    boundary.update_stock_sales_history(engine_native)
    boundary.update_stock_sales_history_python(engine_ref)
    assert 'stock_sales_history' not in engine_native.accounts['a1']
    assert engine_native.accounts == engine_ref.accounts


def test_stock_sales_history_keeps_last_three():
    trades = [dict(seller='a1', good='wood', quantity=1)]
    old = [dict(sold=m) for m in (4, 5, 6)]
    account = make_account('a1', stock={'wood': 9})
    account['stock_sales_history'] = {'wood': old}
    engine_native = FakeEngine(trades=trades, accounts=[account])
    engine_ref = copy.deepcopy(engine_native)
    boundary.update_stock_sales_history(engine_native)
    boundary.update_stock_sales_history_python(engine_ref)
    rows = engine_native.accounts['a1']['stock_sales_history']['wood']
    assert [r['sold'] for r in rows] == [5, 6, 1]
    assert engine_native.accounts == engine_ref.accounts


# --- 3. report aggregates -------------------------------------------------------

def test_aggregates_native_matches_python_reference():
    accounts = [make_account('a1', kind='firm', balance=100.0, stock={'wood': 3}),
                make_account('a2', kind='household', balance=50, stock={}),
                make_account('h1', kind='household', balance=25.5, stock={})]
    engine_native = FakeEngine(accounts=accounts)
    engine_native.report['flows'] = [
        dict(sender='a1', recipient='a2', gold=10.0),
        dict(sender='a2', recipient='a1', gold=4),
        dict(sender='a1', recipient='h1', gold=1.5)]
    engine_ref = copy.deepcopy(engine_native)
    result_native = boundary.build_report_aggregates(engine_native, engine_native.report)
    result_ref = boundary.build_report_aggregates_python(engine_ref, engine_ref.report)
    assert engine_native.report == engine_ref.report
    assert result_native == result_ref
    report = engine_native.report
    flows = {row['id']: row for row in report['account_flows']}
    assert flows['a1']['incoming'] == 4 and flows['a1']['outgoing'] == 11.5
    assert flows['a1']['net'] == 4-11.5
    assert report['balances'] == {'firm': 100.0, 'household': 75.5}
    assert report['gold_after'] == 175.5
    assert report['money_shares']['firm'] == 100.0/175.5
    assert report['active_enterprises'] == 0
    assert report['sector_flows'] == sorted(report['sector_flows'],
                                            key=lambda r: (r['sender_kind'], r['recipient_kind']))


def test_aggregates_empty_flows_zero_defaults_are_float():
    accounts = [make_account('a1', kind='firm', balance=10.0)]
    engine_native = FakeEngine(accounts=accounts)
    engine_ref = copy.deepcopy(engine_native)
    boundary.build_report_aggregates(engine_native, engine_native.report)
    boundary.build_report_aggregates_python(engine_ref, engine_ref.report)
    row = engine_native.report['account_flows'][0]
    assert row['incoming'] == 0.0 and type(row['incoming']) is float
    assert row['net'] == 0.0
    assert engine_native.report == engine_ref.report


def test_aggregates_count_active_enterprises():
    enterprises = [make_enterprise('e1', status='active'),
                   make_enterprise('e2', status='closed'),
                   make_enterprise('e3', status='idle')]
    engine_native = FakeEngine(enterprises=enterprises)
    boundary.build_report_aggregates(engine_native, engine_native.report)
    assert engine_native.report['active_enterprises'] == 1
    assert engine_native.report['open_enterprises'] == 2


# --- 4. Fallback без .pyd --------------------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    orders = [sell_order('o1', 'e1', 'flour', 3, 1)]
    base = FakeEngine(orders=orders, enterprises=[make_enterprise('e1')])
    native_engine = copy.deepcopy(base)
    boundary.update_sales_history(native_engine, EPS)
    monkeypatch.setattr(boundary, 'update_sales_history',
                        boundary.update_sales_history_python)
    monkeypatch.setattr(boundary, 'update_stock_sales_history',
                        boundary.update_stock_sales_history_python)
    monkeypatch.setattr(boundary, 'build_report_aggregates',
                        boundary.build_report_aggregates_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    ref_engine = copy.deepcopy(base)
    boundary.update_sales_history(ref_engine, EPS)
    assert ref_engine.enterprises == native_engine.enterprises
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('report_history_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_report_history_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('update_sales_history', 'update_sales_history_python',
                 'update_stock_sales_history', 'update_stock_sales_history_python',
                 'build_report_aggregates', 'build_report_aggregates_python',
                 'load_report_history_kernel'):
        assert callable(getattr(boundary, name)), name


# --- 5. Реальный мир: exact траектории -------------------------------------------

def build_world(seed, months):
    tmp = workdir('test_report_history')
    with tmp as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for month in range(months):
                service.advance_turn(
                    command_id='rh-test-%d-%d' % (seed, month), source='audit')
            return service.economy_state().engine
        finally:
            db.close()


@pytest.fixture(scope='module')
def world_17():
    return build_world(17, 3)


@pytest.fixture(scope='module')
def world_73129():
    return build_world(73129, 3)


@pytest.mark.parametrize('seed,fixture_name', [(17, 'world_17'),
                                               (73129, 'world_73129')])
def test_exact_three_months_are_unchanged(seed, fixture_name, request):
    engine = request.getfixturevalue(fixture_name)
    assert engine.meta['month'] == 3
    assert engine_digest(engine) == GOLDEN[seed]['engine']
    assert digest(engine.report) == GOLDEN[seed]['report']


@pytest.mark.parametrize('seed,fixture_name', [(17, 'world_17'),
                                               (73129, 'world_73129')])
def test_exact_rng_state_is_unchanged(seed, fixture_name, request):
    engine = request.getfixturevalue(fixture_name)
    assert digest(engine.rng.getstate()) == GOLDEN[seed]['rng']


@pytest.mark.parametrize('seed', [17, 73129])
def test_real_world_history_field_types(seed):
    engine = build_world(seed, 2)
    checked = 0
    for enterprise in engine.enterprises.values():
        for row in enterprise.get('sales_history', []):
            assert type(row['sold']) in (int, float)
            assert type(row['offered']) in (int, float)
            assert type(row['output']) in (int, float)
            assert type(row['month']) is int
            assert type(row['input_limited']) is bool
            assert type(row['additional_demand']) is bool
            checked += 1
    assert checked > 10
