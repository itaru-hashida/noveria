"""Focused-тесты settlement-цикла предприятий: побитовое совпадение с V3.

Проверяется числовой срез ``Economy.settle``: оброк (``fixed_cost_due`` /
``fixed_cost_paid``), ``expenses``, ``profit``, ``loss_months``,
``profitable_months`` и ``unviable_months``. Перевод оброка, возврат капитала
и закрытие предприятия остаются Python-хуками: сравниваются не только числа
и типы, но и порядок вызовов хуков.

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
from modules.economy.native import enterprise_settlement_boundary as boundary  # noqa: E402
from modules.economy import Economy  # noqa: E402

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
    """Минимальный носитель состояния: ядру нужны meta и cfg."""

    def __init__(self, month=5, loss_limit=12):
        self.meta = {'month': month}
        self.cfg = {'loss_months_to_close': loss_limit}


class RecordingHooks:
    """Хуки с журналом: ядро и эталон обязаны звать их в одном порядке."""

    def __init__(self, free_cash=None, overheads=None):
        self.journal = []
        self.free = dict(free_cash or {})
        self.overheads = dict(overheads or {})

    def overhead(self, e):
        value = self.overheads.get(e['id'], 8.0)
        self.journal.append(('overhead', e['id'], value))
        return value

    def free_cash(self, account_id):
        value = self.free.get(account_id, 100.0)
        self.journal.append(('free_cash', account_id, value))
        return value

    def pay_overhead(self, e, paid):
        self.journal.append(('pay_overhead', e['id'], paid))

    def prepare_financial_inputs(self, e, due, paid):
        return boundary.prepare_financial_inputs(e, due, paid)

    def prepare_unviable_inputs(self, e, month, loss_limit):
        return boundary.prepare_unviable_inputs(e, month, loss_limit)

    def apply_financial_results(self, e, result):
        boundary.apply_financial_results(e, result)

    def apply_unviable_months(self, e, result):
        boundary.apply_unviable_months(e, result)

    def return_capital(self, e):
        self.journal.append(('return_capital', e['id']))

    def close_enterprise(self, e):
        self.journal.append(('close', e['id']))


def make_enterprise(eid, **overrides):
    enterprise = {
        'id': eid, 'state_id': 'st1', 'status': 'active',
        'available_from': 1, 'cost_of_sales': 4, 'revenue': 30, 'tax': 2,
        'loss_months': 0, 'profitable_months': 0, 'unviable_months': 0,
    }
    enterprise.update(overrides)
    return enterprise


def run_both(enterprises, hooks_kwargs=None, month=5, loss_limit=12, **hook_overrides):
    """Гоняет native и Python-эталон на одинаковом состоянии."""
    engine = FakeEngine(month=month, loss_limit=loss_limit)
    merged = dict(hooks_kwargs or {})
    merged.update(hook_overrides)
    hooks_native = RecordingHooks(**merged)
    rows_native = copy.deepcopy(enterprises)
    boundary.run_settle(engine, rows_native, hooks_native, EPS)

    engine_ref = FakeEngine(month=month, loss_limit=loss_limit)
    hooks_ref = RecordingHooks(**merged)
    rows_ref = copy.deepcopy(enterprises)
    boundary.run_settle_python(engine_ref, rows_ref, hooks_ref, EPS)
    return rows_native, hooks_native.journal, rows_ref, hooks_ref.journal


# --- 1. Совпадение native и Python-эталона ---------------------------------

def test_native_matches_python_reference():
    rows = [
        make_enterprise('a'),
        make_enterprise('b', revenue=5, cost_of_sales=7, loss_months=2,
                        unviable_months=3),
        make_enterprise('c', status='closed'),
        make_enterprise('d', available_from=9),
    ]
    native, journal_n, ref, journal_r = run_both(rows)
    assert native == ref
    assert journal_n == journal_r
    # Оброк уплачен первым предприятием, закрытое получило возврат капитала.
    assert ('pay_overhead', 'a', 8.0) in journal_n
    assert ('return_capital', 'c',) in journal_n or ('return_capital', 'c') in journal_n


def test_profit_expenses_and_month_counters():
    rows = [make_enterprise('a', revenue=30, tax=2, cost_of_sales=4)]
    native, _, ref, _ = run_both(rows, overheads={'a': 8.0})
    e = native[0]
    assert e['fixed_cost_due'] == 8.0
    assert e['fixed_cost_paid'] == 8.0
    assert e['expenses'] == 4+8.0
    assert e['profit'] == 30-2-e['expenses']
    assert e['profitable_months'] == 1
    assert e['loss_months'] == 0


def test_loss_months_accumulate_and_close_at_limit():
    rows = [make_enterprise('a', revenue=1, cost_of_sales=9, loss_months=11,
                            unviable_months=11, available_from=1)]
    native, journal, ref, journal_r = run_both(rows, overheads={'a': 8.0},
                                               loss_limit=12)
    assert native == ref
    assert native[0]['unviable_months'] == 12
    assert ('close', 'a') in journal
    assert ('close', 'a') in journal_r


def test_not_yet_available_is_not_unviable():
    rows = [make_enterprise('a', revenue=1, cost_of_sales=9, loss_months=3,
                            available_from=9)]
    native, journal, _, _ = run_both(rows, month=5)
    assert native[0]['unviable_months'] == 0
    assert ('close', 'a') not in journal


def test_closed_enterprise_skips_unviable_and_gets_capital_back():
    rows = [make_enterprise('c', status='closed', loss_months=5)]
    native, journal, _, _ = run_both(rows)
    assert ('return_capital', 'c') in journal
    assert ('close', 'c') not in journal
    assert native[0]['unviable_months'] == 0


def test_closed_enterprise_pays_no_overhead():
    rows = [make_enterprise('c', status='closed')]
    native, journal, _, _ = run_both(rows, overheads={'c': 0.0})
    # Хук отдаёт 0.0, оброк не платится, капитал возвращается.
    assert ('overhead', 'c', 0.0) in journal
    assert all(call[0] != 'pay_overhead' for call in journal)
    assert native[0]['fixed_cost_due'] == 0.0


def test_zero_due_paid_nothing():
    rows = [make_enterprise('a')]
    _, journal, _, _ = run_both(rows, overheads={'a': 0.0})
    assert ('pay_overhead', 'a', 0.0) not in journal


def test_free_cash_limits_payment():
    rows = [make_enterprise('a')]
    native, journal, _, _ = run_both(rows, free_cash={'a': 3.0},
                                     overheads={'a': 8.0})
    assert native[0]['fixed_cost_paid'] == 3.0
    assert ('pay_overhead', 'a', 3.0) in journal


def test_int_fields_stay_int_and_float_stay_float():
    rows = [make_enterprise('a')]
    native, _, _, _ = run_both(rows, overheads={'a': 7})
    e = native[0]
    assert type(e['fixed_cost_due']) is int
    assert type(e['fixed_cost_paid']) is int
    assert type(e['expenses']) is int
    assert type(e['profit']) is int
    rows = [make_enterprise('a', revenue=30.5, cost_of_sales=4.25, tax=2.5)]
    native, _, _, _ = run_both(rows, overheads={'a': 8.0})
    e = native[0]
    assert type(e['profit']) is float
    assert type(e['expenses']) is float


def test_update_key_order_matches_previous_code():
    rows = [make_enterprise('a')]
    native, _, _, _ = run_both(rows)
    keys = [k for k in native[0].keys()
            if k in ('fixed_cost_due', 'fixed_cost_paid')]
    assert keys == ['fixed_cost_due', 'fixed_cost_paid']


def test_empty_list_is_a_no_op():
    native, journal, ref, _ = run_both([])
    assert native == [] and ref == []
    assert journal == []


def test_rng_state_is_not_touched():
    rows = [make_enterprise('a')]
    native, _, _, _ = run_both(rows)
    # Фейковый движок без rng: срез не обращается к случайности вовсе.
    assert 'rng' not in FakeEngine().__dict__


def test_input_rows_are_not_mutated_by_caller_state():
    rows = [make_enterprise('a')]
    before = copy.deepcopy(rows)
    run_both(rows)
    # Граница работает со своими копиями: исходные строки не тронуты.
    assert rows == before
    assert 'profit' not in rows[0]


def actual_engine():
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True,
                province_id='p', resources=['зерно', 'лес', 'железо', 'рыба'],
                terrain='plain')
    cfg = json.loads((ROOT / 'content/economy.json').read_text(encoding='utf-8'))
    engine = Economy(dict(entities=entities,
        links=[dict(source='s', target=i, kind='owns') for i in ('p', 'c')]),
        [tile], meta=dict(id='game', version=3, month=1, config=cfg))
    engine.bootstrap()
    engine.cfg['loss_months_to_close'] = 1
    return engine


def owner_snapshot(engine, enterprise):
    ids = {enterprise['id'], enterprise['owner_id'], 'treasury:'+enterprise['state_id']}
    cash = engine.cash_store
    cash_values = {aid: (cash.get_balance(aid), cash.get_reserved_cash(aid),
                         cash.get_protected_cash(aid)) for aid in ids}
    projects = {aid: (('project' in engine.accounts[aid]) and
                      copy.deepcopy(engine.accounts[aid]['project']))
                for aid in ids}
    return (copy.deepcopy(enterprise), cash_values, projects,
            copy.deepcopy(engine.report), engine.order_store.columns())


def test_distribution_failure_rolls_back_both_settlement_phases(monkeypatch):
    engine = actual_engine()
    enterprise = next(iter(engine.enterprises.values()))
    household = engine.accounts['h']
    treasury = 'treasury:'+enterprise['state_id']
    cash = engine.cash_store
    cash.set_balance(enterprise['id'], 20.0)
    cash.set_balance(treasury, 100.0)
    engine.cfg['public_spending'] = 0.5
    before = owner_snapshot(engine, enterprise)
    household_balance = cash.get_balance(household['id'])
    rng = engine.rng.getstate()
    transfer = engine.transfer

    def fail_after_distribution(sender, recipient, amount, reason):
        transfer(sender, recipient, amount, reason)
        if recipient == household['id'] and amount > 0:
            assert 'profit' in enterprise
            assert cash.get_balance(recipient) != household_balance
            raise RuntimeError('distribution failed')

    monkeypatch.setattr(engine, 'transfer', fail_after_distribution)
    with pytest.raises(RuntimeError, match='distribution failed'):
        engine.settle([enterprise], [household])
    assert owner_snapshot(engine, enterprise) == before
    assert cash.get_balance(household['id']) == household_balance
    assert engine.rng.getstate() == rng


def test_actual_overhead_failure_rolls_back_authoritative_cash(monkeypatch):
    engine = actual_engine()
    enterprise = next(iter(engine.enterprises.values()))
    engine.cash_store.set_balance(enterprise['id'], 20.0)
    before = owner_snapshot(engine, enterprise)
    original = engine.transfer

    def fail_after_payment(*args):
        original(*args)
        raise RuntimeError('injected overhead failure')

    monkeypatch.setattr(engine, 'transfer', fail_after_payment)
    with pytest.raises(RuntimeError, match='injected overhead failure'):
        boundary.settle(engine, [enterprise])
    assert owner_snapshot(engine, enterprise) == before


def test_actual_return_capital_failure_rolls_back_owner_stores(monkeypatch):
    engine = actual_engine()
    enterprise = next(iter(engine.enterprises.values()))
    enterprise['status'] = 'closed'
    account = engine.accounts[enterprise['id']]
    engine.cash_store.set_balance(account['id'], 20.0)
    before = owner_snapshot(engine, enterprise)
    original = engine.return_capital

    def fail_after_return(row):
        original(row)
        raise RuntimeError('injected return failure')

    monkeypatch.setattr(engine, 'return_capital', fail_after_return)
    with pytest.raises(RuntimeError, match='injected return failure'):
        boundary.settle(engine, [enterprise])
    assert owner_snapshot(engine, enterprise) == before


def test_actual_closure_failure_rolls_back_cash_order_and_report(monkeypatch):
    engine = actual_engine()
    enterprise = next(iter(engine.enterprises.values()))
    enterprise.update(revenue=0, cost_of_sales=100, loss_months=0,
                      available_from=0)
    engine.cash_store.set_balance(enterprise['id'], 20.0)
    order = engine.order(enterprise['id'], 'bread', 1, 'buy', 1, 'test')
    before = owner_snapshot(engine, enterprise)
    original = engine.close_enterprise

    def fail_after_close(row):
        original(row)
        raise RuntimeError('injected closure failure')

    monkeypatch.setattr(engine, 'close_enterprise', fail_after_close)
    with pytest.raises(RuntimeError, match='injected closure failure'):
        boundary.settle(engine, [enterprise])
    assert owner_snapshot(engine, enterprise) == before
    assert order['status'] == 'active'
    assert engine.order_store.slot(order) >= 0
    assert engine.order_store.is_live(order, EPS)
    assert engine.order_store.columns()['status'][engine.order_store.slot(order)] == 'active'


# --- 2. Fallback без .pyd ----------------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    rows = [make_enterprise('a'), make_enterprise('b', status='closed')]
    engine = FakeEngine()
    hooks_native = RecordingHooks()
    rows_native = copy.deepcopy(rows)
    boundary.run_settle(engine, rows_native, hooks_native, EPS)
    monkeypatch.setattr(boundary, 'run_settle', boundary.run_settle_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    hooks_ref = RecordingHooks()
    rows_ref = copy.deepcopy(rows)
    boundary.run_settle(engine, rows_ref, hooks_ref, EPS)
    assert rows_ref == rows_native
    assert hooks_ref.journal == hooks_native.journal
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('enterprise_settlement_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_enterprise_settlement_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('settle', 'run_settle', 'run_settle_python',
                 'calculate_financial_results', 'calculate_unviable_months',
                 'load_enterprise_settlement_kernel', 'EnterpriseSettlementHooks'):
        assert callable(getattr(boundary, name)), name


def test_native_kernel_exports_calculations_only():
    kernel = boundary.load_enterprise_settlement_kernel()
    assert kernel is not None
    assert callable(kernel.calculate_financial_results)
    assert callable(kernel.calculate_unviable_months)
    assert not hasattr(kernel, 'run_settle')


# --- 3. Реальный мир: exact траектории --------------------------------------

def build_world(seed, months):
    tmp = workdir('test_enterprise_settlement')
    with tmp as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for month in range(months):
                service.advance_turn(
                    command_id='es-test-%d-%d' % (seed, month), source='audit')
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
def test_real_world_settlement_fields_have_reference_types(seed):
    engine = build_world(seed, 2)
    checked = 0
    for enterprise in engine.enterprises.values():
        if enterprise['available_from'] > engine.meta['month']:
            continue
        for key in ('fixed_cost_due', 'fixed_cost_paid', 'expenses', 'profit',
                    'loss_months', 'profitable_months', 'unviable_months'):
            assert type(enterprise[key]) in (int, float), (enterprise['id'], key)
        checked += 1
    assert checked > 10
