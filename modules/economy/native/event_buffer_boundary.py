"""Граница event-buffer ядра: одна привязка реализации на импорт.

Синхронная часть пайплайна событий живёт в Cython (``event_buffer_kernel``):
упорядоченные записи, фильтрация ``standard``/``deep``, батчи, backpressure и
учёт отброшенных diagnostic-only событий. Здесь — эталонная Python-реализация
того же буфера (образец для focused-тестов и fallback, если расширение не
собрано) и привязка реализации один раз при импорте.

Границы, которые модуль соблюдает:

  * буфер — это поток вывода, а не состояние экономики: он не держит ни
    счетов, ни склада, ни заявок, ни RNG и не создаёт второго ledger'а;
  * ``report['events']`` остаётся единственным источником событий replay:
    записи в буфер лишь ссылаются на уже созданные строки отчёта, поэтому
    порядок и количество state-affecting событий не меняются;
  * фильтрация — та же, что была в ``build_causal_events``: ``events`` только в
    ``deep``, ``diagnostics``/``history`` — в ``standard`` и ``deep``;
  * форматирование, JSON/Parquet/SQLite и доставка в UI — не здесь: ими
    занимается фоновый writer (``event_writer``), а не игровой цикл.
"""

import importlib

# Виды записей: replay-события нельзя терять, diagnostic-only — можно, и
# только по явной политике буфера.
REPLAY = 'replay'
DIAGNOSTIC = 'diagnostic'

# Каналы вывода месяца.
EVENTS = 'events'
DIAGNOSTICS = 'diagnostics'
HISTORY = 'history'

DETAILS = ('none', 'standard', 'deep')


class EventRecorderPython(object):
    """Эталон ``EventRecorder``: те же записи, фильтры, батчи и счётчики."""

    def __init__(self, detail='none', batch_size=256, capacity=4096,
                 drop_diagnostic=False):
        self.configure(detail, batch_size, capacity, drop_diagnostic)

    def configure(self, detail='none', batch_size=256, capacity=4096,
                  drop_diagnostic=False):
        """Задать детализацию, размер батча, предел очереди и политику отбрасывания."""
        if detail not in DETAILS:
            raise ValueError('Неизвестная детализация буфера: '+str(detail))
        if int(batch_size) < 1:
            raise ValueError('Размер батча обязан быть больше нуля')
        if int(capacity) < 1:
            raise ValueError('Предел очереди обязан быть больше нуля')
        self.detail = detail
        self.batch_size = int(batch_size)
        self.capacity = int(capacity)
        self.drop_diagnostic = bool(drop_diagnostic)
        self._pending = []
        self._batches = []
        self._queued = 0
        self.seq = 0
        self.accepted = 0
        self.replay_accepted = 0
        self.filtered = 0
        self.dropped = 0
        self.batches = 0
        self.backpressure = 0

    # --- фильтрация -------------------------------------------------------
    def channel_open(self, channel=EVENTS):
        """Открыт ли канал вывода при текущей детализации отчёта."""
        if channel == EVENTS:
            return self.detail == 'deep'
        if channel in (DIAGNOSTICS, HISTORY):
            return self.detail != 'none'
        raise ValueError('Неизвестный канал вывода: '+str(channel))

    # --- запись -----------------------------------------------------------
    def record(self, row, kind=REPLAY, channel=EVENTS):
        """Добавить упорядоченную запись; вернуть True, если она принята."""
        if not self.channel_open(channel):
            self.filtered += 1
            return False
        replay = kind == REPLAY
        if self._queued >= self.capacity:
            if replay or not self.drop_diagnostic:
                self.backpressure += 1
            else:
                self.dropped += 1
                return False
        self.seq += 1
        self._pending.append((self.seq, kind, row))
        self._queued += 1
        self.accepted += 1
        if replay:
            self.replay_accepted += 1
        if len(self._pending) >= self.batch_size:
            self._batches.append(self._pending)
            self._pending = []
            self.batches += 1
        return True

    # --- выдача -----------------------------------------------------------
    def record_rows(self, rows, kind=REPLAY, channel=EVENTS):
        """Добавить пачку записей, сохранив их порядок; вернуть число принятых."""
        if not self.channel_open(channel):
            self.filtered += len(rows)
            return 0
        replay = kind == REPLAY
        accepted = 0
        for row in rows:
            if self._queued >= self.capacity:
                if replay or not self.drop_diagnostic:
                    self.backpressure += 1
                else:
                    self.dropped += 1
                    continue
            self.seq += 1
            self._pending.append((self.seq, kind, row))
            self._queued += 1
            self.accepted += 1
            if replay:
                self.replay_accepted += 1
            accepted += 1
            if len(self._pending) >= self.batch_size:
                self._batches.append(self._pending)
                self._pending = []
                self.batches += 1
        return accepted

    def drain(self):
        """Отдать завершённые батчи в порядке записи; неполный батч остаётся."""
        batches, self._batches = self._batches, []
        self._queued -= sum(len(batch) for batch in batches)
        return batches

    def flush(self):
        """Отдать все батчи и остаток незавершённого, опустошив очередь."""
        batches, self._batches = self._batches, []
        rest, self._pending = self._pending, []
        if rest:
            batches.append(rest)
        self._queued -= sum(len(batch) for batch in batches)
        return batches

    # --- наблюдение -------------------------------------------------------
    def queued(self):
        """Сколько записей ещё ждёт выдачи."""
        return self._queued

    def pending(self):
        """Сколько записей в незавершённом батче."""
        return len(self._pending)

    def stats(self):
        """Числовая сводка буфера: counters, политика и порядок."""
        return dict(detail=self.detail, batch_size=self.batch_size,
                    capacity=self.capacity, drop_diagnostic=self.drop_diagnostic,
                    seq=self.seq, accepted=self.accepted,
                    replay_accepted=self.replay_accepted, filtered=self.filtered,
                    dropped=self.dropped, batches=self.batches,
                    backpressure=self.backpressure, queued=self._queued,
                    pending=len(self._pending))


def load_event_buffer_kernel():
    """Вернуть native-модуль event_buffer_kernel или None.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.event_buffer_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_event_buffer_kernel()
if _kernel is not None:
    EventRecorder = _kernel.EventRecorder
    NATIVE_KERNEL = True
else:
    EventRecorder = EventRecorderPython
