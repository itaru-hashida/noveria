"""Focused-тесты зарплатного ядра публичных строек.

Ядро считает ``object``-арифметикой намеренно: ``max_workers``/``work_required``
бывают ``int``, ``labor`` и ``free_cash`` — ``float``, а ``min`` возвращает один
из аргументов вместе с его типом. Проверяется не только значение, но и тип,
порядок обхода, порядок аргументов ``min`` и однократность side effects.

Группы тестов:

  * числовая часть (готовность, работники, wage bill) против эталона;
  * полный ``advance_public_projects`` против того же метода с подменённым на
    эталон ядром — деньги, склад, отчёт и RNG;
  * exact replay двух seed'ов и fallback без .pyd.
"""
import contextlib
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
from modules.economy.native import wage_boundary  # noqa: E402

FIXTURE = ROOT/'tests/fixtures/economy.json'
EPS = support.EPS


# ---------------------------------------------------------------------------
# Маленький движок на общей фикстуре
# ---------------------------------------------------------------------------

def build_engine(seed=17):
    """Движок на фикстуре: одно государство, домохозяйство и два предприятия."""
    cfg = json.loads(FIXTURE.read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['grain', 'wood', 'ore', 'flax', 'fish'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = 'standard'
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def add_household(engine, identity, population=100000, balance=100000.0):
    """Копия домохозяйства фикстуры под новым идентификатором."""
    row = copy.deepcopy(engine.accounts['h'])
    row['id'] = identity
    row['group_id'] = identity
    row['balance'] = balance
    engine.accounts[identity] = row
    engine.entities[identity] = dict(id=identity, kind='SocialGroup', name=identity,
        properties=dict(engine.entities['h']['properties'], population=population))
    return row


def account(engine, identity='treasury-00001'):
    """Счёт-заказчик стройки с заданной кассой.

    Счёт создаётся в обход границы намеренно: тест готовит состояние так, как
    его готовит ``bootstrap`` (прямая запись в ``accounts``), а дальше все
    деньги двигает только ``distribute``/``transfer``.
    """
    row = dict(id=identity, kind='treasury', name=identity, state_id='s',
        province_id='p', tile_id='t', balance=0.0, reserved=0.0, protected=0.0,
        stock={}, stock_reserved={}, stock_sales_history={}, flow={}, needs={})
    engine.accounts[identity] = row
    return row


def project(identity='proj-1', mode='normal', max_workers=4, work_required=10,
            work_done=0, wage_multiplier=1):
    """Одна стройка: только те поля, которые читает зарплатный срез."""
    return dict(id=identity, kind='school', city_id='c', institution_id='inst-'+identity,
        name='Школа '+identity, mode=mode, max_workers=max_workers,
        work_required=work_required, work_done=work_done, status='preparing',
        location=dict(state_id='s', province_id='p', tile_id='t'),
        materials={}, material_bill={}, equipment={}, equipment_held={},
        equipment_wear={}, support_stock={})


def fund(engine, identity, balance):
    """Задать кассу через cash_store: единственный ledger."""
    engine.cash_store.set_balance(identity, balance)
    return engine.accounts[identity]


def with_project(engine, rows, identity='treasury-00001', balance=10000.0):
    """Счёт с кассой и списком строек; labor выставлен как после производства."""
    row = account(engine, identity)
    fund(engine, identity, balance)
    row['public_projects'] = rows
    engine._construction_labor = engine._construction_labor if hasattr(
        engine, '_construction_labor') else {}
    return row


def labor(engine, workers):
    """Остаток труда месяца: ключ тот же, что у стройки."""
    from collections import defaultdict
    engine._construction_labor = defaultdict(float)
    engine._construction_labor['s', 'p'] = workers
    return engine._construction_labor


# ---------------------------------------------------------------------------
# Снимки и точное сравнение
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS = ('balance', 'reserved')


def rng_digest(engine):
    return hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                     ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def wage_flows(engine):
    """Только зарплатные переводы среза."""
    return [flow for flow in engine.report['flows']
            if flow['reason'] in ('Заработная плата строителей',
                                  'Временное жильё строителей')]


def snapshot(engine):
    """Полный снимок того, что advance_public_projects вправе менять."""
    return {
        'accounts': {aid: {key: a.get(key) for key in ACCOUNT_FIELDS}
                     for aid, a in engine.accounts.items()},
        'projects': {aid: copy.deepcopy(a.get('public_projects'))
                     for aid, a in engine.accounts.items()},
        'stocks': {aid: copy.deepcopy(a.get('stock')) for aid, a in engine.accounts.items()},
        'public_projects': copy.deepcopy(engine.report['public_projects']),
        'construction_labor': copy.deepcopy(engine.report['construction_labor']),
        'consumed': copy.deepcopy(engine.report['consumed']),
        'equipment_wear': copy.deepcopy(engine.report['equipment_wear']),
        'construction_used': copy.deepcopy(engine.report['construction_used']),
        'flows': copy.deepcopy(wage_flows(engine)),
        'labor': dict(getattr(engine, '_construction_labor', {})),
        'entities': sorted(engine.entities),
        'rng': rng_digest(engine),
    }


def assert_same(left, right, path='state'):
    """Точное сравнение: значения, типы и порядок ключей."""
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
        # Тип обязателен: min/max сохраняют тип выбранного аргумента.
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def clone(engine):
    return copy.deepcopy(engine)


@contextlib.contextmanager
def python_reference():
    """Подменить ядро эталоном на время полного вызова."""
    original = wage_boundary.run_advance
    wage_boundary.run_advance = wage_boundary.run_advance_python
    try:
        yield
    finally:
        wage_boundary.run_advance = original


def method_pair(engine):
    """Полный advance_public_projects: эталонное ядро против native-ядра."""
    reference, native = clone(engine), clone(engine)
    with python_reference():
        reference.advance_public_projects()
    native.advance_public_projects()
    return reference, native


def assert_method_matches(engine):
    reference, native = method_pair(engine)
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


# ---------------------------------------------------------------------------
# Числовая часть: готовность и число работников
# ---------------------------------------------------------------------------

def test_ready_requires_materials_and_equipment():
    row = project()
    assert wage_boundary.ready_python(row, EPS) is True
    row['material_bill'] = {'wood': 5}
    row['materials'] = {'wood': 4}
    assert wage_boundary.ready_python(row, EPS) is False
    row['materials'] = {'wood': 5}
    assert wage_boundary.ready_python(row, EPS) is True
    row['equipment'] = {'saw': 1}
    row['equipment_held'] = {}
    assert wage_boundary.ready_python(row, EPS) is False


def test_ready_short_circuits_on_first_missing_material():
    """Второй материал не читается, если первого не хватает."""
    row = project()
    row['material_bill'] = {'wood': 5, 'ore': 5}
    row['materials'] = {'wood': 0}
    engine = build_engine()
    hooks = wage_boundary.WageHooks(engine)
    # Падение при чтении второго товара доказывает short-circuit.
    class Boom(dict):
        def get(self, key, default=None):
            if key == 'ore':
                raise AssertionError('short-circuit нарушен')
            return super().get(key, default)

    row['materials'] = Boom({'wood': 0})
    assert wage_boundary.ready_python(row, EPS) is False


def test_zero_wage_bill_pays_nothing():
    """Нет труда: работников ноль, переводов и зарплатных строк нет."""
    engine = build_engine()
    with_project(engine, [project()])
    labor(engine, 0.0)
    before = engine.accounts['treasury-00001']['balance']
    reference, native = assert_method_matches(engine)
    assert wage_flows(native) == []
    assert native.accounts['treasury-00001']['balance'] == before
    assert native.report['public_projects'][0]['workers'] == 0.0


def test_single_worker_pays_once():
    """Один работник: ровно один зарплатный перевод на сумму wage."""
    engine = build_engine()
    with_project(engine, [project(max_workers=1)])
    labor(engine, 1.0)
    before = engine.accounts['treasury-00001']['balance']
    reference, native = assert_method_matches(engine)
    flows = wage_flows(native)
    assert len(flows) == 1
    assert flows[0]['gold'] == engine.cfg['wage']
    assert native.accounts['treasury-00001']['balance'] == before-engine.cfg['wage']
    assert native.report['public_projects'][0]['workers'] == 1.0


def test_multiple_workers_scale_wage_bill():
    """Три работника: wage bill линеен по числу работников."""
    engine = build_engine()
    with_project(engine, [project(max_workers=3)])
    labor(engine, 10.0)
    reference, native = assert_method_matches(engine)
    flows = wage_flows(native)
    row = native.report['public_projects'][0]
    assert row['workers'] == 3.0
    assert flows[0]['gold'] == 3.0*engine.cfg['wage']


def test_workers_limited_by_remaining_labor():
    labor_engine = build_engine()
    with_project(labor_engine, [project(max_workers=5)])
    labor(labor_engine, 2.0)
    reference, native = assert_method_matches(labor_engine)
    assert native.report['public_projects'][0]['workers'] == 2.0


def test_workers_limited_by_work_required():
    engine = build_engine()
    with_project(engine, [project(max_workers=5, work_required=10, work_done=9)])
    labor(engine, 100.0)
    reference, native = assert_method_matches(engine)
    assert native.report['public_projects'][0]['workers'] == 1.0


def test_min_argument_order_is_preserved():
    """При равных аргументах min возвращает первый: work_required-work_done."""
    engine = build_engine()
    with_project(engine, [project(max_workers=2, work_required=10, work_done=8)])
    labor(engine, 2.0)
    reference, native = assert_method_matches(engine)
    value = native.report['public_projects'][0]['workers']
    assert value == 2.0
    assert type(value) is int or type(value) is float


# ---------------------------------------------------------------------------
# Несколько строек и предприятий
# ---------------------------------------------------------------------------

def test_multiple_projects_in_insertion_order():
    """Две стройки счёта: обход в порядке вставки, труд убывает последовательно."""
    engine = build_engine()
    with_project(engine, [project('proj-1', max_workers=2),
                          project('proj-2', max_workers=2)])
    labor(engine, 3.0)
    reference, native = assert_method_matches(engine)
    rows = native.report['public_projects']
    assert [r['project_id'] for r in rows] == ['proj-1', 'proj-2']
    assert rows[0]['workers'] == 2.0
    # Второй стройке остаётся один работник.
    assert rows[1]['workers'] == 1.0


def test_multiple_accounts_in_id_order():
    """Несколько заказчиков: счета обходятся в порядке sorted по id."""
    engine = build_engine()
    with_project(engine, [project('proj-b')], identity='treasury-b', balance=10000.0)
    with_project(engine, [project('proj-a')], identity='treasury-a', balance=10000.0)
    labor(engine, 100.0)
    seen = []
    original = engine.distribute

    def traced(sender, recipients, amount, reason):
        if sender not in seen:
            seen.append(sender)
        return original(sender, recipients, amount, reason)

    engine.distribute = traced
    try:
        engine.advance_public_projects()
    finally:
        engine.distribute = original
    assert seen == ['treasury-a', 'treasury-b']


def test_multiple_enterprises_are_independent():
    """Касса каждого заказчика ограничивает только его собственные стройки."""
    engine = build_engine()
    with_project(engine, [project('proj-a', max_workers=10)],
        identity='treasury-a', balance=10000.0)
    with_project(engine, [project('proj-b', max_workers=10)],
        identity='treasury-b', balance=0.0)
    labor(engine, 100.0)
    reference, native = assert_method_matches(engine)
    rows = {r['project_id']: r for r in native.report['public_projects']}
    assert rows['proj-a']['workers'] > 0
    assert rows['proj-b']['workers'] == 0.0


# ---------------------------------------------------------------------------
# Деньги: недостаток, частичная выплата, полный баланс
# ---------------------------------------------------------------------------

def test_insufficient_cash_reduces_workers():
    """Кассы меньше wage: работников ограничивает free_cash/wage."""
    engine = build_engine()
    row = with_project(engine, [project(max_workers=10)], balance=0.0)
    labor(engine, 100.0)
    fund(engine, 'treasury-00001', engine.cfg['wage']*2)
    reference, native = assert_method_matches(engine)
    assert native.report['public_projects'][0]['workers'] == 2.0


def test_partial_payment_keeps_cash_non_negative():
    """Частичная выплата: касса после среза не уходит в минус."""
    engine = build_engine()
    with_project(engine, [project(max_workers=10)])
    labor(engine, 100.0)
    fund(engine, 'treasury-00001', engine.cfg['wage']*1.5)
    reference, native = assert_method_matches(engine)
    assert native.accounts['treasury-00001']['balance'] >= 0
    assert wage_flows(native)


def test_full_balance_pays_exactly_wage_bill():
    """Полного баланса хватает: выплата равна wage bill, остаток сохраняется."""
    engine = build_engine()
    with_project(engine, [project(max_workers=4)], balance=10000.0)
    labor(engine, 4.0)
    before = engine.accounts['treasury-00001']['balance']
    reference, native = assert_method_matches(engine)
    paid = sum(flow['gold'] for flow in wage_flows(native))
    assert paid == 4.0*engine.cfg['wage']
    assert native.accounts['treasury-00001']['balance'] == before-paid


def test_lodging_pays_second_transfer_only_when_positive():
    """Нулевая надбавка за жильё не создаёт второго перевода."""
    engine = build_engine()
    with_project(engine, [project(max_workers=2)])
    labor(engine, 2.0)
    reference, native = assert_method_matches(engine)
    assert len(wage_flows(native)) == 1


def test_transfer_count_matches_reference():
    """Число вызовов transfer совпадает с эталоном на ненулевом сценарии."""
    engine = build_engine()
    with_project(engine, [project('proj-1', max_workers=2),
                          project('proj-2', max_workers=2)])
    labor(engine, 100.0)

    def count(target):
        calls = []
        original = target.transfer

        def traced(sender, recipient, amount, reason):
            calls.append((sender, reason))
            return original(sender, recipient, amount, reason)

        target.transfer = traced
        try:
            target.advance_public_projects()
        finally:
            target.transfer = original
        return calls

    reference, native = clone(engine), clone(engine)
    ref_calls = count(reference)
    nat_calls = count(native)
    assert nat_calls == ref_calls
    assert len(nat_calls) == 2, 'две стройки, по одной зарплате'


# ---------------------------------------------------------------------------
# int/float
# ---------------------------------------------------------------------------

def test_int_inputs_stay_int():
    """int-труд и int-ставка: работники и wage bill остаются int.

    В ``flows`` попадает уже доля получателя: ``distribute`` делит сумму по
    населению, поэтому проверяется сумма, переданная в ``distribute``.
    """
    engine = build_engine()
    with_project(engine, [project(max_workers=2)], balance=10000)
    engine.cfg['wage'] = 3
    engine.cfg['construction_modes']['normal']['wage_multiplier'] = 1
    labor(engine, 2)
    amounts = []
    original = engine.distribute

    def traced(sender, recipients, amount, reason):
        amounts.append((amount, reason))
        return original(sender, recipients, amount, reason)

    engine.distribute = traced
    try:
        reference, native = assert_method_matches(engine)
    finally:
        engine.distribute = original
    row = native.report['public_projects'][0]
    assert type(row['workers']) is int
    assert row['workers'] == 2
    assert amounts, 'зарплата обязана пройти через distribute'
    wage_bill, reason = amounts[0]
    assert reason == 'Заработная плата строителей'
    assert type(wage_bill) is int
    assert wage_bill == 6


def test_float_inputs_stay_float():
    """float-мощность даёт float-работников: без усечения к double/int."""
    engine = build_engine()
    with_project(engine, [project(max_workers=2.5)], balance=10000.0)
    labor(engine, 2.5)
    reference, native = assert_method_matches(engine)
    row = native.report['public_projects'][0]
    assert type(row['workers']) is float
    assert row['workers'] == 2.5
    assert type(wage_flows(native)[0]['gold']) is float


def test_min_tie_returns_first_argument_type():
    """Равные аргументы: ``min`` возвращает первый, вместе с его типом.

    ``max_workers=2`` (int), труд ``2.0`` и касса ``2.0*wage`` равны, поэтому
    работники обязаны остаться int — ровно как в прежнем Python-коде.
    """
    engine = build_engine()
    with_project(engine, [project(max_workers=2)], balance=10000.0)
    labor(engine, 2.0)
    reference, native = assert_method_matches(engine)
    row = native.report['public_projects'][0]
    assert row['workers'] == 2
    assert type(row['workers']) is int


def test_zero_workers_is_float_zero():
    """Неготовая стройка пишет 0.0, а не 0: тип наблюдаем в отчёте."""
    engine = build_engine()
    row = with_project(engine, [project()])
    row['public_projects'][0]['material_bill'] = {'wood': 5}
    labor(engine, 100.0)
    reference, native = assert_method_matches(engine)
    assert native.report['public_projects'][0]['workers'] == 0.0
    assert type(native.report['public_projects'][0]['workers']) is float


# ---------------------------------------------------------------------------
# Порядок суммирования и отчёт
# ---------------------------------------------------------------------------

def test_sum_order_of_report_rows():
    """Строки отчёта: по одной на стройку, в порядке обхода."""
    engine = build_engine()
    with_project(engine, [project('proj-1'), project('proj-2'), project('proj-3')])
    labor(engine, 100.0)
    reference, native = assert_method_matches(engine)
    assert [r['project_id'] for r in native.report['public_projects']] == \
        ['proj-1', 'proj-2', 'proj-3']


def test_report_and_events_are_not_duplicated():
    """Строка отчёта пишется ровно один раз на стройку и вызов."""
    engine = build_engine()
    with_project(engine, [project('proj-1'), project('proj-2')])
    labor(engine, 100.0)
    engine.advance_public_projects()
    assert len(engine.report['public_projects']) == 2
    engine.report['public_projects'].clear()
    engine.advance_public_projects()
    assert len(engine.report['public_projects']) == 2


def test_completed_project_leaves_the_list():
    """Финальная стройка уходит из public_projects и создаёт учреждение."""
    engine = build_engine()
    with_project(engine, [project('proj-1', max_workers=2, work_required=2)])
    labor(engine, 2.0)
    reference, native = assert_method_matches(engine)
    assert native.accounts['treasury-00001']['public_projects'] == []
    assert 'inst-proj-1' in native.entities


def test_gold_is_conserved():
    """Сумма золота сохраняется: выплата — перевод, а не создание денег."""
    engine = build_engine()
    with_project(engine, [project(max_workers=3)])
    labor(engine, 3.0)
    before = sum(a['balance'] for a in engine.accounts.values())
    engine.advance_public_projects()
    after = sum(a['balance'] for a in engine.accounts.values())
    assert after == before


# ---------------------------------------------------------------------------
# Эталон против ядра
# ---------------------------------------------------------------------------

def test_reference_equals_native_without_projects():
    engine = build_engine()
    account(engine)
    reference, native = assert_method_matches(engine)
    assert native.report['public_projects'] == []


def accelerated(engine):
    """Режим ускоренной стройки: в фикстуре есть только ``normal``.

    ``activate_rules`` дополняет конфиг режимом ``normal``, поэтому ускоренный
    режим тест добавляет сам — ровно те поля, которые читает срез.
    """
    engine.cfg.setdefault('construction_modes', {})['accelerated'] = dict(
        wage_multiplier=2, lodging_allowance=0.05, support_goods={})
    return engine.cfg['construction_modes']['accelerated']


def test_reference_equals_native_with_two_states():
    """Две стройки с разными режимами: обычная и ускоренная."""
    engine = build_engine()
    accelerated(engine)
    with_project(engine, [project('proj-1', mode='normal', max_workers=2),
                          project('proj-2', mode='accelerated', max_workers=2)])
    labor(engine, 100.0)
    engine._expedited_construction_workers = {'proj-2': 1.0}
    reference, native = assert_method_matches(engine)
    rows = {r['project_id']: r for r in native.report['public_projects']}
    assert rows['proj-2']['workers'] == 1.0


def test_accelerated_does_not_reduce_labor():
    """Ускоренная бригада забронирована заранее и труд не трогает."""
    engine = build_engine()
    accelerated(engine)
    with_project(engine, [project('proj-1', mode='accelerated', max_workers=2)])
    labor(engine, 100.0)
    engine._expedited_construction_workers = {'proj-1': 2.0}
    reference, native = assert_method_matches(engine)
    assert native._construction_labor['s', 'p'] == 100.0
    assert native.report['public_projects'][0]['workers'] == 2.0


def test_accelerated_wage_multiplier_applies():
    """Ускоренный режим платит по wage_multiplier: wage*2 за работника."""
    engine = build_engine()
    mode = accelerated(engine)
    with_project(engine, [project('proj-1', mode='accelerated', max_workers=2)],
        balance=10000.0)
    labor(engine, 100.0)
    engine._expedited_construction_workers = {'proj-1': 2.0}
    amounts = []
    original = engine.distribute

    def traced(sender, recipients, amount, reason):
        amounts.append((amount, reason))
        return original(sender, recipients, amount, reason)

    engine.distribute = traced
    try:
        reference, native = assert_method_matches(engine)
    finally:
        engine.distribute = original
    wage_bill = [a for a, r in amounts if r == 'Заработная плата строителей'][0]
    assert wage_bill == 2*mode['wage_multiplier']*engine.cfg['wage']


# ---------------------------------------------------------------------------
# fallback без .pyd
# ---------------------------------------------------------------------------

def test_boundary_exposes_native_when_built():
    built = wage_boundary.load_wage_kernel() is not None
    assert wage_boundary.NATIVE_KERNEL is built
    if built:
        assert wage_boundary.run_advance is wage_boundary.run_advance_native
        assert wage_boundary.advance_public_projects.__module__.endswith('wage_boundary')
    else:
        assert wage_boundary.run_advance is wage_boundary.run_advance_python


def test_native_entry_points_are_compiled():
    kernel = wage_boundary.load_wage_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение wage_kernel не собрано')
    assert wage_boundary.run_advance is wage_boundary.run_advance_native
    assert kernel.calculate_workers.__module__.endswith('wage_kernel')
    assert not hasattr(kernel, 'run_advance')
    assert wage_boundary.run_advance is not wage_boundary.run_advance_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('wage_kernel'):
            raise ImportError("cannot import name 'wage_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert wage_boundary.load_wage_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = build_engine()
    with_project(engine, [project(max_workers=3)])
    labor(engine, 3.0)
    expected = snapshot(build_engine())
    reference, _ = method_pair(engine)
    expected = snapshot(reference)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('wage_kernel'):
            raise ImportError("cannot import name 'wage_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(wage_boundary)
    try:
        assert wage_boundary.NATIVE_KERNEL is False
        assert wage_boundary.run_advance is wage_boundary.run_advance_python
        fallback = clone(engine)
        fallback.advance_public_projects()
        assert_same(expected, snapshot(fallback))
    finally:
        monkeypatch.undo()
        importlib.reload(wage_boundary)


# ---------------------------------------------------------------------------
# deepcopy
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = build_engine()
    with_project(engine, [project(max_workers=2)])
    labor(engine, 2.0)
    before = copy.deepcopy({aid: row['balance'] for aid, row in engine.accounts.items()})
    clone_engine = copy.deepcopy(engine)
    clone_engine.advance_public_projects()
    assert {aid: row['balance'] for aid, row in engine.accounts.items()} == before, \
        'выплата в копии изменила оригинал'
    left, right = copy.deepcopy(engine), copy.deepcopy(engine)
    left.advance_public_projects()
    right.advance_public_projects()
    assert_same(snapshot(left), snapshot(right))


# ---------------------------------------------------------------------------
# forced-case и RNG
# ---------------------------------------------------------------------------

def test_forced_case_pays_workers():
    """Явный сценарий: труд есть, касса есть, бригада получает зарплату."""
    engine = build_engine()
    with_project(engine, [project(max_workers=2)], balance=50000.0)
    labor(engine, 2.0)
    before = engine.accounts['treasury-00001']['balance']
    engine.advance_public_projects()
    flows = wage_flows(engine)
    assert flows, 'перевод обязан быть записан'
    assert engine.accounts['treasury-00001']['balance'] < before
    assert engine.report['public_projects'][0]['workers'] == 2.0


def test_rng_is_not_touched():
    """Числовой срез не тратит RNG: порядок случайности остаётся за Python."""
    engine = build_engine()
    with_project(engine, [project(max_workers=2)])
    labor(engine, 2.0)
    before = rng_digest(engine)
    engine.advance_public_projects()
    assert rng_digest(engine) == before


def test_rng_equal_between_branches():
    engine = build_engine()
    with_project(engine, [project(max_workers=2)])
    labor(engine, 2.0)
    reference, native = assert_method_matches(engine)
    assert rng_digest(reference) == rng_digest(native)


def test_v3_has_single_call_site():
    """В advance_public_projects один вызов границы, а не вторая копия цикла."""
    source = inspect.getsource(Economy.advance_public_projects)
    assert '_wage.advance_public_projects(self)' in source
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_wage.advance_public_projects(') == 1
    assert "'Заработная плата строителей'" not in module_source
    assert "'Временное жильё строителей'" not in module_source


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

# Эти digest'ы сняты на ветке до переноса среза: они доказывают, что шаг
# не изменился ни на бит. Значения совпадают с константами дивидендного
# focused-теста, потому что digest полного состояния общий.
REPLAY_17 = (
    ('841f2a5eddc798a94257375d7ba345e56568282750f8e25a34b2304861b41a8b',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('5a12242069537e8b3280e928461cbb58d21da3122d71fd34bd93f2becb82c06e',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('03c1ad58356aa19ec6fda56643da7018027f562db5d642d4aafdab6ceed6c9af',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('6895f3193e5d48b5cc1d88e6a4d9dfc32af67ab4c14805680176c223bb61cc5a',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('053059ce0cada486de276f16a496d645d2937f62a36e544d049a40e6d2581e1b',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('e9a575a7edba0c06214c09759dd611ea6dceccdc0d9902196b9b79b593a50198',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def replay_digest(engine):
    """Digest полного состояния месяца."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'dividends': engine.report.get('dividends'),
        'diagnostics': [r for r in engine.report.get('diagnostics', [])
                        if r.get('kind') == 'dividend'],
        'flows': engine.report.get('flows'),
        'events': engine.report.get('events'),
    })


def wage_digest(engine):
    """Digest именно зарплатного среза месяца."""
    return digest({
        'public_projects': engine.report.get('public_projects'),
        'construction_labor': engine.report.get('construction_labor'),
        'consumed': engine.report.get('consumed'),
        'equipment_wear': engine.report.get('equipment_wear'),
        'flows': [flow for flow in engine.report.get('flows', [])
                  if flow['reason'] in ('Заработная плата строителей',
                                        'Временное жильё строителей')],
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_wage_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='wage-replay-%d-%d' % (seed, index),
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


def test_wage_digest_matches_reference_on_replay():
    """Зарплатный срез реального replay совпадает с эталоном на всех месяцах."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    original = wage_boundary.run_advance
    digests = {'native': [], 'reference': []}
    try:
        for mode, kernel in (('native', original),
                             ('reference', wage_boundary.run_advance_python)):
            wage_boundary.run_advance = kernel
            with workdir('test_wage_digest_%s' % mode) as directory:
                db, service = sr.build_world(73129, directory, report_detail='standard')
                try:
                    for index in range(3):
                        service.advance_turn(command_id='wage-digest-%s-%d' % (mode, index),
                                             source='audit')
                        digests[mode].append(wage_digest(service.economy_state().engine))
                finally:
                    db.close()
    finally:
        wage_boundary.run_advance = original
    assert digests['native'] == digests['reference']


def test_replay_calls_slice_without_changing_state():
    """Срез вызывается каждый месяц; digest'ы месяца от этого не меняются.

    Публичных строек мир за три месяца не заводит, поэтому проверяется
    другое: вызов происходит, а состояние шага остаётся бит-в-бит прежним.
    Пустой срез — тоже исполняемый путь, и он обязан быть бесплатным.
    """
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    counts = {'calls': 0, 'accounts': 0, 'projects': 0}
    original = Economy.advance_public_projects

    def traced(self):
        result = original(self)
        counts['calls'] += 1
        counts['accounts'] += len(self.accounts)
        counts['projects'] += sum(len(a.get('public_projects', []))
                                  for a in self.accounts.values())
        return result

    Economy.advance_public_projects = traced
    try:
        with workdir('test_wage_branch') as directory:
            db, service = sr.build_world(73129, directory, report_detail='standard')
            try:
                for index in range(3):
                    service.advance_turn(command_id='wage-branch-%d' % index,
                                         source='audit')
            finally:
                db.close()
    finally:
        Economy.advance_public_projects = original
    assert counts['calls'] == 3, 'срез обязан вызываться каждый месяц'
    assert counts['accounts'] > 0, 'обход счетов не выполнялся'
    assert counts['projects'] == 0, 'в этом мире публичных строек нет'
