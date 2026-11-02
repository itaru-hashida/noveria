"""Синхронная очередь уведомлений; история хранится независимо в БД."""
import itertools
import logging
from copy import deepcopy

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self.queue = []
        self.handlers = {}
        self.sequence = itertools.count()
        self.processing = False

    def subscribe(self, event_type, handler, priority=5):
        self.handlers.setdefault(event_type, []).append((priority, next(self.sequence), handler))
        self.handlers[event_type].sort(key=lambda h: h[:2])

    def publish(self, event_type, data, source='unknown', priority=5):
        self.queue.append((priority, next(self.sequence), event_type, deepcopy(data), source))
        log.debug('Публикация %s от %s', event_type, source)

    def process_queue(self, max_events=10000):
        # Новые события входят в ту же очередь; повторный вход не запускает второй цикл.
        if self.processing:
            return
        self.processing = True
        count = 0
        try:
            while self.queue:
                if count >= max_events:
                    raise RuntimeError('Превышен лимит обработки очереди')
                self.queue.sort(key=lambda event: event[:2])
                _, _, event_type, data, source = self.queue.pop(0)
                log.debug('Обработка %s от %s', event_type, source)
                for _, _, handler in self.handlers.get(event_type, []):
                    try:
                        handler(deepcopy(data))
                    except Exception:
                        log.exception('Ошибка обработчика %s', event_type)
                count += 1
        finally:
            self.processing = False
