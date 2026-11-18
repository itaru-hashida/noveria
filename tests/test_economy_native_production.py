"""Focused-тесты production-ядра: точное совпадение с Python-эталоном V3.

Ядро работает с ``object``-арифметикой намеренно: остатки V3 бывают и int, и
float, а ``q`` берётся из ``max(0, constraints[reason])``, поэтому тип выпуска
виден в складе и отчёте. Проверяется не только значение, но и тип, порядок
операций, порядок ключей отчёта и расход RNG.

Эталон — ``production_boundary.produce_python``, fallback — та же функция при
отсутствии собранного расширения.
"""
import copy
import hashlib
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
from modules.economy.native import production_boundary  # noqa: E402

FIXTURE = ROOT/'tests/fixtures/economy.json'
EPS = support.EPS


# ---------------------------------------------------------------------------
# Маленький движок на общей фикстуре: 11 фирм, 17 счетов, 11 товаров
# ---------------------------------------------------------------------------

def build_engine(detail='standard', seed=17):
    """Движок на фикстуре плюс поля, которые реальный step сбрасывает.

    ``Economy.step`` перед фазами обнуляет output/workers/production_cost и
    ``equipment_wear`` каждой фирмы, а ``plan_enterprises`` выставляет
    ``production_limit``. Фикстура обязана воспроизвести это состояние, иначе
    production читает отсутствующие поля.
    """
    cfg = json.loads(FIXTURE.read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
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


def firm(engine, good):
    """Единственная фирма, выпускающая товар."""
    return next(e for e in engine.enterprises.values() if e['good'] == good)


def plan(engine, e, output):
    """Месячный план выпуска: без него planned=0.0 и фирма уходит в idle."""
    e['production_plan'] = dict(month=engine.meta['month'], output=output,
                                sale_price=e['price'])
    return e


def fund(engine, e, cash=10**7):
    """Щедрая касса: ограничение «Деньги на зарплату» не срезает выпуск."""
    engine.accounts[e['id']]['balance'] = cash


def stock(engine, e, good, quantity, value=None):
    """Положить материал на склад фирмы без побочных эффектов."""
    account = engine.accounts[e['id']]
    account['stock'][good] = quantity
    account['values'][good] = quantity if value is None else value
    account['stock_reserved'][good] = 0


def prepare_single(engine):
    """Одна фирма с планом и полным обеспечением: гарантированный выпуск."""
    e = firm(engine, 'grain')
    plan(engine, e, 6)
    fund(engine, e)
    return engine


def prepare_many(engine):
    """Ресурсные, сырьевые, составные и оборудованные рецепты одновременно."""
    # Ресурсные рецепты. Зерно делят grain и flax, поэтому план ниже мощности:
    # так проверяется и конкуренция за общий природный ресурс.
    for good, output in (('wood', 6), ('ore', 6), ('flax', 2), ('fish', 6), ('grain', 2)):
        e = firm(engine, good)
        plan(engine, e, output)
        fund(engine, e)
    flour = firm(engine, 'flour')
    plan(engine, flour, 240)
    fund(engine, flour)
    stock(engine, flour, 'grain', 1000, 2000)
    planks = firm(engine, 'planks')
    plan(engine, planks, 9.6)
    fund(engine, planks)
    stock(engine, planks, 'wood', 100, 200)
    bread = firm(engine, 'bread')
    plan(engine, bread, 240)
    fund(engine, bread)
    stock(engine, bread, 'flour', 500, 1000)
    stock(engine, bread, 'wood', 100, 200)
    # Оборудование: в фикстуре рецепт пуст, добавляем инструменты в износ.
    tools = firm(engine, 'tools')
    plan(engine, tools, 6)
    fund(engine, tools)
    stock(engine, tools, 'iron', 100, 200)
    stock(engine, tools, 'wood', 100, 200)
    stock(engine, tools, 'tools', 50, 100)
    engine.goods['tools']['equipment'] = {'tools': 0.1}
    engine.goods['tools']['durable'] = {'wear_rate': 0.5}
    engine.enterprises[tools['id']]['equipment_wear'] = {}
    return engine


# ---------------------------------------------------------------------------
# Снимки состояния и побитовое сравнение
# ---------------------------------------------------------------------------

ENTERPRISE_FIELDS = ('output', 'status', 'workers', 'production_cost', 'unit_cost',
                     'wages', 'price', 'planned_output', 'production_limit', 'reason',
                     'input_limited', 'equipment_wear', 'production_observation')
REPORT_KEYS = ('production', 'inputs_used', 'equipment_wear', 'labor_available',
               'events', 'orders', 'consumed', 'trades')


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def rng_digest(engine):
    return digest(engine.rng.getstate())


def snapshot(engine):
    """Полный снимок того, что production вправе менять."""
    return {
        'accounts': {aid: {'stock': dict(a['stock']), 'values': dict(a['values']),
                           'balance': a['balance'], 'protected': a['protected'],
                           'reserved': a.get('reserved', 0)}
                     for aid, a in engine.accounts.items()},
        'enterprises': {eid: {key: copy.deepcopy(e.get(key)) for key in ENTERPRISE_FIELDS}
                        for eid, e in engine.enterprises.items()},
        'report': {key: copy.deepcopy(engine.report.get(key)) for key in REPORT_KEYS},
        'construction_labor': copy.deepcopy(dict(getattr(engine, '_construction_labor', {}))),
        'rng': rng_digest(engine),
    }


def assert_same(left, right, path='state'):
    """Точное сравнение: значения, типы и порядок ключей.

    Порядок ключей важен: report['production']/inputs_used заполняются хуками
    в порядке обхода предприятий, а constraints — в порядке вставки.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        assert list(left.keys()) == list(right.keys()), path+'.keys'
        for key in left:
            assert_same(left[key], right[key], path+'['+repr(key)+']')
        return
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        assert type(left) is type(right), path+'.type'
        assert len(left) == len(right), path+'.length'
        for index, (a, b) in enumerate(zip(left, right)):
            assert_same(a, b, path+'['+str(index)+']')
        return
    if isinstance(left, bool) or isinstance(right, bool):
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        # Тип обязателен: int + int -> int, int/float -> float.
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def run_pair(engine):
    """Прогнать эталон и ядро на одинаковых копиях движка."""
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    production_boundary.produce_python(reference, list(reference.enterprises.values()))
    production_boundary.produce(native, list(native.enterprises.values()))
    return reference, native


def assert_pair_matches(engine):
    reference, native = run_pair(engine)
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


# ---------------------------------------------------------------------------
# Один завод, несколько предприятий, несколько рецептов
# ---------------------------------------------------------------------------

def test_single_plant_matches_python_reference():
    engine = prepare_single(build_engine())
    e = firm(engine, 'grain')
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    assert row['status'] == 'active'
    assert row['output'] > 0
    assert row['production_cost'] == row['wages']
    assert row['unit_cost'] == row['production_cost']/row['output']
    assert native.report['production'][e['good']] == row['output']


def test_multiple_enterprises_match():
    engine = build_engine()
    for good in ('wood', 'ore', 'flax', 'fish'):
        e = firm(engine, good)
        plan(engine, e, e['capacity'])
        fund(engine, e)
    reference, native = assert_pair_matches(engine)
    produced = [eid for eid, row in native.enterprises.items() if row['status'] == 'active']
    assert len(produced) == 4
    # Один и тот же список предприятий обойдён в обоих прогонах.
    assert list(native.report['production']) == list(reference.report['production'])


def test_multiple_recipes_match():
    engine = prepare_many(build_engine())
    reference, native = run_pair(engine)
    assert_same(snapshot(reference), snapshot(native))
    # Ресурсный, сырьевой, составной и оборудованный рецепты дали выпуск.
    for good in ('grain', 'flour', 'bread', 'planks', 'tools'):
        assert native.report['production'][good] > 0, good
    assert native.report['inputs_used']['grain'] > 0
    assert native.report['inputs_used']['flour'] > 0
    assert native.report['equipment_wear']['tools'] > 0
    tools_id = firm(engine, 'tools')['id']
    assert native.enterprises[tools_id]['equipment_wear']['tools'] > 0


def test_missing_materials_match():
    engine = build_engine()
    flour = firm(engine, 'flour')
    plan(engine, flour, 100)
    fund(engine, flour)
    stock(engine, flour, 'grain', 0, 0)
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[flour['id']]
    assert row['status'] == 'idle'
    assert row['output'] == 0
    assert row['input_limited'] is True
    assert row['production_limit'] == 'Сырьё: grain'
    assert native.report['inputs_used'] == reference.report['inputs_used'] == {}


def test_zero_output_matches():
    engine = build_engine()
    e = firm(engine, 'grain')
    plan(engine, e, 0)
    fund(engine, e)
    before_plan = dict(e['production_plan'])
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    assert row['status'] == 'idle'
    assert row['output'] == 0
    assert row['planned_output'] == 0
    # План не превышает EPS, поэтому production_limit остаётся прежним.
    assert row['production_limit'] == reference.enterprises[e['id']]['production_limit'] == 'План'
    assert e['production_plan'] == before_plan
    assert native.report['production'] == {}


def test_plan_for_another_month_is_ignored():
    engine = build_engine()
    e = firm(engine, 'grain')
    plan(engine, e, 6)
    e['production_plan']['month'] = engine.meta['month']+1
    fund(engine, e)
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    assert row['planned_output'] == 0.0
    assert type(row['planned_output']) is float
    assert row['status'] == 'idle'


def test_closed_and_suspended_are_skipped():
    engine = build_engine()
    closed, suspended = firm(engine, 'wood'), firm(engine, 'ore')
    plan(engine, closed, 6)
    plan(engine, suspended, 6)
    fund(engine, closed)
    fund(engine, suspended)
    closed['status'] = 'closed'
    suspended['suspended'] = True
    reference, native = assert_pair_matches(engine)
    assert native.enterprises[closed['id']]['status'] == 'closed'
    assert native.enterprises[suspended['id']]['status'] == 'active'
    assert native.enterprises[suspended['id']]['output'] == 0
    assert native.report['production'] == {}


# ---------------------------------------------------------------------------
# int/float-семантика остатков и выпуска
# ---------------------------------------------------------------------------

def test_int_plan_gives_int_output():
    """q = max(0, constraints[reason]): int-план даёт int-выпуск."""
    engine = build_engine()
    e = firm(engine, 'grain')
    plan(engine, e, 6)
    fund(engine, e)
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    assert row['output'] == 6
    assert type(row['output']) is int
    assert type(native.report['production']['grain']) is int
    # Себестоимость всегда float: зарплата — q на float-ставку.
    assert type(row['production_cost']) is float
    assert type(row['unit_cost']) is float


def test_float_plan_gives_float_output():
    engine = build_engine()
    e = firm(engine, 'grain')
    plan(engine, e, 6.0)
    fund(engine, e)
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    assert row['output'] == 6.0
    assert type(row['output']) is float
    assert type(native.report['production']['grain']) is float


def test_int_stock_stays_int_on_output_good():
    """Остаток выпуска сохраняет int, если выпуск целый."""
    engine = build_engine()
    e = firm(engine, 'grain')
    account = engine.accounts[e['id']]
    account['stock']['grain'] = 10
    account['values']['grain'] = 20
    plan(engine, e, 6)
    fund(engine, e)
    before = account['stock']['grain']
    reference, native = assert_pair_matches(engine)
    account = native.accounts[e['id']]
    produced = native.enterprises[e['id']]['output']
    assert type(produced) is int
    assert account['stock']['grain'] == before+produced
    assert type(account['stock']['grain']) is int


def test_float_stock_with_float_limit_keeps_float():
    """Ограничение по float-сырью даёт float-выпуск.

    Остаток 10.0 при расходе 1.2 даёт 8.333..., поэтому min выбирает
    «Сырьё: grain» и q остаётся float, а не обрезается до int.
    """
    engine = build_engine()
    e = firm(engine, 'flour')
    plan(engine, e, 240)
    fund(engine, e)
    stock(engine, e, 'grain', 10.0, 20.0)
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    assert row['status'] == 'active'
    assert type(row['output']) is float
    assert row['output'] == 10.0/1.2
    # Списание упирается в ноль: max(0, available-quantity) возвращает int 0,
    # ровно как прежний код V3 — тип остатка на этом переходе не сохраняется.
    account = native.accounts[e['id']]
    assert account['stock']['grain'] == 0
    assert type(account['stock']['grain']) is int
    assert native.report['inputs_used']['grain'] == 10.0


def test_int_stock_gives_int_output_when_limiting():
    """Целый остаток сырья и целый коэффициент дают целый выпуск."""
    engine = build_engine()
    e = firm(engine, 'flour')
    plan(engine, e, 240)
    fund(engine, e)
    stock(engine, e, 'grain', 120, 240)
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    # 120/1.2 — истинное деление, поэтому ограничение float, а не int.
    assert type(row['output']) is float
    assert row['output'] == 120/1.2
    assert native.accounts[e['id']]['stock']['grain'] <= EPS


# ---------------------------------------------------------------------------
# Порядок операций и побочные эффекты
# ---------------------------------------------------------------------------

def trace(engine):
    """Записать последовательность вызовов движка из production.

    Обёртки ставятся на экземпляр, поэтому их видят и Python-эталон, и ядро:
    ядро вызывает те же атрибуты по имени.
    """
    calls = []

    def wrap(name, record):
        original = getattr(engine, name)

        def wrapper(*args, **kwargs):
            record(calls, name, args)
            return original(*args, **kwargs)

        setattr(engine, name, wrapper)

    wrap('free_stock', lambda log, name, args: log.append((name, args[0], args[1])))
    wrap('distribute', lambda log, name, args: log.append((name, args[0], args[2])))
    wrap('use_stock', lambda log, name, args: log.append((name, args[0], args[1])))
    wrap('add_stock', lambda log, name, args: log.append((name, args[0], args[1])))
    wrap('equipment_wear', lambda log, name, args: log.append((name, args[1])))
    return calls


def test_operation_order_is_preserved():
    engine = prepare_many(build_engine())
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    ref_calls, nat_calls = trace(reference), trace(native)
    production_boundary.produce_python(reference, list(reference.enterprises.values()))
    production_boundary.produce(native, list(native.enterprises.values()))
    assert nat_calls == ref_calls
    assert ref_calls, 'ни одного вызова движка: тест не про порядок'
    names = [row[0] for row in nat_calls]
    # Внутри фирмы порядок жёсткий: distribute, затем use_stock по сырью,
    # затем износ, затем add_stock.
    assert names.index('distribute') < names.index('add_stock')
    assert names.count('add_stock') == sum(1 for row in native.enterprises.values()
                                           if row['status'] == 'active')
    # free_stock вызывается до distribute для каждой фирмы.
    assert names[0] == 'free_stock'


def test_enterprise_order_follows_given_order():
    """Ядро обходит ровно переданный порядок, а не порядок словаря."""
    engine = prepare_many(build_engine())
    engine.shuffled = lambda rows: list(reversed(rows))
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    production_boundary.produce_python(reference, list(reference.enterprises.values()))
    production_boundary.produce(native, list(native.enterprises.values()))
    assert_same(snapshot(reference), snapshot(native))
    # Порядок ключей отчёта повторяет обратный порядок обхода предприятий.
    order = list(reversed(list(native.enterprises.values())))
    expected = [e['good'] for e in order if e['status'] == 'active']
    assert list(native.report['production']) == expected
    # Обратный порядок отличается от исходного: тест ловит игнорирование входа.
    forward = [e['good'] for e in native.enterprises.values() if e['status'] == 'active']
    assert expected != forward


def test_side_effects_called_once():
    """Каждый хук пишет ровно один раз: без удвоения отчёта и износа."""
    engine = prepare_many(build_engine())
    reference, native = assert_pair_matches(engine)
    # Совокупный отчёт равен сумме по предприятиям, а не её удвоению.
    active_output = sum(e['output'] for e in native.enterprises.values()
                        if e['status'] == 'active')
    assert sum(native.report['production'].values()) == active_output
    assert active_output > 0
    assert sum(native.report['inputs_used'].values()) > 0
    # Стоимость списания вошла в себестоимость ровно один раз.
    bread = firm(engine, 'bread')
    row = native.enterprises[bread['id']]
    assert row['production_cost'] > row['wages']


def test_observation_hook_matches():
    engine = prepare_many(build_engine(detail='deep'))
    reference, native = assert_pair_matches(engine)
    observed = [e for e in native.enterprises.values() if e.get('production_observation')]
    assert observed, 'в deep-режиме наблюдение обязано заполняться'
    for row in observed:
        observation = row['production_observation']
        assert observation['month'] == native.meta['month']
        assert list(observation['constraints']) == list(
            reference.enterprises[row['id']]['production_observation']['constraints'])
        assert observation['stocks_before'] == reference.enterprises[row['id']][
            'production_observation']['stocks_before']
    assert native.report['labor_available'] == reference.report['labor_available']


def test_no_observation_in_standard_mode():
    engine = prepare_many(build_engine(detail='standard'))
    reference, native = assert_pair_matches(engine)
    assert not any(e.get('production_observation') for e in native.enterprises.values())
    assert 'labor_available' not in native.report


def test_labor_and_resources_are_decremented():
    engine = prepare_many(build_engine())
    reference, native = assert_pair_matches(engine)
    labor = dict(native._construction_labor)
    assert labor, '_construction_labor обязан остаться на движке'
    assert_same(dict(reference._construction_labor), labor)
    # Труд израсходован: суммарный остаток меньше исходного.
    fresh = prepare_many(build_engine())
    production_boundary.produce_python(fresh, list(fresh.enterprises.values()))
    assert sum(labor.values()) < sum(
        v for v in fresh._construction_labor.values()) + sum(
        e['wages']/fresh.cfg['wage'] for e in fresh.enterprises.values()
        if e['status'] == 'active') + 1


# ---------------------------------------------------------------------------
# deepcopy и граница
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = prepare_single(build_engine())

    def stocks(target):
        return digest({aid: {'stock': dict(a['stock']), 'values': dict(a['values'])}
                       for aid, a in target.accounts.items()})

    before = stocks(engine)
    clone = copy.deepcopy(engine)
    production_boundary.produce(clone, list(clone.enterprises.values()))
    assert stocks(engine) == before, 'production в копии изменил оригинал'
    # Две копии дают одинаковый результат: ядро не держит состояние на модуле.
    left, right = copy.deepcopy(engine), copy.deepcopy(engine)
    production_boundary.produce(left, list(left.enterprises.values()))
    production_boundary.produce(right, list(right.enterprises.values()))
    assert_same(snapshot(left), snapshot(right))


def test_boundary_exposes_native_when_built():
    built = hasattr(production_boundary.load_production_kernel(), 'calculate_output')
    assert production_boundary.NATIVE_KERNEL is built
    if built:
        assert production_boundary.calculate_output.__module__.endswith('production_kernel')
        assert production_boundary.produce.__module__.endswith('production_boundary')
    else:
        assert production_boundary.calculate_output is production_boundary.calculate_output_python


def test_native_entry_points_are_compiled():
    kernel = production_boundary.load_production_kernel()
    if not hasattr(kernel, 'calculate_output'):
        pytest.skip('Cython-расширение production_kernel не собрано')
    assert production_boundary.calculate_output is kernel.calculate_output
    assert not hasattr(kernel, 'run_enterprises')


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('production_kernel'):
            raise ImportError("cannot import name 'production_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert production_boundary.load_production_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = prepare_many(build_engine())
    reference, _ = run_pair(engine)
    expected = snapshot(reference)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('production_kernel'):
            raise ImportError("cannot import name 'production_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(production_boundary)
    try:
        assert production_boundary.NATIVE_KERNEL is False
        assert production_boundary.calculate_output is production_boundary.calculate_output_python
        assert production_boundary.produce.__module__.endswith('production_boundary')
        fallback = copy.deepcopy(engine)
        production_boundary.produce(fallback, list(fallback.enterprises.values()))
        assert_same(expected, snapshot(fallback))
    finally:
        monkeypatch.undo()
        importlib.reload(production_boundary)


def test_v3_has_single_call_site():
    """V3.produce — тонкая обёртка над границей, а не вторая копия цикла."""
    source = inspect.getsource(Economy.produce)
    assert '_production.produce(self, existing)' in source
    executable = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped == 'def produce(self, existing, sells):':
            continue
        if stripped == '_production.produce(self, existing)':
            continue
        if stripped.startswith('"""') or stripped.endswith('"""'):
            continue
        executable.append(stripped)
    assert executable == [], executable
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_production.produce(') == 1


def test_rng_equal_between_branches():
    """RNG расходуется одинаково: перемешивание осталось в Python."""
    engine = prepare_many(build_engine())
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    before = rng_digest(engine)
    production_boundary.produce_python(reference, list(reference.enterprises.values()))
    production_boundary.produce(native, list(native.enterprises.values()))
    assert rng_digest(reference) == rng_digest(native)
    assert rng_digest(native) != before, 'перемешивание обязано тратить RNG'


def test_forced_case_produces_goods():
    """Явный сценарий: production реально выпускает товар и меняет склад."""
    engine = build_engine()
    e = firm(engine, 'flour')
    plan(engine, e, 100)
    fund(engine, e)
    stock(engine, e, 'grain', 500, 1000)
    before = engine.accounts[e['id']]['stock'].get('flour', 0)
    reference, native = assert_pair_matches(engine)
    row = native.enterprises[e['id']]
    assert row['status'] == 'active'
    assert row['output'] == 100
    assert native.accounts[e['id']]['stock']['flour'] == before+100
    assert native.report['production']['flour'] == 100
    assert native.report['inputs_used']['grain'] == 100*1.2
    assert native.accounts[e['id']]['stock']['grain'] == 500-100*1.2


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

REPLAY_17 = (
    ('1f610dabf54474937090017e2cd14eae21e9c7d4873be53e66a801881e54d626',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('465b369cfc76bba0e8eb44f44a47d92a62b2e18fd7575831b9f8fd56e82ae173',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('b1dc4adbfa1d3146711167d1fa84c255823e43dba33070bf404e7ca5e0448fbe',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('260f194aeedff4e7137101c3d86c30453da0af0c0df385ce4467f3a40552089f',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('f146f082c06de3280d66c0ae2f9e9fd5adf7547fe0ec00cf1365a6279b9c40a6',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('fac75224e18963162780f6b5528c813a850f326e293d9590b1c86b4f58a58e92',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def replay_digest(engine):
    """Тот же digest, что у work/project_batch_compare.py, плюс отчёт production."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'production': engine.report.get('production'),
        'inputs_used': engine.report.get('inputs_used'),
        'equipment_wear': engine.report.get('equipment_wear'),
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_production_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='prod-replay-%d-%d' % (seed, index),
                                     source='audit')
                engine = service.economy_state().engine
                rows.append((replay_digest(engine), rng_digest(engine)))
        finally:
            db.close()
    return rows


@pytest.fixture(scope='module')
def replay_seed_17():
    return run_replay(17)


@pytest.fixture(scope='module')
def replay_seed_73129():
    return run_replay(73129)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_17(replay_seed_17, index):
    state, rng = replay_seed_17[index]
    assert (state, rng) == REPLAY_17[index], 'месяц %d' % (index+1)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_73129(replay_seed_73129, index):
    state, rng = replay_seed_73129[index]
    assert (state, rng) == REPLAY_73129[index], 'месяц %d' % (index+1)


def test_replay_is_deterministic(replay_seed_17, replay_seed_73129):
    for rows in (replay_seed_17, replay_seed_73129):
        assert len(rows) == 3
        assert all(rng for _, rng in rows)
    # Повторный прогон даёт те же digest'ы: ядро не тянет состояние из модуля.
    assert run_replay(17) == list(replay_seed_17)


def test_replay_actually_produced():
    """Exact replay не должен проходить на пустом выпуске."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    with workdir('test_production_outputs') as directory:
        db, service = sr.build_world(73129, directory, report_detail='standard')
        try:
            for index in range(3):
                service.advance_turn(command_id='prod-outputs-%d' % index, source='audit')
            engine = service.economy_state().engine
        finally:
            db.close()
    active = [e for e in engine.enterprises.values() if e['status'] == 'active']
    assert len(active) > 50
    assert sum(e['output'] for e in active) > 1000
    assert sum(engine.report['production'].values()) > 1000
