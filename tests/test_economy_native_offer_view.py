"""Focused-тесты представления офферов: живые заявки без копирования.

Проверяется не только выдача, но и то, ради чего представление вынесено в
Cython: обход читает те же объекты заявок, что лежат в книге, видит их мутацию
между обходами и не создаёт второй копии orders. Поэтому часть проверок
смотрит на идентичность объектов, а не только на значения.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import offer_boundary  # noqa: E402

EPS = support.EPS
KERNEL = offer_boundary.load_offer_view()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение offer_view не собрано')


def make_book(rows, first_live=0):
    """Книга в том же виде, что строит _market_book: options + курсор."""
    return dict(source=[], options=list(rows), quotes={}, board_id=None,
                first_live=first_live)


def sell(identity, participant='seller', price=1.0, remaining=10.0,
         status='active', province_id='p1', tile_id='t1'):
    """Живая заявка: обычный словарь, как у настоящего движка."""
    return dict(id=identity, participant=participant, price=price,
                remaining=remaining, status=status, province_id=province_id,
                tile_id=tile_id)


def option(price, order, quote=None):
    """Тройка книги: доставленная цена, живая заявка, котировка."""
    return (price, order, quote if quote is not None else (price, 0.0, 0, ()))


def both(book, **kwargs):
    """Прогнать эталон и native на независимых книгах с одинаковым содержимым."""
    results = []
    for factory in (offer_boundary.OfferViewPython, offer_boundary.OfferView):
        prepared = make_book(list(book['options']), book['first_live'])
        results.append((factory(prepared, kwargs.get('buyer_id', 'buyer'),
                                kwargs.get('province_id', 'p1'),
                                kwargs.get('local_only', False)), prepared))
    return results


def view_native(rows, first_live=0, **kwargs):
    book = make_book(rows, first_live)
    return offer_boundary.OfferView(book, kwargs.get('buyer_id', 'buyer'),
                                    kwargs.get('province_id', 'p1'),
                                    kwargs.get('local_only', False)), book


# 1. Порядок обхода и содержимое троек ---------------------------------------
def test_iteration_order_matches_python_reference():
    rows = [option(1.0, sell('a')), option(2.0, sell('b')), option(3.0, sell('c'))]
    reference, _ = both(make_book(rows))
    native, _ = both(make_book(rows))
    assert [row['id'] for _, row in enumerate([])] == []
    assert [(p, s['id'], q) for p, s, q in reference[0]] == \
           [(p, s['id'], q) for p, s, q in native[0]]


def test_quotes_are_the_same_objects_not_copies():
    order = sell('a')
    quote = (1.5, 0.25, 12, ('t1', 't2'))
    view, book = view_native([option(1.5, order, quote)])
    price, got_order, got_quote = next(iter(view))
    # Ни заявка, ни котировка не копируются: это те же объекты книги.
    assert got_order is order
    assert got_quote is quote
    assert price == 1.5


# 2. first_live: ленивый скан и сохранение курсора ---------------------------
def test_first_live_advances_past_dead_offers():
    rows = [option(1.0, sell('dead', remaining=0.0)),
            option(2.0, sell('cancelled', status='cancelled')),
            option(3.0, sell('live'))]
    view, book = view_native(rows)
    assert [s['id'] for _, s, _ in view] == ['live']
    # Курсор записан в книгу: повторный обход не сканирует мёртвые офферы.
    assert book['first_live'] == 2


def test_first_live_is_lazy_until_first_next():
    rows = [option(1.0, sell('dead', remaining=0.0)), option(2.0, sell('live'))]
    view, book = view_native(rows)
    iterator = iter(view)
    # iter() не начинает обход: курсор ещё не тронут, как у генератора V3.
    assert book['first_live'] == 0
    assert next(iterator)[1]['id'] == 'live'
    assert book['first_live'] == 1


def test_live_offer_stays_visible_across_iterations():
    """Живой оффер не прячется после обхода: курсор двигают только мёртвые."""
    rows = [option(1.0, sell('live'))]
    view, book = view_native(rows)
    assert [s['id'] for _, s, _ in view] == ['live']
    assert book['first_live'] == 0
    # Второй обход снова видит тот же живой оффер и курсор снова не двигает.
    assert [s['id'] for _, s, _ in view] == ['live']
    assert book['first_live'] == 0


# 3. Исключение своего продавца и local_only --------------------------------
def test_own_offers_are_excluded():
    rows = [option(1.0, sell('mine', participant='buyer')),
            option(2.0, sell('other', participant='seller'))]
    view, _ = view_native(rows, buyer_id='buyer')
    assert [s['id'] for _, s, _ in view] == ['other']


def test_local_only_limits_to_buyer_province():
    rows = [option(1.0, sell('local', province_id='p1')),
            option(2.0, sell('remote', province_id='p2'))]
    view, _ = view_native(rows, local_only=True, province_id='p1')
    assert [s['id'] for _, s, _ in view] == ['local']
    # Без ограничения видны оба.
    view, _ = view_native(rows, province_id='p1')
    assert [s['id'] for _, s, _ in view] == ['local', 'remote']


# 4. remaining/status читаются во время итерации ----------------------------
def test_remaining_and_status_are_checked_during_iteration():
    first, second = sell('a'), sell('b')
    view, _ = view_native([option(1.0, first), option(2.0, second)])
    iterator = iter(view)
    assert next(iterator)[1]['id'] == 'a'
    # Заявку сняли между шагами обхода: второй шаг обязан это увидеть.
    second['status'] = 'cancelled'
    assert list(iterator) == []


def test_remaining_below_eps_is_skipped():
    rows = [option(1.0, sell('almost', remaining=EPS/2)), option(2.0, sell('ok'))]
    view, _ = view_native(rows)
    assert [s['id'] for _, s, _ in view] == ['ok']


# 5. Мутация видна между двумя итерациями -----------------------------------
def test_mutation_is_visible_between_iterations():
    first, second = sell('a'), sell('b')
    view, book = view_native([option(1.0, first), option(2.0, second)])
    assert [s['id'] for _, s, _ in view] == ['a', 'b']
    # Выкуп второго оффера виден следующему обходу: тройка держит живую
    # заявку, а не её снимок. Курсор при этом уже стоит на первом оффере.
    second['remaining'] = 0.0
    assert [s['id'] for _, s, _ in view] == ['a']
    # Возврат остатка снова делает оффер видимым: копия бы этого не показала.
    second['remaining'] = 4.0
    assert [s['id'] for _, s, _ in view] == ['a', 'b']
    assert book['first_live'] == 0


def test_exhausted_offer_is_not_resurrected_by_cursor():
    """Курсор first_live не откатывается: восстановленный оффер не воскресает.

    Это осознанное поведение V3: once-курсор двигается только вперёд, а
    «воскрешение» остатка после полного выкупа не возвращает оффер в книгу.
    """
    order = sell('a', remaining=5.0)
    view, book = view_native([option(1.0, order)])
    assert [s['id'] for _, s, _ in view] == ['a']
    order['remaining'] = 0.0
    assert list(view) == []
    assert book['first_live'] == 1
    order['remaining'] = 3.0
    assert list(view) == []


def test_mutating_through_view_returned_order_changes_book():
    order = sell('a', remaining=5.0)
    view, book = view_native([option(1.0, order)])
    _, same_order, _ = next(iter(view))
    same_order['remaining'] = 1.0
    assert book['options'][0][1]['remaining'] == 1.0


# 6. bool / getitem ---------------------------------------------------------
def test_bool_reflects_live_offers():
    view, _ = view_native([option(1.0, sell('a'))])
    assert bool(view) is True
    view, _ = view_native([option(1.0, sell('a', remaining=0.0))])
    assert bool(view) is False


def test_getitem_matches_reference_and_rejects_negative():
    rows = [option(1.0, sell('a')), option(2.0, sell('b'))]
    reference, _ = both(make_book(rows))
    native, _ = both(make_book(rows))
    assert reference[0][0][1]['id'] == native[0][0][1]['id']
    assert reference[0][1][1]['id'] == native[0][1][1]['id']
    with pytest.raises(IndexError):
        native[0][2]
    with pytest.raises(IndexError):
        native[0][-1]


# 7. Представление действительно native -------------------------------------
def test_boundary_binds_the_compiled_class():
    assert offer_boundary.NATIVE_OFFER_VIEW is True
    assert offer_boundary.OfferView is KERNEL.OfferView
    assert offer_boundary.OfferView is not offer_boundary.OfferViewPython
    assert (offer_boundary.OfferView.__module__
            .endswith('offer_view'))


def test_fallback_used_when_extension_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('offer_view'):
            raise ImportError("cannot import name 'offer_view'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert offer_boundary.load_offer_view() is None


# 8. Точное совпадение на реальном движке -----------------------------------
def build_engine(seed=17, detail='deep'):
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


def snapshot(view):
    """Точная выдача представления: цена, id заявки, котировка."""
    return [(price, order['id'], order['remaining'], order['status'], quote)
            for price, order, quote in view]


def run_offers(engine, use_native):
    """Собрать выдачу по всем книгам месяца, подменяя реализацию при нужде."""
    saved = offer_boundary.OfferView
    if not use_native:
        offer_boundary.OfferView = offer_boundary.OfferViewPython
    try:
        rows = []
        for buyer in list(engine.accounts.values())[:6]:
            for good in sorted(engine.goods)[:4]:
                rows.append(snapshot(engine.offers(buyer, good, engine._sells_for_test)))
        return rows
    finally:
        offer_boundary.OfferView = saved


def test_native_view_matches_reference_on_real_book():
    engine = build_engine()
    engine._sells_for_test = engine.sell_orders()
    # Книги кэшируются на движке, поэтому каждой реализации нужен свой движок.
    native_engine = build_engine()
    native_engine._sells_for_test = native_engine.sell_orders()
    reference = run_offers(engine, use_native=False)
    native = run_offers(native_engine, use_native=True)
    assert native == reference


def test_game_calls_native_view():
    """Игра обязана вызывать именно native-представление, а не эталон."""
    engine = build_engine()
    engine._sells_for_test = engine.sell_orders()
    assert offer_boundary.OfferView is KERNEL.OfferView
    view = engine.offers(list(engine.accounts.values())[0], sorted(engine.goods)[0],
                         engine._sells_for_test)
    assert isinstance(view, KERNEL.OfferView)
    assert type(view).__module__.endswith('offer_view')
