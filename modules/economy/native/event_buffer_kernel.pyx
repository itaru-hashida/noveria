# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Event buffer kernel: упорядоченные event records, фильтрация и батчи.

Срез, который остаётся синхронным в Cython:

  * создание упорядоченных буферизованных записей события (``seq`` присваивается
    в порядке вызовов, как и раньше присваивался ``event_id``);
  * фильтрация standard/deep: канал ``events`` открыт только в ``deep``,
    ``diagnostics`` и ``history`` — в ``standard``/``deep``;
  * batching: записи накапливаются в батчи ровно по ``batch_size``, остаток
    уходит только при ``flush``;
  * учёт backpressure и явная политика отбрасывания diagnostic-only событий.

Ядро ничего не форматирует и ничего не пишет: ни строк, ни JSON, ни файлов.
Оно держит только числа, порядок и ссылки на уже готовые строки отчёта,
поэтому тяжёлая сериализация остаётся фоновому writer'у, а игровой цикл,
state, settlement и RNG не трогаются. Второго ledger'а/состояния ядро не
заводит: ``report['events']`` по-прежнему единственный источник событий
replay, а буфер — лишь упорядоченный поток вывода.
"""

REPLAY = 'replay'
DIAGNOSTIC = 'diagnostic'

EVENTS = 'events'
DIAGNOSTICS = 'diagnostics'
HISTORY = 'history'

DETAILS = ('none', 'standard', 'deep')


cdef class EventRecorder:
    """Упорядоченный буфер событий месяца с фильтрацией, батчами и backpressure.

    ``record`` вызывается синхронно из игрового цикла и обязан быть дешёвым:
    он не форматирует строки и не касается диска. ``drain`` отдаёт готовые
    батчи в порядке записи, ``flush`` — остаток незавершённого батча.
    """

    cdef public object detail
    cdef public int batch_size
    cdef public long capacity
    cdef public bint drop_diagnostic
    cdef public object _pending
    cdef public object _batches
    cdef public long seq
    cdef public long accepted
    cdef public long replay_accepted
    cdef public long filtered
    cdef public long dropped
    cdef public long batches
    cdef public long backpressure
    cdef long _queued

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
        if channel == DIAGNOSTICS or channel == HISTORY:
            return self.detail != 'none'
        raise ValueError('Неизвестный канал вывода: '+str(channel))

    # --- запись -----------------------------------------------------------
    def record(self, row, kind=REPLAY, channel=EVENTS):
        """Добавить упорядоченную запись; вернуть True, если она принята.

        state-affecting (``replay``) записи не отбрасываются никогда: при
        переполнении они принимаются, а переполнение явно учитывается в
        ``backpressure``. Diagnostic-only записи отбрасываются только при
        явно включённой политике ``drop_diagnostic``.
        """
        if not self.channel_open(channel):
            self.filtered += 1
            return False
        cdef bint replay = kind == REPLAY
        if self._queued >= self.capacity:
            if replay or not self.drop_diagnostic:
                # Simulation никогда не блокируется: запись принимается, а
                # переполнение видно в счётчике и требует flush.
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
        """Добавить пачку записей, сохранив их порядок; вернуть число принятых.

        Числовой цикл пачки живёт здесь, поэтому обход сотен diagnostics-строк
        не превращается в сотни переходов Python→Cython.
        """
        if not self.channel_open(channel):
            self.filtered += len(rows)
            return 0
        cdef bint replay = kind == REPLAY
        cdef long accepted = 0
        cdef object row
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
        cdef object batches = self._batches
        cdef long size = 0
        cdef object batch
        self._batches = []
        for batch in batches:
            size += len(batch)
        self._queued -= size
        return batches

    def flush(self):
        """Отдать все батчи и остаток незавершённого, опустошив очередь."""
        cdef object batches = self._batches
        cdef object rest = self._pending
        cdef long size = 0
        cdef object batch
        self._batches = []
        self._pending = []
        if rest:
            batches.append(rest)
        for batch in batches:
            size += len(batch)
        self._queued -= size
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
