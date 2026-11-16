"""Focused-тесты ядра спроса и прогноза продаж.

Проверяются ``customer_quotes``, ``customer_demand_groups``,
``_customer_demand_rows``, ``reachable_demand`` и ``expected_stock_sales``:
значения, типы, порядок групп и заявок, семантика кэшей движка
(``_customer_quotes_cache``, ``_customer_demand_rows_cache``,
``_stock_sales_cache``) и отсутствие мутаций отчёта.

Эталон — функции ``*_python`` в границе, fallback — те же эквиваленты при
отсутствии собранного расширения.
"""
import copy
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy, v3  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import demand_boundary as boundary  # noqa: E402

pytest.importorskip('modules.economy.native.demand_kernel')

EPS = support.EPS
FIXTURE = ROOT/'tests/fixtures/economy.json'


def build_engine(seed=17, detail='standard'):
    """Синтетический мир на общей фикстуре."""
    cfg = json.loads(FIXTURE.read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H',
             properties={'owner_id': 'p', 'population': 100000}),
        dict(id='h2', kind='SocialGroup', name='H2',
             properties={'owner_id': 'p', 'population': 50000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'лён', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def clear(engine):
    """Оставить один товар и пустые склады: рынок строится заново."""
    engine.report['orders'] = []
    engine._order_store = None
    for a in engine.accounts.values():
        a.update(stock={}, values={}, stock_reserved={})
    engine.reset_report()
    return engine


def firm(engine, good='bread'):
    return next(e for e in engine.enterprises.values() if e['good'] == good)


def typed(value):
    """Значение и его тип: 5 и 5.0 различаются так же, как в V3."""
    return (type(value).__name__, value)


def order(engine, participant, good, quantity, side, price, purpose='Базовые нужды'):
    return engine.order(participant, good, quantity, side, price, purpose)


def sell_order(oid, participant, good, price, remaining, status='active', tile='t',
               province='p', quantity=None):
    return dict(id=oid, participant=participant, good=good, price=price,
                remaining=remaining, status=status, tile_id=tile,
                province_id=province,
                quantity=remaining if quantity is None else quantity,
                side='sell', reserved=0.0)


def buy_order(oid, participant, good, price, remaining, reserved, status='active',
              tile='t', province='p', final_consumption=False):
    return dict(id=oid, participant=participant, good=good, price=price,
                remaining=remaining, status=status, tile_id=tile,
                province_id=province, quantity=remaining, reserved=reserved,
                side='buy', final_consumption=final_consumption)


# --- 1. customer_quotes -------------------------------------------------------

def test_customer_quotes_returns_price_minus_freight():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    first = order(engine, 'h', 'bread', 4, 'buy', 5.0)
    second = order(engine, 'h2', 'bread', 2, 'buy', 6.0)

    quotes = engine.customer_quotes(bakery)
    assert [q[0]['id'] for q in quotes] == [first['id'], second['id']]
    # Площадка одна, поэтому доставка нулевая и предел равен цене заявки.
    assert [q[1] for q in quotes] == [5.0, 6.0]


def test_customer_quotes_excludes_own_dead_and_empty_orders():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    own = buy_order('own', bakery['id'], 'bread', 5.0, 4, 20.0)
    dead = buy_order('dead', 'h', 'bread', 5.0, 4, 20.0, status='cancelled')
    empty = buy_order('empty', 'h2', 'bread', 5.0, 0.0, 0.0)
    alive = buy_order('alive', 'merchant:s', 'bread', 5.0, 4, 20.0)
    engine.report['orders'] = [own, dead, empty, alive]

    assert [q[0]['id'] for q in engine.customer_quotes(bakery)] == ['alive']


def test_customer_quotes_matches_python_reference_and_keeps_order():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0),
                               buy_order('b', 'h2', 'bread', 7.5, 2, 15.0)]
    native = engine.customer_quotes(bakery)
    engine.__dict__.pop('_customer_quotes_cache', None)
    reference = boundary.customer_quotes_python(engine, bakery)
    assert [(o['id'], typed(limit)) for o, limit in native] == \
           [(o['id'], typed(limit)) for o, limit in reference]


def test_customer_quotes_uses_planning_snapshot_when_present():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('live', 'h', 'bread', 5.0, 4, 20.0)]
    engine._planning_orders = [buy_order('snapshot', 'h2', 'bread', 9.0, 1, 9.0)]
    try:
        assert [o['id'] for o, _ in engine.customer_quotes(bakery)] == ['snapshot']
        # Явно переданный список имеет приоритет над планировочным снимком.
        assert [o['id'] for o, _ in
                engine.customer_quotes(bakery, engine.report['orders'])] == ['live']
    finally:
        engine.__dict__.pop('_planning_orders', None)


def test_customer_quotes_caches_index_per_order_list():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0)]
    engine.customer_quotes(bakery)
    cache = engine._customer_quotes_cache
    assert cache
    engine.customer_quotes(bakery)
    assert engine._customer_quotes_cache is cache


# --- 2. customer_demand_groups ------------------------------------------------

def test_demand_groups_aggregate_same_participant_and_price():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0),
                               buy_order('b', 'h', 'bread', 5.0, 2, 10.0),
                               buy_order('c', 'h2', 'bread', 5.0, 3, 15.0)]
    groups = engine.customer_demand_groups(bakery)
    assert len(groups) == 2
    assert groups[0][0] == 'h' and groups[0][2] == 5.0
    # Платёжеспособность каждой заявки: min(q, cash/price), затем сложение.
    assert groups[0][5] == pytest.approx(min(4, 20/5.0)+min(2, 10/5.0))
    assert groups[1][0] == 'h2' and groups[1][5] == pytest.approx(min(3, 15/5.0))


def test_demand_groups_keep_solvency_per_order():
    """Одна заявка не финансирует объём другой: min(q, cash/price) по каждой."""
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('rich', 'h', 'bread', 10.0, 1, 100.0),
                               buy_order('poor', 'h', 'bread', 10.0, 10, 0.0)]
    groups = engine.customer_demand_groups(bakery)
    assert len(groups) == 1
    assert groups[0][5] == pytest.approx(1)


def test_demand_groups_drop_rows_below_the_price_border():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('at', 'h', 'bread', 2.0, 4, 100.0),
                               buy_order('below', 'merchant:s', 'bread',
                                         2.0-3e-7, 4, 100.0)]
    groups = engine.customer_demand_groups(bakery)
    assert {row[0] for row in groups} == {'h'}
    bakery['price'] = 2.0+3e-7
    assert {row[0] for row in engine.customer_demand_groups(bakery)} == set()


def test_demand_groups_match_python_reference():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0),
                               buy_order('b', 'h2', 'bread', 3.5, 2, 7.0),
                               buy_order('c', 'merchant:s', 'bread', 1.0, 5, 5.0)]
    native = engine.customer_demand_groups(bakery)
    reference = boundary.customer_demand_groups_python(engine, bakery)
    assert [[typed(v) for v in row] for row in native] == \
           [[typed(v) for v in row] for row in reference]


# --- 3. _customer_demand_rows --------------------------------------------------

def test_demand_rows_are_tuple_of_scalars():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0)]
    rows = engine._customer_demand_rows('t', 'bread', engine.report['orders'])
    assert isinstance(rows, tuple) and len(rows) == 1
    assert rows[0] == ('h', 't', 5.0, 5.0, 0, 4, 20.0)


def test_demand_rows_cache_is_invalidated_by_demand_revision():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    first = order(engine, 'h', 'bread', 10, 'buy', 5.0)
    first['reserved'] = 1000
    orders = engine.report['orders']
    assert engine._customer_demand_rows('t', 'bread', orders)
    warm_key = (id(orders), len(orders), 't', 'bread', engine._demand_revision['bread'])

    engine.cancel_order(first, 'Тестовая отмена')
    assert first['status'] == 'cancelled' and first['reserved'] == 0
    rows = engine._customer_demand_rows('t', 'bread', orders)
    assert rows == ()
    # Словарь кэша тот же (прежняя семантика), но запись по старой ревизии
    # больше не используется: ключ сменился вместе с `_demand_revision`.
    assert engine._customer_demand_rows_cache[warm_key][1] != rows
    new_key = (id(orders), len(orders), 't', 'bread', engine._demand_revision['bread'])
    assert engine._customer_demand_rows_cache[new_key][1] == ()


def test_demand_rows_match_python_reference():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(tile_id='t', price=2.0)
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0),
                               buy_order('b', 'h2', 'bread', 3.0, 0.0, 0.0)]
    orders = engine.report['orders']
    native = engine._customer_demand_rows('t', 'bread', orders)
    reference = boundary._customer_demand_rows_python(engine, 't', 'bread', orders)
    assert native == reference


# --- 4. reachable_demand -------------------------------------------------------

def test_reachable_demand_counts_uncovered_volume():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    buy = order(engine, 'h', 'bread', 10, 'buy', 5.0)
    buy['reserved'] = 1000
    assert engine.reachable_demand(bakery, {'bread': []}) == pytest.approx(10)

    engine.add_stock('private:s', 'bread', 4, 4)
    sell = order(engine, 'private:s', 'bread', 4, 'sell', 1.0)
    assert engine.reachable_demand(bakery, {'bread': [sell]}) == pytest.approx(6)


def test_reachable_demand_ignores_own_offers_and_expensive_ones():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    buy = order(engine, 'h', 'bread', 10, 'buy', 5.0)
    buy['reserved'] = 1000

    engine.add_stock(bakery['id'], 'bread', 4, 4)
    own = order(engine, bakery['id'], 'bread', 4, 'sell', 1.0)
    engine.add_stock('private:s', 'bread', 4, 4)
    too_expensive = order(engine, 'private:s', 'bread', 4, 'sell', 10.0)
    # Своё предложение и предложение дороже заявки объём не покрывают.
    assert engine.reachable_demand(
        bakery, {'bread': [own, too_expensive]}) == pytest.approx(10)


def test_reachable_demand_shares_remaining_between_buyers():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    for participant in ('h', 'h2'):
        row = order(engine, participant, 'bread', 4, 'buy', 5.0)
        row['reserved'] = 1000
    engine.add_stock('private:s', 'bread', 4, 4)
    sell = order(engine, 'private:s', 'bread', 4, 'sell', 1.0)
    # Четыре единицы оффера покрывают первую группу целиком; второй достаётся 4.
    assert engine.reachable_demand(bakery, {'bread': [sell]}) == pytest.approx(4)


def test_reachable_demand_does_not_mutate_report():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    buy = order(engine, 'h', 'bread', 4, 'buy', 5.0)
    buy['reserved'] = 1000
    before = copy.deepcopy(engine.report)
    engine.reachable_demand(bakery, {'bread': []})
    assert engine.report == before


def test_reachable_demand_matches_python_reference():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    buy = order(engine, 'h', 'bread', 10, 'buy', 5.0)
    buy['reserved'] = 1000
    engine.add_stock('private:s', 'bread', 4, 4)
    sell = order(engine, 'private:s', 'bread', 4, 'sell', 1.0)
    native = engine.reachable_demand(bakery, {'bread': [sell]})
    reference = boundary.reachable_demand_python(engine, bakery, {'bread': [sell]})
    assert typed(native) == typed(reference)


# --- 5. expected_stock_sales --------------------------------------------------

def test_expected_stock_sales_clipped_by_stock_offer_and_demand():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    engine.add_stock(bakery['id'], 'bread', 8, 8)
    sell = order(engine, bakery['id'], 'bread', 3, 'sell', 2.0)
    assert engine.expected_stock_sales(bakery, {'bread': [sell]},
                                       reachable=10) == pytest.approx(3)
    engine.__dict__.pop('_stock_sales_cache', None)
    assert engine.expected_stock_sales(bakery, {'bread': [sell]},
                                       reachable=1) == pytest.approx(1)


def test_expected_stock_sales_zero_without_own_offers():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    engine.add_stock(bakery['id'], 'bread', 8, 8)
    assert engine.expected_stock_sales(bakery, {'bread': []}, reachable=10) == 0.0


def test_expected_stock_sales_uses_average_history_when_demand_is_small():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t',
                  sales_history=[dict(sold=2.0), dict(sold=4.0), dict(sold=6.0)])
    engine.add_stock(bakery['id'], 'bread', 100, 100)
    sell = order(engine, bakery['id'], 'bread', 100, 'sell', 2.0)
    assert engine.expected_stock_sales(bakery, {'bread': [sell]},
                                       reachable=1.0) == pytest.approx(4.0)


def test_expected_stock_sales_caches_per_enterprise():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    engine.add_stock(bakery['id'], 'bread', 8, 8)
    sell = order(engine, bakery['id'], 'bread', 3, 'sell', 2.0)
    engine.expected_stock_sales(bakery, {'bread': [sell]}, reachable=10)
    assert engine._stock_sales_cache[bakery['id']] == pytest.approx(3)
    # Повторный вызов отдаёт запись кэша, а не пересчитывает спрос.
    engine.add_stock(bakery['id'], 'bread', 1, 1)
    assert engine.expected_stock_sales(bakery, {'bread': [sell]},
                                       reachable=0) == pytest.approx(3)


def test_expected_stock_sales_matches_python_reference():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t',
                  sales_history=[dict(sold=1.5), dict(sold=2.5)])
    engine.add_stock(bakery['id'], 'bread', 8, 8)
    sell = order(engine, bakery['id'], 'bread', 6, 'sell', 2.0)
    native = engine.expected_stock_sales(bakery, {'bread': [sell]}, reachable=3)
    engine.__dict__.pop('_stock_sales_cache', None)
    reference = boundary.expected_stock_sales_python(
        engine, bakery, {'bread': [sell]}, reachable=3)
    assert typed(native) == typed(reference)


# --- 6. Fallback и call site ---------------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0)]
    native_rows = engine._customer_demand_rows('t', 'bread', engine.report['orders'])

    monkeypatch.setattr(boundary, 'customer_quotes', boundary.customer_quotes_python)
    monkeypatch.setattr(boundary, 'customer_demand_groups',
                        boundary.customer_demand_groups_python)
    monkeypatch.setattr(boundary, '_customer_demand_rows',
                        boundary._customer_demand_rows_python)
    monkeypatch.setattr(boundary, 'reachable_demand',
                        boundary.reachable_demand_python)
    monkeypatch.setattr(boundary, 'expected_stock_sales',
                        boundary.expected_stock_sales_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    engine.__dict__.pop('_customer_demand_rows_cache', None)
    engine.__dict__.pop('_customer_quotes_cache', None)
    # Та же форма ключа и то же значение: fallback не отдельная ветка данных.
    fallback_rows = engine._customer_demand_rows(
        't', 'bread', engine.report['orders'])
    assert fallback_rows == native_rows
    assert engine.customer_demand_groups(bakery) == \
           boundary.customer_demand_groups_python(engine, bakery)
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('demand_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_demand_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('customer_quotes', 'customer_quotes_python',
                 'customer_demand_groups', 'customer_demand_groups_python',
                 '_customer_demand_rows', '_customer_demand_rows_python',
                 'reachable_demand', 'reachable_demand_python',
                 'expected_stock_sales', 'expected_stock_sales_python',
                 'load_demand_kernel'):
        assert callable(getattr(boundary, name)), name


def test_v3_has_single_call_site_per_slice():
    """В v3 расчётные тела заменены вызовами границы, без вторых копий."""
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    for name in ('customer_quotes', 'customer_demand_groups',
                 '_customer_demand_rows', 'reachable_demand',
                 'expected_stock_sales', '_customer_orders',
                 '_customer_quote_index', '_orders_in_route_component'):
        assert module_source.count(f'_demand.{name}(') == 1, name
    for body in (inspect.getsource(Economy.customer_quotes),
                 inspect.getsource(Economy.customer_demand_groups),
                 inspect.getsource(Economy._customer_demand_rows),
                 inspect.getsource(Economy.reachable_demand),
                 inspect.getsource(Economy.expected_stock_sales),
                 inspect.getsource(Economy._customer_orders),
                 inspect.getsource(Economy._customer_quote_index),
                 inspect.getsource(Economy._orders_in_route_component)):
        assert '_demand.' in body
    # Кэши называют прежними именами и больше не ведутся в v3.
    assert '_customer_demand_rows_cache = cache' not in module_source
    assert '_customer_quotes_cache = cache' not in module_source
    assert '_stock_sales_cache = cache' not in module_source


def test_native_kernel_is_used_on_the_standard_path():
    """Штатный путь зовёт Cython: ядро не просто импортировано, а вызвано."""
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    bakery.update(price=2.0, tile_id='t')
    engine.report['orders'] = [buy_order('a', 'h', 'bread', 5.0, 4, 20.0)]
    seen = []
    real = boundary.run_groups
    boundary.run_groups = lambda *args: seen.append(args) or real(*args)
    try:
        engine.customer_demand_groups(bakery)
    finally:
        boundary.run_groups = real
    assert seen, 'штатный путь обязан звать ядро demand_kernel'
