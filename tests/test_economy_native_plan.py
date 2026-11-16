"""Focused-тесты ядра планирования закупок предприятий.

Проверяются ``plan_enterprises`` и ``input_requests``: порядок обхода по
убыванию глубины товара, условия восстановления/планирования, ``cash_needed``,
40 итераций бисекции по кассе, потребности в сырье и оборудовании, резерв
зарплат и поля ``production_plan``. Сравниваются значения, типы, порядок
заявок и расход RNG.

Эталон — ``input_requests_python``/``plan_enterprises_python`` в границе,
fallback — те же эквиваленты при отсутствии собранного расширения.
"""
import copy
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import plan_boundary as boundary  # noqa: E402

pytest.importorskip('modules.economy.native.plan_kernel')

EPS = support.EPS
FIXTURE = ROOT/'tests/fixtures/economy.json'


def build_engine(seed=17, detail='standard'):
    cfg = json.loads(FIXTURE.read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H',
             properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'лён', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    for e in engine.enterprises.values():
        e.update(production_limit='План', equipment_wear={}, equipment_wear_total={},
                 output=0, workers=0, production_cost=0, input_limited=False,
                 expected_margin=0.0, expected_unit_cost=0.0, expected_price=e['price'],
                 suspended=False)
    return engine


def firm(engine, good='bread'):
    return next(e for e in engine.enterprises.values() if e['good'] == good)


def clear(engine):
    engine.report['orders'] = []
    engine._order_store = None
    for a in engine.accounts.values():
        a.update(stock={}, values={}, stock_reserved={})
    engine.reset_report()
    return engine


def typed(value):
    return (type(value).__name__, value)


def state(engine):
    """Снимок чисел, которые трогает планирование закупок."""
    return {
        'enterprises': {e['id']: {k: v for k, v in e.items()
                                  if k in ('planned_output', 'input_target_output',
                                           'production_limit', 'production_plan',
                                           'reachable_demand', 'sales_forecast',
                                           'expected_margin')}
                        for e in engine.enterprises.values()},
        'accounts': {a['id']: {k: a.get(k) for k in ('protected', 'payroll_protected',
                                                     'operating_protected', 'balance',
                                                     'reserved', 'stock')}
                     for a in engine.accounts.values()},
        'orders': [(o['id'], o['participant'], o['good'], o['side'], o['quantity'],
                    o['price'], o['purpose'], o.get('adaptive_input_budget'))
                   for o in engine.report['orders']],
        'rng': repr(engine.rng.getstate()),
    }


def sell(engine, account, good, quantity, price):
    engine.add_stock(account, good, quantity, quantity*price)
    return engine.order(account, good, quantity, 'sell', price, 'Продажа запасов')


# --- 1. input_requests -------------------------------------------------------

def test_input_requests_free_cash_path_matches_reference():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 10**7
    sells = {}
    native = copy.deepcopy(engine)
    reference = copy.deepcopy(engine)

    engine.input_requests(bakery, sells)
    native_state = state(engine)
    reference_engine = reference
    boundary.input_requests_python(reference_engine, reference_engine.enterprises[bakery['id']],
                                   copy.deepcopy(sells))
    assert native_state == state(reference_engine)


def test_input_requests_bisection_limits_target_to_cash():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    account = engine.accounts[bakery['id']]
    account['balance'] = 3.0
    bakery.update(sales_forecast=100.0, expected_margin=1.0, expected_unit_cost=1.0,
                  expected_price=2.0, planned_price=2.0, reachable_demand=100.0)
    engine.input_requests(bakery, {})
    plan = bakery['production_plan']
    assert 0 < plan['output'] < plan['requested_output']
    assert plan['reason'] == 'Деньги на выпуск'
    # Касса после планирования: резерв зарплат плюс резервы заявок не выше баланса.
    assert account['payroll_protected'] <= account['balance']+EPS


def test_input_requests_keeps_payroll_protected_and_protected_sum():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    account = engine.accounts[bakery['id']]
    account['balance'] = 10**7
    account['operating_protected'] = 5.0
    account['protected'] = 7.0
    engine.input_requests(bakery, {})
    # Прежний operating_protected снят, резерв зарплат добавлен к protected.
    assert 'operating_protected' not in account
    assert account['payroll_protected'] == pytest.approx(
        bakery['production_plan']['output']*engine.goods[bakery['good']]['labor']
        *engine.cfg['wage'])
    assert account['protected'] == pytest.approx(2.0+account['payroll_protected'])


def test_input_requests_orders_materials_then_equipment():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 10**7
    engine.input_requests(bakery, {})
    plan = bakery['production_plan']
    created = [o for o in engine.report['orders'] if o['side'] == 'buy']
    assert [o['purpose'] for o in created] == (
        ['Сырьё']*len(plan['input_ratios'])
        + ['Оборудование']*len(plan['equipment_required']))
    assert [o['id'] for o in created] == plan['input_orders']
    # Сырьевые заявки помечены адаптивным бюджетом, оборудование — нет.
    assert all(o.get('adaptive_input_budget') for o in created
               if o['purpose'] == 'Сырьё')
    assert not any(o.get('adaptive_input_budget') for o in created
                   if o['purpose'] == 'Оборудование')


def test_input_requests_plan_keeps_all_keys_and_order():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 10**7
    engine.input_requests(bakery, {})
    plan = bakery['production_plan']
    assert list(plan) == [
        'month', 'output', 'sale_price', 'expected_net_price', 'input_prices',
        'input_ratios', 'wage_unit', 'equipment_required', 'equipment_wear',
        'fixed_unit', 'requested_output', 'free_cash', 'known_order_count',
        'reachable_demand', 'sales_forecast', 'expected_margin', 'reason',
        'input_orders']
    assert plan['wage_unit'] == engine.goods[bakery['good']]['labor']*engine.cfg['wage']


def test_input_requests_does_not_touch_rng():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 10**7
    before = repr(engine.rng.getstate())
    engine.input_requests(bakery, {})
    assert repr(engine.rng.getstate()) == before


def test_input_requests_matches_reference_including_types():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 10**7
    reference = copy.deepcopy(engine)
    engine.input_requests(bakery, {})
    reference_bakery = next(e for e in reference.enterprises.values()
                            if e['good'] == 'bread')
    boundary.input_requests_python(reference, reference_bakery, {})
    native_plan = bakery['production_plan']
    reference_plan = reference_bakery['production_plan']
    assert list(native_plan) == list(reference_plan)
    for key in native_plan:
        assert typed(native_plan[key]) == typed(reference_plan[key]) or \
            native_plan[key] == reference_plan[key], key


# --- 2. plan_enterprises -----------------------------------------------------

def test_plan_enterprises_plans_deepest_goods_first():
    engine = build_engine()
    clear(engine)
    for a in engine.accounts.values():
        a['balance'] = 10**7
    existing = list(engine.enterprises.values())
    seen = []
    original = engine.input_requests

    def spy(e, sells):
        seen.append((engine.depth(e['good']), e['id']))
        return original(e, sells)

    engine.input_requests = spy
    try:
        engine.plan_enterprises(existing, {})
    finally:
        engine.input_requests = original
    depths = [depth for depth, _ in seen]
    assert depths == sorted(depths, reverse=True)
    assert len(seen) == len(existing)


def test_plan_enterprises_recovers_closed_and_broke_enterprises():
    engine = build_engine()
    clear(engine)
    closed = firm(engine)
    closed['status'] = 'closed'
    broke = next(e for e in engine.enterprises.values()
                 if e['id'] != closed['id'] and e['status'] != 'closed')
    engine.accounts[broke['id']]['balance'] = 0.0
    recovered, planned = [], []
    engine.recover_enterprises = lambda sells, entities: recovered.extend(
        e['id'] for e in entities)
    original = engine.input_requests

    def spy(e, sells):
        planned.append(e['id'])
        return original(e, sells)

    engine.input_requests = spy
    try:
        engine.plan_enterprises([closed, broke], {})
    finally:
        engine.input_requests = original
    assert set(recovered) == {closed['id'], broke['id']}
    # Закрытое предприятие не планируется; нищее — планируется после восстановления.
    assert closed['id'] not in planned
    assert broke['id'] in planned


def test_plan_enterprises_skips_suspended_and_future_enterprises():
    engine = build_engine()
    clear(engine)
    suspended = firm(engine)
    suspended['suspended'] = True
    future = next(e for e in engine.enterprises.values()
                  if e['id'] != suspended['id'])
    future['available_from'] = engine.meta['month']+5
    planned = []
    original = engine.input_requests

    def spy(e, sells):
        planned.append(e['id'])
        return original(e, sells)

    engine.input_requests = spy
    try:
        engine.plan_enterprises([suspended, future], {})
    finally:
        engine.input_requests = original
    assert planned == []


def test_plan_enterprises_matches_reference_order_and_state():
    engine = build_engine()
    clear(engine)
    for a in engine.accounts.values():
        a['balance'] = 10**7
    existing = list(engine.enterprises.values())
    engine.plan_enterprises(existing, {})
    native_state = state(engine)

    reference = build_engine()
    clear(reference)
    for a in reference.accounts.values():
        a['balance'] = 10**7
    boundary.plan_enterprises_python(
        reference, list(reference.enterprises.values()), {})
    assert native_state == state(reference)


def test_plan_enterprises_shuffles_before_order_inside_native():
    """RNG остаётся в Python: перемешивание расходуется до вызова ядра."""
    engine = build_engine()
    clear(engine)
    existing = list(engine.enterprises.values())
    before = repr(engine.rng.getstate())
    engine.shuffled(existing)
    after = repr(engine.rng.getstate())
    assert before != after
    # Ядро перемешивание не повторяет: прямой вызов ядра RNG не тратит.
    boundary.run_plan(engine, existing, {}, EPS)
    assert repr(engine.rng.getstate()) == after


# --- 3. Fallback и call site ---------------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 10**7
    native = copy.deepcopy(engine)
    engine.input_requests(bakery, {})
    native_state = state(engine)

    monkeypatch.setattr(boundary, 'input_requests', boundary._input_requests_python)
    monkeypatch.setattr(boundary, 'plan_enterprises',
                        boundary._plan_enterprises_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    reference = copy.deepcopy(native)
    reference_bakery = next(e for e in reference.enterprises.values()
                            if e['good'] == 'bread')
    reference.input_requests(reference_bakery, {})
    assert state(reference) == native_state
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('plan_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_plan_kernel() is None


def test_v3_delegates_to_plan_boundary():
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_plan.input_requests(') == 1
    assert module_source.count('_plan.plan_enterprises(') == 1
    for body in (inspect.getsource(Economy.input_requests),
                 inspect.getsource(Economy.plan_enterprises)):
        assert '_plan.' in body
        # Прежние Python-тела (бисекция, cash_needed, обход) в v3 не остались.
        assert 'cash_needed(' not in body
        assert 'range(40)' not in body
        assert 'for e in sorted' not in body
    assert 'def cash_needed' not in module_source


def test_native_kernel_is_used_on_the_standard_path():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    engine.accounts[bakery['id']]['balance'] = 10**7
    seen = []
    real = boundary.run_input_requests
    boundary.run_input_requests = lambda *args: seen.append(args) or real(*args)
    try:
        engine.input_requests(bakery, {})
    finally:
        boundary.run_input_requests = real
    assert seen, 'штатный путь обязан звать ядро plan_kernel'
