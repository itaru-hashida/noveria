"""Persistent native state of update_prices.

Проверяем контракт состояния, которое живёт на движке между ходами:

- буферы и стабильные массивы переиспользуются, а не создаются заново;
- динамические значения обновляются на месте и совпадают с холодным pack'ом;
- смена топологии, товаров и индексации инвалидирует состояние;
- порядок float-операций не меняется, поэтому значения остаются битовыми;
- копия мира получает собственное состояние, а не общий буфер.
"""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

from modules.economy import Economy

pytest.importorskip('modules.economy.native.serial_step')
from modules.economy.native import update_prices_boundary as boundary  # noqa: E402
from modules.economy.native import serial_step  # noqa: E402
from modules.economy.native.update_prices_boundary import (  # noqa: E402
    _native_update_state, pack_update_prices)

NativeUpdateState = serial_step.NativeUpdateState


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


# Cython сам заполняет выходные скретч-буферы, поэтому их содержимое до
# вызова не определено: сравнивать его между pack'ами нельзя.
_SCRATCH_PREFIXES = ('new_prices', 'trace_', 'sell_order_', 'sell_active',
                     'sell_remaining', 'household_', 'owner_', 'public_',
                     'demand_group_', 'recovery_quote_', 'recovery_enterprise',
                     'recovery_transfer', 'recovery_hold', 'recovery_reopened',
                     'recovery_cancel_', 'recovery_outlook_count', 'recovery_debug_',
                     'recovery_stock_cache_set', 'recovery_market_version',
                     'recovery_expected_', 'recovery_planned_', 'recovery_fixed_',
                     'recovery_production_limit', 'recovery_outlook_valid')


def _is_scratch(name):
    return name.startswith(_SCRATCH_PREFIXES)


def cold_pack(instance):
    """pack_update_prices с полностью сброшенным состоянием."""
    instance.__dict__.pop('_native_update_state', None)
    boundary._UPDATE_PRICES_STATIC.clear()
    boundary._UPDATE_PRICES_DISTANCES.clear()
    return pack_update_prices(instance)


def assert_same_pack(cold, warm):
    """Входные массивы обязаны совпадать побитово, NaN — в тех же местах."""
    checked = 0
    for name, value in cold.items():
        if not isinstance(value, np.ndarray) or _is_scratch(name):
            continue
        assert np.array_equal(value, warm[name], equal_nan=True), name
        checked += 1
    assert checked > 20, 'ожидалось сравнить входные массивы, а не скретч'


def test_state_lives_on_engine_and_reuses_buffers():
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    state = instance._native_update_state
    assert isinstance(state, serial_step.NativeUpdateState)
    second, _ = pack_update_prices(instance)
    # Тот же объект состояния и те же массивы, а не новые копии.
    assert instance._native_update_state is state
    assert first['distances'] is second['distances']
    for name in ('account_stock', 'account_locked', 'sell_good',
                 'enterprise_history_sold', 'account_balance', 'subject_price'):
        assert first[name] is second[name], name
    assert state.layout_key is not None


def test_warm_pack_matches_cold_pack_field_by_field():
    instance = engine()
    instance.step(native_mode='cython')
    warm, _ = pack_update_prices(instance)
    warm_again, _ = pack_update_prices(instance)
    cold, _ = cold_pack(instance)
    assert_same_pack(cold, warm)
    assert_same_pack(cold, warm_again)


def clone(instance):
    """Копия мира без диагностических dict'ов native (они держат memoryview)."""
    original = dict(instance.__dict__)
    for name in [key for key in original if key.startswith('native_')]:
        instance.__dict__.pop(name)
    try:
        return copy.deepcopy(instance)
    finally:
        instance.__dict__.update(original)


def test_state_is_not_shared_by_world_copy():
    instance = engine()
    instance.step(native_mode='cython')
    pack_update_prices(instance)
    clone_instance = clone(instance)
    assert getattr(clone_instance, '_native_update_state', None) \
        is not instance._native_update_state
    # Копия обязана собрать тот же pack самостоятельно.
    cold, _ = cold_pack(clone_instance)
    warm, _ = pack_update_prices(clone_instance)
    assert_same_pack(cold, warm)


def test_topology_change_invalidates_state():
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    state = instance._native_update_state
    instance.invalidate_routes()
    second, _ = pack_update_prices(instance)
    assert instance._native_update_state is state, 'объект состояния остаётся на движке'
    assert first['distances'] is not second['distances'], \
        'после смены топологии матрица пересобирается'
    assert np.array_equal(first['distances'], second['distances'], equal_nan=True)


def _depends_on(goods, good, target):
    """Зависит ли товар good от target по цепочке входов."""
    seen, stack = set(), [good]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        inputs = goods[current]['inputs']
        if target in inputs:
            return True
        stack.extend(inputs)
    return False


def test_goods_change_invalidates_static_arrays():
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    goods = list(instance.goods)
    target = goods[0]
    original = instance.goods[target]['inputs']
    # Донор не должен зависеть от target, иначе получится цикл в depth().
    donor = next(g for g in goods if g != target and g not in original
                 and not _depends_on(instance.goods, g, target))
    instance.goods[target]['inputs'] = dict(original, **{donor: 0.5})
    try:
        second, _ = pack_update_prices(instance)
        assert first['recovery_input_value'] is not second['recovery_input_value']
        pairs = list(zip(second['recovery_input_good'],
                         second['recovery_input_value']))
        assert any(int(good) == goods.index(donor) and float(value) == 0.5
                   for good, value in pairs)
    finally:
        instance.goods[target]['inputs'] = original
    restored, _ = pack_update_prices(instance)
    assert np.array_equal(first['recovery_input_value'],
                          restored['recovery_input_value'], equal_nan=True)


def test_indexation_change_invalidates_state():
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    state = instance._native_update_state
    previous_layout = state.layout_key
    order = list(instance.goods)
    reordered = {good: instance.goods[good] for good in reversed(order)}
    instance.goods = reordered
    instance.cfg['goods'] = reordered
    second, _ = pack_update_prices(instance)
    assert state.layout_key != previous_layout, \
        'смена индексации обязана сменить ключ состояния'
    assert list(second['good_weight']) == list(reversed(list(first['good_weight'])))
    assert list(second['account_stock'][0]) == list(reversed(list(first['account_stock'][0])))
    # Значения обязаны совпасть с полностью холодным pack'ом.
    cold, _ = cold_pack(instance)
    assert_same_pack(cold, second)


def test_order_buffers_do_not_corrupt_canonical_order():
    """Native перемешивает seller/household/owner order на месте.

    Канонический порядок в состоянии обязан остаться неизменным, иначе
    следующий ход начнёт с уже перемешанного массива и поведение изменится.
    """
    instance = engine()
    instance.step(native_mode='cython')
    pack_update_prices(instance)
    state = instance._native_update_state
    canonical = {name: state.static[name].copy()
                 for name in ('seller_order', 'household_order', 'owner_order',
                              'recovery_order', 'enterprise_recovery_order')}
    instance.native_market = None
    # Полный native-проход перемешивает рабочие копии.
    boundary.run_native_recovery(instance)
    for name, expected in canonical.items():
        assert np.array_equal(state.static[name], expected), name
    work = {name: state.data[name + '_work'] for name in canonical}
    assert any(not np.array_equal(work[name], canonical[name])
               for name in ('seller_order', 'household_order', 'owner_order')), \
        'ожидалось, что рабочие копии действительно перемешаны'


def test_tile_resource_change_invalidates_enterprise_flags():
    """Ресурсы клетки меняются не через invalidate_routes.

    Флаг «у провинции есть ресурс фирмы» обязан пересобраться: иначе после
    правки карты предприятие осталось бы с устаревшим resource_ok. Буферы
    данных переиспользуются, поэтому снимки делаются копиями.
    """
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    before = np.array(first['enterprise_resource_ok'], copy=True)
    tile = next(iter(instance.tiles.values()))
    original = list(tile['resources'])
    try:
        tile['resources'] = []
        second, _ = pack_update_prices(instance)
        after = np.array(second['enterprise_resource_ok'], copy=True)
        assert list(after) != list(before), \
            'после удаления ресурсов карты флаги обязаны сброситься'
        assert any(flag == 0 for flag in after)
    finally:
        tile['resources'] = original
    restored, _ = pack_update_prices(instance)
    assert list(restored['enterprise_resource_ok']) == list(before)


def test_state_reset_clears_everything():
    instance = engine()
    instance.step(native_mode='cython')
    first, _ = pack_update_prices(instance)
    state = instance._native_update_state
    state.reset()
    assert state.data == {} and state.static == {} and state.layout_key is None
    second, _ = pack_update_prices(instance)
    assert instance._native_update_state is state
    assert_same_pack(first, second)


def test_new_state_is_independent_of_previous_world():
    first_world = engine()
    second_world = engine()
    first_world.step(native_mode='cython')
    second_world.step(native_mode='cython')
    left, _ = pack_update_prices(first_world)
    right, _ = pack_update_prices(second_world)
    # У миров одинаковый seed и одинаковое состояние, но массивы свои.
    assert first_world._native_update_state is not second_world._native_update_state
    for name in ('account_stock', 'sell_good', 'enterprise_history_sold'):
        assert left[name] is not right[name], name
        assert np.array_equal(left[name], right[name], equal_nan=True), name


def test_native_state_class_roundtrips_copy_and_pickle():
    state = NativeUpdateState()
    state.buffer('probe', (3,), np.int32, 7)
    assert list(state.data['probe']) == [7, 7, 7]
    clone = copy.deepcopy(state)
    assert clone.data == {}
    assert copy.copy(state).data == {}
    import pickle
    restored = pickle.loads(pickle.dumps(state))
    assert restored.data == {}
    # Заполнитель по умолчанию переиспользует память, а не выделяет новую.
    buffer = state.buffer('probe', (3,), np.int32, 0)
    assert list(buffer) == [0, 0, 0]
    assert state.buffer('probe', (3,), np.int32, None) is buffer
