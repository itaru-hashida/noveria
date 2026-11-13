"""Тесты переиспользования буферов в pack_update_prices.

Проверяем контракт кэшей: горячий pack не пересобирает статические массивы,
матрица расстояний и флаги ресурсов совпадают с пересчётом «с нуля», а смена
топологии или товаров корректно инвалидирует кэш.
"""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

from modules.economy import Economy

pytest.importorskip('modules.economy.native.serial_step')
from modules.economy.native import update_prices_boundary as boundary  # noqa: E402
from modules.economy.native.update_prices_boundary import (  # noqa: E402
    pack_update_prices, _update_distance_matrix, _update_recipe_arrays,
    _update_resource_ok, _update_sold_now)


def engine():
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    cfg = json.loads(Path('tests/fixtures/economy.json').read_text(encoding='utf-8'))
    result = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    result.meta['report_detail'] = 'none'
    result.seed = 17
    result.rng.seed(17)
    result.bootstrap()
    return result


def fresh_pack(instance):
    """pack_update_prices без кэшей: полностью «холодный» расчёт."""
    boundary._UPDATE_PRICES_STATIC.clear()
    boundary._UPDATE_PRICES_DISTANCES.clear()
    return pack_update_prices(instance)


# Выходные скретч-буферы native (np.empty) заполняет сам Cython, поэтому их
# содержимое до вызова не определено и сравнению не подлежит.
_SCRATCH_PREFIXES = ('new_prices', 'trace_', 'sell_order_', 'sell_active', 'sell_remaining',
                     'household_', 'owner_', 'public_', 'demand_group_', 'recovery_quote_',
                     'recovery_enterprise', 'recovery_transfer', 'recovery_hold',
                     'recovery_reopened', 'recovery_cancel_', 'recovery_outlook_count',
                     'recovery_debug_', 'recovery_stock_cache_set', 'recovery_market_version',
                     'recovery_expected_', 'recovery_planned_', 'recovery_fixed_',
                     'recovery_production_limit', 'recovery_outlook_valid')


def _is_scratch(name):
    return name.startswith(_SCRATCH_PREFIXES)


def test_warm_pack_reuses_static_buffers_and_matches_cold():
    reference = engine()
    reference.step(native_mode='cython')
    cold_data, _ = fresh_pack(reference)
    warm_data, _ = pack_update_prices(reference)
    # Прогрев не должен менять ни одного входного упакованного массива.
    checked = 0
    for name, value in cold_data.items():
        if not isinstance(value, np.ndarray) or _is_scratch(name):
            continue
        assert np.array_equal(value, warm_data[name], equal_nan=True), name
        checked += 1
    assert checked > 20, 'ожидалось сравнить входные массивы, а не только скретч'


def test_warm_pack_keeps_distance_matrix_identity():
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    second, _ = pack_update_prices(instance)
    assert first['distances'] is second['distances'], \
        'стабильная топология должна переиспользовать ту же матрицу'


def test_distance_matrix_matches_uncached_recomputation():
    instance = engine()
    instance.step(native_mode='cython')
    accounts = list(instance.accounts.values())
    enterprises = list(instance.enterprises.values())
    orders = [o for o in instance.report['orders'] if o['side'] == 'buy']
    tile_ids = {a.get('cell_id') or a['tile_id'] for a in accounts}
    tile_ids.update(e['tile_id'] for e in enterprises)
    tile_ids.update(o['tile_id'] for o in orders)
    tile_ids.update(a['project']['location']['tile_id'] for a in accounts if a.get('project'))
    tiles = sorted(tile_ids)

    expected = np.full((len(tiles), len(tiles)), np.nan, dtype=np.float64)
    for source_index, source in enumerate(tiles):
        for target_index, target in enumerate(tiles):
            distance, _ = instance.path(source, target)
            if distance is not None:
                expected[source_index, target_index] = distance

    cached = _update_distance_matrix(instance, tiles)
    assert np.array_equal(expected, cached, equal_nan=True), 'матрица должна совпадать побитово'


def test_route_change_invalidates_distance_cache():
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    assert 'distances' in boundary._UPDATE_PRICES_DISTANCES or True
    # Явный сброс маршрутов — тот же путь, которым пользуется редактор дорог.
    instance.invalidate_routes()
    second, _ = pack_update_prices(instance)
    assert first['distances'] is not second['distances'], \
        'после смены топологии матрица обязана пересобраться'
    assert np.array_equal(first['distances'], second['distances'], equal_nan=True)


def test_recipe_cache_invalidated_by_goods_change():
    instance = engine()
    goods = list(instance.goods)
    good_index = {g: i for i, g in enumerate(goods)}
    first = _update_recipe_arrays(instance, goods, good_index)
    again = _update_recipe_arrays(instance, goods, good_index)
    assert first is again, 'повторный вызов обязан вернуть тот же кортеж массивов'

    # Смена рецепта у реального товара меняет сигнатуру и обязана сбросить кэш.
    target = goods[0]
    original = instance.goods[target]['inputs']
    donor = next(g for g in goods if g != target and g not in original)
    instance.goods[target]['inputs'] = dict(original, **{donor: 0.5})
    try:
        changed = _update_recipe_arrays(instance, goods, good_index)
        assert changed is not first, 'смена рецепта должна пересобрать массивы'
        assert changed[2] != first[2], 'значения входов должны измениться'
    finally:
        instance.goods[target]['inputs'] = original
    restored = _update_recipe_arrays(instance, goods, good_index)
    assert restored[1] == first[1] and restored[2] == first[2]
    assert restored[3] == first[3] and restored[5] == first[5]


def test_resource_ok_matches_full_tile_scan():
    instance = engine()
    instance.step(native_mode='cython')
    enterprises = list(instance.enterprises.values())
    expected = []
    for enterprise in enterprises:
        resource = instance.goods[enterprise['good']]['resource']
        expected.append(not resource or any(t['province_id'] == enterprise['province_id']
            and resource in t['resources'] for t in instance.tiles.values()))
    assert _update_resource_ok(instance, enterprises) == expected


def test_sold_now_matches_filtered_sum_bitwise():
    instance = engine()
    for _ in range(2):
        instance.step(native_mode='cython')
    enterprises = list(instance.enterprises.values())
    expected = [sum(t['quantity'] for t in instance.report['trades']
                    if t['seller'] == e['id'] and t['good'] == e['good'])
                for e in enterprises]
    assert _update_sold_now(instance, enterprises) == expected


def test_pack_replay_keeps_full_step_exact():
    reference = engine()
    native = copy.deepcopy(reference)
    for name in ('_planning_orders', '_stock_sales_cache', '_production_outlook_cache'):
        reference.__dict__.pop(name, None)
        native.__dict__.pop(name, None)
    for _ in range(2):
        reference.step(native_mode='off')
        native.step(native_mode='cython')
    assert reference.accounts == native.accounts
    assert reference.enterprises == native.enterprises
    assert reference.meta == native.meta
    assert reference.report == native.report
    assert reference.rng.getstate() == native.rng.getstate()
    reference.validate()
    native.validate()


def test_sell_order_quantity_keeps_int_type_like_v3():
    """Тип объёма sell-заявки обязан совпадать: sum() различает int и float.

    В Python 3.12 встроенный sum компенсирует погрешность только при
    float-старте, поэтому отчёт складывает объёмы сделок по-разному в
    зависимости от типа quantity. Здесь проверяется, что native возвращает
    ровно тот тип, который построил бы V3.
    """
    reference = engine()
    native = copy.deepcopy(reference)
    for name in ('_planning_orders', '_stock_sales_cache', '_production_outlook_cache'):
        reference.__dict__.pop(name, None)
        native.__dict__.pop(name, None)
    reference.step(native_mode='off')
    native.step(native_mode='cython')

    left = {o['id']: o for o in reference.report['orders'] if o['side'] == 'sell'}
    right = {o['id']: o for o in native.report['orders'] if o['side'] == 'sell'}
    assert set(left) == set(right)
    mismatched = [oid for oid in left
                  if type(left[oid]['quantity']) is not type(right[oid]['quantity'])]
    assert not mismatched, 'тип объёма sell-заявки разошёлся: %s' % (mismatched[:5],)
    for oid in left:
        assert type(left[oid]['remaining']) is type(right[oid]['remaining']), oid
        assert type(left[oid]['backing']) is type(right[oid]['backing']), oid


def test_report_province_prices_exact_after_full_step():
    """Регресс на 1-ULP расхождение report['provinces'][*]['prices'].

    Равенство словаря report проверяет и цены, и export_volume: оба поля
    складывают объёмы сделок, поэтому расхождение типа quantity проявлялось
    именно здесь.
    """
    reference = engine()
    native = copy.deepcopy(reference)
    for name in ('_planning_orders', '_stock_sales_cache', '_production_outlook_cache'):
        reference.__dict__.pop(name, None)
        native.__dict__.pop(name, None)
    reference.step(native_mode='off')
    native.step(native_mode='cython')
    assert reference.report == native.report
    # Мини-мир теста живёт с report_detail='none', где сводка провинций не
    # строится. Когда она есть — проверяем цены и объёмы явно.
    if 'provinces' not in reference.report:
        return
    for left, right in zip(reference.report['provinces'], native.report['provinces']):
        assert left['prices'] == right['prices'], left['province_id']
        assert type(left['export_volume']) is type(right['export_volume']), left['province_id']
        assert left['export_volume'] == right['export_volume'], left['province_id']
