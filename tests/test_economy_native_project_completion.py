"""Focused-тесты завершения проектов: побитовое совпадение с V3.

Проверяются оба числовых среза project_completion_boundary:

  * цикл ``finish_projects``: проверка сметы и кассы, накопление ``asset``
    (доставка плюс списания со склада в порядке сметы), запись
    ``e['capacity']`` и ``e['asset_value']`` при расширении;
  * ``limit_project_capacity``: пересчёт ``requested_capacity``, ``capacity``,
    ``working_cash``, ``expected_profit``, ``expected_investment``.

Снятие проекта, переводы, склад, создание предприятия и отчёты остаются
Python-хуками: сравниваются числа, типы и порядок вызовов хуков.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
гоняет обе реализации на одинаковом состоянии.
"""
import copy
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy.native import project_completion_boundary as boundary  # noqa: E402

EPS = 1e-9
REASON = boundary.CAPACITY_LIMIT_REASON


class FakeEngine:
    """Минимальный носитель: ядру нужны enterprises и живые dict-ы."""

    def __init__(self, enterprises=None):
        self.enterprises = enterprises if enterprises is not None else {}


class RecordingHooks:
    """Хуки с журналом: ядро и эталон обязаны звать их в одном порядке."""

    def __init__(self, stock=None, free_cash=None, bills=None):
        self.journal = []
        self.stock = dict(stock or {})
        self.free = dict(free_cash or {})
        self.bills = dict(bills or {})

    def construction_bill(self, capacity):
        bill = self.bills.get(capacity, {'wood': 2})
        self.journal.append(('construction_bill', capacity, dict(bill)))
        return bill

    def free_cash(self, account_id):
        value = self.free.get(account_id, 1000.0)
        self.journal.append(('free_cash', account_id, value))
        return value

    def startup_cash(self):
        self.journal.append(('startup_cash',))
        return 50.0

    def recipe_requirements(self, good, capacity):
        value = {good: capacity}
        self.journal.append(('recipe_requirements', good, capacity))
        return value

    def pending(self, a, p):
        self.journal.append(('pending', a['id']))

    def detach_project(self, a):
        self.journal.append(('detach_project', a['id']))
        a.pop('project')
        a['protected'] = 0

    def pay_delivery(self, a, p):
        self.journal.append(('pay_delivery', a['id'], p.get('delivery', 0)))

    def use_stock(self, account_id, good, quantity):
        self.journal.append(('use_stock', account_id, good, quantity))
        self.stock[(account_id, good)] = self.stock.get((account_id, good), 0)-quantity
        return quantity*3

    def construction_used(self, good, quantity):
        self.journal.append(('construction_used', good, quantity))

    def expanded(self, e, p):
        self.journal.append(('expanded', e['id']))

    def built(self, a, p, bill, asset):
        self.journal.append(('built', a['id'], asset))
        e = {'id': 'new-'+a['id'], 'capacity': p['capacity'], 'price': p['expected_price']}
        self.engine_ref = e
        return e

    def decision(self, a, p, e):
        self.journal.append(('decision', a['id'], e['id']))


def make_account(aid, project=None, **overrides):
    account = {'id': aid, 'state_id': 'st1', 'protected': 0.0}
    if project is not None:
        account['project'] = project
    account.update(overrides)
    return account


def make_project(pid='p1', capacity=10, **overrides):
    project = {
        'id': pid, 'capacity': capacity, 'working_cash': 100.0,
        'delivery': 20.0, 'expansion': True, 'expected_price': 7.5,
        'materials': {'wood': 5, 'stone': 5},
        'location': {'state_id': 'st1', 'province_id': 'pr1', 'tile_id': 't1'},
    }
    project.update(overrides)
    return project


def run_finish_both(accounts, enterprises=None, hooks_kwargs=None):
    engine_native = FakeEngine(enterprises=copy.deepcopy(enterprises or {}))
    hooks_native = RecordingHooks(**(hooks_kwargs or {}))
    rows_native = copy.deepcopy(accounts)
    boundary.run_finish(engine_native, rows_native, hooks_native, EPS)

    engine_ref = FakeEngine(enterprises=copy.deepcopy(enterprises or {}))
    hooks_ref = RecordingHooks(**(hooks_kwargs or {}))
    rows_ref = copy.deepcopy(accounts)
    boundary.run_finish_python(engine_ref, rows_ref, hooks_ref, EPS)
    return (rows_native, hooks_native.journal, engine_native.enterprises,
            rows_ref, hooks_ref.journal, engine_ref.enterprises)


# --- 1. finish_projects: совпадение native и Python-эталона -----------------

def test_finish_native_matches_python_reference_expansion():
    accounts = [make_account('a1', project=make_project())]
    enterprises = {'a1': {'id': 'a1', 'good': 'flour', 'capacity': 10,
                          'asset_value': 100.0}}
    (native, journal_n, _, ref, journal_r, _) = run_finish_both(
        accounts, enterprises, hooks_kwargs={'bills': {10: {'wood': 2, 'stone': 1}}})
    assert native == ref
    assert journal_n == journal_r
    assert 'project' not in native[0]
    assert native[0]['protected'] == 0


def test_finish_writes_capacity_and_asset_value():
    accounts = [make_account('a1', project=make_project())]
    enterprises = {'a1': {'id': 'a1', 'good': 'flour', 'capacity': 10,
                          'asset_value': 100.0}}
    (_, journal, ent_native, ref, _, ent_ref) = run_finish_both(
        accounts, enterprises, hooks_kwargs={'bills': {10: {'wood': 2}}})
    # Расширение: capacity и asset_value пишет ядро, хук лишь журналирует.
    assert ('expanded', 'a1') in journal
    e = ent_native['a1']
    assert e['capacity'] == 20
    assert e['asset_value'] == 100.0+(20.0+2*3)
    assert ent_ref == ent_native


def test_finish_pending_when_materials_short():
    project = make_project(materials={'wood': 1, 'stone': 5})
    accounts = [make_account('a1', project=project)]
    (native, journal, _, ref, _, _) = run_finish_both(
        accounts, hooks_kwargs={'bills': {10: {'wood': 2, 'stone': 1}}})
    assert ('pending', 'a1') in journal
    assert native[0]['project'] == project
    assert native == ref


def test_finish_pending_when_cash_short():
    accounts = [make_account('a1', project=make_project())]
    (native, journal, _, ref, _, _) = run_finish_both(
        accounts, hooks_kwargs={'bills': {10: {'wood': 1}},
                                'free_cash': {'a1': 0.0}})
    assert ('pending', 'a1') in journal
    assert 'project' in native[0]
    assert native == ref


def test_finish_built_path_calls_hooks_in_order():
    project = make_project(expansion=False)
    accounts = [make_account('a1', project=project)]
    (_, journal, _, _, _, _) = run_finish_both(
        accounts, hooks_kwargs={'bills': {10: {'wood': 1}}})
    kinds = [entry[0] for entry in journal]
    assert kinds == ['construction_bill', 'free_cash', 'detach_project',
                     'pay_delivery', 'use_stock', 'construction_used',
                     'built', 'decision']


def test_finish_without_project_is_skipped():
    accounts = [make_account('a1')]
    (native, journal, _, ref, _, _) = run_finish_both(accounts)
    assert journal == []
    assert native == ref


def test_finish_materials_short_circuit_before_cash_check():
    # Смета не сходится: free_cash не должен зваться вообще.
    project = make_project(materials={'wood': 0})
    accounts = [make_account('a1', project=project)]
    (_, journal, _, _, _, _) = run_finish_both(
        accounts, hooks_kwargs={'bills': {10: {'wood': 2}}})
    assert ('pending', 'a1') in journal
    assert all(entry[0] != 'free_cash' for entry in journal)


def test_finish_int_and_float_types_preserved():
    accounts = [make_account('a1', project=make_project(delivery=20))]
    enterprises = {'a1': {'id': 'a1', 'good': 'flour', 'capacity': 10,
                          'asset_value': 100}}
    (native, _, ent_native, _, _, _) = run_finish_both(accounts, enterprises,
        hooks_kwargs={'bills': {10: {'wood': 1}}})
    # asset_value начался с int 100: накопление сохраняет тип арифметики V3.
    assert ent_native['a1']['asset_value'] == 100+(20.0+1*3)
    assert native[0]['protected'] == 0


# --- 2. limit_project_capacity ----------------------------------------------

def make_limit_project(**overrides):
    project = {
        'id': 'p1', 'good': 'flour', 'capacity': 100, 'working_cash': 200.0,
        'expected_investment': 500.0, 'expected_profit': 80.0,
        'expected_unit_cost': 2.0, 'expansion': False,
    }
    project.update(overrides)
    return project


def run_limit_both(project, capacity):
    hooks_native = RecordingHooks()
    native_project = copy.deepcopy(project)
    native_result = boundary.limit_capacity(native_project, capacity, EPS,
                                            hooks_native, REASON)
    hooks_ref = RecordingHooks()
    ref_project = copy.deepcopy(project)
    ref_result = boundary.limit_project_capacity_python(ref_project, capacity,
                                                        EPS, hooks_ref, REASON)
    return native_project, native_result, hooks_native.journal, \
        ref_project, ref_result, hooks_ref.journal


def test_limit_native_matches_python_reference():
    project = make_limit_project()
    native, result_n, journal_n, ref, result_r, journal_r = run_limit_both(project, 40)
    assert result_n == result_r is True
    assert native == ref
    assert journal_n == journal_r
    assert native['requested_capacity'] == 100
    assert native['capacity'] == 40
    assert native['working_cash'] == max(50.0, 40*2.0)
    assert native['expected_profit'] == 80.0*40/100
    assert native['expected_investment'] == (500.0-200.0)*40/100+native['working_cash']
    assert native['capacity_limit'] == REASON
    assert list(native.keys())[-1] == 'capacity_limit'


def test_limit_no_op_when_capacity_not_smaller():
    project = make_limit_project()
    native, result, journal, ref, result_r, _ = run_limit_both(project, 100)
    assert result is False and result_r is False
    assert native == project == ref
    assert journal == []


def test_limit_no_op_within_eps():
    project = make_limit_project()
    _, result, _, _, _, _ = run_limit_both(project, 100-1e-12)
    assert result is False


def test_limit_expansion_keeps_zero_working_cash():
    project = make_limit_project(expansion=True)
    native, result, _, _, _, _ = run_limit_both(project, 40)
    assert result is True
    assert native['working_cash'] == 0


# --- 3. Fallback без .pyd ----------------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    kernel = boundary.load_project_completion_kernel()
    assert boundary.materials_ready is kernel.materials_ready
    assert boundary.cash_ready is kernel.cash_ready
    assert boundary.capacity_values is kernel.capacity_values
    assert not hasattr(kernel, 'run_finish')
    accounts = [make_account('a1', project=make_project())]
    enterprises = {'a1': {'id': 'a1', 'good': 'flour', 'capacity': 10,
                          'asset_value': 100.0}}
    kwargs = {'bills': {10: {'wood': 1}}}
    (native, journal_n, ent_n, ref, journal_r, ent_r) = run_finish_both(
        accounts, enterprises, kwargs)
    monkeypatch.setattr(boundary, 'run_finish', boundary.run_finish_python)
    monkeypatch.setattr(boundary, 'limit_capacity',
                        boundary.limit_project_capacity_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    (native2, journal_n2, ent_n2, ref2, _, ent_r2) = run_finish_both(
        accounts, enterprises, kwargs)
    assert native2 == native == ref == ref2
    assert ent_n2 == ent_n == ent_r == ent_r2
    assert journal_n2 == journal_n
    project = make_limit_project()
    native_p, result_n, _, ref_p, _, _ = run_limit_both(project, 40)
    assert native_p == ref_p and result_n is True
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('project_completion_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_project_completion_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('finish_projects', 'limit_project_capacity', 'run_finish',
                 'run_finish_python', 'limit_project_capacity_python',
                 'load_project_completion_kernel', 'ProjectCompletionHooks'):
        assert callable(getattr(boundary, name)), name
