"""Focused-тесты consumption-ядра: точное совпадение с Python-эталоном V3.

Ядро работает с ``object``-арифметикой намеренно: ``needed`` приходит из
need-словаря, а остаток — из ``free_stock``, поэтому оба бывают и int, и float.
Встроенный ``min`` при равенстве возвращает первый аргумент, и тип результата
виден в ``deficit`` и в накоплении ``report['consumed']``. Проверяется не
только значение, но и тип, порядок обхода и порядок списаний.

Эталон — ``consumption_boundary.consume_households_python``, fallback — та же
функция при отсутствии собранного расширения.
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

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import consumption_boundary  # noqa: E402

FIXTURE = ROOT/'tests/fixtures/economy.json'
EPS = support.EPS


# ---------------------------------------------------------------------------
# Маленький движок на общей фикстуре
# ---------------------------------------------------------------------------

def build_engine(seed=17):
    """Движок на фикстуре: один счёт-домохозяйство и готовые товары."""
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


def household(engine, account_id=None):
    """Первый счёт-домохозяйство (у фикстуры он один)."""
    if account_id is not None:
        return engine.accounts[account_id]
    return next(a for a in engine.accounts.values() if a['kind'] == 'household')


def need(needed, consumed=0, deficit=None):
    """Строка need в том виде, в каком её создаёт household_requests."""
    return dict(needed=needed, consumed=consumed,
                deficit=needed if deficit is None else deficit)


def put(engine, a, good, quantity, value=None, reserved=0):
    """Положить товар на склад счёта без побочных эффектов."""
    a['stock'][good] = quantity
    a['values'][good] = quantity if value is None else value
    a['stock_reserved'][good] = reserved


# ---------------------------------------------------------------------------
# Снимки состояния и побитовое сравнение
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS = ('stock', 'values', 'stock_reserved')
REPORT_KEYS = ('consumed', 'orders', 'events', 'trades')


def rng_digest(engine):
    return hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                     ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def snapshot(engine):
    """Полный снимок того, что consumption вправе менять."""
    return {
        'accounts': {aid: {key: copy.deepcopy(a.get(key)) for key in ACCOUNT_FIELDS}
                     for aid, a in engine.accounts.items()},
        'needs': {aid: copy.deepcopy(a.get('needs')) for aid, a in engine.accounts.items()},
        'report': {key: copy.deepcopy(engine.report.get(key)) for key in REPORT_KEYS},
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
        # Тип обязателен: min сохраняет тип первого аргумента при равенстве.
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def run_pair(engine, households):
    """Прогнать эталон и ядро на одинаковых копиях движка и того же списка."""
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    reference_households = [reference.accounts[a['id']] for a in households]
    native_households = [native.accounts[a['id']] for a in households]
    consumption_boundary.consume_households_python(reference, reference_households)
    consumption_boundary.consume_households(native, native_households)
    return reference, native


def assert_pair_matches(engine, households):
    reference, native = run_pair(engine, households)
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


# ---------------------------------------------------------------------------
# Одно домохозяйство, один товар
# ---------------------------------------------------------------------------

def test_single_household_single_good_full_stock():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(5)}
    put(engine, a, 'bread', 5, 10)
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['bread']
    assert row['consumed'] == 5
    assert row['deficit'] == 0
    assert native.accounts[a['id']]['stock']['bread'] == 0
    assert native.report['consumed'] == {'bread': 5}


def test_zero_stock_still_records_unmet():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(2.0)}
    put(engine, a, 'bread', 0)
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['bread']
    assert row['consumed'] == 0
    assert type(row['consumed']) is int
    assert row['deficit'] == 2.0
    assert native.report['consumed']['bread'] == 0


def test_missing_good_is_zero_not_error():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'cloth': need(1.5)}
    # Товара нет в словаре склада вовсе: free_stock возвращает get-дефолт 0,
    # а use_stock записывает слот нулём — ровно как прежний код V3.
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['cloth']
    assert row['consumed'] == 0
    assert row['deficit'] == 1.5
    assert native.accounts[a['id']]['stock']['cloth'] == 0
    assert native.report['consumed'] == {'cloth': 0}


def test_no_needs_is_noop():
    engine = build_engine()
    a = household(engine)
    a.pop('needs', None)
    reference, native = assert_pair_matches(engine, [a])
    assert native.report['consumed'] == {}
    assert not any(row.get('needs') for row in native.accounts.values())


# ---------------------------------------------------------------------------
# Частичный запас, несколько товаров, несколько домохозяйств
# ---------------------------------------------------------------------------

def test_partial_stock_limits_consumption():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(9)}
    put(engine, a, 'bread', 4, 8)
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['bread']
    assert row['consumed'] == 4
    assert row['deficit'] == 5
    assert native.accounts[a['id']]['stock']['bread'] == 0
    assert native.report['consumed']['bread'] == 4


def test_reserved_stock_reduces_available():
    """free_stock вычитает резерв: списывается только свободная часть."""
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'fish': need(6)}
    put(engine, a, 'fish', 10, 20, reserved=7)
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['fish']
    assert row['consumed'] == 3
    assert row['deficit'] == 3
    assert native.accounts[a['id']]['stock']['fish'] == 7


def test_multiple_goods_follow_insertion_order():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(2), 'fish': need(1), 'cloth': need(0.5)}
    put(engine, a, 'bread', 10, 20)
    put(engine, a, 'fish', 10, 20)
    put(engine, a, 'cloth', 10, 20)
    reference, native = assert_pair_matches(engine, [a])
    assert list(native.report['consumed']) == ['bread', 'fish', 'cloth']
    assert native.report['consumed'] == {'bread': 2, 'fish': 1, 'cloth': 0.5}


def test_multiple_households_accumulate_report():
    engine = build_engine()
    first = household(engine)
    second = copy.deepcopy(first)
    second['id'] = 'h2'
    engine.accounts['h2'] = second
    first['needs'] = {'bread': need(2.0)}
    second['needs'] = {'bread': need(3.0)}
    put(engine, first, 'bread', 2.0, 4.0)
    put(engine, second, 'bread', 1.0, 2.0)
    reference, native = assert_pair_matches(engine, [first, second])
    assert native.accounts['h']['needs']['bread']['consumed'] == 2.0
    assert native.accounts['h2']['needs']['bread']['consumed'] == 1.0
    # Отчёт накапливает оба списания в порядке обхода.
    assert native.report['consumed'] == {'bread': 3.0}


def test_household_order_follows_given_list():
    """Ядро обходит ровно переданный порядок, а не порядок словаря."""
    engine = build_engine()
    first = household(engine)
    second = copy.deepcopy(first)
    second['id'] = 'h2'
    engine.accounts['h2'] = second
    for a, quantity in ((first, 5.0), (second, 6.0)):
        a['needs'] = {'bread': need(2.0)}
        put(engine, a, 'bread', quantity, quantity)
    calls = []
    original = engine.use_stock

    def traced(account, good, quantity):
        calls.append(account)
        return original(account, good, quantity)

    engine.use_stock = traced
    consumption_boundary.consume_households(engine, [second, first])
    assert calls == ['h2', 'h']
    # Повторный проход в прямом порядке даёт обратную последовательность.
    calls.clear()
    engine.report['consumed'] = {}
    for a in (first, second):
        a['needs'] = {'bread': need(2.0)}
        put(engine, a, 'bread', 5.0, 5.0)
    consumption_boundary.consume_households(engine, [first, second])
    assert calls == ['h', 'h2']


# ---------------------------------------------------------------------------
# int/float-семантика
# ---------------------------------------------------------------------------

def test_int_need_and_int_stock_stay_int():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(3)}
    put(engine, a, 'bread', 10, 20)
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['bread']
    assert row['consumed'] == 3
    assert type(row['consumed']) is int
    assert type(row['deficit']) is int
    assert type(native.report['consumed']['bread']) is int


def test_float_need_with_int_stock_keeps_need_type():
    """min возвращает первый аргумент: при равенстве int-остаток даёт int."""
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(3)}
    put(engine, a, 'bread', 3, 6)
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['bread']
    assert row['consumed'] == 3
    assert type(row['consumed']) is int
    assert type(native.accounts[a['id']]['stock']['bread']) is int


def test_float_stock_stays_float():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(10.0)}
    put(engine, a, 'bread', 4.5, 9.0)
    reference, native = assert_pair_matches(engine, [a])
    row = native.accounts[a['id']]['needs']['bread']
    assert row['consumed'] == 4.5
    assert type(row['consumed']) is float
    assert row['deficit'] == 10.0-4.5
    assert type(native.report['consumed']['bread']) is float


def test_int_and_float_goods_in_one_household():
    """Соседние товары с разным типом остатка не смешивают арифметику."""
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(2), 'fish': need(1.5)}
    put(engine, a, 'bread', 7, 14)          # int
    put(engine, a, 'fish', 0.75, 1.5)       # float
    reference, native = assert_pair_matches(engine, [a])
    assert native.accounts[a['id']]['needs']['bread'] == need(2, consumed=2, deficit=0)
    assert native.accounts[a['id']]['needs']['fish'] == need(1.5, consumed=0.75,
                                                             deficit=1.5-0.75)
    assert type(native.report['consumed']['bread']) is int
    assert type(native.report['consumed']['fish']) is float


# ---------------------------------------------------------------------------
# Побочные эффекты и порядок списаний
# ---------------------------------------------------------------------------

def trace(engine):
    """Записать последовательность use_stock/free_stock из consumption.

    Глубина вызова отличает вызовы самого цикла от вложенных проверок внутри
    ``use_stock``: граница обязана вызывать ядро в том же порядке, но не
    обязана повторять внутренние проверки хука.
    """
    calls = []
    depth = [0]

    def wrap(name):
        original = getattr(engine, name)

        def wrapper(*args, **kwargs):
            calls.append((name, args[0], args[1], depth[0]))
            depth[0] += 1
            try:
                return original(*args, **kwargs)
            finally:
                depth[0] -= 1

        setattr(engine, name, wrapper)

    wrap('free_stock')
    wrap('use_stock')
    return calls


def test_operation_order_is_preserved():
    engine = build_engine()
    a = household(engine)
    second = copy.deepcopy(a)
    second['id'] = 'h2'
    engine.accounts['h2'] = second
    for row in (a, second):
        row['needs'] = {'bread': need(2), 'fish': need(1)}
        put(engine, row, 'bread', 5, 10)
        put(engine, row, 'fish', 5, 10)
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    ref_calls, nat_calls = trace(reference), trace(native)
    consumption_boundary.consume_households_python(reference,
                                                   [reference.accounts['h'], reference.accounts['h2']])
    consumption_boundary.consume_households(native, [native.accounts['h'], native.accounts['h2']])
    assert nat_calls == ref_calls
    top = [row for row in nat_calls if row[3] == 0]
    # Для каждого товара free_stock идёт до use_stock; порядок товаров и
    # домохозяйств — прежний, вложенные проверки хука помечены глубиной.
    assert [(name, account, good) for name, account, good, _ in top] == [
        ('free_stock', 'h', 'bread'), ('use_stock', 'h', 'bread'),
        ('free_stock', 'h', 'fish'), ('use_stock', 'h', 'fish'),
        ('free_stock', 'h2', 'bread'), ('use_stock', 'h2', 'bread'),
        ('free_stock', 'h2', 'fish'), ('use_stock', 'h2', 'fish')]


def test_side_effects_called_once():
    """Каждый хук пишет ровно один раз: отчёт равен сумме consumed по needs."""
    engine = build_engine()
    a = household(engine)
    second = copy.deepcopy(a)
    second['id'] = 'h2'
    engine.accounts['h2'] = second
    for row, quantities in ((a, (2.0, 1.0)), (second, (1.5, 0.5))):
        row['needs'] = {'bread': need(2.0), 'fish': need(1.0)}
        put(engine, row, 'bread', quantities[0], quantities[0])
        put(engine, row, 'fish', quantities[1], quantities[1])
    calls = []
    original_use = engine.use_stock

    def traced(account, good, quantity):
        calls.append((account, good, quantity))
        return original_use(account, good, quantity)

    engine.use_stock = traced
    consumption_boundary.consume_households(engine, [a, second])
    assert len(calls) == 4, calls
    expected = sum(row['needs'][good]['consumed']
                   for row in (a, second) for good in row['needs'])
    assert sum(engine.report['consumed'].values()) == expected
    # deficit пересчитан ровно один раз от needed, а не накоплен повторно.
    for row in (a, second):
        for good, need_row in row['needs'].items():
            assert need_row['deficit'] == need_row['needed']-need_row['consumed']


# ---------------------------------------------------------------------------
# deepcopy и граница
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(5)}
    put(engine, a, 'bread', 5, 10)

    def stocks(target):
        return copy.deepcopy({aid: dict(row['stock']) for aid, row in target.accounts.items()})

    before = stocks(engine)
    clone = copy.deepcopy(engine)
    consumption_boundary.consume_households(clone, [household(clone)])
    assert stocks(engine) == before, 'consumption в копии изменил оригинал'
    left, right = copy.deepcopy(engine), copy.deepcopy(engine)
    consumption_boundary.consume_households(left, [household(left)])
    consumption_boundary.consume_households(right, [household(right)])
    assert_same(snapshot(left), snapshot(right))


def test_boundary_exposes_native_when_built():
    built = consumption_boundary.load_consumption_kernel() is not None
    assert consumption_boundary.NATIVE_KERNEL is built
    if built:
        assert consumption_boundary.run_households.__module__.endswith('consumption_kernel')
        assert consumption_boundary.consume_households.__module__.endswith('consumption_boundary')
    else:
        assert consumption_boundary.run_households is consumption_boundary.run_households_python


def test_native_entry_points_are_compiled():
    kernel = consumption_boundary.load_consumption_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение consumption_kernel не собрано')
    assert consumption_boundary.run_households is kernel.run_households
    assert consumption_boundary.run_households is not consumption_boundary.run_households_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('consumption_kernel'):
            raise ImportError("cannot import name 'consumption_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert consumption_boundary.load_consumption_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(4.0), 'fish': need(1.0)}
    put(engine, a, 'bread', 2.5, 5.0)
    put(engine, a, 'fish', 1.0, 2.0)
    reference, _ = run_pair(engine, [a])
    expected = snapshot(reference)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('consumption_kernel'):
            raise ImportError("cannot import name 'consumption_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(consumption_boundary)
    try:
        assert consumption_boundary.NATIVE_KERNEL is False
        assert consumption_boundary.run_households is consumption_boundary.run_households_python
        assert consumption_boundary.consume_households.__module__.endswith('consumption_boundary')
        fallback = copy.deepcopy(engine)
        consumption_boundary.consume_households(fallback, [household(fallback)])
        assert_same(expected, snapshot(fallback))
    finally:
        monkeypatch.undo()
        importlib.reload(consumption_boundary)


def test_v3_has_single_call_site():
    """Один вызов границы на всю оркестрацию, а не вторая копия цикла.

    Call site переехал вместе с оркестрацией месяца: ``Economy.step`` стал
    тонким фасадом, а последовательность фаз живёт в ``monthly_driver``
    (он же держит Python-эталон в ``monthly_boundary``). Смысл проверки тот
    же: у правила ровно одна точка вызова и ни одной второй копии цикла.
    """
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_consumption.consume_households(') == 0, \
        'оркестрация ушла из v3.py, второй call site там не нужен'
    native_source = (ROOT/'modules/economy/native/monthly_driver.pyx').read_text(encoding='utf-8')
    reference_source = (ROOT/'modules/economy/native/monthly_boundary.py').read_text(encoding='utf-8')
    assert native_source.count('_consumption.consume_households(') == 1
    assert reference_source.count('_consumption.consume_households(') == 1
    assert module_source.count('def consume_households(') == 0, \
        'тело цикла не должно вернуться в v3.py'
    # Прежний цикл по needs в оркестрации больше не дублируется.
    assert "consumed = min(need['needed'], self.free_stock(a['id'], good))" not in native_source
    assert "consumed = min(need['needed'], engine.free_stock(a['id'], good))" not in native_source


# ---------------------------------------------------------------------------
# RNG и forced-case
# ---------------------------------------------------------------------------

def test_rng_equal_between_branches():
    """RNG расходуется одинаково: consumption не трогает генератор."""
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(3.0)}
    put(engine, a, 'bread', 2.0, 4.0)
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    before = rng_digest(engine)
    consumption_boundary.consume_households_python(reference, [household(reference)])
    consumption_boundary.consume_households(native, [household(native)])
    assert rng_digest(reference) == rng_digest(native)
    # Consumption не перемешивает и не тратит RNG вовсе.
    assert rng_digest(native) == before


def test_forced_case_consumes_stock():
    """Явный сценарий: consumption реально списывает товар и меняет склад."""
    engine = build_engine()
    a = household(engine)
    a['needs'] = {'bread': need(6.0), 'fish': need(2.0)}
    put(engine, a, 'bread', 10.0, 20.0)
    put(engine, a, 'fish', 1.0, 2.0)
    before = dict(engine.accounts[a['id']]['stock'])
    reference, native = assert_pair_matches(engine, [a])
    assert native.accounts[a['id']]['stock']['bread'] == before['bread']-6.0
    assert native.accounts[a['id']]['stock']['fish'] == before['fish']-1.0
    assert native.report['consumed'] == {'bread': 6.0, 'fish': 1.0}
    assert native.accounts[a['id']]['needs']['fish']['deficit'] == 1.0


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

REPLAY_17 = (
    ('4dc936b8e9c7dc9155e9255a1b65e95009370e540a9898034accef3b16b4081a',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('4d61d0153d46bfaa3086a08ee59c3670abeb0ceb142374eeae5e72978a208e5b',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('4c296023641cfdd43c72eb9b5094d6fad020b36fd97886729b771668d8049f59',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('2aa1ddc5ec592593e9ded98594f3791fd0eed568a1a9a7f3bee7741f29ff3e0e',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('80cd07e2450a2ee169a4303119b92a7bd93c28cbe5121d109c9934629989348d',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('34ade1cdfd9a35bde8ea8462981b47c5f6c4b90d501398dd259eeea8a3e53c94',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def replay_digest(engine):
    """Digest состояния и результата consumption за месяц."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'consumed': engine.report.get('consumed'),
        'needs': {a['id']: a.get('needs') for a in engine.accounts.values()},
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_consumption_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='cons-replay-%d-%d' % (seed, index),
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


def test_replay_is_deterministic_and_consumed(replay_seed_17, replay_seed_73129):
    for rows in (replay_seed_17, replay_seed_73129):
        assert len(rows) == 3
        assert all(rng for _, rng in rows)
    # Повторный прогон даёт те же digest'ы: ядро не тянет состояние из модуля.
    assert run_replay(17) == list(replay_seed_17)


def test_replay_actually_consumed():
    """Exact replay не должен проходить на пустом потреблении."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    with workdir('test_consumption_totals') as directory:
        db, service = sr.build_world(73129, directory, report_detail='standard')
        try:
            for index in range(3):
                service.advance_turn(command_id='cons-totals-%d' % index, source='audit')
            engine = service.economy_state().engine
        finally:
            db.close()
    consumed = engine.report['consumed']
    assert consumed, 'потребление пустое'
    assert sum(consumed.values()) > 100
    needs = [row for a in engine.accounts.values() if a['kind'] == 'household'
             for row in a.get('needs', {}).values()]
    assert needs
    # Каждая строка need пересчитана consumption, а не осталась initial-значением.
    for row in needs:
        assert row['deficit'] == row['needed']-row['consumed']
