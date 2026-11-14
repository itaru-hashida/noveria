"""Тесты Cython-owned полей заявок: колонки — источник истины.

Проверяется главное свойство шага: значения ``remaining``/``reserved``/
``status``/``price`` живут в колонках ``OrderStore``, а Python-dict остаётся
compatibility view. Поэтому часть проверок смотрит на колонки, а не на словарь,
и отдельно проверяет, что dict не может разойтись с колонкой: запись идёт
только через ``set_*``.

Отдельный блок — устойчивость колонок к внешнему ``append`` в
``report['orders']``. Ветки ``native_mode='cython'`` материализуют заявки прямо
в список движка, и колонки обязаны догнать список, иначе слот съедет. Там же
регрессия на переиспользование ``id``: устаревший слот не должен давать запись
в чужую заявку или чтение за границей списка.
"""
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

order_boundary.configure(EPS)
offer_boundary.configure(EPS)

FIELDS = ('remaining', 'reserved', 'status', 'price')


def order(identity='a', remaining=10.0, reserved=0.0, status='active', price=1.0):
    """Живая заявка: обычный словарь, как у настоящего движка."""
    return dict(id=identity, participant='seller', good='grain', quantity=remaining,
                remaining=remaining, reserved=reserved, status=status, price=price,
                side='sell', province_id='p1', tile_id='t1')


def make_store(*orders):
    return order_boundary.OrderStore(list(orders))


def slot_of(store, live):
    return store.index[id(live)]


# --- 1. Импорт текущих orders --------------------------------------------
def test_store_imports_current_orders_into_columns():
    first = order('a', remaining=7.5, reserved=1.25, status='partial', price=2.5)
    second = order('b', remaining=3, reserved=0, status='active', price=1)
    store = make_store(first, second)
    assert store.orders[0] is first and store.orders[1] is second
    assert store.remaining == [7.5, 3]
    assert store.reserved == [1.25, 0]
    assert store.status == ['partial', 'active']
    assert store.price == [2.5, 1]


def test_columns_preserve_int_and_float_types():
    """int остаётся int, float — float: приведение сломало бы точность."""
    live = order('a', remaining=3, reserved=0, price=2)
    store = make_store(live)
    assert type(store.remaining[0]) is int
    assert type(store.price[0]) is int
    store.set_remaining(live, 0.1+0.2)
    store.set_reserved(live, 5)
    assert type(store.remaining[0]) is float
    assert type(store.reserved[0]) is int
    # Последний разряд сохраняется побитово.
    assert store.remaining[0].hex() == (0.1+0.2).hex()


def test_missing_optional_fields_get_defaults():
    live = dict(id='a', participant='s', good='g', remaining=2.0, status='active')
    store = make_store(live)
    assert store.reserved == [0]
    assert store.price == [0]


# --- 2. Колонки — источник истины ----------------------------------------
def test_set_writes_column_and_mirrors_to_dict():
    live = order('a')
    store = make_store(live)
    store.set_remaining(live, 4.0)
    # Истина в колонке, dict — её отражение.
    assert store.remaining[0] == 4.0
    assert live['remaining'] == 4.0
    store.set_status(live, 'filled')
    assert store.status[0] == 'filled'
    assert live['status'] == 'filled'


def test_each_setter_touches_only_its_own_column():
    live = order('a', remaining=6.0, reserved=2.0, status='active', price=3.0)
    store = make_store(live)
    store.set_remaining(live, 5.0)
    assert store.reserved == [2.0] and store.status == ['active'] and store.price == [3.0]
    store.set_price(live, 4.0)
    assert store.remaining == [5.0] and store.reserved == [2.0] and store.status == ['active']
    store.set_reserved(live, 1.0)
    assert store.price == [4.0] and store.status == ['active']
    store.set_status(live, 'partial')
    assert store.price == [4.0] and store.reserved == [1.0]


def test_columns_are_independent_per_order():
    first, second = order('a', remaining=8.0), order('b', remaining=9.0)
    store = make_store(first, second)
    store.set_remaining(second, 1.0)
    assert store.remaining == [8.0, 1.0]
    assert first['remaining'] == 8.0


# --- 3. OfferView читает колонки ----------------------------------------
def book_for(*orders):
    return dict(source=list(orders),
                options=[(float(i+1), o, (float(i+1), 0.0, 0, ()))
                         for i, o in enumerate(orders)],
                quotes={}, board_id=None, first_live=0)


def test_view_sees_writes_that_went_through_the_boundary():
    """Представление читает зеркало словаря, которое граница держит точным."""
    first, second = order('a', remaining=5.0), order('b', remaining=5.0)
    store = make_store(first, second)
    view = offer_boundary.OfferView(book_for(first, second), 'buyer', 'p1')
    assert [row[1]['id'] for row in view] == ['a', 'b']
    store.set_remaining(second, 0.0)
    assert [row[1]['id'] for row in view] == ['a']
    store.set_remaining(second, 2.0)
    assert [row[1]['id'] for row in view] == ['a', 'b']


def test_view_sees_mutation_through_store_immediately():
    first, second = order('a', remaining=5.0), order('b', remaining=5.0)
    store = make_store(first, second)
    view = offer_boundary.OfferView(book_for(first, second), 'buyer', 'p1')
    assert [row[1]['id'] for row in view] == ['a', 'b']
    store.set_remaining(second, 0.0)
    assert [row[1]['id'] for row in view] == ['a']
    store.set_remaining(second, 2.0)
    assert [row[1]['id'] for row in view] == ['a', 'b']


def test_view_hides_cancelled_status_from_columns():
    live = order('a')
    store = make_store(live)
    view = offer_boundary.OfferView(book_for(live), 'buyer', 'p1')
    assert bool(view) is True
    store.set_status(live, 'cancelled')
    assert bool(view) is False
    store.set_status(live, 'expired')
    assert bool(view) is False


def test_view_without_store_still_reads_dict():
    """Обратная совместимость: без хранилища представление читает словарь."""
    live = order('a', remaining=5.0)
    view = offer_boundary.OfferView(book_for(live), 'buyer', 'p1')
    assert [row[1]['id'] for row in view] == ['a']
    live['remaining'] = 0.0
    assert list(view) == []


def test_view_keeps_first_live_cursor_semantics():
    first = order('a', remaining=0.0)
    second = order('b', remaining=4.0)
    store = make_store(first, second)
    book = book_for(first, second)
    view = offer_boundary.OfferView(book, 'buyer', 'p1')
    assert [row[1]['id'] for row in view] == ['b']
    assert book['first_live'] == 1


def test_python_reference_view_matches_native():
    native = make_store(order('a', remaining=2.0), order('b', remaining=0.0))
    reference = order_boundary.OrderStorePython(
        [dict(o) for o in native.orders])
    native_book = book_for(*native.orders)
    reference_book = book_for(*reference.orders)
    got_native = [(p, s['remaining'], s['status'])
                  for p, s, _ in offer_boundary.OfferView(native_book, 'buyer', 'p1')]
    got_reference = [(p, s['remaining'], s['status'])
                     for p, s, _ in offer_boundary.OfferViewPython(
                         reference_book, 'buyer', 'p1')]
    assert got_native == got_reference


# --- 4. Добавление и удаление -------------------------------------------
def test_append_extends_columns():
    store = make_store(order('a'))
    fresh = order('b', remaining=2.0, reserved=0.5, status='active', price=7.0)
    store.append(fresh)
    assert store.orders[-1] is fresh
    assert store.remaining == [10.0, 2.0]
    assert store.reserved == [0.0, 0.5]
    assert store.status == ['active', 'active']
    assert store.price == [1.0, 7.0]
    assert slot_of(store, fresh) == 1


def test_remove_drops_slot_and_reindexes_tail():
    first, second, third = order('a'), order('b'), order('c')
    store = make_store(first, second, third)
    store.remove(second)
    assert store.orders == [first, third]
    assert store.remaining == [10.0, 10.0]
    assert len(store.remaining) == len(store.orders)
    # Хвост переиндексирован: третья заявка теперь в слоте 1.
    assert slot_of(store, third) == 1
    assert id(second) not in store.index
    store.set_remaining(third, 3.0)
    assert store.remaining == [10.0, 3.0]
    assert third['remaining'] == 3.0


def test_remove_unknown_order_raises():
    store = make_store(order('a'))
    with pytest.raises(ValueError):
        store.remove(order('zzz'))


# --- 5. Внешний append в report['orders'] -------------------------------
def test_external_append_is_picked_up_by_sync():
    """native-ветки добавляют заявки прямо в список: колонки обязаны догнать."""
    store = make_store(order('a'))
    fresh = order('b', remaining=4.0, reserved=1.0, status='active', price=6.0)
    store.orders.append(fresh)          # в обход границы
    assert len(store.remaining) == 1    # колонки ещё не знают
    store.sync()
    assert store.remaining == [10.0, 4.0]
    assert store.reserved == [0.0, 1.0]
    assert store.price == [1.0, 6.0]
    assert slot_of(store, fresh) == 1
    # И сеттер уже пишет в новый слот.
    store.set_remaining(fresh, 2.0)
    assert store.remaining == [10.0, 2.0]
    assert fresh['remaining'] == 2.0


def test_setter_syncs_before_writing():
    """Сеттер сам выравнивает колонки: отдельный sync не обязателен."""
    store = make_store(order('a'))
    fresh = order('b', remaining=4.0)
    store.orders.append(fresh)
    store.set_remaining(fresh, 1.0)
    assert store.remaining == [10.0, 1.0]


def test_shortened_list_rebuilds_columns():
    store = make_store(order('a'), order('b'))
    store.orders.pop()
    store.sync()
    assert store.remaining == [10.0]
    assert len(store.index) == 1


# --- 6. Переиспользование id: устаревший слот не должен врать ----------
def test_stale_slot_is_detected_by_identity():
    """id() переиспользуется после сборки мусора — слот проверяется тождеством.

    Без проверки запись ушла бы в чужую заявку, а при boundscheck=False
    чтение колонки могло бы выйти за границу списка.
    """
    live = order('a')
    store = make_store(live)
    stale = store.index[id(live)]
    # Имитируем переиспользование: слот в индексе указывает на другой объект.
    store.index[id(live)] = stale
    store.orders[stale] = order('impostor')
    store.set_remaining(live, 1.0)
    # Слот не подтвердился тождеством: колонка не тронута, запись ушла в dict.
    assert store.remaining[stale] == 10.0
    assert live['remaining'] == 1.0
    assert id(live) not in store.index


def test_unknown_order_gets_dict_only_write():
    store = make_store(order('a'))
    ghost = order('ghost')
    store.set_remaining(ghost, 3.0)
    assert ghost['remaining'] == 3.0
    assert store.remaining == [10.0]


def test_is_live_uses_columns():
    live = order('a', remaining=5.0)
    store = make_store(live)
    assert store.is_live(live, EPS) is True
    store.remaining[0] = 0.0
    assert store.is_live(live, EPS) is False
    store.remaining[0] = 5.0
    store.status[0] = 'cancelled'
    assert store.is_live(live, EPS) is False


# --- 7. deepcopy и pickle ------------------------------------------------
def test_deepcopy_rebuilds_columns_from_copied_orders():
    first = order('a', remaining=6.0, reserved=1.0)
    store = make_store(first)
    store.set_status(first, 'partial')
    clone = copy.deepcopy(store)
    assert clone.orders is not store.orders
    assert clone.orders[0] is not first
    assert clone.remaining == store.remaining
    assert clone.status == ['partial']
    assert clone.index[id(clone.orders[0])] == 0
    # Копия независима: мутация клона не трогает исходник.
    clone.set_remaining(clone.orders[0], 0.0)
    assert store.remaining == [6.0]
    assert first['remaining'] == 6.0


def test_engine_deepcopy_keeps_one_store_bound_to_report_orders():
    engine = build_engine()
    engine.step()
    clone = copy.deepcopy(engine)
    assert clone.order_store is not engine.order_store
    assert clone.order_store.orders is clone.report['orders']
    assert len(clone.order_store.remaining) == len(clone.report['orders'])
    # Колонки копии отражают свои заявки, а не заявки исходного движка.
    for position, live in enumerate(clone.report['orders']):
        assert clone.order_store.remaining[position] == live['remaining']


# --- 8. Реальный движок: колонки и dict не расходятся -------------------
def test_real_month_keeps_columns_in_sync():
    engine = build_engine()
    engine.step()
    store = engine.order_store
    store.sync()
    assert len(store.remaining) == len(engine.report['orders'])
    assert len(store.index) == len(engine.report['orders'])
    for position, live in enumerate(engine.report['orders']):
        assert store.orders[position] is live
        for field, column in (('remaining', store.remaining),
                              ('reserved', store.reserved),
                              ('status', store.status),
                              ('price', store.price)):
            assert column[position] == live[field], (live['id'], field)


def test_native_mode_month_keeps_columns_in_sync():
    """native-ветки пишут заявки в обход границы: колонки обязаны совпасть."""
    engine = build_engine()
    engine.step()
    engine.step(native_mode='cython')
    store = engine.order_store
    store.sync()
    assert len(store.remaining) == len(engine.report['orders'])
    for position, live in enumerate(engine.report['orders']):
        assert store.remaining[position] == live['remaining']
        assert store.status[position] == live['status']


def test_boundary_binds_compiled_class():
    assert order_boundary.NATIVE_ORDER_STORE is True
    assert order_boundary.OrderStore is KERNEL.OrderStore
    assert order_boundary.OrderStore is not order_boundary.OrderStorePython
    assert order_boundary.OrderStore.__module__.endswith('order_store')


def test_fallback_used_when_extension_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('order_store'):
            raise ImportError("cannot import name 'order_store'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert order_boundary.load_order_store() is None


def test_reference_store_has_the_same_surface():
    for name in ('append', 'remove', 'set_remaining', 'set_reserved',
                 'set_status', 'set_price', 'is_live', 'sync', 'columns'):
        assert callable(getattr(order_boundary.OrderStorePython, name))


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
