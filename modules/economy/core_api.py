"""Фасад EconomyCore: стабильная точка входа поверх Python Economy.

Шаг намеренно узкий: фасад только делегирует вызовы существующему
`modules.economy.v3.Economy` и не содержит собственной экономической
логики. Поведение игры не меняется: RNG, порядок исполнения и состояние
Economy остаются нетронутыми. Фасад не добавляет новых полей в Economy.
"""
from .protocol import Command, Event


class EconomyCore:
    """Тонкая обёртка над Economy для будущего Cython-ядра и сервера."""

    def __init__(self, economy):
        self._economy = economy

    @property
    def economy(self):
        """Исходный объект Economy без копирования."""
        return self._economy

    def step(self, **kwargs):
        """Делегирует economy.step(**kwargs) и возвращает его результат."""
        return self._economy.step(**kwargs)

    def snapshot(self):
        """Минимальный read-only snapshot: month и report.

        Глубокая копия мира не делается: report копируется поверхностно,
        отсутствующие поля заменяются безопасными значениями.
        """
        meta = getattr(self._economy, 'meta', None)
        month = meta.get('month') if isinstance(meta, dict) else None
        report = getattr(self._economy, 'report', None)
        report = dict(report) if isinstance(report, dict) else {}
        return {'month': month, 'report': report}

    def drain_events(self):
        """Возвращает накопленные события и очищает очередь.

        Если у текущего Economy ещё нет очереди событий, возвращает [].
        """
        events = getattr(self._economy, 'events', None)
        if not isinstance(events, list):
            return []
        drained = list(events)
        events.clear()
        return drained

    def dispatch(self, command):
        """Выполняет Command и возвращает события шага списком Event.

        `advance_turn` вызывает step() без аргументов, `step` передаёт
        payload как keyword arguments. Неизвестный тип — ValueError до
        любого обращения к Economy, поэтому состояние не меняется.
        """
        if not isinstance(command, Command):
            raise TypeError('Ожидается Command, получено: '+type(command).__name__)
        if command.type == 'advance_turn':
            self.step()
        elif command.type == 'step':
            self.step(**command.payload)
        else:
            raise ValueError('Неизвестный тип команды: '+str(command.type))
        return [_as_event(item) for item in self.drain_events()]


def _as_event(item):
    """Приводит элемент очереди к Event, не копируя готовое событие.

    Пока у Economy нет очереди событий, drain_events() возвращает пустой
    список, так что преобразование не срабатывает. dict превращается в
    Event, готовый Event возвращается как есть.
    """
    if isinstance(item, Event):
        return item
    if isinstance(item, dict):
        return Event(type=item.get('type', ''), payload=item.get('payload', {}))
    return item
