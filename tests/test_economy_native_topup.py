"""Focused-тесты top-up-ядра: точное совпадение с Python-эталоном V3.

Ядро работает с ``object``-арифметикой намеренно: ``population*rate``,
``needed-free_stock`` и ``free_cash/price`` бывают и int, и float, а тип
результата виден в количестве заявки, резерве и деньгах сделки. Проверяется не
только значение, но и тип, порядок обхода, порядок операций и однократность
side effects.

Две группы тестов:

  * ядро границы (``topup_boundary.top_up``) против эталона
    (``topup_boundary.top_up_python``) — числовая часть, порядок и side effects;
  * полный ``Economy.household_top_up`` против того же метода с подменённым на
    эталон ядром — отчёт, остаточная книга спроса и RNG.
"""
import copy
import contextlib
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
from modules.economy.native import topup_boundary  # noqa: E402

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


def household(engine, account_id='h'):
    return engine.accounts[account_id]


def add_household(engine, identity, balance=100000.0, reserved=0.0):
    """Копия домохозяйства фикстуры под новым идентификатором."""
    row = copy.deepcopy(engine.accounts['h'])
    row['id'] = identity
    row['balance'] = balance
    row['reserved'] = reserved
    engine.accounts[identity] = row
    return row


def put(engine, account, good, quantity, value=None, reserved=0):
    """Положить товар на склад счёта без побочных эффектов."""
    account['stock'][good] = quantity
    account['values'][good] = quantity if value is None else value
    if reserved:
        account.setdefault('stock_reserved', {})[good] = reserved


def publish(engine, seller, good, quantity, price):
    """Выставить оффер продавца и вернуть заявку для книги ``sells``."""
    account = engine.accounts[seller]
    account['stock'][good] = account['stock'].get(good, 0)+quantity
    account['values'][good] = account['values'].get(good, 0)+quantity*price
    order = engine.order(seller, good, quantity, 'sell', price, 'Продажа')
    assert order is not None
    return order


def sells_from(*orders):
    """Книга офферов в том же виде, в каком её строит sell_orders."""
    sells = {}
    for order in orders:
        sells.setdefault(order['good'], []).append(order)
    return sells


def copy_sells(engine, sells):
    """Те же офферы в копии движка: книга ссылается на её заявки."""
    by_id = {order['id']: order for order in engine.report['orders']}
    return {good: [by_id[order['id']] for order in orders]
            for good, orders in sells.items()}


def nearby(engine, households, sells):
    """Копии движка, домохозяйств и книги для парного прогона."""
    clone = copy.deepcopy(engine)
    return clone, [clone.accounts[a['id']] for a in households], copy_sells(clone, sells)


# ---------------------------------------------------------------------------
# Снимки состояния и побитовое сравнение
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS = ('stock', 'values', 'stock_reserved', 'balance', 'reserved')
REPORT_KEYS = ('orders', 'trades', 'events', 'household_top_up')


def rng_digest(engine):
    return hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                     ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def snapshot(engine):
    """Полный снимок того, что top-up вправе менять."""
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
        # Тип обязателен: min/max сохраняют тип выбранного аргумента.
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def kernel_pair(engine, households, sells):
    """Эталон и ядро на одинаковых копиях движка и той же книге."""
    reference, ref_households, ref_sells = nearby(engine, households, sells)
    native, nat_households, nat_sells = nearby(engine, households, sells)
    topup_boundary.top_up_python(reference, ref_households, ref_sells, EPS)
    topup_boundary.top_up(native, nat_households, nat_sells, EPS)
    return reference, native


def assert_kernel_matches(engine, households, sells):
    reference, native = kernel_pair(engine, households, sells)
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


@contextlib.contextmanager
def python_reference():
    """Подменить ядро эталоном на время полного вызова household_top_up."""
    original = topup_boundary.run_top_up
    topup_boundary.run_top_up = topup_boundary.run_top_up_python
    try:
        yield
    finally:
        topup_boundary.run_top_up = original


def method_pair(engine, households, sells):
    """Полный household_top_up: эталонное ядро против native-ядра."""
    reference, ref_households, ref_sells = nearby(engine, households, sells)
    native, nat_households, nat_sells = nearby(engine, households, sells)
    with python_reference():
        reference.household_top_up(ref_households, ref_sells)
    native.household_top_up(nat_households, nat_sells)
    return reference, native


def assert_method_matches(engine, households, sells):
    reference, native = method_pair(engine, households, sells)
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


def top_up_orders(engine):
    return [o for o in engine.report['orders'] if o.get('household_top_up')
            and not o.get('household_top_up_residual')]


def residual_orders(engine):
    return [o for o in engine.report['orders'] if o.get('household_top_up_residual')]


# ---------------------------------------------------------------------------
# Нулевой top-up
# ---------------------------------------------------------------------------

def test_zero_top_up_when_stock_is_enough():
    """Склад покрывает норму: кандидатов нет, заявок на допкупку нет."""
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 1000.0, 2000.0)
    put(engine, a, 'fish', 1000.0, 2000.0)
    sell = publish(engine, 'firm-00002', 'bread', 500.0, 2.0)
    sells = sells_from(sell)
    before = rng_digest(engine)
    reference, native = assert_kernel_matches(engine, [a], sells)
    assert top_up_orders(native) == []
    assert native.report['trades'] == []
    # Пустой отбор даже не перемешивает: RNG не тронут.
    assert rng_digest(native) == before


def test_zero_top_up_when_no_cash():
    """Денег нет: free_cash <= EPS, домохозяйство не попадает в кандидаты."""
    engine = build_engine()
    a = household(engine)
    a['balance'] = 0.0
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 500.0, 2.0)
    sells = sells_from(sell)
    before = rng_digest(engine)
    reference, native = assert_kernel_matches(engine, [a], sells)
    assert top_up_orders(native) == []
    assert rng_digest(native) == before


def test_zero_top_up_when_cash_is_reserved():
    """Резерв съедает кассу: свободных денег ноль, допкупки нет."""
    engine = build_engine()
    a = household(engine)
    a['balance'] = 100.0
    a['reserved'] = 100.0
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 500.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_kernel_matches(engine, [a], sells)
    assert top_up_orders(native) == []
    assert native.report['trades'] == []


def test_zero_top_up_report_is_written():
    """Полный метод пишет нулевой отчёт: агрегат не остаётся прошлого месяца."""
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 1000.0, 2000.0)
    put(engine, a, 'fish', 1000.0, 2000.0)
    sell = publish(engine, 'firm-00002', 'bread', 500.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_method_matches(engine, [a], sells)
    assert native.report['household_top_up'] == dict(quantity=0, gold=0,
        by_good={'bread': 0, 'fish': 0})
    assert top_up_orders(native) == []


# ---------------------------------------------------------------------------
# Частичный top-up и касса
# ---------------------------------------------------------------------------

def test_partial_top_up_buys_only_the_gap():
    """Частичный запас: покупается ровно недостача до нормы."""
    engine = build_engine()
    a = household(engine)
    needed = engine.population(a)*engine.cfg['basic_needs']['bread']
    put(engine, a, 'bread', 0.5, 1.0)
    sell = publish(engine, 'firm-00002', 'bread', 1000.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_method_matches(engine, [a], sells)
    bought = top_up_orders(native)
    assert len(bought) == 1
    assert bought[0]['good'] == 'bread'
    assert bought[0]['quantity'] == needed-0.5
    assert bought[0]['status'] == 'filled'
    assert native.report['household_top_up']['quantity'] == needed-0.5
    assert native.report['household_top_up']['gold'] == (needed-0.5)*2.0


def test_sufficient_cash_buys_full_gap():
    """Кассы хватает на всю недостачу — заявка на полную норму."""
    engine = build_engine()
    a = household(engine)
    needed = engine.population(a)*engine.cfg['basic_needs']['bread']
    put(engine, a, 'bread', needed/2, needed)
    sell = publish(engine, 'firm-00002', 'bread', needed*4, 1.0)
    sells = sells_from(sell)
    reference, native = assert_method_matches(engine, [a], sells)
    bought = top_up_orders(native)
    assert bought[0]['quantity'] == needed-needed/2
    assert bought[0]['status'] == 'filled'


def test_insufficient_cash_limits_quantity():
    """Кассы не хватает: quantity = free_cash/price, касса уходит в ноль."""
    engine = build_engine()
    a = household(engine)
    a['balance'] = 10.0
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 1000.0, 4.0)
    sells = sells_from(sell)
    reference, native = assert_kernel_matches(engine, [a], sells)
    bought = top_up_orders(native)
    assert len(bought) == 1
    # 10.0/4.0 — ровно две с половиной единицы, столько же ушло денег.
    assert bought[0]['quantity'] == 2.5
    assert native.accounts['h']['balance'] == 0.0
    assert bought[0]['status'] == 'filled'


def test_offer_remaining_limits_quantity():
    """Остаток оффера меньше недостачи: покупается остаток оффера."""
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 1.25, 2.0)
    sells = sells_from(sell)
    reference, native = assert_kernel_matches(engine, [a], sells)
    bought = top_up_orders(native)
    assert len(bought) == 1
    assert bought[0]['quantity'] == 1.25
    assert bought[0]['remaining'] == 0.0


# ---------------------------------------------------------------------------
# Несколько домохозяйств и порядок обхода
# ---------------------------------------------------------------------------

def test_multiple_households_buy_in_turn():
    """Каждое домохозяйство покупает свою недостачу своей кассой."""
    engine = build_engine()
    first = household(engine)
    second = add_household(engine, 'h2')
    put(engine, first, 'bread', 0.0)
    put(engine, second, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_method_matches(engine, [first, second], sells)
    needed = native.population(first)*native.cfg['basic_needs']['bread']
    assert native.report['household_top_up']['quantity'] == needed*2
    assert len(top_up_orders(native)) == 2
    assert len({o['participant'] for o in top_up_orders(native)}) == 2


def test_household_order_follows_given_list():
    """Ядро обходит ровно тот список, который вернул хук перемешивания."""
    engine = build_engine()
    first = household(engine)
    second = add_household(engine, 'h2')
    for row in (first, second):
        put(engine, row, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    calls = []
    original = engine.order

    def traced(participant, good, quantity, side, price, purpose):
        if side == 'buy':
            calls.append(participant)
        return original(participant, good, quantity, side, price, purpose)

    class Hooks(topup_boundary.TopUpHooks):
        def shuffle_candidates(self, households):
            return [second, first]

    engine.order = traced
    topup_boundary.run_top_up([first, second], engine, sells,
                              engine.cfg['basic_needs'], EPS, Hooks(engine, sells))
    assert calls == ['h2', 'h']


def test_goods_order_follows_shuffle():
    """Товары обходятся в перемешанном хуком порядке, а не в порядке словаря."""
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 0.0)
    put(engine, a, 'fish', 0.0)
    bread = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    fish = publish(engine, 'firm-00001', 'fish', 10000.0, 3.0)
    sells = sells_from(bread, fish)
    seen = []

    class Hooks(topup_boundary.TopUpHooks):
        def shuffle_goods(self):
            return ['fish', 'bread']

    hooks = Hooks(engine, sells)
    original = hooks.place

    def traced(account, good, quantity, price):
        seen.append(good)
        return original(account, good, quantity, price)

    hooks.place = traced
    topup_boundary.run_top_up([a], engine, sells, engine.cfg['basic_needs'], EPS, hooks)
    assert seen == ['fish', 'bread']


def test_rng_shuffle_matches_between_branches():
    """Перемешивание тратит RNG одинаково и даёт тот же порядок заявок."""
    engine = build_engine()
    first = household(engine)
    second = add_household(engine, 'h2')
    third = add_household(engine, 'h3')
    for row in (first, second, third):
        put(engine, row, 'bread', 0.0)
        put(engine, row, 'fish', 0.0)
    bread = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    fish = publish(engine, 'firm-00001', 'fish', 10000.0, 3.0)
    sells = sells_from(bread, fish)
    before = rng_digest(engine)
    reference, native = assert_kernel_matches(engine, [first, second, third], sells)
    assert [o['participant'] for o in native.report['orders']] == \
        [o['participant'] for o in reference.report['orders']]
    assert rng_digest(reference) == rng_digest(native)
    assert rng_digest(native) != before, 'перемешивание обязано тратить RNG'


# ---------------------------------------------------------------------------
# int/float-семантика
# ---------------------------------------------------------------------------

def test_int_population_and_rate_keep_int_quantity():
    """int-население и int-норма дают int-недостачу в quantity заявки.

    ``reserved`` после снятия остатка равен ``0.0``, поэтому тип виден только в
    количестве и в цене сделки: 500 int * 2.0 float = 1000.0 float.
    """
    engine = build_engine()
    engine.entities['h']['properties']['population'] = 1000
    engine.cfg['basic_needs'] = {'bread': 2, 'fish': 1}
    a = household(engine)
    needed = engine.population(a)*2
    assert type(needed) is int and needed == 2000
    put(engine, a, 'bread', 1500, 3000)
    put(engine, a, 'fish', 10000, 20000)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_kernel_matches(engine, [a], sells)
    bought = top_up_orders(native)
    assert len(bought) == 1
    assert bought[0]['quantity'] == 500
    assert type(bought[0]['quantity']) is int
    assert native.report['trades'][0]['quantity'] == 500
    assert native.accounts['h']['balance'] == 100000-1000.0


def test_float_rate_keeps_float_quantity():
    """Дробная норма даёт float-недостачу: тип не приводится к double-результату."""
    engine = build_engine()
    a = household(engine)
    needed = engine.population(a)*engine.cfg['basic_needs']['bread']
    assert type(needed) is float
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_kernel_matches(engine, [a], sells)
    bought = top_up_orders(native)
    assert bought[0]['quantity'] == needed
    assert type(bought[0]['quantity']) is float


def test_missing_uses_object_arithmetic_not_double():
    """float-склад и float-норма: недостача считается без приведения к double."""
    engine = build_engine()
    a = household(engine)
    needed = engine.population(a)*engine.cfg['basic_needs']['bread']
    partial = needed/3
    put(engine, a, 'bread', partial, partial)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_kernel_matches(engine, [a], sells)
    bought = top_up_orders(native)
    # Побитово: needed-partial, а не пересчёт через double.
    assert bought[0]['quantity'] == needed-partial
    assert type(bought[0]['quantity']) is float


def test_int_stock_and_int_need_stay_int_in_missing():
    """int-склад и int-норма: недостача остаётся int, как у max(0, int)."""
    engine = build_engine()
    engine.entities['h']['properties']['population'] = 100
    engine.cfg['basic_needs'] = {'bread': 5, 'fish': 1}
    a = household(engine)
    put(engine, a, 'bread', 100, 200)
    put(engine, a, 'fish', 1000, 2000)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_kernel_matches(engine, [a], sells)
    bought = top_up_orders(native)
    assert bought[0]['quantity'] == 400
    assert type(bought[0]['quantity']) is int


# ---------------------------------------------------------------------------
# Побочные эффекты: ровно один раз
# ---------------------------------------------------------------------------

def test_side_effects_called_once():
    """Каждая покупка — один order, один match и один cancel_order."""
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    calls = []
    originals = {name: getattr(engine, name) for name in ('order', 'match', 'cancel_order')}
    for name, original in originals.items():
        def wrapper(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        setattr(engine, name, wrapper)
    try:
        topup_boundary.top_up(engine, [a], sells, EPS)
    finally:
        for name, original in originals.items():
            setattr(engine, name, original)
    assert calls == ['order', 'match', 'cancel_order']
    assert len(top_up_orders(engine)) == 1


def test_operation_order_matches_reference():
    """Порядок вызовов order/match/cancel_order совпадает с эталоном."""
    engine = build_engine()
    first = household(engine)
    second = add_household(engine, 'h2')
    for row in (first, second):
        put(engine, row, 'bread', 0.0)
        put(engine, row, 'fish', 0.0)
    bread = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    fish = publish(engine, 'firm-00001', 'fish', 10000.0, 3.0)
    sells = sells_from(bread, fish)
    reference, ref_households, ref_sells = nearby(engine, [first, second], sells)
    native, nat_households, nat_sells = nearby(engine, [first, second], sells)
    ref_calls, nat_calls = trace(reference), trace(native)
    topup_boundary.top_up_python(reference, ref_households, ref_sells, EPS)
    topup_boundary.top_up(native, nat_households, nat_sells, EPS)
    assert nat_calls == ref_calls
    assert len([row for row in nat_calls if row[0] == 'order']) > 0


def trace(engine):
    """Записать последовательность order/match/cancel_order."""
    calls = []
    for name in ('order', 'match', 'cancel_order'):
        original = getattr(engine, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            subject = args[0]
            if isinstance(subject, dict):
                subject = (subject.get('good'), subject.get('remaining'))
            elif _name == 'order':
                subject = (args[1], args[2])
            calls.append((_name, subject))
            return _original(*args, **kwargs)

        setattr(engine, name, wrapper)
    return calls


def test_residual_book_is_flagged_once():
    """Остаточная книга спроса помечается флагом и проходит match один раз."""
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    reference, native = assert_method_matches(engine, [a], sells)
    residual = residual_orders(native)
    assert residual, 'остаточная книга обязана быть'
    assert all(o['household_top_up'] for o in residual)
    assert all(o['purpose'] in ('Базовые нужды', 'Дополнительные нужды')
               for o in residual)


# ---------------------------------------------------------------------------
# deepcopy и граница
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)

    def state(target):
        return copy.deepcopy({aid: dict(row['stock'], balance=row['balance'])
                              for aid, row in target.accounts.items()})

    before = state(engine)
    clone = copy.deepcopy(engine)
    topup_boundary.top_up(clone, [clone.accounts['h']], copy_sells(clone, sells), EPS)
    assert state(engine) == before, 'top-up в копии изменил оригинал'
    left, _, left_sells = nearby(engine, [a], sells)
    right, _, right_sells = nearby(engine, [a], sells)
    topup_boundary.top_up(left, [left.accounts['h']], left_sells, EPS)
    topup_boundary.top_up(right, [right.accounts['h']], right_sells, EPS)
    assert_same(snapshot(left), snapshot(right))


def test_boundary_exposes_native_when_built():
    built = topup_boundary.load_top_up_kernel() is not None
    assert topup_boundary.NATIVE_KERNEL is built
    if built:
        assert topup_boundary.run_top_up.__module__.endswith('topup_kernel')
        assert topup_boundary.top_up.__module__.endswith('topup_boundary')
    else:
        assert topup_boundary.run_top_up is topup_boundary.run_top_up_python


def test_native_entry_points_are_compiled():
    kernel = topup_boundary.load_top_up_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение topup_kernel не собрано')
    assert topup_boundary.run_top_up is kernel.run_top_up
    assert topup_boundary.run_top_up is not topup_boundary.run_top_up_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('topup_kernel'):
            raise ImportError("cannot import name 'topup_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert topup_boundary.load_top_up_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = build_engine()
    a = household(engine)
    put(engine, a, 'bread', 0.25, 0.5)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    reference, _ = kernel_pair(engine, [a], sells)
    expected = snapshot(reference)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('topup_kernel'):
            raise ImportError("cannot import name 'topup_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(topup_boundary)
    try:
        assert topup_boundary.NATIVE_KERNEL is False
        assert topup_boundary.run_top_up is topup_boundary.run_top_up_python
        assert topup_boundary.top_up.__module__.endswith('topup_boundary')
        fallback, fallback_households, fallback_sells = nearby(engine, [a], sells)
        topup_boundary.top_up(fallback, fallback_households, fallback_sells, EPS)
        assert_same(expected, snapshot(fallback))
    finally:
        monkeypatch.undo()
        importlib.reload(topup_boundary)


def test_v3_has_single_call_site():
    """В household_top_up один вызов границы, а не вторая копия цикла."""
    source = inspect.getsource(Economy.household_top_up)
    assert '_topup.top_up(self, households, sells, EPS)' in source
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_topup.top_up(') == 1
    # Прежний числовой цикл по офферам больше не дублируется.
    assert "quantity = min(missing, sell['remaining'], self.free_cash(a['id'])/price)" \
        not in module_source


# ---------------------------------------------------------------------------
# RNG и forced-case
# ---------------------------------------------------------------------------

def test_rng_equal_between_branches():
    """RNG расходуется одинаково: эталон и ядро тратят генератор в тех же точках."""
    engine = build_engine()
    first = household(engine)
    second = add_household(engine, 'h2')
    for row in (first, second):
        put(engine, row, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    before = rng_digest(engine)
    reference, native = assert_method_matches(engine, [first, second], sells)
    assert rng_digest(reference) == rng_digest(native)
    assert rng_digest(native) != before, 'top-up обязан тратить RNG на перемешивание'


def test_forced_case_buys_and_spends_cash():
    """Явный сценарий: допкупка реально покупает товар и списывает деньги."""
    engine = build_engine()
    a = household(engine)
    needed = engine.population(a)*engine.cfg['basic_needs']['bread']
    put(engine, a, 'bread', 0.0)
    sell = publish(engine, 'firm-00002', 'bread', 10000.0, 2.0)
    sells = sells_from(sell)
    before_cash = engine.accounts['h']['balance']
    before_stock = engine.accounts['firm-00002']['stock']['bread']
    reference, native = assert_kernel_matches(engine, [a], sells)
    spent = needed*2.0
    assert native.accounts['h']['balance'] == before_cash-spent
    assert native.accounts['h']['stock']['bread'] == needed
    assert native.accounts['firm-00002']['stock']['bread'] == before_stock-needed
    assert native.report['trades'], 'сделка обязана быть записана'
    assert native.report['trades'][0]['quantity'] == needed


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

REPLAY_17 = (
    ('c812ad6f521000f860f41de23262b37fdc25d252d61a73e64583549d62506665',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('b6679cc96ad6ac0dd0c62416b336ae87398ec144d5081f219a2ed565599b0996',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('6db4d6d3689520a5106c7600f73bf58fb50e06e00236b32e5089fe87979a63ed',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('1c2c439d2a8b63ae3f246a3ac091a4b308be6b7e28e0fe8f71b25204d88b21ac',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('b3501e0b8141d559c165d35bbc109ffab6bace8adf2a2a8d13735cd2fc9699e7',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('215425bbac5e8ac5e1bd82f568f5a7e349d76595d0b99bffa45686a119bad6ce',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def replay_digest(engine):
    """Digest состояния, заявок, сделок и результата допкупки за месяц."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'trades': engine.report['trades'],
        'top_up': engine.report.get('household_top_up'),
        'consumed': engine.report.get('consumed'),
        'needs': {a['id']: a.get('needs') for a in engine.accounts.values()},
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_topup_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='topup-replay-%d-%d' % (seed, index),
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


def test_replay_actually_runs_top_up_branch():
    """Exact replay не должен проходить на пустом отборе кандидатов."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    counts = {'candidates': 0, 'top_up': 0, 'residual': 0}
    original = Economy.household_top_up

    def traced(self, households, sells):
        candidates = [a for a in households if self.free_cash(a['id']) > EPS
            and any(self.free_stock(a['id'], g) + EPS < self.population(a)*r
                for g, r in self.cfg['basic_needs'].items())]
        counts['candidates'] += len(candidates)
        result = original(self, households, sells)
        counts['top_up'] += len(top_up_orders(self))
        counts['residual'] += len(residual_orders(self))
        return result

    Economy.household_top_up = traced
    try:
        with workdir('test_topup_branch') as directory:
            db, service = sr.build_world(73129, directory, report_detail='standard')
            try:
                for index in range(3):
                    service.advance_turn(command_id='topup-branch-%d' % index, source='audit')
            finally:
                db.close()
    finally:
        Economy.household_top_up = original
    assert counts['candidates'] > 0, 'ветка допкупки не выполнялась'
    assert counts['residual'] > 0, 'остаточная книга спроса не создавалась'
