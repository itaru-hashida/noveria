"""Focused-тесты границы мутаций заявок: один словарь заявки, одна граница.

Проверяется не только выдача операций, но и то, ради чего введена граница:
хранилище не создаёт вторую копию заявки, ``OfferView`` видит мутацию сразу,
значения и типы остаются точными, а выбранный call path V3 не пишет в поля
заявки в обход хранилища. Поэтому часть проверок смотрит на идентичность
объектов и на AST файла ``v3.py``.
"""
import ast
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import offer_boundary  # noqa: E402
from modules.economy.native import order_boundary  # noqa: E402

EPS = support.EPS
KERNEL = order_boundary.load_order_store()
OFFERS = offer_boundary.load_offer_view()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение order_store не собрано')


def order(identity='a', remaining=10.0, reserved=0.0, status='active', price=1.0):
    """Живая заявка: обычный словарь, как у настоящего движка."""
    return dict(id=identity, participant='seller', good='grain', quantity=remaining,
                remaining=remaining, reserved=reserved, status=status, price=price,
                side='sell', province_id='p1', tile_id='t1')


# 1. Источник истины — живой список заявок --------------------------------
def test_store_holds_the_same_list_not_a_copy():
    orders = [order('a')]
    store = order_boundary.OrderStore(orders)
    assert store.orders is orders
    live = order('b')
    store.append(live)
    # append/remove меняют именно список движка: копии нет.
    assert orders[-1] is live
    store.remove(live)
    assert live not in orders
    assert len(orders) == 1


def test_writes_go_to_the_same_dict_object():
    live = order('a', remaining=10.0)
    store = order_boundary.OrderStore([live])
    store.set_remaining(live, 4.5)
    store.set_reserved(live, 2.5)
    store.set_status(live, 'partial')
    store.set_price(live, 3.25)
    # Запись не заменяет словарь и не заводит второй: это тот же объект.
    assert store.orders[0] is live
    assert live == dict(id='a', participant='seller', good='grain', quantity=10.0,
                        remaining=4.5, reserved=2.5, status='partial', price=3.25,
                        side='sell', province_id='p1', tile_id='t1')


# 2. OfferView видит мутацию сразу ----------------------------------------
def test_offer_view_sees_mutation_through_store_immediately():
    live = order('a', remaining=5.0)
    other = order('b', remaining=5.0)
    store = order_boundary.OrderStore([live, other])
    book = dict(source=[live, other],
                options=[(1.0, live, (1.0, 0.0, 0, ())),
                         (2.0, other, (2.0, 0.0, 0, ()))],
                quotes={}, board_id=None, first_live=0)
    view = offer_boundary.OfferView(book, 'buyer', 'p1')
    assert [row[1]['id'] for row in view] == ['a', 'b']
    # Мутация через границу: остаток обнулён — представление обязано это увидеть,
    # потому что тройка книги держит тот же словарь, а не снимок.
    store.set_remaining(other, 0.0)
    assert [row[1]['id'] for row in view] == ['a']
    # Возврат остатка через границу снова делает оффер видимым: копия книги
    # такого не показала бы, курсор first_live стоит на первом оффере.
    store.set_remaining(other, 2.0)
    assert [row[1]['id'] for row in view] == ['a', 'b']
    assert book['options'][1][1] is other


def test_status_mutation_through_store_hides_offer():
    live = order('a')
    store = order_boundary.OrderStore([live])
    book = dict(source=[live], options=[(1.0, live, (1.0, 0.0, 0, ()))],
                quotes={}, board_id=None, first_live=0)
    view = offer_boundary.OfferView(book, 'buyer', 'p1')
    assert bool(view) is True
    store.set_status(live, 'cancelled')
    assert bool(view) is False


# 3. Точные значения и типы int/float -------------------------------------
def test_values_and_numeric_types_are_preserved_exactly():
    live = order('a', remaining=3, reserved=1, price=2)
    store = order_boundary.OrderStore([live])
    store.set_remaining(live, 2)
    assert live['remaining'] == 2
    assert type(live['remaining']) is int
    store.set_reserved(live, 0.1+0.2)
    assert type(live['reserved']) is float
    assert live['reserved'] == 0.1+0.2
    # Граница не приводит значения: последний разряд сохраняется побитово.
    exact = 0.1+0.2
    store.set_price(live, exact)
    assert live['price'].hex() == exact.hex()
    store.set_remaining(live, 0.0)
    assert type(live['remaining']) is float


def test_boundary_does_not_recompute_arithmetic():
    """Арифметику считает вызывающий код: граница только записывает."""
    live = order('a', remaining=10.0)
    store = order_boundary.OrderStore([live])
    computed = max(0, live['remaining']-3.7)
    store.set_remaining(live, computed)
    assert live['remaining'].hex() == (10.0-3.7).hex()


# 4. native-привязка и эталон ---------------------------------------------
def test_boundary_binds_the_compiled_class():
    assert order_boundary.NATIVE_ORDER_STORE is True
    assert order_boundary.OrderStore is KERNEL.OrderStore
    assert order_boundary.OrderStore is not order_boundary.OrderStorePython
    assert order_boundary.OrderStore.__module__.endswith('order_store')


def test_python_reference_has_the_same_operations():
    for name in ('append', 'remove', 'set_remaining', 'set_reserved',
                 'set_status', 'set_price'):
        assert callable(getattr(order_boundary.OrderStorePython, name))
        assert callable(getattr(order_boundary.OrderStore, name))


def test_python_reference_mutates_the_same_objects():
    live = order('a', remaining=7.0)
    store = order_boundary.OrderStorePython([live])
    store.set_remaining(live, 5.0)
    store.set_status(live, 'partial')
    assert live['remaining'] == 5.0
    assert live['status'] == 'partial'
    assert store.orders[0] is live


def test_fallback_used_when_extension_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('order_store'):
            raise ImportError("cannot import name 'order_store'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert order_boundary.load_order_store() is None


def test_native_and_reference_give_identical_state(monkeypatch):
    """native и эталон дают один и тот же месяц при одинаковом входе."""
    reference = run_month(order_boundary.OrderStorePython)
    native = run_month(order_boundary.OrderStore)
    assert native == reference


# 5. Копия движка: одна граница на копию ----------------------------------
def test_store_survives_engine_deepcopy_with_single_orders_list():
    engine = build_engine()
    engine.step()
    store = engine.order_store
    assert store is engine.order_store, 'граница кэшируется на месяц'
    clone = copy.deepcopy(engine)
    # Копия получает собственную границу над собственным списком заявок.
    assert clone.order_store is not store
    assert clone.order_store.orders is clone.report['orders']
    assert clone.report['orders'] == engine.report['orders']
    clone.order_store.set_remaining(clone.report['orders'][0], 0.0)
    assert engine.report['orders'][0]['remaining'] != 0.0


def test_store_rebinds_when_report_is_replaced():
    engine = build_engine()
    engine.step()
    store = engine.order_store
    engine.report['orders'] = []
    assert engine.order_store is not store
    assert engine.order_store.orders is engine.report['orders']


def test_store_ignores_pickle_protocol():
    """export_state пишет только счета/предприятия/meta; граница не мешает."""
    live = order('a')
    store = order_boundary.OrderStore([live])
    clone = copy.deepcopy(store)
    assert clone.orders == [live]
    assert clone.orders is not store.orders


# 6. Выбранный call path не пишет поля заявки в обход ---------------------
ORDER_RECEIVERS = {'order', 'o', 'offer', 'buy', 'sell', 'row', 'ask', 'entry'}
FIELDS = {'remaining', 'reserved', 'status', 'price'}


def direct_order_writes(source):
    """Прямые записи в поля заявки в v3.py (без is-проверок по имени приёмника)."""
    tree = ast.parse(source)
    found = []

    def add(target, line, node):
        if not isinstance(target, ast.Subscript):
            return
        key = target.slice
        if not (isinstance(key, ast.Constant) and key.value in FIELDS):
            return
        name = target.value.id if isinstance(target.value, ast.Name) else None
        if name in ORDER_RECEIVERS:
            found.append((line, name, key.value, ast.unparse(node)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                add(target, node.lineno, node)
        elif isinstance(node, ast.AugAssign):
            add(node.target, node.lineno, node)
        elif isinstance(node, ast.AnnAssign):
            add(node.target, node.lineno, node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ('update', 'setdefault'):
                if any(kw.arg in FIELDS for kw in node.keywords):
                    receiver = node.func.value
                    name = receiver.id if isinstance(receiver, ast.Name) else None
                    if name in ORDER_RECEIVERS:
                        found.append((node.lineno, name, 'update', ast.unparse(node)))
    return found


def test_v3_has_no_direct_writes_to_order_fields_in_chosen_path():
    source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    writes = direct_order_writes(source)
    assert writes == [], f'Прямые записи в поля заявки: {writes}'


def test_v3_mutations_go_through_store_on_real_month():
    """Счётчик вызовов границы: match и отмена обязаны ходить через неё."""
    engine = build_engine()
    calls = []
    original = order_boundary.OrderStore

    class Counting(original):
        def set_remaining(self, order, value):
            calls.append(('remaining', order['id']))
            return original.set_remaining(self, order, value)

        def set_reserved(self, order, value):
            calls.append(('reserved', order['id']))
            return original.set_reserved(self, order, value)

        def set_status(self, order, value):
            calls.append(('status', order['id']))
            return original.set_status(self, order, value)

        def set_price(self, order, value):
            calls.append(('price', order['id']))
            return original.set_price(self, order, value)

    import modules.economy.v3 as v3
    saved = v3._order.OrderStore
    v3._order.OrderStore = Counting
    try:
        engine.meta['report_detail'] = 'none'
        engine.step()
    finally:
        v3._order.OrderStore = saved
    fields = {field for field, _ in calls}
    assert engine.report['orders'], 'месяц должен создать заявки'
    # Сделки и закрытие рынка трогают все четыре поля через границу.
    assert {'remaining', 'reserved', 'status'} <= fields
    assert all(order_id for _, order_id in calls)


# 7. Привязка в движке: хранилище берётся из v3 --------------------------
def test_engine_exposes_store_for_its_orders():
    engine = build_engine()
    engine.step()
    store = engine.order_store
    assert isinstance(store, order_boundary.OrderStore)
    assert store.orders is engine.report['orders']
    # Все заявки месяца живут в том же списке: append их не копировал.
    assert len(store.orders) == len(engine.report['orders'])


# --- общие помощники ------------------------------------------------------
def build_engine(seed=17, detail='none'):
    cfg = json.loads((ROOT/'tests/fixtures/economy.json').read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H',
             properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def run_month(store_class):
    """Один месяц подменённой границей и точный отпечаток результата."""
    import modules.economy.v3 as v3
    saved = v3._order.OrderStore
    v3._order.OrderStore = store_class
    try:
        engine = build_engine()
        engine.step()
    finally:
        v3._order.OrderStore = saved
    return dict(
        orders=[(o['id'], o['remaining'], o['reserved'], o['status'], o['price'])
                for o in engine.report['orders']],
        trades=[(t['trade_id'], t['quantity'], t['price'], t['gold'])
                for t in engine.report['trades']],
        accounts={a['id']: (a['balance'], sorted(a.get('stock_reserved', {}).items()))
                  for a in engine.accounts.values()},
        markets=engine.meta.get('market_history'),
        rng=repr(engine.rng.getstate()))
