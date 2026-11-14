"""OfferView читает Cython-колонки по слоту, без id(order) в горячем цикле.

Проверяется главное свойство шага: книга передаёт представлению номер слота
вместе со заявкой, и обход читает ``remaining``/``status`` из колонок
``OrderStore``. Поэтому тесты смотрят на колонки (а не только на выдачу),
подменяют словарь заявки и убеждаются, что представление читает именно
колонку, и отдельно проверяют, что номера слотов пересчитываются после
структурной правки раскладки и подмены хранилища.

Файл также держит регрессию на снимки planning-книг: их заявки — копии, а не
объекты ``report['orders']``, и для них единственный источник значений
остаётся словарь.
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
STORE = order_boundary.load_order_store()
VIEW = offer_boundary.load_offer_view()

pytestmark = pytest.mark.skipif(STORE is None or VIEW is None,
                                reason='Cython-расширения order_store/offer_view не собраны')

order_boundary.configure(EPS)
offer_boundary.configure(EPS)


def order(identity='a', remaining=10.0, reserved=0.0, status='active', price=1.0,
          participant='seller', province_id='p1'):
    """Живая заявка: обычный словарь, как у настоящего движка."""
    return dict(id=identity, participant=participant, good='grain', quantity=remaining,
                remaining=remaining, reserved=reserved, status=status, price=price,
                side='sell', province_id=province_id, tile_id='t1')


def book_for(store, *orders, first_live=0):
    """Книга в том же виде, что строит _market_book: слоты и их версия."""
    rows = [(float(i+1), live, (float(i+1), 0.0, 0, ()))
            for i, live in enumerate(orders)]
    return dict(source=list(orders), options=rows, quotes={}, board_id=None,
                first_live=first_live, slot_store=store, slots=None, slot_version=None)


def view(store_class=order_boundary.OrderStore):
    """Хранилище и представление над одними и теми же заявками."""
    first, second = order('a', remaining=5.0), order('b', remaining=5.0)
    store = store_class([first, second])
    return store, book_for(store, first, second)


# --- 1. Слоты: форма выдачи и стабильность --------------------------------
def test_iter_live_yields_order_with_slot():
    first, second = order('a'), order('b')
    store = order_boundary.OrderStore([first, second])
    pairs = list(store.iter_live())
    assert [live for live, _ in pairs] == [first, second]
    assert [slot for _, slot in pairs] == [0, 1]
    # Пара выдаёт те же объекты, а не копии.
    assert pairs[0][0] is first


def test_iter_live_skips_nothing_and_keeps_order():
    """iter_live отдаёт весь список: фильтр живости — дело представления."""
    live = [order(str(i), remaining=float(i)) for i in range(4)]
    store = order_boundary.OrderStore(live)
    assert [(o['id'], s) for o, s in store.iter_live()] == \
           [('0', 0), ('1', 1), ('2', 2), ('3', 3)]


def test_slots_resolves_all_book_orders_in_one_call():
    live = [order('a'), order('b'), order('c')]
    store = order_boundary.OrderStore(live)
    assert store.slots(live) == [0, 1, 2]


def test_slot_is_stable_across_tail_append():
    """Дописывание в хвост не сдвигает номера: версия раскладки та же."""
    first = order('a')
    store = order_boundary.OrderStore([first])
    version = store.version
    store.append(order('b'))
    assert store.version == version
    assert store.slots([first]) == [0]
    assert list(store.iter_live())[0] == (first, 0)


def test_remove_shifts_layout_and_bumps_version():
    """Структурная правка сдвигает номера — её обязан заметить version."""
    first, second, third = order('a'), order('b'), order('c')
    store = order_boundary.OrderStore([first, second, third])
    version = store.version
    store.remove(first)
    assert store.version != version
    assert store.slots([second, third]) == [0, 1]


# --- 2. Представление читает колонки, а не словарь -----------------------
def test_view_reads_remaining_from_column_not_dict():
    """Источник остатка — колонка: прямая подмена dict её не меняет.

    Граница — источник истины: колонка и dict держатся равными только через
    ``set_*``. Поэтому подмена dict напрямую не должна менять выдачу, пока
    книга не пересобрана.
    """
    store, book = view()
    live = book['options'][1][1]
    rows = list(offer_boundary.OfferView(book, 'buyer', 'p1'))
    assert [row[1]['id'] for row in rows] == ['a', 'b']
    # Подмена словаря в обход границы не видна представлению: читается колонка.
    live['remaining'] = 0.0
    assert store.remaining[1] == 5.0
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a', 'b']
    # Граница обнуляет колонку — оффер пропадает.
    store.set_remaining(live, 0.0)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a']


def test_view_reads_status_from_column():
    store, book = view()
    live = book['options'][0][1]
    store.set_status(live, 'cancelled')
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['b']


def test_view_sees_mutation_between_iterations_immediately():
    store, book = view()
    live = book['options'][1][1]
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a', 'b']
    store.set_remaining(live, 0.0)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a']
    # Возврат остатка снова делает оффер видимым: курсор стоит на первом.
    store.set_remaining(live, 2.0)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a', 'b']
    assert book['first_live'] == 0


def test_book_slots_are_bound_once_and_reused():
    """Номера считаются при первом обходе и переиспользуются дальше."""
    store, book = view()
    assert book['slots'] is None
    list(offer_boundary.OfferView(book, 'buyer', 'p1'))
    slots = book['slots']
    assert slots == [0, 1]
    list(offer_boundary.OfferView(book, 'buyer', 'p1'))
    # Второй обход не пересчитывает номера.
    assert book['slots'] is slots


# --- 3. Exhausted offer не воскресает после движения курсора -------------
def test_exhausted_offer_is_not_resurrected_after_cursor_moves():
    first, second = order('a', remaining=5.0), order('b', remaining=5.0)
    store = order_boundary.OrderStore([first, second])
    book = book_for(store, first, second)
    shown = offer_boundary.OfferView(book, 'buyer', 'p1')
    assert [row[1]['id'] for row in shown] == ['a', 'b']
    # Первый выкуплен полностью: курсор уходит за него.
    store.set_remaining(first, 0.0)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['b']
    assert book['first_live'] == 1
    # Возврат остатка не воскрешает оффер: курсор двигается только вперёд.
    store.set_remaining(first, 3.0)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['b']
    assert book['first_live'] == 1


def test_first_live_cursor_records_scan_result():
    dead, live = order('dead', remaining=0.0), order('live')
    store = order_boundary.OrderStore([dead, live])
    book = book_for(store, dead, live)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['live']
    assert book['first_live'] == 1


# --- 4. Снимок planning-книги: заявки без слота --------------------------
def test_orders_without_slot_fall_back_to_dict():
    """Копии planning-снимка не принадлежат хранилищу: читается их словарь."""
    live = order('a')
    store = order_boundary.OrderStore([live])
    snapshot = order('copy', remaining=4.0)
    # В книгу попадают обе заявки: живая из хранилища и копия снимка.
    book = book_for(store, live, snapshot)
    rows = list(offer_boundary.OfferView(book, 'buyer', 'p1'))
    assert [row[1]['id'] for row in rows] == ['a', 'copy']
    # Живая заявка получила слот, снимок — нет: источник значений словарь.
    assert book['slots'] == [0, -1]


def test_mixed_book_reads_both_sources():
    """Живая заявка + снимок: каждый читается из своего источника."""
    live = order('live', remaining=5.0)
    store = order_boundary.OrderStore([live])
    snapshot = order('snap', remaining=5.0)
    book = book_for(store, live, snapshot)
    view_ = offer_boundary.OfferView(book, 'buyer', 'p1')
    assert [row[1]['id'] for row in view_] == ['live', 'snap']
    # Граница гасит живую заявку, а снимок продолжает жить своим словарём.
    store.set_remaining(live, 0.0)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['snap']
    snapshot['remaining'] = 0.0
    assert list(offer_boundary.OfferView(book, 'buyer', 'p1')) == []


# --- 5. Смена раскладки: номера пересчитываются --------------------------
def test_new_store_invalidates_cached_slots():
    """Другое хранилище в книге: номер пересчитывается, выдача берётся из него."""
    first = order('a', remaining=5.0)
    store = order_boundary.OrderStore([first])
    # Заявка книги принадлежит и первому, и второму хранилищу.
    fresh = order_boundary.OrderStore([first, order('b', remaining=5.0)])
    book = book_for(store, first)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a']
    assert book['slot_version'] == store.version
    # Книгу перепривязали к другому хранилищу: номера обязаны пересчитаться.
    book['slot_store'] = fresh
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a']
    assert book['slot_version'] == fresh.version


def test_layout_change_invalidates_cached_slots():
    """remove сдвигает номера: сверка с version пересчитывает их."""
    first, second = order('a', remaining=5.0), order('b', remaining=5.0)
    store = order_boundary.OrderStore([first, second])
    book = book_for(store, first, second)
    assert [row[1]['id'] for row in offer_boundary.OfferView(book, 'buyer', 'p1')] == ['a', 'b']
    version = book['slot_version']
    # Из середины убрали заявку: номера сдвинулись, версия изменилась.
    store.remove(first)
    assert store.version != version
    # Книга после правки содержит только уцелевшую заявку.
    book['options'] = [row for row in book['options'] if row[1] is second]
    book['first_live'] = 0
    rows = list(offer_boundary.OfferView(book, 'buyer', 'p1'))
    assert [row[1]['id'] for row in rows] == ['b']
    # Номер пересчитан по новой раскладке: заявка теперь в слоте 0.
    assert book['slots'] == [0]
    assert book['slot_version'] == store.version
    assert store.remaining[0] == 5.0


# --- 6. Python compatibility dict продолжает отражать изменения -----------
def test_dict_stays_in_sync_with_columns():
    store, book = view()
    live = book['options'][0][1]
    store.set_remaining(live, 3.5)
    store.set_status(live, 'partial')
    assert store.remaining[0] == 3.5 and store.status[0] == 'partial'
    assert live['remaining'] == 3.5 and live['status'] == 'partial'


def test_view_output_keeps_execution_semantics():
    """Тройка остаётся исходной: цена, заявка и котировка не копируются."""
    first = order('a', remaining=5.0)
    store = order_boundary.OrderStore([first])
    quote = (1.5, 0.25, 12, ('t1', 't2'))
    book = dict(source=[first], options=[(1.5, first, quote)], quotes={},
                board_id=None, first_live=0, slot_store=store, slots=None,
                slot_version=None)
    price, sell, got_quote = next(iter(offer_boundary.OfferView(book, 'buyer', 'p1')))
    assert price == 1.5 and sell is first and got_quote is quote


def test_python_reference_view_matches_native_on_columns():
    store, book = view()
    # У эталона своё хранилище над копиями заявок: сравнивать можно только
    # значения, поэтому копии получают те же поля, что и оригиналы.
    reference_store = order_boundary.OrderStorePython(
        [dict(o) for o in store.orders])
    reference_book = book_for(reference_store,
                             *[dict(o) for o in store.orders])
    native = [(p, s['id'], s['remaining'], s['status'])
              for p, s, _ in offer_boundary.OfferView(book, 'buyer', 'p1')]
    reference = [(p, s['id'], s['remaining'], s['status'])
                 for p, s, _ in offer_boundary.OfferViewPython(reference_book, 'buyer', 'p1')]
    assert native == reference


# --- 7. Реальный движок: книга получает хранилище ------------------------
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


def test_engine_book_carries_store_and_slots():
    engine = build_engine()
    sells = engine.sell_orders()
    book = engine._market_book(engine.accounts['h'], 'grain', sells)
    assert book['slot_store'] is engine.order_store
    assert book['slots'] is None
    list(offer_boundary.OfferView(book, 'h', 'p'))
    assert book['slots'] is not None
    assert len(book['slots']) == len(book['options'])


def test_engine_view_call_sites_use_the_bound_book():
    engine = build_engine()
    sells = engine.sell_orders()
    buyer = engine.accounts['h']
    view_ = engine.offers(buyer, 'grain', sells)
    rows = list(view_)
    book = engine._market_book(buyer, 'grain', sells)
    assert book['slot_store'] is engine.order_store
    # Выдача представления — ровно тройки книги, без копий.
    assert rows == book['options']


def test_real_month_view_matches_direct_columns():
    """За месяц представление и прямое чтение колонок дают одно и то же."""
    engine = build_engine()
    engine.step()
    store = engine.order_store
    store.sync()
    sells = engine.sell_orders()
    for buyer in list(engine.accounts.values())[:4]:
        for good in sorted(engine.goods)[:4]:
            book = engine._market_book(buyer, good, sells)
            money = offer_boundary.OfferView(book, buyer['id'], buyer['province_id'])
            for price, sell, _ in money:
                slot = store.slots([sell])[0]
                assert slot >= 0
                assert store.remaining[slot] == sell['remaining']
                assert store.status[slot] == sell['status']
                assert store.price[slot] == sell['price']


# --- 8. Копия движка: своё хранилище и свои номера -----------------------
def test_deepcopy_engine_gets_its_own_slots():
    engine = build_engine()
    engine.step()
    sells = engine.sell_orders()
    buyer = engine.accounts['h']
    engine._market_book(buyer, 'grain', sells)
    clone = copy.deepcopy(engine)
    clone_store = clone.order_store
    assert clone_store is not engine.order_store
    clone_sells = clone.sell_orders()
    book = clone._market_book(clone.accounts['h'], 'grain', clone_sells)
    assert book['slot_store'] is clone_store
    for price, sell, _ in offer_boundary.OfferView(book, 'h', 'p'):
        slot = clone_store.slots([sell])[0]
        assert slot >= 0
        assert clone_store.remaining[slot] == sell['remaining']
