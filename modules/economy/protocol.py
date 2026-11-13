"""Протокол команд и событий для будущего server loop и TCP/Godot-клиента.

Только структуры данных, без экономической логики и без транспорта:
единственная точка соприкосновения с движком — `EconomyCore.dispatch`.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    """Команда от клиента: тип, нагрузка и необязательный id запроса."""

    type: str
    payload: dict[str, Any]
    request_id: str | None = None


@dataclass(frozen=True)
class Event:
    """Событие, порождённое ходом: тип, нагрузка и необязательный номер тика."""

    type: str
    payload: dict[str, Any]
    tick: int | None = None
