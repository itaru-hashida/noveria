"""Тесты протокола команд/событий и EconomyCore.dispatch на fake economy.

Проверяем только контракт: frozen dataclasses, маршрутизацию двух команд,
проброс payload, возврат событий и отказ на неизвестной команде.
"""
import dataclasses

import pytest

from modules.economy.core_api import EconomyCore
from modules.economy.protocol import Command, Event


class FakeEconomy:
    """Минимальная заглушка Economy: считает вызовы step и отдаёт события."""

    def __init__(self, events=None):
        self.meta = {'month': 0}
        self.report = {}
        self.calls = []
        if events is not None:
            self.events = events

    def step(self, **kwargs):
        self.calls.append(kwargs)
        return 'step-result'


def test_command_and_event_are_frozen_dataclasses():
    assert dataclasses.is_dataclass(Command)
    assert dataclasses.is_dataclass(Event)
    command = Command(type='advance_turn', payload={})
    event = Event(type='month', payload={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.type = 'step'
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.type = 'trade'


def test_command_request_id_defaults_and_keeps_value():
    assert Command(type='advance_turn', payload={}).request_id is None
    command = Command(type='advance_turn', payload={}, request_id='req-42')
    assert command.request_id == 'req-42'
    assert Event(type='month', payload={}).tick is None


def test_advance_turn_calls_step():
    economy = FakeEconomy()
    core = EconomyCore(economy)
    core.dispatch(Command(type='advance_turn', payload={}))
    assert economy.calls == [{}]


def test_step_passes_payload_as_kwargs():
    economy = FakeEconomy()
    core = EconomyCore(economy)
    payload = {'planning_mode': 'lagged_batch', 'native_mode': 'off', 'investment_workers': 2}
    core.dispatch(Command(type='step', payload=payload))
    assert economy.calls == [payload]


def test_dispatch_returns_events_from_drain_events():
    events = [{'type': 'month', 'payload': {'month': 5}}]
    economy = FakeEconomy(events=events)
    core = EconomyCore(economy)
    result = core.dispatch(Command(type='advance_turn', payload={}))
    assert len(result) == 1
    assert isinstance(result[0], Event)
    assert result[0].type == 'month'
    assert result[0].payload == {'month': 5}
    assert economy.events == [], 'dispatch должен опустошить очередь'


def test_dispatch_returns_empty_list_without_events():
    assert EconomyCore(FakeEconomy()).dispatch(Command(type='advance_turn', payload={})) == []


def test_dispatch_keeps_ready_events_as_is():
    ready = Event(type='trade', payload={'qty': 1}, tick=3)
    economy = FakeEconomy(events=[ready])
    result = EconomyCore(economy).dispatch(Command(type='step', payload={}))
    assert result == [ready]


def test_unknown_command_raises_and_does_not_step():
    economy = FakeEconomy()
    core = EconomyCore(economy)
    with pytest.raises(ValueError, match='Неизвестный тип команды'):
        core.dispatch(Command(type='teleport', payload={'x': 1}))
    assert economy.calls == [], 'неизвестная команда не должна вызывать step'
