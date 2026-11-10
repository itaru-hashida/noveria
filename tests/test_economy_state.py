"""Хранение экономики: состояние в памяти и checkpoint в Parquet.

Проверяется архитектура, а не отдельная функция: в обычном ходу экономика не
касается Ladybug, два хода используют один объект состояния, а восстановление из
checkpoint продолжает игру с тем же состоянием — деньги, товары и резервы
сходятся бит-в-бит.
"""
import hashlib
import json
from pathlib import Path

import pytest

from core.db import WorldDB
from core.economy_state import EconomyState, checkpoint_size, default_checkpoint_path
from core.events import EventBus
from core.world_service import WorldService
from loader import load_world

DYNAMIC_TABLES = ('EconomyAccount', 'Enterprise', 'EconomyMeta', 'EconomyMonth')


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
