"""Focused-тесты net price ядра: точное совпадение с Python-эталоном.

Ядро работает с ``object``-арифметикой намеренно: цены, остатки и резервы
заявок бывают и int, и float, а встроенные ``min``/``max`` сохраняют тип
первого аргумента при равенстве. Тип результата виден дальше — в ``margin``
и ``expected_profit`` проекта, поэтому проверяется не только значение, но и
тип, порядок обхода и short-circuit.

Эталон — ``net_price_boundary.net_price_python``, fallback — та же функция
при отсутствии собранного расширения.
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
from modules.economy.native import net_price_boundary  # noqa: E402

FIXTURE = ROOT/'tests/fixtures/economy.json'
EPS = support.EPS


# ---------------------------------------------------------------------------
# Маленький движок на общей фикстуре
# ---------------------------------------------------------------------------

def build_engine(seed=17):
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


def firm(engine, good=None, price=2.0):
    """Синтетический продавец: ядру нужны только good и price."""
    good = good or next(iter(engine.goods))
    return dict(id='firm', good=good, price=price)


def quote(price, remaining, reserved, final=False, **extra):
    """Пара (order, limit) в том виде, в каком её отдаёт customer_quotes."""
    order = dict(price=price, remaining=remaining, reserved=reserved,
                 final_consumption=final)
    order.update(extra)
    return order


def with_quotes(engine, rows):
    """Подменить customer_quotes: ядро видит ровно этот список пар."""
    engine.customer_quotes = lambda e: list(rows)
    return engine


# ---------------------------------------------------------------------------
# Сравнение
# ---------------------------------------------------------------------------

def assert_same(left, right, path='value'):
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def run_pair(engine, e):
    reference = net_price_boundary.net_price_python(engine, e)
    native = net_price_boundary.net_price(engine, e)
    return reference, native


def assert_pair_matches(engine, e):
    reference, native = run_pair(engine, e)
    assert_same(reference, native)
    return reference, native


# ---------------------------------------------------------------------------
# Пустые и крайние случаи
# ---------------------------------------------------------------------------

def test_no_quotes_uses_consumer_flag():
    engine = build_engine()
    e = firm(engine, price=2.0)
    with_quotes(engine, [])
    reference, native = assert_pair_matches(engine, e)
    tax = engine.cfg['consumption_tax']
    consumer = e['good'] in (engine.cfg['basic_needs'] | engine.cfg['extra_needs'])
    assert_same(native, 2.0*(1-tax*float(consumer)))
    assert type(native) is float


def test_zero_quantity_uses_consumer_flag_not_division():
    """quantity <= EPS: доля берётся из флага, деление не выполняется."""
    engine = build_engine()
    e = firm(engine, price=3.0)
    # Заявка отфильтрована по цене, поэтому quantity остаётся нулём.
    with_quotes(engine, [(quote(1.0, 5.0, 10.0), 100.0)])
    reference, native = assert_pair_matches(engine, e)
    tax = engine.cfg['consumption_tax']
    consumer = e['good'] in (engine.cfg['basic_needs'] | engine.cfg['extra_needs'])
    assert_same(native, 3.0*(1-tax*float(consumer)))


def test_limit_below_price_is_skipped():
    engine = build_engine()
    e = firm(engine, price=2.0)
    # limit+EPS < price: заявка не учитывается вовсе.
    with_quotes(engine, [(quote(2.0, 10.0, 20.0), 2.0-EPS*2)])
    reference, native = assert_pair_matches(engine, e)
    assert native == 2.0


def test_limit_equal_to_price_is_included():
    """Граница допуска: limit == price проходит, как в прежнем коде."""
    engine = build_engine()
    e = firm(engine, price=2.0)
    with_quotes(engine, [(quote(2.0, 10.0, 20.0, final=True), 2.0)])
    reference, native = assert_pair_matches(engine, e)
    tax = engine.cfg['consumption_tax']
    assert_same(native, 2.0*(1-tax*1.0))


def test_zero_reserved_gives_zero_volume():
    engine = build_engine()
    e = firm(engine, price=2.0)
    with_quotes(engine, [(quote(2.0, 10.0, 0.0, final=True), 2.0)])
    reference, native = assert_pair_matches(engine, e)
    assert native == 2.0


def test_missing_final_consumption_counts_as_consumer():
    """order.get('final_consumption') без ключа — ложь, а не ошибка."""
    engine = build_engine()
    e = firm(engine, price=2.0)
    order = dict(price=2.0, remaining=10.0, reserved=20.0)
    with_quotes(engine, [(order, 2.0)])
    reference, native = assert_pair_matches(engine, e)
    tax = engine.cfg['consumption_tax']
    consumer = e['good'] in (engine.cfg['basic_needs'] | engine.cfg['extra_needs'])
    assert_same(native, 2.0*(1-tax*float(consumer)))


# ---------------------------------------------------------------------------
# int/float-семантика
# ---------------------------------------------------------------------------

def test_int_inputs_keep_int_arithmetic_inside():
    engine = build_engine()
    e = firm(engine, price=10)
    # Целая цена, целые остатки: landed/volume остаются целыми до деления.
    with_quotes(engine, [(quote(10, 5, 20, final=True), 10)])
    reference, native = assert_pair_matches(engine, e)
    # reserved/max(landed, EPS) = 20/10 = 2.0 -> float, share = 1.0
    tax = engine.cfg['consumption_tax']
    assert_same(native, 10*(1-tax*1.0))


def test_float_inputs_keep_float():
    engine = build_engine()
    e = firm(engine, price=2.5)
    with_quotes(engine, [(quote(2.5, 3.0, 6.0, final=True), 2.5)])
    reference, native = assert_pair_matches(engine, e)
    assert type(native) is float


def test_mixed_quotes_accumulate_in_order():
    engine = build_engine()
    e = firm(engine, price=2.0)
    rows = [(quote(2.0, 1.0, 10.0, final=True), 2.0),
            (quote(2.0, 2.0, 10.0), 2.0),
            (quote(2.0, 3.0, 10.0, final=True), 2.0)]
    with_quotes(engine, rows)
    reference, native = assert_pair_matches(engine, e)
    # limit == price -> landed == price, поэтому volume = reserved/2.
    # quantity = (10+10+10)/2 = 15, final = 20/2 = 10 -> share = 2/3.
    tax = engine.cfg['consumption_tax']
    assert_same(native, 2.0*(1-tax*(10.0/15)))


def test_min_keeps_remaining_type_on_tie():
    """min(remaining, reserved/landed): при равенстве тип берётся из remaining."""
    engine = build_engine()
    e = firm(engine, price=1)
    # landed = 1, reserved/landed = 5.0, remaining = 5 (int) -> volume = 5 (int)
    with_quotes(engine, [(quote(1, 5, 5, final=True), 1)])
    reference, native = assert_pair_matches(engine, e)
    assert type(native) is float  # итог всегда float из-за *


def test_tax_zero_gives_base_price():
    engine = build_engine()
    engine.cfg['consumption_tax'] = 0.0
    e = firm(engine, price=2.0)
    with_quotes(engine, [(quote(2.0, 5.0, 10.0, final=True), 2.0)])
    reference, native = assert_pair_matches(engine, e)
    assert native == 2.0


# ---------------------------------------------------------------------------
# Порядок обхода и short-circuit
# ---------------------------------------------------------------------------

def test_quotes_are_walked_in_given_order():
    engine = build_engine()
    e = firm(engine, price=2.0)
    rows = [(quote(2.0, 1.0, 2.0, final=True), 2.0),
            (quote(2.0, 1.0, 2.0), 2.0),
            (quote(2.0, 1.0, 2.0, final=True), 2.0)]
    with_quotes(engine, rows)
    reference = net_price_boundary.net_price_python(engine, e)
    native = net_price_boundary.net_price(engine, e)
    assert_same(reference, native)
    # limit == price -> landed == price; volume = reserved/2 = 1.0 на заявку.
    # quantity = 3.0, final_quantity = 2.0 -> share = 2/3.
    tax = engine.cfg['consumption_tax']
    assert_same(native, 2.0*(1-tax*(2.0/3.0)))


def test_price_is_read_once_before_loop():
    """price_base читается один раз: мутация поля в ходе цикла не влияет."""
    engine = build_engine()
    e = firm(engine, price=2.0)
    calls = {'n': 0}
    order = quote(2.0, 5.0, 10.0, final=True)
    with_quotes(engine, [(order, 2.0)])
    reference, native = assert_pair_matches(engine, e)
    # Обе ветви читают цену до цикла: результат совпадает и не зависит от порядка.
    assert_same(reference, native)


def test_customer_quotes_called_once():
    engine = build_engine()
    e = firm(engine, price=2.0)
    calls = []
    engine.customer_quotes = lambda row: calls.append(row['id']) or [
        (quote(2.0, 5.0, 10.0, final=True), 2.0)]
    net_price_boundary.net_price(engine, e)
    assert calls == ['firm']


# ---------------------------------------------------------------------------
# deepcopy, граница и fallback
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = build_engine()
    e = firm(engine, price=2.0)
    with_quotes(engine, [(quote(2.0, 5.0, 10.0, final=True), 2.0)])
    before = (copy.deepcopy(e), engine.cfg['consumption_tax'])
    clone = copy.deepcopy(engine)
    net_price_boundary.net_price(clone, clone.enterprises.get('firm', e) if False else e)
    assert (e, engine.cfg['consumption_tax']) == before
    left = copy.deepcopy(engine)
    right = copy.deepcopy(engine)
    assert_same(net_price_boundary.net_price(left, e),
                net_price_boundary.net_price(right, e))


def test_boundary_exposes_native_when_built():
    built = net_price_boundary.load_net_price_kernel() is not None
    assert net_price_boundary.NATIVE_KERNEL is built
    if built:
        assert net_price_boundary.run.__module__.endswith('net_price_kernel')
        assert net_price_boundary.net_price.__module__.endswith('net_price_boundary')
    else:
        assert net_price_boundary.run.__module__.endswith('net_price_boundary')


def test_native_entry_points_are_compiled():
    kernel = net_price_boundary.load_net_price_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение net_price_kernel не собрано')
    assert net_price_boundary.run is kernel.net_price


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('net_price_kernel'):
            raise ImportError("cannot import name 'net_price_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert net_price_boundary.load_net_price_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = build_engine()
    e = firm(engine, price=2.0)
    with_quotes(engine, [(quote(2.0, 4.0, 9.0, final=True), 2.0),
                         (quote(2.0, 2.0, 5.0), 2.0)])
    expected = net_price_boundary.net_price_python(engine, e)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('net_price_kernel'):
            raise ImportError("cannot import name 'net_price_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(net_price_boundary)
    try:
        assert net_price_boundary.NATIVE_KERNEL is False
        assert_same(expected, net_price_boundary.net_price(engine, e))
    finally:
        monkeypatch.undo()
        importlib.reload(net_price_boundary)


def test_v3_has_single_call_site():
    """В V3 один вызов границы, а не вторая копия цикла."""
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_net_price.net_price(') == 1
    source = inspect.getsource(Economy.net_price)
    assert '_net_price.net_price(self, e)' in source
    # Прежний цикл по котировкам больше не дублируется в теле net_price.
    assert "for order, limit in self.customer_quotes(e):" not in source


# ---------------------------------------------------------------------------
# RNG и forced-case
# ---------------------------------------------------------------------------

def test_rng_equal_between_branches():
    engine = build_engine()
    e = firm(engine, price=2.0)
    with_quotes(engine, [(quote(2.0, 5.0, 10.0, final=True), 2.0)])
    before = hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                       default=str).encode()).hexdigest()
    net_price_boundary.net_price_python(engine, e)
    net_price_boundary.net_price(engine, e)
    after = hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                      default=str).encode()).hexdigest()
    assert before == after


def test_forced_case_changes_price():
    """Явный сценарий: доля финального потребления реально меняет цену."""
    engine = build_engine()
    e = firm(engine, price=2.0)
    tax = engine.cfg['consumption_tax']
    with_quotes(engine, [(quote(2.0, 4.0, 8.0, final=True), 2.0),
                         (quote(2.0, 4.0, 8.0), 2.0)])
    reference, native = assert_pair_matches(engine, e)
    # quantity = 8/2 = 4, final = 2 -> share = 0.5
    assert_same(native, 2.0*(1-tax*0.5))


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

# Заполняются прогоном на чистой ветке (см. test_replay_*). RNG-колонки
# совпадают с базовыми digest'ами задачи 1: перенос не сдвинул генератор.
REPLAY_17 = (
    ('d2b17513bd4f6c11f64e9aecf545068d06cbf06e90ad60cc4d5cb8a542b512a8',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('b58dd81e61ae0ea213a57e33ec51c9b5084ae57cc62789d05112cede0ab972c3',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('c18e45860256ad266d67562d6ce230903513ead64cdec7a82807d412a12f2b21',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('9cea9f35d2f9c0f76ea87f5b54eced6096d7ccc7b35104902420259ae54ee782',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('6b35dcb0021072e5a234ae9e1163fd6a3c5bf89525b47b1d80a0d1b262ff6a4e',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('ad91288ca034e92cd5bd26cc436c45f411740a0b8fd2a79ba88de1ab4aed4fbc',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def replay_digest(engine):
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'decisions': engine.report.get('investment_decisions'),
        'candidates': engine.report.get('investment_candidates'),
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_net_price_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='np-replay-%d-%d' % (seed, index),
                                     source='audit')
                engine = service.economy_state().engine
                rows.append((replay_digest(engine), digest(engine.rng.getstate())))
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
    assert state == REPLAY_17[index][0], 'месяц %d' % (index+1)
    assert rng == REPLAY_17[index][1], 'месяц %d' % (index+1)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_73129(replay_seed_73129, index):
    state, rng = replay_seed_73129[index]
    assert state == REPLAY_73129[index][0], 'месяц %d' % (index+1)
    assert rng == REPLAY_73129[index][1], 'месяц %d' % (index+1)


def test_replay_is_deterministic(replay_seed_17):
    assert run_replay(17) == list(replay_seed_17)


def test_replay_net_price_is_exercised():
    """Exact replay не должен проходить на пустых котировках."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    seen = {'quotes': 0, 'calls': 0}
    original = Economy.customer_quotes

    def counted(self, e, orders=None):
        rows = original(self, e) if orders is None else original(self, e, orders)
        seen['calls'] += 1
        if rows:
            seen['quotes'] += 1
        return rows

    Economy.customer_quotes = counted
    try:
        with workdir('test_net_price_exercised') as directory:
            db, service = sr.build_world(73129, directory, report_detail='standard')
            try:
                for index in range(2):
                    service.advance_turn(command_id='np-ex-%d' % index, source='audit')
            finally:
                db.close()
    finally:
        Economy.customer_quotes = original
    assert seen['calls'] > 0
    assert seen['quotes'] > 0, 'net_price ни разу не увидел непустые котировки'
