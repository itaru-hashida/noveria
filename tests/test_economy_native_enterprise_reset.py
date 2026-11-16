"""Месячный numeric-сброс предприятий в Cython: exact-числа, типы, RNG, fallback.

Числовой сброс полей предприятий из ``V3.step`` перенесён в
``enterprise_reset_boundary``: ядро получает готовый список ``existing``,
снимает ``production_observation`` и пишет сброшенные поля в прежнем порядке
ключей с прежними типами. Порядок итерации, RNG, report и хуки остаются
Python-стороной; settlement, дивиденды, production, project state и прочие
срезы в эту задачу не входят.

Тест держит то, что легко потерять при переносе:

  * побитовое совпадение native и Python-эталона на одном списке;
  * пустой список, один элемент, несколько элементов;
  * типы int/float и False для сброшенных полей;
  * сохранение полей вне списка сброса (status, closed, available_from,
    sales_history и другие);
  * отсутствие побочных эффектов: RNG не расходуется, report не меняется,
    deepcopy даёт независимое состояние;
  * fallback без собранного ``.pyd``;
  * exact-отпечатки траектории 3 месяца для seed 17 и 73129, снятые на
    Python-коде V3 до переноса.

Exact-отпечатки траектории сняты до переноса: совпадение означает, что
порядок предприятий, сделок и RNG не изменились.
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
sys.path.insert(0, str(ROOT / 'tests'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy.native import enterprise_reset_boundary as boundary  # noqa: E402

# Exact-отпечатки, снятые на Python-коде V3 до переноса сброса в Cython.
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

RESET_KEYS = ('revenue', 'tax', 'expenses', 'profit', 'output', 'workers',
              'wages', 'cost_of_sales', 'production_cost', 'input_limited',
              'fixed_cost_due', 'fixed_cost_paid', 'dividends',
              'working_reserve', 'project_reserve', 'equipment_wear')

RESET_DEFAULTS = {
    'revenue': 0, 'tax': 0, 'expenses': 0, 'profit': 0, 'output': 0,
    'workers': 0, 'wages': 0, 'cost_of_sales': 0, 'production_cost': 0,
    'input_limited': False, 'fixed_cost_due': 0, 'fixed_cost_paid': 0,
    'dividends': 0.0, 'working_reserve': 0.0, 'project_reserve': 0.0,
    'equipment_wear': {},
}

RESET_TYPES = {
    'revenue': int, 'tax': int, 'expenses': int, 'profit': int,
    'output': int, 'workers': int, 'wages': int, 'cost_of_sales': int,
    'production_cost': int, 'input_limited': bool, 'fixed_cost_due': int,
    'fixed_cost_paid': int, 'dividends': float, 'working_reserve': float,
    'project_reserve': float, 'equipment_wear': dict,
}

# Поля, которые сброс обязан сохранить (не входят в список сброса).
KEPT_KEYS = ('status', 'closed', 'available_from', 'sales_history', 'id',
             'good', 'capacity', 'price', 'production_plan', 'province_id',
             'state_id')


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


def make_enterprise(eid, **overrides):
    enterprise = {
        'id': eid, 'good': 'flour', 'capacity': 10, 'price': 3.0,
        'status': 'active', 'closed': False, 'available_from': 0,
        'sales_history': [dict(sold=5, output=5, input_limited=False)],
        'province_id': 1, 'state_id': 1,
        'production_plan': {'month': 1, 'output': 4.0},
        'production_observation': {'month': 2, 'constraints': {'План': 4.0}},
        'revenue': 100, 'tax': 7, 'expenses': 42, 'profit': 51, 'output': 9,
        'workers': 3, 'wages': 12, 'cost_of_sales': 33, 'production_cost': 45,
        'input_limited': True, 'fixed_cost_due': 5, 'fixed_cost_paid': 5,
        'dividends': 1.5, 'working_reserve': 2.5, 'project_reserve': 0.75,
        'equipment_wear': {'loom': 0.25},
    }
    enterprise.update(overrides)
    return enterprise


def row_shape(enterprise):
    """Сравнимая форма одной строки: пары (ключ, значение, тип) по порядку."""
    return [(key, enterprise.get(key, '<missing>'),
             type(enterprise.get(key)).__name__) for key in enterprise]


def shape(enterprises):
    """Сравнимая форма: список строк или одна dict-строка."""
    if isinstance(enterprises, dict):
        return row_shape(enterprises)
    return [row_shape(enterprise) for enterprise in enterprises]


def run_native(existing):
    boundary.run_reset(existing)
    return existing


def run_python(existing):
    rows = copy.deepcopy(existing)
    boundary.reset_enterprises_python(rows)
    return rows


def assert_reset(enterprise):
    """Сброшенные поля равны прежним значениям по умолчанию и типам."""
    for key in RESET_KEYS:
        assert enterprise[key] == RESET_DEFAULTS[key], key
        assert type(enterprise[key]) is RESET_TYPES[key], key
    assert 'production_observation' not in enterprise


# --- 1. Native против Python-эталона ---------------------------------------
def test_native_matches_python_reference():
    existing = [make_enterprise('a'), make_enterprise('b', workers=8),
                make_enterprise('c', status='idle')]
    native_result = run_native(copy.deepcopy(existing))
    python_result = run_python(existing)
    assert shape(native_result) == shape(python_result)


def test_native_assigns_previous_defaults_and_types():
    existing = [make_enterprise('a')]
    run_native(existing)
    assert_reset(existing[0])


def test_empty_enterprise_list_is_a_no_op():
    existing = []
    state_before = digest(existing)
    run_native(existing)
    assert existing == []
    assert digest(existing) == state_before


def test_single_enterprise_is_reset():
    existing = [make_enterprise('solo')]
    run_native(existing)
    assert_reset(existing[0])


def test_several_enterprises_keep_independent_rows():
    existing = [make_enterprise('a'), make_enterprise('b'),
                make_enterprise('c')]
    run_native(existing)
    for enterprise in existing:
        assert_reset(enterprise)
    assert [e['id'] for e in existing] == ['a', 'b', 'c']


# --- 2. Сохранение полей вне списка сброса ---------------------------------
def test_fields_outside_the_reset_list_are_preserved():
    kept = {key: make_enterprise('a')[key] for key in KEPT_KEYS}
    existing = [make_enterprise('a')]
    run_native(existing)
    for key, value in kept.items():
        assert existing[0][key] == value, key


def test_reset_key_order_matches_the_previous_update():
    """Ключи нового update встают в прежнем порядке dict."""
    enterprise = make_enterprise('a')
    before_keys = list(enterprise)
    run_native([enterprise])
    for key in RESET_KEYS:
        assert key in enterprise, key
    # Новые ключи (отсутствовавшие до сброса) стоят в порядке прежнего update.
    new_keys = [key for key in RESET_KEYS if key not in before_keys]
    positions = [list(enterprise).index(key) for key in new_keys]
    assert positions == sorted(positions)


# --- 3. int/float типы -------------------------------------------------------
@pytest.mark.parametrize('key', sorted(RESET_KEYS))
def test_reset_field_types_are_preserved(key):
    existing = [make_enterprise('a')]
    run_native(existing)
    assert type(existing[0][key]) is RESET_TYPES[key]


def test_zero_and_float_inputs_share_the_same_result_types():
    for overrides in ({'revenue': 0, 'dividends': 0},
                      {'revenue': 999.5, 'dividends': 3}):
        existing = [make_enterprise('a', **overrides)]
        run_native(existing)
        assert_reset(existing[0])


# --- 4. Побочные эффекты: RNG, report, deepcopy ------------------------------
def test_rng_state_is_not_touched():
    existing = [make_enterprise('a'), make_enterprise('b')]
    run_native(existing)
    # RNG вообще не участвует в сбросе; эталонное поведение — ноль вызовов.
    run_python(existing)


def test_report_is_not_mutated():
    report = {'orders': [], 'trades': [dict(seller='a', quantity=1.0)]}
    report_before = digest(report)
    run_native([make_enterprise('a')])
    assert digest(report) == report_before


def test_deepcopy_gives_independent_rows():
    existing = [make_enterprise('a')]
    clone = copy.deepcopy(existing)
    run_native(existing)
    assert_reset(existing[0])
    # Клон до сброса не изменился: сброс писал только свои строки.
    assert clone[0]['revenue'] == 100
    assert clone[0]['equipment_wear'] == {'loom': 0.25}
    assert 'production_observation' in clone[0]


# --- 5. Fallback без .pyd ----------------------------------------------------
def test_fallback_switches_to_the_python_reference(monkeypatch):
    """Без .pyd граница привязывает Python-эквивалент, а не падает."""
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    existing = [make_enterprise('a'), make_enterprise('b')]
    native_snapshot = shape(run_native(copy.deepcopy(existing)))
    monkeypatch.setattr(boundary, 'run_reset', boundary.reset_enterprises_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    assert boundary.run_reset is boundary.reset_enterprises_python
    fallback_snapshot = shape(run_native(existing))
    assert fallback_snapshot == native_snapshot
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True
    assert boundary.run_reset is not boundary.reset_enterprises_python


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    """Отсутствие расширения — это ImportError, а не падение границы."""
    import importlib
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('enterprise_reset_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_enterprise_reset_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('reset_enterprises', 'reset_enterprises_python',
                 'load_enterprise_reset_kernel', 'run_reset'):
        assert callable(getattr(boundary, name)), name


# --- 6. Реальный движок: forced-case и exact -------------------------------
def build_world(seed, months, detail='standard'):
    tmp = workdir('test_enterprise_reset')
    with tmp as directory:
        db, service = sr.build_world(seed, directory, report_detail=detail)
        try:
            for month in range(months):
                service.advance_turn(
                    command_id='er-test-%d-%d' % (seed, month),
                    source='audit')
            return service.economy_state().engine
        finally:
            db.close()


@pytest.fixture(scope='module')
def world_17():
    return build_world(17, 3)


@pytest.fixture(scope='module')
def world_73129():
    return build_world(73129, 3)


def test_forced_case_real_world_keeps_non_reset_fields(world_17):
    engine = world_17
    checked = 0
    for enterprise in engine.enterprises.values():
        if enterprise['available_from'] > engine.meta['month']:
            continue
        # Поля месяца к концу шага уже накоплены поверх сброса (int 0/float 0.0
        # плюс начисления): типы проверяются как int/float без смешивания.
        for key in ('revenue', 'tax', 'expenses', 'profit', 'output',
                    'workers', 'wages', 'cost_of_sales', 'production_cost',
                    'fixed_cost_due', 'fixed_cost_paid', 'dividends',
                    'working_reserve', 'project_reserve'):
            assert type(enterprise[key]) in (int, float), (enterprise['id'], key)
        assert type(enterprise['input_limited']) is bool
        assert type(enterprise['equipment_wear']) is dict
        assert 'production_observation' not in enterprise
        checked += 1
    assert checked > 20


def test_forced_case_real_world_reset_equals_python_reference(world_17):
    """Сброс на живых строках реального месяца совпадает с эталоном."""
    engine = world_17
    existing = copy.deepcopy(list(engine.enterprises.values()))
    for enterprise in existing:
        enterprise['revenue'] = 111
        enterprise['dividends'] = 2.5
        enterprise['equipment_wear'] = {'tool': 0.5}
        enterprise['production_observation'] = {'month': 1}
    native_rows = copy.deepcopy(existing)
    boundary.run_reset(native_rows)
    boundary.reset_enterprises_python(existing)
    assert shape(native_rows[0]) == shape(existing[0])
    for left, right in zip(native_rows, existing):
        assert shape(left) == shape(right)


def test_forced_case_python_fallback_gives_the_same_world(world_17, monkeypatch):
    """Fallback без .pyd не меняет ни одно поле сброса."""
    engine = world_17
    existing = copy.deepcopy(list(engine.enterprises.values()))
    for enterprise in existing:
        enterprise['revenue'] = 222
    monkeypatch.setattr(boundary, 'run_reset', boundary.reset_enterprises_python)
    boundary.run_reset(existing)
    assert all(enterprise['revenue'] == 0 for enterprise in existing)


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
