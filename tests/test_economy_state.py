"""Хранение экономики: состояние в памяти и checkpoint в Parquet.

Проверяется архитектура, а не отдельная функция: в обычном ходу экономика не
касается Ladybug, два хода используют один объект состояния, а восстановление из
checkpoint продолжает игру с тем же состоянием — деньги, товары и резервы
сходятся бит-в-бит.

Сквозной регресс ``test_save_load_continues_next_turn_exactly`` проверяет не
только состояние на момент сохранения, а сам следующий ход: контрольная ветка
делает его без перезапуска, вторая — после штатного save/load в свежем
экземпляре. Сравниваются точное состояние, RNG и отчёт, включая порядок заявок
и сделок, их ID, остатки и резервы, в обеих ветках: Python (``off``) и native
(``cython``).
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from core.db import WorldDB
from core.economy_state import EconomyState, checkpoint_size, default_checkpoint_path
from core.events import EventBus
from core.world_service import WorldService
from loader import load_world

DYNAMIC_TABLES = ('EconomyAccount', 'Enterprise', 'EconomyMeta', 'EconomyMonth')
SEED = 73129
DETAIL = 'standard'


def small_world(tmp_path, seed=73129):
    db = WorldDB(tmp_path / 'world.lbug')
    content = json.loads(Path('content/initial_world.json').read_text(encoding='utf-8'))
    content['generation']['seed'] = seed
    scenario = tmp_path / 'scenario.json'
    scenario.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
    load_world(db, scenario)
    service = WorldService(db, EventBus())
    service.initialize_economy()
    return db, service


def fingerprint(state):
    """Канонический отпечаток: канонический JSON вместо repr для порядка ключей."""
    def canonical(value):
        if isinstance(value, dict):
            return {str(k): canonical(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [canonical(v) for v in value]
        if isinstance(value, float):
            return repr(value)
        return value

    engine = state.engine
    payload = dict(
        month=engine.meta['month'],
        gold=repr(engine.gold()),
        accounts={a['id']: dict(kind=a['kind'], balance=repr(a['balance']),
                                reserved=repr(a.get('reserved')),
                                protected=repr(a.get('protected')),
                                stock={g: repr(q) for g, q in sorted(a['stock'].items())},
                                values={g: repr(q) for g, q in sorted(a.get('values', {}).items())},
                                stock_reserved={g: repr(q) for g, q in
                                                sorted(a.get('stock_reserved', {}).items())})
                  for a in engine.accounts.values()},
        enterprises={e['id']: dict(status=e['status'], capacity=repr(e['capacity']),
                                    price=repr(e['price']), output=repr(e['output']),
                                    forecast=repr(e.get('sales_forecast')),
                                    history=canonical(e.get('sales_history')))
                     for e in engine.enterprises.values()},
        population={e['id']: e['properties'].get('population') for e in engine.entities.values()
                    if e['kind'] == 'SocialGroup'},
        market_history=canonical(engine.meta.get('market_history')),
        rng=repr(engine.rng.getstate()),
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_two_turns_use_one_state_object(tmp_path):
    db, service = small_world(tmp_path)
    try:
        state = service.economy_state()
        engine = state.engine
        service.advance_turn(command_id='one', source='test')
        service.advance_turn(command_id='two', source='test')
        assert service.economy_state() is state
        assert state.engine is engine
        assert state.month == 2
        assert state.last_report['month'] == 2
    finally:
        db.close()


@pytest.mark.parametrize('table', DYNAMIC_TABLES)
def test_economy_tables_are_gone_from_ladybug(tmp_path, table):
    db, service = small_world(tmp_path)
    try:
        service.advance_turn(command_id='schema', source='test')
        # Схема больше не содержит динамических таблиц: обращение к ним падает,
        # а не возвращает пустой список.
        with pytest.raises(RuntimeError):
            db.economy_records(table)
        with pytest.raises(RuntimeError):
            db.store_economy(table, [dict(id='x')])
        with pytest.raises(RuntimeError):
            db.economy_month(1)
        with pytest.raises(RuntimeError):
            db.economy_month_ids()
    finally:
        db.close()


def test_ladybug_holds_only_structural_data(tmp_path):
    db, service = small_world(tmp_path)
    try:
        for step in range(3):
            service.advance_turn(command_id=f'structural-{step}', source='test')
        counts = db.counts()
        assert not set(counts) & set(DYNAMIC_TABLES)
        # Счета, предприятия и отчёты не появились в графе под своими ID.
        identities = [e['id'] for e in db.snapshot()['entities']]
        for prefix in ('firm-', 'treasury:', 'merchant:', 'private:', 'municipal:', 'carrier:'):
            assert not [i for i in identities if i.startswith(prefix)]
        # А структура мира и её связи на месте.
        assert counts['State'] and counts['Province'] and counts['City']
        assert set(counts) <= {'State', 'Province', 'City', 'Character', 'SocialGroup',
                               'Army', 'Institution', 'Event'}
    finally:
        db.close()


def test_turn_writes_no_dynamic_economy_to_ladybug(tmp_path, monkeypatch):
    db, service = small_world(tmp_path)
    try:
        calls = []
        for name in ('store_economy', 'economy_records', 'economy_month', 'economy_month_ids'):
            original = getattr(db, name)

            def make(original, name):
                def wrapper(*args, **kwargs):
                    calls.append(name)
                    return original(*args, **kwargs)
                return wrapper

            monkeypatch.setattr(db, name, make(original, name))
        service.advance_turn(command_id='no-db-economy', source='test')
        assert calls == [], f'ход обратился к динамическому хранению: {calls}'
    finally:
        db.close()


def test_checkpoint_round_trip_keeps_state_exactly(tmp_path):
    db, service = small_world(tmp_path)
    try:
        for step in range(1, 4):
            service.advance_turn(command_id=f'round-{step}', source='test')
        state = service.economy_state()
        before = fingerprint(state)
        checkpoint = tmp_path / 'economy'
        state.save_checkpoint(checkpoint)
        assert checkpoint_size(checkpoint)['total'] > 0
    finally:
        db.close()

    with WorldDB(tmp_path / 'world.lbug') as db:
        service = WorldService(db, EventBus())
        loaded = EconomyState.load_checkpoint(checkpoint, service.world_graph())
        service.attach_economy(loaded)
        assert fingerprint(loaded) == before
        assert loaded.engine.meta['month'] == state.engine.meta['month']
        # Восстановление продолжает игру: следующий ход идёт с того же места.
        service.advance_turn(command_id='after-load', source='test')
        assert loaded.engine.meta['month'] == state.engine.meta['month'] + 1


def test_native_storage_commits_monotonic_revision(tmp_path):
    db, service = small_world(tmp_path)
    try:
        state = service.economy_state()
        checkpoint = tmp_path / 'revisioned'
        state.save_checkpoint(checkpoint)
        assert state.committed_revision == 1
        schema = json.loads((checkpoint / 'schema.json').read_text(encoding='utf-8'))
        assert schema['committed_revision'] == 1
        state.save_checkpoint(checkpoint)
        assert state.committed_revision == 2
        schema = json.loads((checkpoint / 'schema.json').read_text(encoding='utf-8'))
        assert schema['committed_revision'] == 2
    finally:
        db.close()

    with WorldDB(tmp_path / 'world.lbug') as db:
        loaded = EconomyState.load_checkpoint(checkpoint, WorldService(db, EventBus()).world_graph())
        assert loaded.committed_revision == 2


@pytest.mark.parametrize('interrupted', [False, True])
def test_checkpoint_publication_preserves_previous_save(tmp_path, monkeypatch, interrupted):
    import os

    db, service = small_world(tmp_path)
    checkpoint = tmp_path / 'economy'
    try:
        service.advance_turn(command_id='before-save', source='test')
        service.save_checkpoint(checkpoint)
        before = state_digest(service.economy_state())
        service.advance_turn(command_id='next-turn', source='test')
        expected = state_digest(service.economy_state())
        replace = os.replace
        error = KeyboardInterrupt if interrupted else OSError

        def fail_publication(source, destination):
            if source == checkpoint.with_suffix('.staging') and destination == checkpoint:
                raise error('publication failed')
            return replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(os, 'replace', fail_publication)
            with pytest.raises(error, match='publication failed'):
                service.save_checkpoint(checkpoint)
    finally:
        db.close()

    with WorldDB(tmp_path / 'world.lbug') as db:
        service = WorldService(db, EventBus())
        loaded = service.load_checkpoint(checkpoint)
        assert state_digest(loaded) == before
        service.advance_turn(command_id='retry-next-turn', source='test')
        assert state_digest(loaded) == expected
        service.save_checkpoint(checkpoint)
        assert state_digest(service.load_checkpoint(checkpoint)) == expected


def test_money_goods_and_reserves_converge_after_restore(tmp_path):
    db, service = small_world(tmp_path)
    try:
        for step in range(1, 4):
            service.advance_turn(command_id=f'money-{step}', source='test')
        state = service.economy_state()
        gold = state.engine.gold()
        stock = state.engine.total_stock()
        reserved = sum(a.get('reserved', 0.0) for a in state.engine.accounts.values())
        checkpoint = tmp_path / 'money'
        state.save_checkpoint(checkpoint)
    finally:
        db.close()

    with WorldDB(tmp_path / 'world.lbug') as db:
        loaded = EconomyState.load_checkpoint(checkpoint,
                                              WorldService(db, EventBus()).world_graph())
        assert loaded.engine.gold() == gold
        assert loaded.engine.total_stock() == stock
        assert sum(a.get('reserved', 0.0) for a in loaded.engine.accounts.values()) == reserved
        assert loaded.engine.meta['initial_gold'] == state.engine.meta['initial_gold']


def test_replay_after_checkpoint_matches_continuous_run(tmp_path):
    db, service = small_world(tmp_path)
    try:
        for step in range(1, 4):
            service.advance_turn(command_id=f'continuous-{step}', source='test')
        state = service.economy_state()
        checkpoint = tmp_path / 'split'
        state.save_checkpoint(checkpoint)
        continuous = []
        for step in range(4, 7):
            service.advance_turn(command_id=f'continuous-{step}', source='test')
            continuous.append(fingerprint(state))
    finally:
        db.close()

    with WorldDB(tmp_path / 'world.lbug') as db:
        replay_service = WorldService(db, EventBus())
        loaded = EconomyState.load_checkpoint(checkpoint, replay_service.world_graph())
        replay_service.attach_economy(loaded)
        replay = []
        for step in range(4, 7):
            replay_service.advance_turn(command_id=f'replay-{step}', source='test')
            replay.append(fingerprint(loaded))
        # Повтор уже выполненной команды не двигает экономику дважды: защита от
        # двойного хода теперь обязана работать и для состояния в памяти.
        replay_service.advance_turn(command_id='replay-6', source='test')
        assert fingerprint(loaded) == replay[-1]
    assert replay == continuous


def test_12_and_36_months_keep_one_state_and_replay_exactly(tmp_path):
    """Приёмка миграции на длинном прогоне, а не только на игрушечном ходе."""
    db, service = small_world(tmp_path)
    try:
        state = service.economy_state()
        engine = state.engine
        checkpoint = tmp_path / 'long-run'
        for month in range(1, 37):
            service.advance_turn(command_id=f'continuous-{month}', source='test')
            assert all(state.last_report['invariants'].values())
            assert service.economy_state() is state
            assert state.engine is engine
            if month == 12:
                state.save_checkpoint(checkpoint)
        continuous = fingerprint(state)
        assert state.month == 36
        assert not set(db.counts()) & set(DYNAMIC_TABLES)
    finally:
        db.close()

    with WorldDB(tmp_path / 'world.lbug') as replay_db:
        replay = WorldService(replay_db, EventBus())
        restored = EconomyState.load_checkpoint(checkpoint, replay.world_graph())
        replay.attach_economy(restored)
        assert restored.month == 12
        for month in range(13, 37):
            replay.advance_turn(command_id=f'replay-{month}', source='test')
            assert all(restored.last_report['invariants'].values())
        assert fingerprint(restored) == continuous


def test_checkpoint_rejects_foreign_world(tmp_path):
    db, service = small_world(tmp_path)
    try:
        state = service.economy_state()
        checkpoint = tmp_path / 'foreign'
        state.save_checkpoint(checkpoint)
        graph = service.world_graph()
        # Другой мир не должен принять чужую экономику: счета ссылаются на его
        # города и клетки.
        graph['snapshot']['entities'] = graph['snapshot']['entities'][:-1]
        with pytest.raises(ValueError, match='другом мире'):
            EconomyState.load_checkpoint(checkpoint, graph)
    finally:
        db.close()


def test_autosave_is_off_by_default(tmp_path):
    db, service = small_world(tmp_path)
    try:
        assert service.autosave == 'off'
        service.advance_turn(command_id='no-autosave', source='test')
        assert not default_checkpoint_path(db).exists()
        service.autosave = 'turn'
        service.advance_turn(command_id='with-autosave', source='test')
        assert default_checkpoint_path(db).exists()
    finally:
        db.close()


def test_explicit_save_and_load_commands(tmp_path):
    db, service = small_world(tmp_path)
    try:
        service.advance_turn(command_id='explicit', source='test')
        path = service.save_checkpoint()
        assert Path(path).exists()
        assert Path(path).parent == Path(db.path).parent
        state = service.load_checkpoint()
        assert state.engine.meta['month'] == 1
    finally:
        db.close()


# --- Сквозной регресс: следующий ход после штатного save/load ---------------

# Поля отчёта, которые содержат wall-clock тайминги и потому не сравниваются.
# Список явный: всё остальное, включая порядок строк, сравнивается без
# округления и без сортировки. На текущем отчёте (`standard`) этих полей нет —
# тест ниже это фиксирует, поэтому появление нового поля таймингов не спрячется.
WALL_CLOCK_FIELDS = ('duration', 'wall', 'timings', 'phase_timings',
                     'elapsed_seconds', 'seconds')


def build_scenario_world(directory, seed=SEED, detail=DETAIL):
    """Мир из штатного сценария: тем же seed'ом, что и у остальных прогонов."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    content = json.loads(Path('content/initial_world.json').read_text(encoding='utf-8'))
    content['generation']['seed'] = seed
    scenario = directory / 'scenario.json'
    scenario.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
    db = WorldDB(directory / 'world.lbug')
    load_world(db, scenario)
    service = WorldService(db, EventBus())
    service.initialize_economy()
    # Режим отчёта задаётся у состояния, а не в БД, — как в audit/scenario_runner.
    service.economy_state().engine.meta['report_detail'] = detail
    return db, service


def force_native_mode(monkeypatch, native_mode):
    """Провести все ходы теста через запрошенную ветку движка."""
    from modules.economy import Economy

    original = Economy.step

    def step(self, *args, **kwargs):
        kwargs['native_mode'] = native_mode
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Economy, 'step', step)


def canonical(value):
    """Точный вид значения: float через repr, порядок списков и ключей сохранён.

    Сортировки нет намеренно: порядок заявок, сделок и потоков — часть
    результата, и перестановка должна ломать сравнение.
    """
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, float):
        return repr(value)
    return value


def state_digest(state):
    """Точное состояние симуляции, RNG и история отчётов."""
    engine = state.engine
    return canonical(dict(
        month=engine.meta.get('month'),
        accounts=engine.accounts,
        enterprises=engine.enterprises,
        entities={e['id']: e['properties'] for e in engine.entities.values()},
        meta={k: v for k, v in engine.meta.items() if k != 'market_history'},
        market_history=engine.meta.get('market_history'),
        receipts=engine.construction_receipts,
        pending=engine.pending_institutions,
        reports=state.reports,
        keep_reports=state.keep_reports,
        rng=repr(engine.rng.getstate()),
    ))


def report_digest(report, stripped=None):
    """Отчёт хода без полей wall-clock; всё остальное — как есть."""
    if isinstance(report, dict):
        result = {}
        for key, value in report.items():
            if key in WALL_CLOCK_FIELDS:
                if stripped is not None:
                    stripped.add(key)
                continue
            result[key] = report_digest(value, stripped)
        return result
    if isinstance(report, (list, tuple)):
        return [report_digest(item, stripped) for item in report]
    return canonical(report)


def order_rows(report):
    """Заявки в исходном порядке: ID, остатки и резервы идут вместе с ними."""
    return [(row['id'], row['side'], row['participant'], row['good'], row['purpose'],
             row['quantity'], row['remaining'], row['reserved'], row['backing'],
             row['price'], row['status'], row['reason'], row['expires'])
            for row in report['orders']]


def trade_rows(report):
    """Сделки в исходном порядке: ID, деньги, количество и пара заявок."""
    return [(row['trade_id'], row['buy_order'], row['sell_order'], row['buyer'],
             row['seller'], row['good'], row['quantity'], row['price'], row['gold'],
             row['freight'], row['tax'], row['seller_gold'])
            for row in report['trades']]


def flow_rows(report):
    return [(row['sender'], row['recipient'], row['gold'], row['reason'],
             row.get('trade_id'), row.get('good')) for row in report['flows']]


def balance_rows(state):
    """Деньги, склад и резервы по счетам — тоже часть следующего хода."""
    engine = state.engine
    return {account['id']: dict(
        balance=repr(account['balance']),
        reserved=repr(account.get('reserved', 0.0)),
        protected=repr(account.get('protected', 0.0)),
        stock={good: repr(quantity) for good, quantity in account['stock'].items()},
        values={good: repr(quantity) for good, quantity in account.get('values', {}).items()},
        stock_reserved={good: repr(quantity)
                        for good, quantity in account.get('stock_reserved', {}).items()},
    ) for account in engine.accounts.values()}


def first_difference(expected, actual, path=''):
    """Первое расхождение двух точных значений: путь, ожидание, факт."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            if key not in actual:
                return (f'{path}/{key}', expected[key], 'ОТСУТСТВУЕТ')
            difference = first_difference(expected[key], actual[key], f'{path}/{key}')
            if difference is not None:
                return difference
        for key in actual:
            if key not in expected:
                return (f'{path}/{key}', 'ОТСУТСТВУЕТ', actual[key])
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return (f'{path}/длина', len(expected), len(actual))
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, f'{path}/{index}')
            if difference is not None:
                return difference
        return None
    if type(expected) is not type(actual):
        return (f'{path}/тип', type(expected).__name__, type(actual).__name__)
    if expected != actual:
        return (path, expected, actual)
    return None


def assert_same_state(expected, actual, what):
    difference = first_difference(expected, actual)
    assert difference is None, f'{what}: первое расхождение {difference}'


@pytest.mark.parametrize('native_mode', ('off', 'cython'))
def test_save_load_continues_next_turn_exactly(tmp_path, monkeypatch, native_mode):
    """Следующий ход после штатного save/load совпадает с непрерывным прогоном.

    Контрольная ветка делает ход без перезапуска. Вторая сохраняет состояние
    штатным ``save_checkpoint``, открывает его штатным ``load_checkpoint`` в
    новом экземпляре мира и делает тот же ход. Свежий экземпляр не должен
    переиспользовать движок, RNG, кэши или in-memory-хранилища контрольной
    ветки, поэтому тождество объектов проверяется отдельно.
    """
    if native_mode == 'cython':
        # Режим заявлен явно: молчаливый уход в Python-эталон скрыл бы предмет
        # проверки, поэтому расширения обязаны быть собраны и загружены.
        from modules.economy.native import cash_boundary, monthly_boundary
        assert monthly_boundary.NATIVE_MONTHLY_DRIVER, \
            'Cython-драйвер месяца не собран: соберите modules/economy/native'
        assert cash_boundary.NATIVE_CASH_STORE, \
            'Cython-хранилище денег не собрано: соберите modules/economy/native'
    force_native_mode(monkeypatch, native_mode)

    control_dir = tmp_path / f'control-{native_mode}'
    fresh_dir = tmp_path / f'fresh-{native_mode}'
    checkpoint = tmp_path / f'economy-{native_mode}'

    # Контрольная ветка: ход, сохранение, следующий ход без перезапуска.
    db, service = build_scenario_world(control_dir)
    try:
        state = service.economy_state()
        service.advance_turn(command_id=f'control-first-{native_mode}', source='test')
        service.save_checkpoint(checkpoint)
        saved_state = state_digest(state)
        service.advance_turn(command_id=f'control-next-{native_mode}', source='test')
        control_state = state_digest(state)
        control_balances = balance_rows(state)
        control_report = report_digest(state.last_report)
        control_orders = order_rows(state.last_report)
        control_trades = trade_rows(state.last_report)
        control_flows = flow_rows(state.last_report)
        control_engine, control_rng = state.engine, state.engine.rng
        control_cash, control_stock = state.engine.cash_store, state.engine.stock_store
        control_orders_store = state.engine.order_store
        control_world = state.engine.__dict__.get('_world_state')
    finally:
        db.close()

    # Свежий экземпляр: новый мир, новая БД, загрузка того же сохранения.
    fresh_db, fresh_service = build_scenario_world(fresh_dir)
    try:
        loaded = fresh_service.load_checkpoint(checkpoint)
        assert loaded is not state, 'загрузка вернула объект контрольной ветки'
        assert loaded.engine is not control_engine, 'движок переиспользован'
        assert loaded.engine.rng is not control_rng, 'RNG переиспользован'
        assert loaded.engine.cash_store is not control_cash, 'CashStore переиспользован'
        assert loaded.engine.stock_store is not control_stock, 'StockStore переиспользован'
        assert loaded.engine.order_store is not control_orders_store, \
            'OrderStore переиспользован'
        if control_world is not None:
            # Native-контейнер создаётся только в ветке ``cython``; если он был,
            # свежий экземпляр обязан построить свой, а не взять чужой.
            assert loaded.engine.__dict__.get('_world_state') is not control_world, \
                'native-контейнер мира переиспользован'
        assert_same_state(saved_state, state_digest(loaded), 'состояние после загрузки')

        fresh_service.advance_turn(command_id=f'replay-next-{native_mode}', source='test')
        assert_same_state(control_state, state_digest(loaded),
                          f'состояние после хода ({native_mode})')
        assert_same_state(control_balances, balance_rows(loaded),
                          f'деньги/склад/резервы после хода ({native_mode})')
        stripped = set()
        replay_report = report_digest(loaded.last_report, stripped)
        assert stripped <= set(WALL_CLOCK_FIELDS), \
            f'исключены не только wall-clock поля: {sorted(stripped)}'
        assert_same_state(control_report, replay_report,
                          f'отчёт следующего хода ({native_mode})')
        assert_same_state(control_orders, order_rows(loaded.last_report),
                          f'порядок заявок и остатки ({native_mode})')
        assert_same_state(control_trades, trade_rows(loaded.last_report),
                          f'порядок сделок и ID ({native_mode})')
        assert_same_state(control_flows, flow_rows(loaded.last_report),
                          f'порядок денежных потоков ({native_mode})')
    finally:
        fresh_db.close()
