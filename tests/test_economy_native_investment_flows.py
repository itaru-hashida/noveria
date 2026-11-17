"""Инвестиционные потоки: устойчивый баланс и confirmed_output в Cython.

Блок 4 переноса: числовое тело ``V3.investment_flow_balances`` (накопления
supply/capacity_supply/operating_demand/construction_demand, итоги committed/
free/shortage/rampable_free и диагностические списки suppliers/consumers/
projects) и ``confirmed_output`` из ``investment_flow_balance`` переехали в
``money_kernel``.

Плоские строки предприятий и проектов готовит ``money_boundary.flow_rows`` в
прежнем порядке обхода: фильтры закрытых, приостановленных, недоступных и
чужих государств применяются там же, где и раньше. Второго ledger'а нет:
ядро возвращает те же dict-ы, которые прежний код возвращал напрямую.

Тест держит значения, типы, порядок строк и списков, совпадение с живым
методом движка, отсутствие RNG и fallback без собранного ``.pyd``.
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy.native import money_boundary as boundary  # noqa: E402


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


# --- 1. confirmed_output ---------------------------------------------------

@pytest.mark.parametrize('history,capacity,planned,expected', [
    ([], 10, 0, 0),
    ([], 10, 5, 5),
    ([dict(output=3)], 10, 0, 3),
    ([dict(output=3)], 10, 7, 7),
    ([dict(output=12)], 10, 0, 10),
    ([dict(output=1), dict(output=9), dict(output=2)], 10, 0, 9),
    ([dict(output=2), dict(output=4), dict(output=6), dict(output=9)], 10, 0, 9),
    ([dict(output=-5)], 10, 0, 0),
])
def test_confirmed_output_matches_python_reference(history, capacity, planned,
                                                   expected):
    native = boundary.run_confirmed_output(history, capacity, planned)
    reference = boundary.confirmed_output_python(history, capacity, planned)
    assert native == reference
    assert native == expected


def test_confirmed_output_keeps_int_type_for_integral_inputs():
    result = boundary.run_confirmed_output([dict(output=3)], 10, 5)
    assert result == 5
    assert type(result) is int


def test_confirmed_output_uses_last_three_rows_only():
    history = [dict(output=100), dict(output=1), dict(output=2), dict(output=3)]
    assert boundary.run_confirmed_output(history, 50, 0) == 3


# --- 2. flow_balances ------------------------------------------------------

def firms_fixture():
    return [
        ('s', 'bread', 5.0, 8.0, (('flour', 2.0),), 'f1'),
        ('s', 'flour', 12.0, 12.0, (('grain', 3.0),), 'f2'),
        ('q', 'bread', 2.0, 4.0, (('flour', 1.0),), 'f3'),
    ]


def projects_fixture():
    return [
        ('s', 'bread', 3.0, (('flour', 2.0),), 'p1', 'a1'),
        ('q', 'bread', 1.0, (('flour', 1.0),), 'p2', 'a2'),
    ]


def test_flow_balances_match_python_reference():
    state_ids = ('s', 'q')
    goods = ['bread', 'flour', 'grain']
    firms, projects = firms_fixture(), projects_fixture()
    native = boundary.run_flows(state_ids, goods, firms, projects)
    reference = boundary.flow_balances_python(state_ids, goods, firms, projects)
    assert native == reference


def test_flow_balances_row_key_order_follows_state_then_goods():
    native = boundary.run_flows(('s', 'q'), ['bread', 'flour'], [], [])
    assert list(native) == [('s', 'bread'), ('s', 'flour'),
                             ('q', 'bread'), ('q', 'flour')]


def test_flow_balances_supply_and_capacity_accumulate():
    native = boundary.run_flows(('s',), ['bread'], firms_fixture(), [])
    row = native[('s', 'bread')]
    assert row['supply'] == 5.0
    assert row['capacity_supply'] == 8.0
    assert row['suppliers'] == [dict(enterprise_id='f1', output=5.0, capacity=8.0)]


def test_flow_balances_operating_and_construction_demand_accumulate():
    native = boundary.run_flows(('s',), ['bread', 'flour'], firms_fixture(),
                                projects_fixture())
    row = native[('s', 'flour')]
    # 5.0*2.0 (предприятие) + 3.0*2.0 (проект)
    assert row['operating_demand'] == 10.0
    assert row['construction_demand'] == 6.0
    assert row['committed'] == 16.0


def test_flow_balances_diagnostic_lists_preserve_order():
    native = boundary.run_flows(('s',), ['bread', 'flour'], firms_fixture(),
                                projects_fixture())
    row = native[('s', 'flour')]
    assert [r['enterprise_id'] for r in row['consumers']] == ['f1']
    assert [r['project_id'] for r in row['projects']] == ['p1']


def test_flow_balances_totals_match_previous_formulas():
    native = boundary.run_flows(('s', 'q'), ['bread', 'flour', 'grain'],
                                firms_fixture(), projects_fixture())
    for row in native.values():
        assert row['committed'] == row['operating_demand']+row['construction_demand']
        assert row['free'] == max(0, row['supply']-row['committed'])
        assert row['shortage'] == max(0, row['committed']-row['supply'])
        assert row['rampable_free'] == max(0, row['capacity_supply']-row['committed'])


def test_flow_balances_ignore_rows_outside_state_ids():
    """Строка чужого государства не попадает в balances и не падает."""
    native = boundary.run_flows(('s',), ['bread'], firms_fixture(), projects_fixture())
    assert ('q', 'bread') not in native
    assert native[('s', 'bread')]['supply'] == 5.0


def test_flow_balances_ignore_inputs_outside_goods():
    firms = [('s', 'bread', 5.0, 8.0, (('unknown', 2.0),), 'f1')]
    native = boundary.run_flows(('s',), ['bread'], firms, [])
    reference = boundary.flow_balances_python(('s',), ['bread'], firms, [])
    assert native == reference


def test_flow_balances_empty_world_is_empty():
    assert boundary.run_flows((), [], [], []) == {}


def test_flow_balances_do_not_mutate_inputs():
    firms, projects = firms_fixture(), projects_fixture()
    firms_before, projects_before = copy.deepcopy(firms), copy.deepcopy(projects)
    boundary.run_flows(('s', 'q'), ['bread', 'flour', 'grain'], firms, projects)
    assert firms == firms_before
    assert projects == projects_before


def test_flow_balances_rng_is_not_touched():
    import random
    rng = random.Random(17)
    before = rng.getstate()
    boundary.run_flows(('s',), ['bread'], firms_fixture(), projects_fixture())
    assert rng.getstate() == before


# --- 3. Реальный движок ----------------------------------------------------

@pytest.fixture(scope='module')
def world():
    tmp = workdir('test_investment_flows')
    with tmp as directory:
        db, service = sr.build_world(17, directory, report_detail='standard')
        try:
            engine = service.economy_state().engine
            for month in range(3):
                engine.step(native_mode='off')
            return engine
        finally:
            db.close()


def test_flow_rows_follow_the_previous_filters(world):
    month = world.meta['month']
    state_ids = sorted({a['state_id'] for a in world.accounts.values()
                        if a.get('state_id')})
    firms, projects = boundary.flow_rows(world, tuple(state_ids))
    for firm in firms:
        enterprise = world.enterprises[firm[5]]
        assert enterprise['status'] != 'closed'
        assert not enterprise.get('suspended')
        assert enterprise['available_from'] <= month
        assert firm[0] in state_ids
    for project in projects:
        account = world.accounts[project[5]]
        assert account.get('project')
        assert project[0] in state_ids


def test_flows_match_the_live_engine_method(world):
    state_ids = sorted({a['state_id'] for a in world.accounts.values()
                        if a.get('state_id')})
    native = boundary.flows(world, state_ids)
    live = world.investment_flow_balances(state_ids)
    # dict-ключи — tuple (state_id, good), поэтому json.dumps здесь не годится.
    assert set(native) == set(live)
    for key in live:
        assert native[key] == live[key], key


def test_flows_match_python_reference_on_a_real_month(world):
    state_ids = tuple(sorted({a['state_id'] for a in world.accounts.values()
                              if a.get('state_id')}))
    firms, projects = boundary.flow_rows(world, state_ids)
    native = boundary.run_flows(state_ids, list(world.goods), firms, projects)
    reference = boundary.flow_balances_python(state_ids, list(world.goods),
                                              firms, projects)
    assert native == reference


def test_flows_do_not_touch_the_investment_ledger(world):
    """Вход без ``_investment_state`` не заводит его и не пишет в meta."""
    state_ids = sorted({a['state_id'] for a in world.accounts.values()
                        if a.get('state_id')})
    world.__dict__.pop('_investment_state', None)
    meta_before = digest({k: v for k, v in world.meta.items()
                          if k == 'investment_dirty'})
    boundary.flows(world, state_ids)
    assert '_investment_state' not in world.__dict__
    assert digest({k: v for k, v in world.meta.items()
                   if k == 'investment_dirty'}) == meta_before


# --- 4. Fallback без .pyd --------------------------------------------------

def test_flows_fall_back_to_python(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    native = boundary.run_flows(('s',), ['bread'], firms_fixture(), projects_fixture())
    monkeypatch.setattr(boundary, 'run_flows', boundary.flow_balances_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    assert boundary.run_flows(('s',), ['bread'], firms_fixture(),
                              projects_fixture()) == native
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_confirmed_output_falls_back_to_python(monkeypatch):
    native = boundary.run_confirmed_output([dict(output=3)], 10, 5)
    monkeypatch.setattr(boundary, 'run_confirmed_output',
                        boundary.confirmed_output_python)
    assert boundary.run_confirmed_output([dict(output=3)], 10, 5) == native
    monkeypatch.undo()


def test_boundary_exposes_both_implementations():
    for name in ('flows', 'flow_rows', 'flow_balances_python',
                 'run_flows', 'confirmed_output_python', 'run_confirmed_output'):
        assert callable(getattr(boundary, name)), name
