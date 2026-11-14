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
    for name in ('from_economy', 'sync', 'reset', 'snapshot_metadata',
                 'begin_step', 'end_step'):
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


# --- 7. Lifecycle native step -------------------------------------------
class LifecycleSpy(world_boundary.WorldState):
    """Контейнер, записывающий lifecycle-вызовы; поведение — родительское.

    Наследник нужен, чтобы увидеть, сколько раз `step` открыл и закрыл шаг.
    Собственных данных экономики он не заводит: только журнал вызовов.
    """

    def __init__(self):
        super().__init__()
        self.calls = []

    def begin_step(self, economy):
        self.calls.append(('begin', economy.meta['month']))
        return super().begin_step(economy)

    def end_step(self, economy, completed=True):
        self.calls.append(('end', completed, economy.meta['month']))
        return super().end_step(economy, completed=completed)


def install_spy(engine):
    """Поставить spy-контейнер в Economy до первого native-шага."""
    spy = LifecycleSpy()
    engine.__dict__['_world_state'] = spy
    return spy


def test_begin_step_called_once_per_native_step():
    """Один native-шаг открывает lifecycle ровно один раз."""
    engine = build_engine()
    spy = install_spy(engine)
    engine.step(native_mode='cython')
    assert [call[0] for call in spy.calls] == ['begin', 'end']


def test_end_step_called_once_after_successful_step():
    """Успешный шаг закрывает lifecycle ровно один раз, с completed=True."""
    engine = build_engine()
    spy = install_spy(engine)
    engine.step(native_mode='cython')
    ends = [call for call in spy.calls if call[0] == 'end']
    assert len(ends) == 1
    assert ends[0][1] is True
    assert spy.active is False
    assert spy.completed_steps == 1
    assert spy.aborted_steps == 0
    assert spy.diagnostics['completed'] is True
    assert spy.diagnostics['step'] == 1


def test_lifecycle_runs_once_per_month():
    """Три native-месяца — ровно три пары begin/end и один контейнер."""
    engine = build_engine()
    spy = install_spy(engine)
    for _ in range(3):
        engine.step(native_mode='cython')
    assert [call[0] for call in spy.calls] == ['begin', 'end']*3
    assert spy.step_index == 3
    assert spy.completed_steps == 3
    assert spy.aborted_steps == 0
    assert engine.world_state is spy


def test_python_mode_does_not_create_world_state():
    """Python mode не создаёт контейнер: свойство остаётся ленивым."""
    engine = build_engine()
    engine.step()
    assert '_world_state' not in engine.__dict__


def test_python_mode_does_not_touch_existing_container():
    """Если контейнер уже был, Python mode не открывает и не закрывает шаг."""
    engine = build_engine()
    spy = install_spy(engine)
    engine.step()
    assert spy.calls == []
    assert spy.active is False
    assert spy.step_index == 0


def test_mixed_modes_open_lifecycle_only_for_native():
    """Чередование режимов: lifecycle идёт только на native-месяцах."""
    engine = build_engine()
    spy = install_spy(engine)
    engine.step()
    engine.step(native_mode='cython')
    engine.step()
    engine.step(native_mode='cython')
    assert [call[0] for call in spy.calls] == ['begin', 'end', 'begin', 'end']
    assert spy.completed_steps == 2


def test_begin_step_during_step_binds_existing_stores():
    """В момент начала шага контейнер смотрит на текущие хранилища движка."""
    engine = build_engine()
    spy = install_spy(engine)
    captured = {}

    class Probe(LifecycleSpy):
        def begin_step(self, economy):
            result = super().begin_step(economy)
            captured['orders'] = self.orders
            captured['cash'] = self.cash
            captured['live_orders'] = self.orders.orders
            captured['live_accounts'] = self.cash.accounts
            return result

    engine.__dict__['_world_state'] = Probe()
    engine.step(native_mode='cython')
    # Привязка идёт к тем объектам, которые `step` уже пересобрал для месяца.
    assert captured['live_accounts'] is engine.accounts
    assert captured['live_orders'] is engine.report['orders']
    assert captured['orders'] is engine.order_store
    assert captured['cash'] is engine.cash_store


def test_begin_step_does_not_change_game_values():
    """begin_step не меняет ни счета, ни заявки, ни RNG."""
    engine = build_engine()
    engine.step()
    before = game_digest(engine)
    state = engine.world_state
    state.begin_step(engine)
    assert game_digest(engine) == before
    state.end_step(engine)


def test_end_step_reports_rebound_layout():
    """Между begin и end отчёт пересобрали: end_step перепривязывается."""
    engine = build_engine()
    state = engine.world_state
    state.begin_step(engine)
    old_orders = state.orders
    engine.report['orders'] = list(engine.report['orders'])
    state.end_step(engine)
    assert state.orders is not old_orders
    assert state.orders.orders is engine.report['orders']
    assert state.diagnostics['rebound'] is True
    assert state.completed_steps == 1


def test_repeated_begin_without_end_is_rejected():
    """Два begin подряд — ошибка вызывающего кода, а не тихая перезапись."""
    engine = build_engine()
    state = engine.world_state
    state.begin_step(engine)
    with pytest.raises(RuntimeError):
        state.begin_step(engine)
    state.end_step(engine)


def test_end_without_begin_is_rejected():
    engine = build_engine()
    state = engine.world_state
    with pytest.raises(RuntimeError):
        state.end_step(engine)


def test_exception_inside_step_leaves_container_healthy():
    """Исключение в native-фазах: шаг закрыт, контейнер пригоден для нового хода."""
    engine = build_engine()
    spy = install_spy(engine)

    class Boom(RuntimeError):
        pass

    def explode(*args, **kwargs):
        raise Boom('сбой внутри шага')

    original = engine.plan_enterprises
    engine.plan_enterprises = explode
    try:
        with pytest.raises(Boom):
            engine.step(native_mode='cython')
    finally:
        engine.plan_enterprises = original

    # Шаг прерван, но флаг снят: следующий begin_step не упрётся в открытый шаг.
    assert spy.active is False
    assert spy.aborted_steps == 1
    assert spy.completed_steps == 0
    assert spy.diagnostics['completed'] is False
    # Ссылки остались рабочими: контейнер смотрит на живые хранилища.
    assert spy.orders is engine.order_store
    assert spy.orders.orders is engine.report['orders']
    assert spy.cash.accounts is engine.accounts
    # Следующий ход проходит штатно и переиспользует тот же контейнер.
    engine.step(native_mode='cython')
    assert spy.active is False
    assert spy.completed_steps == 1
    assert engine.world_state is spy


def test_warm_native_step_reuses_state():
    """Повторный warm step не пересоздаёт контейнер и хранилища месяцев."""
    engine = build_engine()
    spy = install_spy(engine)
    engine.step(native_mode='cython')
    first_orders, first_cash = spy.orders, spy.cash
    first_sequence = list(spy.calls)
    engine.step(native_mode='cython')
    assert engine.world_state is spy
    assert len(spy.calls) == len(first_sequence)+2
    assert spy.calls[:len(first_sequence)] == first_sequence
    # Хранилища перепривязаны к новому месяцу, а не унаследованы от прошлого.
    assert spy.orders is not first_orders
    assert spy.cash is not first_cash
    assert spy.orders.orders is engine.report['orders']
    assert spy.cash.accounts is engine.accounts
    assert spy.completed_steps == 2


def test_deepcopy_has_independent_lifecycle():
    """Копия движка не наследует открытый шаг и считает свои шаги.

    Копия снимается с движка после Python-месяца: ``deepcopy`` движка с живым
    native update state пока не поддержан (memoryview в его полях), и это
    свойство самого update state, а не контейнера. Проверяется lifecycle.
    """
    engine = build_engine()
    engine.step()
    state = engine.world_state
    state.begin_step(engine)
    clone = copy.deepcopy(engine)
    assert clone.world_state is not state
    assert clone.world_state.active is False
    assert clone.world_state.step_index == 0
    assert clone.world_state.completed_steps == 0
    # Шаг оригинала продолжается и закрывается независимо от копии.
    state.end_step(engine)
    assert state.completed_steps == 1
    assert clone.world_state.completed_steps == 0
    # Копия проходит свой ход на своих хранилищах, оригинал не меняется.
    clone.step()
    assert clone.world_state.orders.orders is clone.report['orders']
    assert clone.world_state.cash.accounts is clone.accounts


def test_lifecycle_does_not_change_game_values():
    """WorldState не трогает значения: digest игры тот же до и после шага.

    Native-фазы — единственный источник изменений месяца. Контейнер лишь
    привязывается и закрывает шаг, поэтому повторный проход ``begin``/``end``
    поверх готового месяца не должен сдвинуть ни одного числа и ни одного
    байта RNG.
    """
    engine = build_engine()
    engine.step(native_mode='cython')
    before = game_digest(engine)
    before_state = state_digest(engine)
    state = engine.world_state
    state.begin_step(engine)
    state.end_step(engine)
    assert game_digest(engine) == before
    assert state_digest(engine) == before_state


def test_snapshot_metadata_reports_lifecycle():
    """Сводка показывает счётчики шагов, не выдавая живых объектов."""
    engine = build_engine()
    engine.step(native_mode='cython')
    meta = engine.world_state.snapshot_metadata()
    assert meta['active'] is False
    assert meta['step_index'] == 1
    assert meta['completed_steps'] == 1
    assert meta['aborted_steps'] == 0
    assert meta['diagnostics']['completed'] is True
    diagnostics = _values(meta['diagnostics'])
    assert engine.report['orders'] not in diagnostics
    assert engine.accounts not in diagnostics


# --- 8. Exact replay: состояние и RNG -----------------------------------
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


def run_world(seed, months, python_state=False, native_mode='cython'):
    """Прогон мира; при ``python_state`` контейнер берёт Python-эталон."""
    import scenario_runner as sr
    from audit.profile_run import workdir
    from modules.economy import v3

    if python_state:
        saved = v3._world
        v3._world = types.SimpleNamespace(WorldState=world_boundary.WorldStatePython)
    try:
        with workdir(f'world_state_{seed}_{python_state}_{native_mode}') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                digests = []
                for _ in range(months):
                    engine.step(native_mode=native_mode)
                    engine.validate()
                    digests.append(state_digest(engine))
                return digests, repr(engine.rng.getstate())
            finally:
                db.close()
    finally:
        if python_state:
            v3._world = saved


def run_world_engine(seed, months):
    """Прогнать реальный мир и вернуть живой движок (для проверки lifecycle)."""
    import scenario_runner as sr
    from audit.profile_run import workdir

    with workdir(f'world_state_lifecycle_{seed}') as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            engine = service.economy_state().engine
            for _ in range(months):
                engine.step(native_mode='cython')
            engine.validate()
            return engine
        finally:
            db.close()


@pytest.mark.parametrize('seed', [17, 73129])
def test_exact_replay_matches_python_reference(seed):
    """native-контейнер и Python-эталон дают одно состояние и один RNG."""
    native_state, native_rng = run_world(seed, 3)
    python_state, python_rng = run_world(seed, 3, python_state=True)
    assert native_state == python_state
    assert native_rng == python_rng


@pytest.mark.parametrize('seed', [17, 73129])
def test_lifecycle_runs_every_native_month_of_real_world(seed):
    """В реальном мире каждый из трёх native-месяцев закрыл свой lifecycle."""
    engine = run_world_engine(seed, 3)
    state = engine.world_state
    assert state.completed_steps == 3
    assert state.aborted_steps == 0
    assert state.active is False
    assert state.step_index == 3
    assert state.diagnostics['step'] == 3
    assert state.orders.orders is engine.report['orders']
    assert state.cash.accounts is engine.accounts


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
