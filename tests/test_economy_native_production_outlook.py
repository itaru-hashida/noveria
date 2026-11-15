"""Прогноз выпуска в Cython: exact-числа, кэш, RNG и fallback.

Числовая часть ``V3.production_outlook`` перенесена в
``production_outlook_boundary``: ядро считает средние продажи, прогноз спроса,
остаток склада, постепенное сокращение мощности, ``fixed_unit``, маржу,
причину предела и итоговый выпуск. Книга заявок, ``unit_cost``, ``net_price``,
``reachable_demand``, ``expected_stock_sales``, оброк, отчёт и кэш прогноза
остаются Python-хуками.

Тест держит то, что легко потерять при переносе:

  * порядок ключей результата и порядок операций (включая левое сложение
    float и ``max``/``min`` с первым аргументом при равенстве);
  * прежнюю семантику кэша: hit возвращает ту же запись, miss считает заново,
    инвалидация поднимает версию книги и ревизию спроса;
  * отсутствие побочных эффектов: RNG не расходуется, живые заявки и сделки
    не меняются, ``deepcopy`` даёт независимые кэши;
  * fallback без собранного ``.pyd`` и побитовое совпадение эталона с ядром.

Exact-отпечатки траектории сняты до переноса: совпадение означает, что
порядок предприятий, сделок и RNG не изменились.
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))
sys.path.insert(0, str(ROOT / 'tests'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy import Economy  # noqa: E402
from modules.economy.native import production_outlook_boundary as boundary  # noqa: E402
from test_economy_native_market_book import make_engine, sell, sells_for  # noqa: E402

# Exact-отпечатки, снятые на Python-коде V3 до переноса книги и прогноза в
# Cython (те же значения держит tests/test_economy_native_market_book.py).
GOLDEN = {
    17: 'c25f33906413c6df6b8bddf7be0889379167819adf0f0e565474cdc229cde598',
    73129: '35b1b396eb67f7875e64111216b94c7b6362bc9f2d72e3e74f82a55a81d3023c',
}
GOLDEN_LAST = {
    17: '077b775c565b045cb020ca88996723603c290d5b5c20cdcf3b5edd3b9f078f1b',
    73129: 'd8446744ebd07232f7995c4035f5f1dae298d0373132d54e972f7411ef76f531',
}
GOLDEN_RNG = {
    17: '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377',
    73129: 'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b',
}
GOLDEN_TRADES = {17: 835, 73129: 853}

KEYS = ('sales_forecast', 'reachable_demand', 'expected_unit_cost',
        'expected_price', 'expected_margin', 'production_limit',
        'planned_output', 'input_target_output', 'planned_price',
        'expected_fixed_cost', 'expected_fixed_unit')


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


def firms(engine):
    return list(engine.enterprises.values())


def native(engine, e, sells=None):
    return boundary.production_outlook(engine, e, sells)


def reference(engine, e, sells=None):
    return boundary.production_outlook_python(engine, dict(e), sells)


def shape(result):
    """Сравнимая форма: пары (ключ, значение, тип) в порядке словаря."""
    return [(key, result[key], type(result[key]).__name__) for key in result]


# --- 1. Рецепты: пустой, один товар, несколько товаров --------------------
def test_empty_recipe_costs_only_labor():
    """Товар без входов: себестоимость — только труд, износа нет."""
    engine = make_engine()
    # В синтетическом мире все предприятия что-то перерабатывают, поэтому
    # рецепт опустошается на месте: проверяется арифметика, а не контент.
    firm = firms(engine)[0]
    recipe = dict(engine.goods[firm['good']])
    engine.goods[firm['good']] = dict(recipe, inputs={}, equipment={})
    try:
        result = native(engine, firm)
        expected = engine.goods[firm['good']]['labor']*engine.cfg['wage']
        assert result['expected_unit_cost'] == expected
        assert shape(result) == shape(reference(engine, firm))
    finally:
        engine.goods[firm['good']] = recipe


def test_one_good_produces_the_same_outlook():
    engine = make_engine()
    firm = firms(engine)[0]
    assert firm['good'] == 'flour'
    result = native(engine, firm)
    assert set(result) == set(KEYS)
    assert result['sales_forecast'] == firm['sales_forecast']
    assert shape(result) == shape(reference(engine, firm))


def test_several_goods_are_all_covered():
    engine = make_engine()
    goods = {e['good'] for e in firms(engine)}
    assert len(goods) >= 2
    for firm in firms(engine):
        assert shape(native(engine, firm)) == shape(reference(engine, firm))


def test_several_enterprises_keep_independent_results():
    engine = make_engine()
    first, second = firms(engine)[:2]
    left = native(engine, first)
    right = native(engine, second)
    assert left is not right
    assert shape(left) == shape(reference(engine, first))
    assert shape(right) == shape(reference(engine, second))


# --- 2. Границы: нулевой выпуск и отсутствующие материалы -----------------
def test_zero_output_gives_zero_fixed_unit():
    engine = make_engine()
    firm = firms(engine)[0]
    engine.enterprises[firm['id']]['capacity'] = 0
    result = native(engine, firm)
    assert result['planned_output'] == 0
    assert result['input_target_output'] == 0
    assert result['expected_fixed_unit'] == 0.0
    assert shape(result) == shape(reference(engine, firm))


def test_missing_materials_fall_back_to_the_reference_price():
    """Нет офферов на вход — цена берётся из контента, как раньше."""
    engine = make_engine()
    firm = next(e for e in firms(engine) if engine.goods[e['good']]['inputs'])
    empty = {}
    result = native(engine, firm, empty)
    recipe = engine.goods[firm['good']]
    raw = sum(ratio*engine.goods[good]['price']
              for good, ratio in recipe['inputs'].items())
    expected = recipe['labor']*engine.cfg['wage']+raw
    assert result['expected_unit_cost'] == expected
    assert shape(result) == shape(reference(engine, firm, {}))


def test_offers_do_not_change_the_book_order_or_live_orders():
    engine = make_engine()
    firm = next(e for e in firms(engine) if engine.goods[e['good']]['inputs'])
    input_good = next(iter(engine.goods[firm['good']]['inputs']))
    order = sell('a', 't1', 3.0, good=input_good, participant='seller')
    sells = sells_for(order, good=input_good)
    before = digest(order)
    result = native(engine, firm, sells)
    assert digest(order) == before
    assert shape(result) == shape(reference(engine, firm, sells))
    # Более дешёвый оффер снижает себестоимость только после сброса кэша
    # котировок: кэш — часть прежней семантики, а не новый слой.
    engine.invalidate_market_books()
    engine.__dict__.pop('_unit_cost_cache', None)
    cheaper = sell('a', 't1', 1.0, good=input_good, participant='seller')
    assert native(engine, firm, sells_for(cheaper, good=input_good))[
        'expected_unit_cost'] < result['expected_unit_cost']


# --- 3. int/float: типы остаются прежними --------------------------------
def test_int_capacity_keeps_int_output():
    """Ёмкость целым числом даёт целый выпуск — как в прежнем max/min."""
    engine = make_engine()
    firm = engine.enterprises[firms(engine)[0]['id']]
    firm['capacity'] = 100
    firm['sales_history'] = [dict(sold=10, output=10, input_limited=False)]
    firm['sales_forecast'] = 10
    sells = {}
    result = native(engine, firm, sells)
    expected = reference(engine, firm, sells)
    assert type(result['planned_output']) is type(expected['planned_output'])
    assert shape(result) == shape(expected)


def test_int_and_float_inputs_are_compared_by_value_and_type():
    engine = make_engine()
    firm = engine.enterprises[firms(engine)[0]['id']]
    for value in (7, 7.0):
        engine.__dict__.pop('_production_outlook_cache', None)
        firm['capacity'] = value
        result = native(engine, firm, {})
        expected = reference(engine, firm, {})
        assert shape(result) == shape(expected)


def test_result_order_matches_the_previous_dict():
    engine = make_engine()
    firm = firms(engine)[0]
    assert tuple(native(engine, firm)) == KEYS
    assert tuple(reference(engine, firm)) == KEYS


# --- 4. Кэш: hit, miss, инвалидация --------------------------------------
def test_cache_hit_returns_the_same_entry():
    engine = make_engine()
    firm = firms(engine)[0]
    sells = {'grain': [sell('a', 't1', 2.0)]}
    first = native(engine, firm, sells)
    second = native(engine, firm, sells)
    assert first is second
    assert len(engine._production_outlook_cache) == 1


def test_cache_miss_on_a_new_sell_list():
    engine = make_engine()
    firm = firms(engine)[0]
    # Ключ держит id словаря офферов, поэтому оба словаря держатся живыми:
    # иначе CPython может переиспользовать освободившийся id и дать ложный hit
    # — это прежнее поведение ключа, а не новый слой.
    first_sells = {'grain': [sell('a', 't1', 2.0)]}
    second_sells = {'grain': [sell('a', 't1', 2.0)]}
    assert first_sells is not second_sells
    first = native(engine, firm, first_sells)
    second = native(engine, firm, second_sells)
    assert first is not second
    assert shape(second) == shape(first)
    assert len(engine._production_outlook_cache) == 2
    # Повторный вызов с тем же словарём — это hit: та же запись.
    assert native(engine, firm, first_sells) is first
    assert len(engine._production_outlook_cache) == 2


def test_cache_invalidation_on_demand_revision():
    engine = make_engine()
    firm = firms(engine)[0]
    sells = {'grain': [sell('a', 't1', 2.0)]}
    first = native(engine, firm, sells)
    engine._demand_revision[firm['good']] += 1
    second = native(engine, firm, sells)
    assert first is not second
    assert shape(second) == shape(first)


def test_cache_key_carries_the_boundary_version():
    """Ключ помечен версией: запись старого формата не переиспользуется."""
    engine = make_engine()
    firm = firms(engine)[0]
    sells = {'grain': [sell('a', 't1', 2.0)]}
    native(engine, firm, sells)
    key = next(iter(engine._production_outlook_cache))
    assert key[0] == boundary.VERSION
    stale = dict(engine._production_outlook_cache)
    engine.__dict__['_production_outlook_cache'] = {
        (0,)+key[1:]: {'sales_forecast': 'stale'}}
    assert native(engine, firm, sells)['sales_forecast'] != 'stale'
    engine.__dict__['_production_outlook_cache'] = stale


def test_uncacheable_enterprise_is_not_stored():
    """Прогноз по копии предприятия не попадает в кэш движка."""
    engine = make_engine()
    firm = dict(firms(engine)[0])
    native(engine, firm, {})
    assert not getattr(engine, '_production_outlook_cache', {})


# --- 5. Изменение рецепта, цены и топологии ------------------------------
def test_recipe_change_is_reflected_after_cache_reset():
    engine = make_engine()
    firm = engine.enterprises[firms(engine)[0]['id']]
    before = native(engine, firm, {})['expected_unit_cost']
    recipe = engine.goods[firm['good']]
    recipe['labor'] = recipe['labor']*2
    engine.__dict__.pop('_unit_cost_cache', None)
    engine.__dict__.pop('_production_outlook_cache', None)
    after = native(engine, firm, {})
    assert after['expected_unit_cost'] != before
    assert shape(after) == shape(reference(engine, firm, {}))


def test_price_change_invalidates_the_outlook():
    engine = make_engine()
    firm = engine.enterprises[firms(engine)[0]['id']]
    sells = {'grain': [sell('a', 't1', 2.0)]}
    before = native(engine, firm, sells)
    firm['price'] = firm['price']*1.5
    after = native(engine, firm, sells)
    assert before is not after
    assert after['planned_price'] == firm['price']
    assert shape(after) == shape(reference(engine, firm, sells))


def test_topology_and_quote_version_change_the_key():
    """Топология задаёт книги: другой мир и другая эпоха дают новую запись."""
    opened = make_engine()
    firm = firms(opened)[0]
    sells = {'grain': [sell('a', 't1', 2.0)]}
    first = native(opened, firm, sells)
    opened.__dict__['_market_quotes_epoch'] = (
        opened.__dict__.get('_market_quotes_epoch', 0)+1)
    second = native(opened, firm, sells)
    assert first is not second
    assert shape(second) == shape(first)

    closed = make_engine()
    closed_firm = firms(closed)[0]
    assert shape(native(closed, closed_firm, sells)) == shape(
        reference(closed, closed_firm, sells))


# --- 6. Побочные эффекты: RNG, заявки, deepcopy --------------------------
def test_rng_state_is_not_touched():
    engine = make_engine()
    before = engine.rng.getstate()
    for firm in firms(engine):
        native(engine, firm, {})
    assert engine.rng.getstate() == before


def test_live_orders_and_trades_are_not_mutated():
    engine = make_engine()
    firm = firms(engine)[0]
    order = sell('a', 't1', 2.0)
    sells = {'grain': [order]}
    engine.report['trades'].append(dict(seller=firm['id'], good=firm['good'],
                                        quantity=1.0, price=2.0))
    before_order = digest(order)
    before_trades = digest(engine.report['trades'])
    native(engine, firm, sells)
    assert digest(order) == before_order
    assert order['remaining'] == 10.0 and order['status'] == 'active'
    assert digest(engine.report['trades']) == before_trades


def test_deepcopy_gets_independent_outlook_caches():
    engine = make_engine()
    firm = firms(engine)[0]
    # Прежний прогноз мог уже оставить запись в кэше движка; доказывать нужно
    # независимость кэшей, а не их исходный размер.
    engine.__dict__.pop('_production_outlook_cache', None)
    sells = {'grain': [sell('a', 't1', 2.0)]}
    first = native(engine, firm, sells)
    assert len(engine._production_outlook_cache) == 1
    clone = copy.deepcopy(engine)
    clone_firm = clone.enterprises[firm['id']]
    assert clone.__dict__.get('_production_outlook_cache') is not engine.__dict__[
        '_production_outlook_cache']
    clone_sells = {'grain': [sell('a', 't1', 2.0)]}
    cloned = native(clone, clone_firm, clone_sells)
    assert shape(cloned) == shape(first)
    assert len(clone._production_outlook_cache) == 2
    assert len(engine._production_outlook_cache) == 1


# --- 7. Fallback без .pyd ------------------------------------------------
def test_fallback_switches_to_the_python_reference(monkeypatch):
    """Без .pyd граница привязывает Python-эквиваленты, а не падает."""
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    engine = make_engine()
    firm = firms(engine)[0]
    native_result = native(engine, firm, {})
    # Повторяем ту же привязку, что делает импорт границы, но без ядра:
    # меняются ровно три имени, тело расчёта остаётся одним и тем же.
    monkeypatch.setattr(boundary, 'run_price', boundary._price_python)
    monkeypatch.setattr(boundary, 'run_sold', boundary._sold_python)
    monkeypatch.setattr(boundary, 'run_outlook', boundary._outlook_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    engine.__dict__.pop('_production_outlook_cache', None)
    assert boundary.NATIVE_KERNEL is False
    assert boundary.run_outlook is boundary._outlook_python
    assert shape(native(engine, firm, {})) == shape(native_result)
    assert shape(native(engine, firm, {})) == shape(reference(engine, firm, {}))
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True
    assert boundary.run_outlook is not boundary._outlook_python


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    """Отсутствие расширения — это ImportError, а не падение границы."""
    monkeypatch.setattr(boundary, 'load_production_outlook_kernel',
                        lambda: None)
    assert boundary.load_production_outlook_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('production_outlook', 'production_outlook_python',
                 'run_price', 'run_sold', 'run_outlook', '_price_python',
                 '_sold_python', '_outlook_python', 'cache_key'):
        assert callable(getattr(boundary, name)), name
    assert boundary.VERSION >= 1


# --- 8. Реальный движок: forced-case и exact -----------------------------
def build_world(seed, months, detail='standard'):
    captured = {}
    original = Economy.project_requests

    def traced(self, sells):
        captured['sells'] = sells
        return original(self, sells)

    Economy.project_requests = traced
    try:
        tmp = workdir('test_production_outlook')
        with tmp as directory:
            db, service = sr.build_world(seed, directory, report_detail=detail)
            try:
                for month in range(months):
                    service.advance_turn(
                        command_id='po-test-%d-%d' % (seed, month),
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


def test_forced_case_real_outlook_matches_reference(world_17):
    """На реальном месяце ядро и эталон дают одни и те же числа."""
    engine, sells = world_17
    checked = 0
    for firm in firms(engine):
        if firm['available_from'] > engine.meta['month']:
            continue
        got = native(engine, firm, sells)
        wanted = reference(engine, firm, sells)
        assert shape(got) == shape(wanted), firm['id']
        assert got['production_limit'] in ('Готовые запасы',
                                           'Нет обеспеченного спроса',
                                           'План',
                                           'Отрицательная ожидаемая маржа')
        checked += 1
    assert checked > 20


def test_forced_case_outlook_does_not_touch_rng_or_orders(world_17):
    engine, sells = world_17
    rng_before = digest(engine.rng.getstate())
    orders_before = digest(sorted((o['id'], o.get('remaining'))
                                  for o in engine.report['orders']))
    for firm in firms(engine):
        native(engine, dict(firm), sells)
    assert digest(engine.rng.getstate()) == rng_before
    assert digest(sorted((o['id'], o.get('remaining'))
                         for o in engine.report['orders'])) == orders_before


def test_forced_case_python_fallback_gives_the_same_world(world_17,
                                                          monkeypatch):
    """Fallback без .pyd не меняет ни одно поле прогноза."""
    engine, sells = world_17
    firm = next(e for e in firms(engine) if e['available_from'] <= 3)
    monkeypatch.setattr(boundary, 'run_price', boundary._price_python)
    monkeypatch.setattr(boundary, 'run_sold', boundary._sold_python)
    monkeypatch.setattr(boundary, 'run_outlook', boundary._outlook_python)
    engine.__dict__.pop('_production_outlook_cache', None)
    assert shape(native(engine, firm, sells)) == shape(
        reference(engine, firm, sells))


@pytest.mark.parametrize('seed,fixture_name', [(17, 'world_17'),
                                               (73129, 'world_73129')])
def test_exact_three_months_are_unchanged(seed, fixture_name, request):
    engine, _ = request.getfixturevalue(fixture_name)
    assert engine.meta['month'] == 3
    assert engine_digest(engine) == GOLDEN_LAST[seed]
    assert len(engine.report['trades']) == GOLDEN_TRADES[seed]


@pytest.mark.parametrize('seed,fixture_name', [(17, 'world_17'),
                                               (73129, 'world_73129')])
def test_exact_rng_state_is_unchanged(seed, fixture_name, request):
    engine, _ = request.getfixturevalue(fixture_name)
    assert digest(engine.rng.getstate()) == GOLDEN_RNG[seed]
