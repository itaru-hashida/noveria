"""Focused-тесты owner consumption ядра: точное совпадение с Python-эталоном.

Ядро работает с ``object``-арифметикой намеренно: ``quantity`` выкупа,
остатки заявок и суммы сделок бывают и int, и float, а встроенный ``sum`` в
Python 3.12 компенсирует погрешность только при float-старте. Встроенный
``max`` при равенстве возвращает ``0`` и сохраняет его тип. Проверяется не
только значение, но и тип, порядок обхода и порядок записей.

Заявки создаются настоящим ``engine.order``: ``OrderStore`` при deepcopy
требует полный набор полей, поэтому самодельные словари здесь не годятся.
Эталон — ``owner_consumption_boundary.calculate_consumption_python``, fallback — та же
функция при отсутствии собранного расширения.
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
from modules.economy.native import owner_consumption_boundary  # noqa: E402

FIXTURE = ROOT/'tests/fixtures/economy.json'
EPS = support.EPS
PURPOSE = 'Потребление владельца'


# ---------------------------------------------------------------------------
# Маленький движок на общей фикстуре
# ---------------------------------------------------------------------------

def build_engine(seed=17):
    """Движок на фикстуре: один счёт владельца и пустой отчёт месяца."""
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


def owner_account(engine, account_id=None):
    """Счёт владельца: первый private/merchant (он же участник потребления)."""
    if account_id is not None:
        return engine.accounts[account_id]
    return next(a for a in engine.accounts.values() if a['kind'] in ('private', 'merchant'))


def make_order(engine, account, good, quantity, remaining=None):
    """Настоящая заявка через engine.order; remaining имитирует исполнение."""
    row = engine.order(account['id'], good, quantity, 'buy', 1.0, PURPOSE)
    assert row is not None
    if remaining is not None:
        row['remaining'] = remaining
    return row


def owner_row(engine, account, orders=(), consumed=None, budget=100.0):
    """Строка report['owner_consumption'] в форме, которую пишет owner_requests."""
    row = dict(owner=account['id'], free_cash=200.0, project_reserve=0.0,
        budget=budget, spent=0.0, tax=0.0, freight=0.0,
        consumed=dict(consumed or {}), orders=list(orders))
    engine.report['owner_consumption'].append(row)
    return row


def put(engine, a, good, quantity, value=None):
    """Положить товар на склад счёта без побочных эффектов."""
    a['stock'][good] = quantity
    a['values'][good] = quantity if value is None else value


# ---------------------------------------------------------------------------
# Снимки состояния и побитовое сравнение
# ---------------------------------------------------------------------------

REPORT_KEYS = ('owner_consumption', 'owner_consumed', 'consumed', 'orders', 'trades')
ACCOUNT_FIELDS = ('stock', 'values', 'stock_reserved', 'owner_consumption_spent',
                  'owner_consumed', 'owner_consumption_tax', 'owner_consumption_freight')


def rng_digest(engine):
    return hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                     ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def snapshot(engine):
    """Полный снимок того, что owner consumption вправе менять."""
    return {
        'accounts': {aid: {key: copy.deepcopy(a.get(key)) for key in ACCOUNT_FIELDS}
                     for aid, a in engine.accounts.items()},
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
        # Тип обязателен: max(0, ...) сохраняет тип нуля, sum — тип старта.
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def run_pair(engine):
    """Прогнать эталон и ядро на одинаковых копиях движка."""
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    owner_consumption_boundary.consume_owner_purchases_python(reference, EPS)
    owner_consumption_boundary.consume_owner_purchases(native, EPS)
    return reference, native


def assert_pair_matches(engine):
    reference, native = run_pair(engine)
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


# ---------------------------------------------------------------------------
# Пустые и крайние случаи
# ---------------------------------------------------------------------------

def test_no_rows_is_noop():
    engine = build_engine()
    before = snapshot(engine)
    reference, native = assert_pair_matches(engine)
    assert native.report['owner_consumption'] == []
    assert native.report['owner_consumed'] == {}
    assert_same(before, snapshot(native))


def test_row_without_orders_still_settles():
    engine = build_engine()
    a = owner_account(engine)
    owner_row(engine, a, orders=(), budget=50.0)
    reference, native = assert_pair_matches(engine)
    settled = native.report['owner_consumption'][0]
    assert settled['spent'] == 0
    assert settled['tax'] == 0
    assert settled['freight'] == 0
    assert settled['unfilled'] == {}
    assert settled['unused_budget'] == 50.0
    assert native.accounts[a['id']]['owner_consumption_spent'] == 0


def test_unfilled_order_consumes_nothing():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10, 20)
    row = make_order(engine, a, 'grain', 5.0)
    owner_row(engine, a, orders=[row['id']])
    reference, native = assert_pair_matches(engine)
    assert native.accounts[a['id']]['stock']['grain'] == 10, 'невыкупленная заявка списала склад'
    assert native.report['owner_consumption'][0]['unfilled'] == {'grain': 5.0}


def test_fully_filled_order_consumes_all():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10, 20)
    row = make_order(engine, a, 'grain', 4.0, remaining=0.0)
    owner_row(engine, a, orders=[row['id']])
    reference, native = assert_pair_matches(engine)
    assert native.accounts[a['id']]['stock']['grain'] == 6
    assert native.report['owner_consumption'][0]['consumed'] == {'grain': 4.0}
    assert native.accounts[a['id']]['owner_consumed'] == {'grain': 4.0}
    assert native.report['owner_consumed'] == {'grain': 4.0}
    assert native.report['consumed'] == {'grain': 4.0}


def test_partial_fill_consumes_filled_part_only():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10, 20)
    row = make_order(engine, a, 'grain', 6.0, remaining=2.5)
    owner_row(engine, a, orders=[row['id']])
    reference, native = assert_pair_matches(engine)
    assert native.accounts[a['id']]['stock']['grain'] == 6.5
    assert native.report['consumed'] == {'grain': 3.5}


def test_already_consumed_quantity_is_not_repeated():
    """Отметка заявки: повторный вызов не списывает то же второй раз."""
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10, 20)
    row = make_order(engine, a, 'grain', 4.0, remaining=0.0)
    owner_row(engine, a, orders=[row['id']])
    owner_consumption_boundary.consume_owner_purchases(engine, EPS)
    assert engine.accounts[a['id']]['stock']['grain'] == 6
    # Второй проход: filled уже равен отметке, списания быть не должно.
    engine.report['owner_consumption'][0]['consumed'] = {}
    engine.report['owner_consumed'] = {}
    engine.report['consumed'] = {}
    owner_consumption_boundary.consume_owner_purchases(engine, EPS)
    assert engine.accounts[a['id']]['stock']['grain'] == 6
    assert engine.report['consumed'] == {}


def test_preseeded_consumed_quantity_is_respected():
    """Отметка, выставленная до вызова, читается так же, как прежним get."""
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    row = make_order(engine, a, 'grain', 6.0, remaining=1.0)
    row['consumed_quantity'] = 2.0
    owner_row(engine, a, orders=[row['id']])
    reference, native = assert_pair_matches(engine)
    # filled=5.0, отметка=2.0 -> списываем 3.0, а не 5.0.
    assert native.report['consumed'] == {'grain': 3.0}
    assert native.accounts[a['id']]['stock']['grain'] == 7.0


def test_null_quantity_inside_eps_is_not_consumed():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    row = make_order(engine, a, 'grain', 1.0, remaining=1.0-EPS/2)
    owner_row(engine, a, orders=[row['id']])
    reference, native = assert_pair_matches(engine)
    assert native.report['consumed'] == {}
    assert native.accounts[a['id']]['stock']['grain'] == 10.0


# ---------------------------------------------------------------------------
# int/float-семантика
# ---------------------------------------------------------------------------

def test_int_quantities_stay_int():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10, 20)
    row = make_order(engine, a, 'grain', 5)
    row['remaining'] = 1
    owner_row(engine, a, orders=[row['id']], budget=100)
    reference, native = assert_pair_matches(engine)
    assert type(native.report['consumed']['grain']) is int
    assert type(native.accounts[a['id']]['owner_consumed']['grain']) is int
    assert native.report['consumed']['grain'] == 4


def test_float_quantities_stay_float():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    row = make_order(engine, a, 'grain', 5.5, remaining=1.5)
    owner_row(engine, a, orders=[row['id']], budget=100.0)
    reference, native = assert_pair_matches(engine)
    assert type(native.report['consumed']['grain']) is float
    assert native.report['consumed']['grain'] == 4.0


def test_mixed_int_and_float_goods_do_not_share_type():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10, 20)
    put(engine, a, 'fish', 10.0, 20.0)
    first = make_order(engine, a, 'grain', 4, remaining=0)
    second = make_order(engine, a, 'fish', 2.5, remaining=0.5)
    owner_row(engine, a, orders=[first['id'], second['id']])
    reference, native = assert_pair_matches(engine)
    assert type(native.report['consumed']['grain']) is int
    assert type(native.report['consumed']['fish']) is float


def test_spent_accumulates_like_builtin_sum():
    """spent = sum(gold+freight): порядок и тип — как у прежнего кода."""
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    row = make_order(engine, a, 'grain', 3.0, remaining=0.0)
    owner_row(engine, a, orders=[row['id']], budget=100.0)
    engine.report['trades'].append(dict(buy_order=row['id'], gold=1.5, freight=0.25, tax=0.1))
    engine.report['trades'].append(dict(buy_order=row['id'], gold=2.5, freight=0.5, tax=0.2))
    reference, native = assert_pair_matches(engine)
    settled = native.report['owner_consumption'][0]
    assert settled['spent'] == sum([1.5+0.25, 2.5+0.5])
    assert settled['tax'] == sum([0.1, 0.2])
    assert settled['freight'] == sum([0.25, 0.5])
    assert settled['unused_budget'] == 100.0-settled['spent']
    assert native.accounts[a['id']]['owner_consumption_spent'] == settled['spent']


def test_foreign_trades_are_ignored():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    mine = make_order(engine, a, 'grain', 3.0, remaining=0.0)
    other = make_order(engine, a, 'grain', 3.0, remaining=0.0)
    owner_row(engine, a, orders=[mine['id']])
    engine.report['trades'].append(dict(buy_order=mine['id'], gold=1.0, freight=0.0, tax=0.0))
    engine.report['trades'].append(dict(buy_order=other['id'], gold=9.0, freight=0.0, tax=0.0))
    reference, native = assert_pair_matches(engine)
    assert native.report['owner_consumption'][0]['spent'] == 1.0


# ---------------------------------------------------------------------------
# Порядок записей и обхода
# ---------------------------------------------------------------------------

def test_rows_follow_report_order():
    engine = build_engine()
    first = owner_account(engine)
    second = copy.deepcopy(first)
    second['id'] = 'owner2'
    engine.accounts['owner2'] = second
    for a, quantity in ((first, 5), (second, 6)):
        put(engine, a, 'grain', quantity, quantity)
    one = make_order(engine, first, 'grain', 2)
    two = make_order(engine, second, 'grain', 3)
    one['remaining'] = 0
    two['remaining'] = 0
    owner_row(engine, first, orders=[one['id']])
    owner_row(engine, second, orders=[two['id']])
    reference, native = assert_pair_matches(engine)
    assert list(native.report['owner_consumed'].items()) == [('grain', 5)]
    assert native.report['owner_consumption'][0]['consumed'] == {'grain': 2}
    assert native.report['owner_consumption'][1]['consumed'] == {'grain': 3}


def test_orders_are_walked_in_book_order():
    """Порядок списаний задаёт книга заявок, а не row['orders']."""
    engine = build_engine()
    a = owner_account(engine)
    for good in ('grain', 'fish'):
        put(engine, a, good, 10, 20)
    first = make_order(engine, a, 'grain', 2)
    second = make_order(engine, a, 'fish', 3)
    first['remaining'] = 0
    second['remaining'] = 0
    # Строка перечисляет заявки в обратном порядке: списание обязано идти по книге.
    owner_row(engine, a, orders=[second['id'], first['id']])
    calls = []
    original = engine.use_stock

    def traced(account, good, quantity):
        calls.append((account, good, quantity))
        return original(account, good, quantity)

    engine.use_stock = traced
    owner_consumption_boundary.consume_owner_purchases(engine, EPS)
    assert calls == [(a['id'], 'grain', 2), (a['id'], 'fish', 3)]
    assert list(engine.report['consumed']) == ['grain', 'fish']


def test_goods_walk_in_report_order_within_row():
    engine = build_engine()
    a = owner_account(engine)
    goods = ('grain', 'fish', 'cloth')
    for good in goods:
        put(engine, a, good, 10.0, 20.0)
    ids = []
    for good in goods:
        row = make_order(engine, a, good, 1.0, remaining=0.0)
        ids.append(row['id'])
    owner_row(engine, a, orders=ids)
    reference, native = assert_pair_matches(engine)
    assert list(native.report['owner_consumption'][0]['consumed']) == list(goods)
    assert list(native.report['consumed']) == list(goods)


def test_rows_do_not_share_consumed_dicts():
    """Каждая строка и каждый счёт получает свою копию consumed."""
    engine = build_engine()
    first = owner_account(engine)
    second = copy.deepcopy(first)
    second['id'] = 'owner2'
    engine.accounts['owner2'] = second
    for a, quantity in ((first, 5.0), (second, 5.0)):
        put(engine, a, 'grain', quantity, quantity)
    one = make_order(engine, first, 'grain', 2.0, remaining=0.0)
    two = make_order(engine, second, 'grain', 3.0, remaining=0.0)
    owner_row(engine, first, orders=[one['id']])
    owner_row(engine, second, orders=[two['id']])
    reference, native = assert_pair_matches(engine)
    rows = native.report['owner_consumption']
    assert rows[0]['consumed'] is not rows[1]['consumed']
    # Зеркало на счёте — deepcopy, а не ссылка на словарь строки.
    assert native.accounts[first['id']]['owner_consumed'] is not rows[0]['consumed']


def test_order_marks_do_not_leak_between_rows():
    """Отметка чужой заявки не влияет на следующую строку."""
    engine = build_engine()
    first = owner_account(engine)
    second = copy.deepcopy(first)
    second['id'] = 'owner2'
    engine.accounts['owner2'] = second
    put(engine, first, 'grain', 10.0, 20.0)
    put(engine, second, 'grain', 10.0, 20.0)
    one = make_order(engine, first, 'grain', 2.0, remaining=0.0)
    two = make_order(engine, second, 'grain', 3.0, remaining=0.0)
    owner_row(engine, first, orders=[one['id']])
    owner_row(engine, second, orders=[two['id']])
    reference, native = assert_pair_matches(engine)
    assert native.accounts[first['id']]['stock']['grain'] == 8.0
    assert native.accounts[second['id']]['stock']['grain'] == 7.0


# ---------------------------------------------------------------------------
# deepcopy, граница и fallback
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    row = make_order(engine, a, 'grain', 2.0, remaining=0.0)
    owner_row(engine, a, orders=[row['id']])
    before = snapshot(engine)
    clone = copy.deepcopy(engine)
    owner_consumption_boundary.consume_owner_purchases(clone, EPS)
    assert_same(before, snapshot(engine))
    # Копия живёт своей жизнью: ledger создаётся на вызов, а не на движке.
    left, right = copy.deepcopy(engine), copy.deepcopy(engine)
    owner_consumption_boundary.consume_owner_purchases(left, EPS)
    owner_consumption_boundary.consume_owner_purchases(right, EPS)
    assert_same(snapshot(left), snapshot(right))


def test_boundary_exposes_native_when_built():
    built = hasattr(owner_consumption_boundary.load_owner_consumption_kernel(),
                    'calculate_consumption')
    assert owner_consumption_boundary.NATIVE_KERNEL is built
    if built:
        assert owner_consumption_boundary.calculate_consumption.__module__.endswith(
            'owner_consumption_kernel')
        assert owner_consumption_boundary.consume_owner_purchases.__module__.endswith(
            'owner_consumption_boundary')
    else:
        assert owner_consumption_boundary.calculate_consumption is (
            owner_consumption_boundary.calculate_consumption_python)


def test_native_entry_points_are_compiled():
    kernel = owner_consumption_boundary.load_owner_consumption_kernel()
    if not hasattr(kernel, 'calculate_consumption'):
        pytest.skip('Cython-расширение owner_consumption_kernel не собрано')
    assert owner_consumption_boundary.calculate_consumption is kernel.calculate_consumption
    assert not hasattr(kernel, 'run')


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('owner_consumption_kernel'):
            raise ImportError("cannot import name 'owner_consumption_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert owner_consumption_boundary.load_owner_consumption_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    put(engine, a, 'fish', 2.0, 4.0)
    one = make_order(engine, a, 'grain', 3.0, remaining=0.5)
    two = make_order(engine, a, 'fish', 1.5, remaining=0.5)
    owner_row(engine, a, orders=[one['id'], two['id']], budget=40.0)
    engine.report['trades'].append(dict(buy_order=one['id'], gold=1.25, freight=0.1, tax=0.05))
    reference, _ = run_pair(engine)
    expected = snapshot(reference)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('owner_consumption_kernel'):
            raise ImportError("cannot import name 'owner_consumption_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(owner_consumption_boundary)
    try:
        assert owner_consumption_boundary.NATIVE_KERNEL is False
        assert owner_consumption_boundary.calculate_consumption is (
            owner_consumption_boundary.calculate_consumption_python)
        fallback = copy.deepcopy(engine)
        owner_consumption_boundary.consume_owner_purchases(fallback, EPS)
        assert_same(expected, snapshot(fallback))
    finally:
        monkeypatch.undo()
        importlib.reload(owner_consumption_boundary)


def test_v3_has_single_call_site():
    """В V3 один вызов границы, а не вторая копия цикла."""
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_owner_consumption.consume_owner_purchases(') == 1
    assert 'def consume_owner_purchases(' in module_source
    # Прежний цикл по строкам больше не дублируется в v3.py.
    assert "for row in self.report['owner_consumption']:" not in module_source
    source = inspect.getsource(Economy.consume_owner_purchases)
    assert '_owner_consumption.consume_owner_purchases(self, EPS)' in source


# ---------------------------------------------------------------------------
# RNG и forced-case
# ---------------------------------------------------------------------------

def test_rng_equal_between_branches():
    """RNG расходуется одинаково: owner consumption не трогает генератор."""
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 5.0, 10.0)
    row = make_order(engine, a, 'grain', 2.0, remaining=0.0)
    owner_row(engine, a, orders=[row['id']])
    before = rng_digest(engine)
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    owner_consumption_boundary.consume_owner_purchases_python(reference, EPS)
    owner_consumption_boundary.consume_owner_purchases(native, EPS)
    assert rng_digest(reference) == rng_digest(native)
    assert rng_digest(native) == before


def test_forced_case_consumes_stock():
    """Явный сценарий: срез реально списывает товар и пишет все три отчёта."""
    engine = build_engine()
    a = owner_account(engine)
    put(engine, a, 'grain', 10.0, 20.0)
    put(engine, a, 'fish', 4.0, 8.0)
    one = make_order(engine, a, 'grain', 6.0, remaining=0.0)
    two = make_order(engine, a, 'fish', 1.0, remaining=0.0)
    owner_row(engine, a, orders=[one['id'], two['id']], budget=30.0)
    before = dict(engine.accounts[a['id']]['stock'])
    reference, native = assert_pair_matches(engine)
    assert native.accounts[a['id']]['stock']['grain'] == before['grain']-6.0
    assert native.accounts[a['id']]['stock']['fish'] == before['fish']-1.0
    assert native.report['consumed'] == {'grain': 6.0, 'fish': 1.0}
    assert native.report['owner_consumed'] == {'grain': 6.0, 'fish': 1.0}
    assert native.report['owner_consumption'][0]['consumed'] == {'grain': 6.0, 'fish': 1.0}
    assert native.report['owner_consumption'][0]['unused_budget'] == 30.0


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

# Заполняются прогоном на чистой ветке до переноса (см. test_replay_*).
REPLAY_17 = (
    ('d35feca96e61235243749bcc69b3db5107368b4f5e1254a6b8cbba1fb0407887',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('9fb6896b13b8937efdc674b0ce862995c6d4aa06bc83d06877513867bbacb356',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('f4917448ae569e9bdb86fa826fdd0f747c8ebbf5337d5a40aa7e14efe1b75684',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('6aa90d035748af57c1350972dedc09e5ed22cf0cbb9c2827f395eaf310b34450',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('4899ef2758bd12d3a3dfade780e3a431b64dd526712a2d0f96d2fa35da0f44f0',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('594a2200bd2c4af50c65adac8773d76330f76772c7247a0a452242ffb46b3953',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def replay_digest(engine):
    """Digest состояния и результата owner consumption за месяц."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'owner_consumption': engine.report.get('owner_consumption'),
        'owner_consumed': engine.report.get('owner_consumed'),
        'consumed': engine.report.get('consumed'),
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_owner_consumption_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='owner-cons-replay-%d-%d' % (seed, index),
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
    assert state == REPLAY_17[index][0], 'месяц %d' % (index+1)
    assert rng == REPLAY_17[index][1], 'месяц %d' % (index+1)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_73129(replay_seed_73129, index):
    state, rng = replay_seed_73129[index]
    assert state == REPLAY_73129[index][0], 'месяц %d' % (index+1)
    assert rng == REPLAY_73129[index][1], 'месяц %d' % (index+1)


def test_replay_is_deterministic(replay_seed_17):
    assert len(replay_seed_17) == 3
    assert run_replay(17) == list(replay_seed_17)


def test_replay_actually_consumed():
    """Exact replay не должен проходить на пустом потреблении владельцев."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    with workdir('test_owner_consumption_totals') as directory:
        db, service = sr.build_world(73129, directory, report_detail='standard')
        try:
            for index in range(3):
                service.advance_turn(command_id='owner-cons-totals-%d' % index, source='audit')
            engine = service.economy_state().engine
        finally:
            db.close()
    consumed = engine.report['owner_consumed']
    assert consumed, 'потребление владельцев пустое'
    assert sum(consumed.values()) > 0
    # Каждая строка отчёта пересчитана срезом, а не осталась начальной.
    for row in engine.report['owner_consumption']:
        assert 'unused_budget' in row
        assert row['unused_budget'] == max(0, row['budget']-row['spent'])
