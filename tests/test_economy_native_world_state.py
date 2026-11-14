"""Тесты единого persistent WorldState: владелец существующего состояния.

Шаг архитектурный: ``WorldState`` не заводит новых полей экономики и не
дублирует данные. Он держит ссылки на уже существующие хранилища —
``OrderStore``, ``CashStore`` и native update state, — поэтому большая часть
проверок смотрит на тождество объектов, а не на значения: вторая копия
mutable-состояния не должна появиться.

Отдельный блок — сверка раскладки. ``step`` пересобирает ``report`` и
пересортировывает ``accounts``, так что ``WorldState`` обязан перепривязать
изменившуюся часть и переиспользовать неизменившуюся. Это проверяется по
тождеству до и после хода.

Последний блок — exact replay реального мира и RNG: наличие контейнера не
должно менять ни состояние, ни расход случайных чисел.
"""
import copy
import hashlib
import json
import pickle
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from modules.economy import Economy  # noqa: E402
from modules.economy.native import world_boundary  # noqa: E402

KERNEL = world_boundary.load_world_state()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение world_state не собрано')


# --- 1. Контейнер строится по Economy и переиспользуется -----------------
def test_world_state_is_persistent_between_calls():
    """Один и тот же контейнер живёт на Economy между ходами."""
    engine = build_engine()
    first = engine.world_state
    second = engine.world_state
    assert first is second
    assert engine.__dict__['_world_state'] is first
    engine.step()
    # После хода перепривязываются хранилища, а не сам контейнер.
    assert engine.world_state is first


def test_world_state_uses_existing_order_store():
    """Контейнер держит тот же OrderStore, а не вторую копию заявок."""
    engine = build_engine()
    state = engine.world_state
    assert state.orders is engine.order_store
    assert state.orders.orders is engine.report['orders']
    state.orders.append(dict(id='probe', participant='h', good='grain', quantity=1.0,
                             remaining=1.0, reserved=0.0, status='active', price=1.0,
                             side='sell'))
    # Мутация через контейнер видна старому пути: объекты те же.
    assert engine.report['orders'][-1]['id'] == 'probe'
    assert engine.order_store.orders[-1]['id'] == 'probe'


def test_world_state_uses_existing_cash_store():
    """Контейнер держит тот же CashStore, а не вторую копию счетов."""
    engine = build_engine()
    state = engine.world_state
    assert state.cash is engine.cash_store
    assert state.cash.accounts is engine.accounts
    account_id = next(iter(engine.accounts))
    before = engine.accounts[account_id]['balance']
    state.cash.add_balance(account_id, 1.5)
    assert engine.accounts[account_id]['balance'] == before+1.5
    assert engine.cash_store.get_balance(account_id) == before+1.5


def test_world_state_holds_native_update_state():
    """Native update state — тот же объект, что в Economy, без пересоздания."""
    engine = build_engine()
    engine.step(native_mode='cython')
    state = engine.world_state
    assert state.native is engine.__dict__['_native_update_state']
    assert state.native is not None
    engine.step(native_mode='cython')
    # Update state переиспользуется движком, значит и контейнером.
    assert engine.world_state.native is engine.__dict__['_native_update_state']


def test_world_state_without_native_state_is_still_bound():
    """Без native-хода update state отсутствует: контейнер это допускает."""
    engine = build_engine()
    state = engine.world_state
    assert state.native is None
    assert state.snapshot_metadata()['native_update_state']['bound'] is False


# --- 2. Повторный sync и инвалидация раскладки ---------------------------
def test_repeated_sync_reuses_state_without_layout_change():
    """Без смены контейнеров sync ничего не пересоздаёт."""
    engine = build_engine()
    state = engine.world_state
    orders, cash = state.orders, state.cash
    version = state.version
    for _ in range(3):
        assert state.sync(engine) is state
    assert state.orders is orders
    assert state.cash is cash
    assert state.version == version


def test_orders_layout_change_invalidates_orders_only():
    """Новый список заявок перепривязывает заявки и не трогает деньги."""
    engine = build_engine()
    state = engine.world_state
    cash, native = state.cash, state.native
    old_orders = engine.report['orders']
    engine.report['orders'] = list(old_orders)
    state.sync(engine)
    assert state.orders is engine.order_store
    assert state.orders.orders is engine.report['orders']
    assert state.orders.orders is not old_orders
    assert state.cash is cash
    assert state.native is native


def test_accounts_layout_change_invalidates_cash_only():
    """Пересобранные счета перепривязывают деньги и не трогают заявки."""
    engine = build_engine()
    state = engine.world_state
    orders = state.orders
    engine.accounts = dict(sorted(engine.accounts.items(), reverse=True))
    state.sync(engine)
    assert state.cash is engine.cash_store
    assert state.cash.accounts is engine.accounts
    assert state.orders is orders


def test_engine_cache_replacement_is_adopted():
    """Движок подменил свой кэш границы: контейнер берёт новый, не старый."""
    engine = build_engine()
    state = engine.world_state
    replacing = type(engine.order_store)(engine.report['orders'])
    engine.__dict__['_order_store'] = replacing
    state.sync(engine)
    assert state.orders is replacing
    assert state.orders is engine.order_store


def test_reset_drops_references_without_touching_values():
    """reset забывает привязки, но не чистит заявки и счета."""
    engine = build_engine()
    state = engine.world_state
    orders_count = len(engine.report['orders'])
    accounts_count = len(engine.accounts)
    snapshot = dict(balance=next(iter(engine.accounts.values()))['balance'])
    state.reset()
    assert state.orders is None and state.cash is None and state.native is None
    assert len(engine.report['orders']) == orders_count
    assert len(engine.accounts) == accounts_count
    assert next(iter(engine.accounts.values()))['balance'] == snapshot['balance']
    state.sync(engine)
    assert state.orders is engine.order_store
    assert state.cash is engine.cash_store


def test_step_rebinds_both_parts_after_rebuild():
    """``step`` пересобирает отчёт и счета: обе части перепривязываются."""
    engine = build_engine()
    before = engine.world_state
    old_orders, old_cash = before.orders, before.cash
    engine.step()
    after = engine.world_state
    assert after is before
    assert after.orders is not old_orders
    assert after.cash is not old_cash
    assert after.orders.orders is engine.report['orders']
    assert after.cash.accounts is engine.accounts


# --- 3. deepcopy и pickle ------------------------------------------------
def test_deepcopy_engine_does_not_share_world_state():
    """Копия движка получает независимый контейнер над своими контейнерами."""
    engine = build_engine()
    engine.step()
    clone = copy.deepcopy(engine)
    assert clone.world_state is not engine.world_state
    assert clone.world_state.orders is not engine.world_state.orders
    assert clone.world_state.cash is not engine.world_state.cash
    assert clone.world_state.orders.orders is clone.report['orders']
    assert clone.world_state.cash.accounts is clone.accounts
    assert clone.world_state.orders.orders is not engine.report['orders']


def test_deepcopy_world_state_is_unbound_and_independent():
    """Копия контейнера пуста: значения живут в движке, а не в контейнере."""
    engine = build_engine()
    state = engine.world_state
    clone = copy.deepcopy(state)
    assert clone is not state
    assert clone.orders is None and clone.cash is None and clone.native is None
    clone.sync(engine)
    assert clone.orders is engine.order_store


def test_pickle_round_trip_restores_binding():
    engine = build_engine()
    state = engine.world_state
    restored = pickle.loads(pickle.dumps(state))
    assert restored.orders is None
    restored.sync(engine)
    assert restored.orders is engine.order_store
    assert restored.cash is engine.cash_store


# --- 4. snapshot_metadata не отдаёт mutable внутренности -----------------
def test_snapshot_metadata_hides_live_objects():
    """Снимок состоит из чисел и кортежей; живых хранилищ в нём нет."""
    engine = build_engine()
    engine.step(native_mode='cython')
    state = engine.world_state
    meta = state.snapshot_metadata()
    assert meta['orders']['count'] == len(engine.report['orders'])
    assert meta['accounts']['count'] == len(engine.accounts)
    assert meta['native_update_state']['bound'] is True
    # Ни один живой объект движка не выдан наружу.
    values = _values(meta)
    assert state.orders not in values
    assert state.cash not in values
    assert state.native not in values
    assert engine.report['orders'] not in values
    assert engine.accounts not in values
    # Изменение снимка не влияет на состояние.
    meta['orders']['count'] = -1
    assert len(engine.report['orders']) == state.snapshot_metadata()['orders']['count']


def test_snapshot_metadata_layout_is_immutable():
    """Раскладка native state отдаётся кортежами, а не живым объектом."""
    engine = build_engine()
    engine.step(native_mode='cython')
    state = engine.world_state
    layout = state.snapshot_metadata()['native_update_state']['layout']
    assert layout is not None
    assert isinstance(layout, tuple)
    for item in layout:
        assert not isinstance(item, list)
        assert isinstance(item, (tuple, str, int, float, bool, type(None)))


def test_snapshot_metadata_counts_match_columns():
    engine = build_engine()
    engine.step()
    meta = engine.world_state.snapshot_metadata()
    assert meta['orders']['count'] == meta['orders']['columns'] == len(engine.report['orders'])
    assert meta['accounts']['count'] == meta['accounts']['columns'] == len(engine.accounts)


def _values(meta):
    """Все значения снимка одним списком (для проверки отсутствия живых ссылок)."""
    found = []
    stack = [meta]
    while stack:
        value = stack.pop()
        found.append(value)
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    return found


# --- 5. Граница native/Python --------------------------------------------
def test_boundary_binds_compiled_class():
    assert world_boundary.NATIVE_WORLD_STATE is True
    assert world_boundary.WorldState is KERNEL.WorldState
    assert world_boundary.WorldState is not world_boundary.WorldStatePython
    assert world_boundary.WorldState.__module__.endswith('world_state')


def test_reference_state_has_the_same_surface():
    for name in ('from_economy', 'sync', 'reset', 'snapshot_metadata'):
        assert callable(getattr(world_boundary.WorldStatePython, name))
        assert callable(getattr(world_boundary.WorldState, name))


def test_fallback_used_when_extension_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('world_state'):
            raise ImportError("cannot import name 'world_state'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert world_boundary.load_world_state() is None


def test_python_reference_binds_the_same_objects():
    """Эталон сверяет раскладку теми же атрибутами, что и native-класс."""
    engine = build_engine()
    engine.step()
    reference = world_boundary.WorldStatePython.from_economy(engine)
    native = world_boundary.WorldState.from_economy(engine)
    assert reference.orders is native.orders is engine.order_store
    assert reference.cash is native.cash is engine.cash_store
    assert reference.snapshot_metadata()['orders'] == native.snapshot_metadata()['orders']
    assert (reference.snapshot_metadata()['accounts']
            == native.snapshot_metadata()['accounts'])


# --- 6. Реальный месяц -----------------------------------------------
def test_month_keeps_world_state_bound_to_live_containers():
    engine = build_engine()
    engine.step()
    state = engine.world_state
    assert state.orders.orders is engine.report['orders']
    assert state.cash.accounts is engine.accounts
    state.orders.sync()
    assert len(state.orders.remaining) == len(engine.report['orders'])
    for account_id, live in engine.accounts.items():
        assert state.cash.get_balance(account_id) == live['balance']
        assert state.cash.get_reserved_cash(account_id) == live.get('reserved', 0)


def test_native_month_keeps_world_state_bound():
    engine = build_engine()
    engine.step()
    engine.step(native_mode='cython')
    state = engine.world_state
    assert state.orders.orders is engine.report['orders']
    assert state.cash.accounts is engine.accounts
    assert state.native is engine.__dict__['_native_update_state']
    assert len(state.orders.remaining) == len(engine.report['orders'])


def test_world_state_does_not_change_game_values():
    """Создание контейнера и sync не меняют ни одного значения игры."""
    engine = build_engine()
    engine.step()
    before = game_digest(engine)
    state = world_boundary.WorldState.from_economy(engine)
    state.sync(engine)
    state.snapshot_metadata()
    state.reset()
    state.sync(engine)
    assert game_digest(engine) == before


# --- 7. Exact replay: состояние и RNG -----------------------------------
def state_digest(engine):
    payload = {
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'trades': engine.report['trades'],
        'flows': engine.report['flows'],
        'rng': repr(engine.rng.getstate()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def game_digest(engine):
    payload = {
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'orders': engine.report['orders'],
        'meta_month': engine.meta['month'],
        'rng': repr(engine.rng.getstate()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def run_world(seed, months, python_state=False):
    """Прогон мира; при ``python_state`` контейнер берёт Python-эталон."""
    import scenario_runner as sr
    from audit.profile_run import workdir
    from modules.economy import v3

    if python_state:
        saved = v3._world
        v3._world = types.SimpleNamespace(WorldState=world_boundary.WorldStatePython)
    try:
        with workdir(f'world_state_{seed}_{python_state}') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                digests = []
                for _ in range(months):
                    engine.step(native_mode='cython')
                    engine.validate()
                    digests.append(state_digest(engine))
                return digests, repr(engine.rng.getstate())
            finally:
                db.close()
    finally:
        if python_state:
            v3._world = saved


@pytest.mark.parametrize('seed', [17, 73129])
def test_exact_replay_matches_python_reference(seed):
    """native-контейнер и Python-эталон дают одно состояние и один RNG."""
    native_state, native_rng = run_world(seed, 3)
    python_state, python_rng = run_world(seed, 3, python_state=True)
    assert native_state == python_state
    assert native_rng == python_rng


@pytest.mark.parametrize('seed', [17, 73129])
def test_exact_replay_is_stable_across_runs(seed):
    """Повторный прогон того же seed даёт те же дайджесты и тот же RNG."""
    first_state, first_rng = run_world(seed, 3)
    second_state, second_rng = run_world(seed, 3)
    assert first_state == second_state
    assert first_rng == second_rng


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
