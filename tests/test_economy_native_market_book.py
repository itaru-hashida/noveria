"""Рыночная книга в Cython: exact-порядок, landed price и fallback.

Тест держит три вещи, которые легко потерять при переносе в ядро:

  * порядок книги — ключ строго ``(landed_price, sell_id)``, включая
    tie-break одинаковых цен по id;
  * ``landed`` — прежние типы, прежний кэш движка и прежняя ветка
    недостижимого маршрута (маршрут запоминается как ``False``);
  * отсутствие побочных эффектов — RNG не расходуется, состояние заявок не
    меняется, ``deepcopy`` даёт независимые книги.

Отдельно проверяется fallback без ``.pyd``: эталон из
``market_book_boundary`` обязан выдавать то же самое, что ядро, поэтому
сравнение «Python reference == Cython» идёт по значениям и по порядку.
"""
import copy
import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import market_book_boundary as boundary  # noqa: E402
from modules.economy.native import offer_boundary  # noqa: E402

EPS = support.EPS

# Exact-отпечатки trajectory, снятые на Python-коде V3 до переноса книги в
# Cython. Совпадение означает, что порядок сделок и RNG не изменились.
GOLDEN = {
    17: ['c25f33906413c6df6b8bddf7be0889379167819adf0f0e565474cdc229cde598',
         '80d7df449d76d9a663551b4bdc03165535af8b58d40ab31f5488dcb75cec9f54',
         '077b775c565b045cb020ca88996723603c290d5b5c20cdcf3b5edd3b9f078f1b'],
    73129: ['35b1b396eb67f7875e64111216b94c7b6362bc9f2d72e3e74f82a55a81d3023c',
            '73ca4036f8bba68df821b9bee237291738788cfcd77791449a2a1c5e4bc154cd',
            'd8446744ebd07232f7995c4035f5f1dae298d0373132d54e972f7411ef76f531'],
}


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


# ---------------------------------------------------------------------------
# Синтетический движок: маленькая карта под конкретный сценарий книги.
# ---------------------------------------------------------------------------
def tile(identity, x, y, passable=True, province='p'):
    return dict(id=identity, x=x, y=y, passable=passable, cost=1, road=True,
                province_id=province, resources=['grain'], terrain='plain')


def make_engine(tiles=None, seed=17, detail='none', cities=None):
    cfg = json.loads((ROOT / 'tests/fixtures/economy.json').read_text(
        encoding='utf-8'))
    tiles = tiles or [tile('t1', 0, 0), tile('t2', 1, 0)]
    entities = [dict(id='s', kind='State', name='S',
                     properties={'capital_id': 'c1'}),
                dict(id='p', kind='Province', name='P',
                     properties={'capital_id': 'c1'}),
                dict(id='c1', kind='City', name='C1',
                     properties={'province_id': 'p', 'tile_id': 't1'}),
                dict(id='h', kind='SocialGroup', name='H',
                     properties={'owner_id': 'p', 'population': 100000})]
    entities.extend(cities or [])
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c1')]
    engine = Economy(dict(entities=entities, links=links), tiles,
                     meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def sell(identity, tile_id, price, remaining=10.0, participant='seller',
         good='grain', province_id='p', status='active'):
    """Живая заявка продавца: обычный словарь, как у настоящего движка."""
    return dict(id=identity, participant=participant, good=good,
                quantity=remaining, remaining=remaining, reserved=0.0,
                status=status, price=price, side='sell', province_id=province_id,
                tile_id=tile_id)


def sells_for(*orders, good='grain'):
    return {good: list(orders)}


def book_of(engine, orders, good='grain', buyer='h'):
    return engine._market_book(engine.accounts[buyer], good,
                               sells_for(*orders, good=good))


def ids(book):
    return [row[1]['id'] for row in book['options']]


def prices(book):
    return [row[0] for row in book['options']]


# --- 1. Форма книги -------------------------------------------------------
def test_empty_book_has_no_options():
    engine = make_engine()
    sells = {'grain': []}
    book = engine._market_book(engine.accounts['h'], 'grain', sells)
    assert book['options'] == []
    assert book['quotes'] == {}
    assert book['source'] is sells['grain']
    assert book['first_live'] == 0
    assert book['board_id'] is None
    assert list(offer_boundary.OfferView(book, 'h', 'p')) == []


def test_missing_good_is_an_empty_book():
    """Отсутствующий товар — пустая книга, а не ошибка."""
    engine = make_engine()
    book = engine._market_book(engine.accounts['h'], 'grain', {})
    assert book['options'] == []
    assert book['source'] == []


def test_one_order_book():
    engine = make_engine()
    only = sell('a', 't1', 3.0)
    book = book_of(engine, [only])
    assert ids(book) == ['a']
    assert prices(book) == [3.0]
    # Тройка хранит ровно тот объект заявки: копий нет.
    assert book['options'][0][1] is only
    assert book['quotes']['a'] == book['options'][0][2]


def test_several_orders_sorted_by_price():
    engine = make_engine()
    orders = [sell('c', 't1', 9.0), sell('a', 't1', 4.0), sell('b', 't1', 7.0)]
    book = book_of(engine, orders)
    assert ids(book) == ['a', 'b', 'c']
    assert prices(book) == [4.0, 7.0, 9.0]


def test_options_carry_landed_quote_shape():
    """Котировка — прежняя четвёрка (landed, freight, distance, path)."""
    engine = make_engine()
    book = book_of(engine, [sell('a', 't2', 5.0)])
    price, row, quote = book['options'][0]
    assert price == quote[0]
    assert row['id'] == 'a'
    assert len(quote) == 4
    assert quote[1] == engine.landed('t2', 't1', 'grain', 0)[1]
    assert isinstance(quote[2], (int, float))
    assert quote[3] == ['t2', 't1']


# --- 2. Разные площадки, разные цены, freight ----------------------------
def test_different_tiles_are_reachable_and_add_freight():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    near = sell('near', 't1', 4.0)
    far = sell('far', 't2', 4.0)
    book = book_of(engine, [far, near])
    # Доставка у дальней площадки больше — она идёт второй при равной цене.
    assert ids(book) == ['near', 'far']
    assert prices(book)[0] < prices(book)[1]


def test_different_prices_win_over_freight_order():
    """Сортировка идёт по landed price, а не по цене продавца."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    cheap_far = sell('far', 't2', 4.0)
    pricey_near = sell('near', 't1', 4.5)
    book = book_of(engine, [pricey_near, cheap_far])
    assert ids(book) == ['far', 'near']


def test_freight_matches_landed_arithmetic():
    """Freight книги — тот же, что считает landed: distance*weight*rate."""
    engine = make_engine([tile('t1', 0, 0), tile('tm', 1, 0), tile('t2', 2, 0)])
    order = sell('a', 't2', 6.0)
    book = book_of(engine, [order])
    price, _, quote = book['options'][0]
    distance, _ = engine.path('t2', 't1')
    weight, rate = engine.freight_factors('grain')
    expected_freight = distance*weight*rate
    assert distance > 0, 'нужен маршрут длиной больше одного ребра'
    assert quote[1] == expected_freight
    assert price == 6.0+expected_freight
    assert quote[2] == distance


def test_unreachable_route_is_excluded():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0, passable=False)])
    reachable = sell('ok', 't1', 5.0)
    blocked = sell('no', 't2', 5.0)
    book = book_of(engine, [reachable, blocked])
    assert ids(book) == ['ok']
    assert 'no' not in book['quotes']


def test_unreachable_only_book_is_empty():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0, passable=False)])
    book = book_of(engine, [sell('no', 't2', 5.0)])
    assert book['options'] == []


# --- 3. Tie-break по id ---------------------------------------------------
def test_equal_prices_tie_break_by_id():
    engine = make_engine()
    orders = [sell('c', 't1', 5.0), sell('a', 't1', 5.0), sell('b', 't1', 5.0)]
    book = book_of(engine, orders)
    assert ids(book) == ['a', 'b', 'c']
    assert prices(book) == [5.0, 5.0, 5.0]


def test_tie_break_is_not_insertion_order():
    """Порядок входа не должен протекать в книгу при равных ценах."""
    engine = make_engine()
    forward = book_of(engine, [sell('a', 't1', 5.0), sell('b', 't1', 5.0)])
    reverse = book_of(engine, [sell('b', 't1', 5.0), sell('a', 't1', 5.0)])
    assert ids(forward) == ids(reverse) == ['a', 'b']


def test_tie_break_between_tiles_uses_price_then_id():
    """Одинаковый landed price с разных площадок разрешается по id."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    # У дальней площадки цена ниже ровно на доставку.
    freight = engine.landed('t2', 't1', 'grain', 0)[1]
    near = sell('z-near', 't1', 5.0)
    far = sell('a-far', 't2', 5.0-freight)
    book = book_of(engine, [near, far])
    assert prices(book)[0] == prices(book)[1]
    assert ids(book) == ['a-far', 'z-near']


# --- 4. Кэш книги: hit, miss и инвалидация -------------------------------
def test_cache_hit_returns_the_same_entry():
    engine = make_engine()
    sells = sells_for(sell('a', 't1', 4.0))
    first = engine._market_book(engine.accounts['h'], 'grain', sells)
    second = engine._market_book(engine.accounts['h'], 'grain', sells)
    assert first is second
    assert first['quote_epoch'] == engine.__dict__['_market_quotes_epoch']


def test_cache_miss_on_new_sell_list():
    engine = make_engine()
    buyer = engine.accounts['h']
    first = engine._market_book(buyer, 'grain', sells_for(sell('a', 't1', 4.0)))
    second = engine._market_book(buyer, 'grain', sells_for(sell('a', 't1', 4.0)))
    assert first is not second


def test_cache_key_includes_buyer_tile():
    """Разные площадки покупателя — разные книги."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    sells = sells_for(sell('a', 't1', 4.0))
    buyer = engine.accounts['h']
    first = engine._market_book(buyer, 'grain', sells)
    other = dict(buyer, id='h2', cell_id='t2', tile_id='t2')
    second = engine._market_book(other, 'grain', sells)
    assert first is not second
    assert prices(first) != prices(second)


def test_invalidate_market_books_drops_the_book():
    engine = make_engine()
    sells = sells_for(sell('a', 't1', 4.0))
    buyer = engine.accounts['h']
    first = engine._market_book(buyer, 'grain', sells)
    engine.invalidate_market_books()
    second = engine._market_book(buyer, 'grain', sells)
    assert first is not second
    assert ids(second) == ['a']


def test_stale_epoch_forces_a_rebuild():
    """Подмена эпохи котировок делает старую книгу негодной."""
    engine = make_engine()
    sells = sells_for(sell('a', 't1', 4.0))
    buyer = engine.accounts['h']
    stale = engine._market_book(buyer, 'grain', sells)
    engine.__dict__['_market_quotes_epoch'] = (
        engine.__dict__.get('_market_quotes_epoch', 0)+1)
    fresh = engine._market_book(buyer, 'grain', sells)
    assert stale is not fresh
    assert fresh['quote_epoch'] == engine.__dict__['_market_quotes_epoch']
    assert ids(fresh) == ids(stale)


def test_stale_source_forces_a_rebuild():
    """Книга от другого списка офферов не переиспользуется."""
    engine = make_engine()
    buyer = engine.accounts['h']
    first = engine._market_book(buyer, 'grain', sells_for(sell('a', 't1', 4.0)))
    # Тот же ключ, но список заменён на месте: источник больше не тот.
    sells = sells_for(sell('a', 't1', 4.0))
    engine._market_books[(id(sells['grain']), 't1', 'grain')] = first
    second = engine._market_book(buyer, 'grain', sells)
    assert second is not first


def test_topology_change_is_visible_in_the_book():
    """Топология задаёт книгу: закрытый тайл не попадает в офферы.

    Граф маршрутов строится при сборке мира, поэтому «изменение топологии» —
    это другой мир с другой картой. Тест заодно фиксирует, что книга не
    тащит через себя маршруты прошлой карты.
    """
    closed = make_engine([tile('t1', 0, 0), tile('t2', 1, 0, passable=False)])
    opened = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    order = sell('no', 't2', 5.0)
    assert ids(book_of(closed, [order])) == []
    assert ids(book_of(opened, [order])) == ['no']


def test_route_invalidation_drops_component_index():
    """invalidate_routes сбрасывает индекс компонент, которым живёт книга.

    Индекс строится только на карте из нескольких компонент: при одной
    компоненте ``_orders_in_route_component`` возвращает список как есть.
    """
    engine = make_engine([tile('t1', 0, 0), tile('t2', 5, 5)])
    assert engine.route_component('t1') != engine.route_component('t2')
    sells = sells_for(sell('a', 't1', 4.0), sell('b', 't2', 6.0))
    buyer = engine.accounts['h']
    engine._orders_in_route_component(sells['grain'], 't1')
    assert '_route_order_components' in engine.__dict__
    engine.invalidate_routes()
    assert '_route_order_components' not in engine.__dict__
    engine.invalidate_market_books()
    # Дальняя компонента в книгу покупателя не попадает ни до, ни после сброса.
    assert ids(engine._market_book(buyer, 'grain', sells)) == ['a']


def test_price_change_after_invalidation_is_visible():
    engine = make_engine()
    buyer = engine.accounts['h']
    order = sell('a', 't1', 4.0)
    sells = sells_for(order)
    assert prices(engine._market_book(buyer, 'grain', sells)) == [4.0]
    order['price'] = 2.0
    engine.invalidate_market_books()
    assert prices(engine._market_book(buyer, 'grain', sells)) == [2.0]


# --- 5. Landed: типы, кэш, недостижимость, отсутствие мутаций ------------
def test_landed_types_stay_exact():
    """Типы прежние: цена остаётся объектом цены, доставка — float."""
    engine = make_engine()
    quote = engine.landed('t1', 't1', 'grain', 3)
    landed_price, freight, distance, path = quote
    # price+freight никогда не даёт int: freight всегда float.
    assert landed_price == 3.0 and isinstance(landed_price, float)
    assert freight == 0.0 and isinstance(freight, float)
    assert distance == 0 and type(distance) is int
    assert path == ['t1']


def test_landed_int_and_float_parity():
    """int-цена и float-цена той же величины дают одну доставку."""
    engine = make_engine([tile('t1', 0, 0), tile('tm', 1, 0), tile('t2', 2, 0)])
    int_quote = engine.landed('t2', 't1', 'grain', 5)
    float_quote = engine.landed('t2', 't1', 'grain', 5.0)
    assert int_quote[1:] == float_quote[1:]
    assert float_quote[0] == 5.0+int_quote[1]


def test_landed_unreachable_returns_none_and_remembers_it():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0, passable=False)])
    assert engine.landed('t2', 't1', 'grain', 5.0) is None
    # Недостижимость закэширована как False: повторный вызов тоже None.
    assert engine.__dict__['_landed_cache'][('t2', 't1', 'grain')] is False
    assert engine.landed('t2', 't1', 'grain', 5.0) is None


def test_landed_cache_is_the_engine_cache():
    engine = make_engine([tile('t1', 0, 0), tile('tm', 1, 0), tile('t2', 2, 0)])
    engine.__dict__.pop('_landed_cache', None)
    assert '_landed_cache' not in engine.__dict__
    assert engine.landed('t2', 't1', 'grain', 5.0) is not None
    cache = engine.__dict__['_landed_cache']
    assert ('t2', 't1', 'grain') in cache
    # Второй вызов не пересчитывает маршрут: значение в кэше ровно одно.
    engine.landed('t2', 't1', 'grain', 7.0)
    assert len(cache) == 1


def test_landed_does_not_mutate_orders_or_accounts():
    engine = make_engine()
    order = sell('a', 't1', 4.0)
    before = digest(order)
    engine.landed('t1', 't1', 'grain', order['price'])
    assert digest(order) == before


def test_landed_repeated_calls_are_stable():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 3, 0)])
    first = engine.landed('t2', 't1', 'grain', 2.5)
    for _ in range(5):
        assert engine.landed('t2', 't1', 'grain', 2.5) == first


# --- 6. Python reference == Cython ---------------------------------------
def reference_book(engine, orders, good='grain', buyer='h'):
    """Собрать книгу эталоном границы, не трогая кэш книги движка."""
    sell_list = list(orders)
    options = boundary.build_options_python(
        engine, sell_list, engine.market_tile(engine.accounts[buyer]), good,
        engine.landed)
    return options


def native_book(engine, orders, good='grain', buyer='h'):
    sell_list = list(orders)
    return boundary.build_options(
        engine, sell_list, engine.market_tile(engine.accounts[buyer]), good,
        engine.landed)


def shape(options):
    """Сравнимая форма: цена, id, котировка и тождество заявок."""
    return [(price, row['id'], quote) for price, row, quote in options]


@pytest.mark.parametrize('seed', [17, 73129])
def test_python_reference_matches_native_on_real_month(seed):
    engine = make_engine(seed=seed)
    engine.step()
    sells = engine.sell_orders()
    checked = 0
    for buyer in list(engine.accounts.values())[:4]:
        for good in sorted(sells):
            if not sells[good]:
                continue
            reference = shape(reference_book(engine, sells[good], good,
                                             buyer['id']))
            native = shape(native_book(engine, sells[good], good, buyer['id']))
            assert native == reference
            checked += 1
    assert checked > 10, 'слишком мало проверенных книг'


def test_python_reference_matches_native_with_ties():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    orders = [sell('c', 't1', 5.0), sell('a', 't2', 5.0 - 0.0028),
              sell('b', 't1', 5.0), sell('d', 't2', 1.0)]
    assert shape(native_book(engine, orders)) == \
        shape(reference_book(engine, orders))


def test_python_reference_matches_native_on_large_branch():
    """Большая ветвь (>= 1000 офферов) идёт через heapq.merge."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    orders = []
    for index in range(600):
        orders.append(sell('n%04d' % index, 't1', 1.0+(index % 7)))
        orders.append(sell('f%04d' % index, 't2', 1.0+(index % 5)))
    assert len(orders) >= 1000
    assert shape(native_book(engine, orders)) == \
        shape(reference_book(engine, orders))


def test_reference_landed_matches_native():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 2, 0)])
    cases = [('t1', 't1', 'grain', 5), ('t2', 't1', 'grain', 5.0),
             ('t1', 't2', 'grain', 7.5), ('t2', 't2', 'grain', 1)]
    for seller, buyer, good, price in cases:
        assert boundary.landed(engine, seller, buyer, good, price) == \
            boundary.landed_python(engine, seller, buyer, good, price)


def test_reference_and_native_agree_on_unreachable():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0, passable=False)])
    assert boundary.landed(engine, 't2', 't1', 'grain', 5.0) is None
    assert boundary.landed_python(engine, 't2', 't1', 'grain', 5.0) is None


def test_reference_and_native_types_match():
    engine = make_engine()
    native = boundary.landed(engine, 't1', 't1', 'grain', 3)
    reference = boundary.landed_python(engine, 't1', 't1', 'grain', 3)
    assert [type(x) for x in native] == [type(x) for x in reference]


# --- 7. OfferView получает тот же порядок --------------------------------
def test_offer_view_yields_book_order():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    orders = [sell('c', 't1', 9.0, participant='s3'),
              sell('a', 't1', 4.0, participant='s1'),
              sell('b', 't1', 7.0, participant='s2')]
    sells = sells_for(*orders)
    buyer = engine.accounts['h']
    book = engine._market_book(buyer, 'grain', sells)
    shown = list(engine.offers(buyer, 'grain', sells))
    assert [row[1]['id'] for row in shown] == ids(book)
    # Тройки те же самые: представление не копирует котировки.
    assert shown[0][2] is book['options'][0][2]


def test_offer_view_skips_own_offers_but_keeps_book_order():
    engine = make_engine()
    own = sell('own', 't1', 1.0, participant='h')
    other = sell('other', 't1', 5.0, participant='s1')
    sells = sells_for(own, other)
    buyer = engine.accounts['h']
    book = engine._market_book(buyer, 'grain', sells)
    assert ids(book) == ['own', 'other']
    assert [row[1]['id'] for row in engine.offers(buyer, 'grain', sells)] == ['other']


def test_offer_view_uses_first_live_from_build():
    engine = make_engine()
    sells = sells_for(sell('a', 't1', 4.0))
    buyer = engine.accounts['h']
    book = engine._market_book(buyer, 'grain', sells)
    assert book['first_live'] == 0
    list(engine.offers(buyer, 'grain', sells))
    assert 'first_live' in book


# --- 8. Побочные эффекты: RNG, состояние, deepcopy -----------------------
def test_building_book_does_not_touch_rng():
    engine = make_engine()
    orders = [sell('b', 't1', 5.0), sell('a', 't1', 6.0)]
    before = engine.rng.getstate()
    book_of(engine, orders)
    engine._market_book(engine.accounts['h'], 'grain', {'grain': orders})
    assert engine.rng.getstate() == before


def test_building_book_does_not_touch_global_random():
    engine = make_engine()
    random.seed(1234)
    before = random.getstate()
    book_of(engine, [sell('a', 't1', 4.0)])
    assert random.getstate() == before


def test_landed_does_not_touch_rng():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 2, 0)])
    before = engine.rng.getstate()
    engine.landed('t2', 't1', 'grain', 5.0)
    assert engine.rng.getstate() == before


def test_building_book_does_not_mutate_orders():
    engine = make_engine()
    orders = [sell('b', 't1', 5.0), sell('a', 't2', 4.0)]
    before = [digest(order) for order in orders]
    book_of(engine, orders)
    assert [digest(order) for order in orders] == before
    # remaining/status при построении не читаются как состояние книги.
    assert all(order['remaining'] == 10.0 for order in orders)
    assert all(order['status'] == 'active' for order in orders)


def test_building_book_does_not_read_remaining_or_status():
    """Книга строится и при нулевом остатке: фильтр живости — у представления."""
    engine = make_engine()
    dead = sell('dead', 't1', 4.0, remaining=0.0, status='cancelled')
    book = book_of(engine, [dead])
    assert ids(book) == ['dead']
    assert list(engine.offers(engine.accounts['h'], 'grain',
                              sells_for(dead))) == []


def test_deepcopy_gets_independent_books():
    engine = make_engine()
    sells = sells_for(sell('a', 't1', 4.0))
    buyer = engine.accounts['h']
    engine._market_book(buyer, 'grain', sells)
    clone = copy.deepcopy(engine)
    clone_sells = sells_for(sell('a', 't1', 4.0))
    clone_book = clone._market_book(clone.accounts['h'], 'grain', clone_sells)
    assert clone_book is not engine._market_books[
        (id(sells['grain']), 't1', 'grain')]
    assert clone_book['slot_store'] is clone.order_store
    assert ids(clone_book) == ['a']


def test_deepcopy_does_not_share_landed_cache():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 2, 0)])
    engine.landed('t2', 't1', 'grain', 5.0)
    clone = copy.deepcopy(engine)
    assert clone.__dict__['_landed_cache'] is not engine.__dict__['_landed_cache']
    clone.landed('t1', 't2', 'grain', 6.0)
    assert ('t1', 't2', 'grain') in clone.__dict__['_landed_cache']
    assert ('t1', 't2', 'grain') not in engine.__dict__['_landed_cache']


# --- 9. Fallback без .pyd ------------------------------------------------
def test_fallback_switches_to_python_reference(monkeypatch):
    """Без собранного ядра граница привязывает эталон, а не падает."""
    assert boundary.NATIVE_MARKET_BOOK is True, 'ожидалось собранное ядро'
    # Повторяем ту же привязку, что делает импорт границы, но без ядра.
    monkeypatch.setattr(boundary, 'load_market_book', lambda: None)
    monkeypatch.setattr(boundary, 'landed', boundary.landed_python)
    monkeypatch.setattr(boundary, 'build_options', boundary.build_options_python)
    monkeypatch.setattr(boundary, 'market_book_is_fresh',
                        boundary.market_book_is_fresh_python)
    monkeypatch.setattr(boundary, 'NATIVE_MARKET_BOOK', False)
    assert boundary.load_market_book() is None
    assert boundary.NATIVE_MARKET_BOOK is False
    assert boundary.landed is boundary.landed_python
    assert boundary.build_options is boundary.build_options_python
    # Подмена локальна для теста: движок снова получает ядро.
    monkeypatch.undo()
    assert boundary.NATIVE_MARKET_BOOK is True
    assert boundary.landed is not boundary.landed_python


def test_python_fallback_builds_the_same_book():
    """Эталон как fallback: v3 с ним даёт ту же книгу."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    orders = [sell('b', 't1', 5.0), sell('a', 't2', 4.0)]
    native = shape(native_book(engine, orders))
    reference = shape(reference_book(engine, orders))
    assert native == reference


def test_boundary_exposes_both_implementations():
    assert callable(boundary.landed) and callable(boundary.landed_python)
    assert callable(boundary.build_options)
    assert callable(boundary.build_options_python)


# --- 10. Реальный движок: forced-case и exact ----------------------------
def build_world(seed, months, detail='standard'):
    captured = {}
    original = Economy.project_requests

    def traced(self, sells):
        captured['sells'] = sells
        return original(self, sells)

    Economy.project_requests = traced
    try:
        tmp = workdir('test_market_book')
        with tmp as directory:
            db, service = sr.build_world(seed, directory,
                                         report_detail=detail)
            try:
                for month in range(months):
                    service.advance_turn(command_id='mb-test-%d-%d' % (seed, month),
                                         source='audit')
                return service.economy_state().engine, captured.get('sells', {})
            finally:
                db.close()
    finally:
        Economy.project_requests = original


@pytest.fixture(scope='module')
def world_17():
    return build_world(17, 3)


@pytest.fixture(scope='module')
def world_73129():
    return build_world(73129, 3)


def test_forced_case_real_options_keep_exact_order(world_17):
    """На реальном месяце книга и представление идут в одном порядке."""
    engine, sells = world_17
    checked = 0
    for buyer in list(engine.accounts.values())[:6]:
        for good in sorted(sells):
            if not sells[good]:
                continue
            book = engine._market_book(buyer, good, sells)
            options = book['options']
            keys = [(price, row['id']) for price, row, _ in options]
            assert keys == sorted(keys), 'книга не отсортирована по (price, id)'
            for price, row, quote in options:
                assert quote[0] == price
                assert quote is book['quotes'][row['id']]
            checked += 1
    assert checked > 20


def test_forced_case_reference_matches_engine_book(world_17):
    """Ядро движка == эталон границы на живых заявках месяца."""
    engine, sells = world_17
    buyer = next(iter(engine.accounts.values()))
    compared = 0
    for good in sorted(sells):
        if not sells[good]:
            continue
        book = engine._market_book(buyer, good, sells)
        reference = reference_book(engine, sells[good], good, buyer['id'])
        assert shape(book['options']) == shape(reference)
        compared += 1
    assert compared > 5


@pytest.mark.parametrize('seed,fixture_name', [(17, 'world_17'),
                                               (73129, 'world_73129')])
def test_exact_trajectory_is_unchanged(seed, fixture_name, request):
    engine, _ = request.getfixturevalue(fixture_name)
    assert engine_digest(engine) == GOLDEN[seed][-1]


def test_exact_seed17_three_months(world_17):
    engine, sells = world_17
    assert engine.meta['month'] == 3
    assert engine_digest(engine) == GOLDEN[17][-1]
    assert len(engine.report['trades']) == 835


def test_exact_seed73129_three_months(world_73129):
    engine, sells = world_73129
    assert engine.meta['month'] == 3
    assert engine_digest(engine) == GOLDEN[73129][-1]
    assert len(engine.report['trades']) == 853


def test_exact_rng_state(world_17, world_73129):
    for engine, seed in ((world_17[0], 17), (world_73129[0], 73129)):
        assert digest(engine.rng.getstate()) == {
            17: '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377',
            73129: 'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b',
        }[seed]
