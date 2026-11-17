"""Фоновый writer вывода событий: форматирование и запись вне игрового цикла.

Разделение ответственности, которое держит этот модуль:

  * синхронно в Cython (``event_buffer_kernel``) остаются упорядоченные записи,
    числа, фильтрация ``standard``/``deep``, батчи и backpressure;
  * здесь — только фон: форматирование строк, JSON/Parquet/SQLite/файлы,
    доставка в UI/telemetry и тяжёлая сериализация.

Writer ничего не решает про экономику: он получает уже готовые батчи записей
(ссылки на строки отчёта) и не имеет доступа к счетам, складу, заявкам или
RNG. Второго ledger'а и второго состояния он не заводит.

Игровой цикл writer не блокирует: ``submit`` никогда не ждёт, а
``Queue.put_nowait`` ловит переполнение и учитывает его как backpressure.
Размер очереди ограничен, поэтому память не растёт бесконечно. Полная запись
на диск происходит в фоновом потоке; ``flush``/``close`` дожидаются её только
там, где это действительно нужно (завершение месяца/сессии).

Политика отбрасывания явная: state-affecting (``replay``) записи не
отбрасываются никогда — при переполнении они принимаются, а переполнение
видно в ``stats()['backpressure']``. Diagnostic-only записи отбрасываются
только если политика ``drop_diagnostic`` включена явно.
"""

import json
import queue
import threading
from collections import deque

from .event_buffer_boundary import DIAGNOSTIC, REPLAY


# ---------------------------------------------------------------------------
# Форматирование: только фон, игровой цикл этих функций не зовёт.
# ---------------------------------------------------------------------------

def format_jsonl(batch):
    """Отформатировать батч как JSONL: одна строка на запись, порядок сохранён."""
    return ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                              default=str)+'\n' for _, _, row in batch)


def json_rows(batch):
    """Плоский список готовых к сериализации словарей того же порядка."""
    return [dict(row) for _, _, row in batch]


# ---------------------------------------------------------------------------
# Приёмники: сюда фон пишет, а не в отчёт.
# ---------------------------------------------------------------------------

class MemorySink(object):
    """Приёмник в память: для тестов и для лабораторных прогонов.

    Потокобезопасен: фон пишет, тест читает. Порядок записей сохраняется.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.rows = []

    def write_batch(self, batch):
        rows = json_rows(batch)
        with self._lock:
            self.rows.extend(rows)

    def snapshot(self):
        with self._lock:
            return list(self.rows)


class JsonlFileSink(object):
    """JSONL-файл: строка на событие, тот же порядок, что и в буфере.

    Файл открывается один раз в фоновом потоке и закрывается ``close``;
    игровой цикл сюда не пишет.
    """

    def __init__(self, path):
        self.path = path
        self._handle = open(path, 'w', encoding='utf-8')

    def write_batch(self, batch):
        self._handle.write(format_jsonl(batch))
        self._handle.flush()

    def close(self):
        self._handle.close()


class SqliteSink(object):
    """SQLite-таблица событий: запись пачками в одной транзакции на батч.

    Схема минимальна и упорядочена: ``seq`` — порядковый номер записи, ``kind``
    — replay/diagnostic, ``row`` — JSON-текст записи. Соединение живёт в
    фоновом потоке, поэтому игровой цикл его не касается.
    """

    def __init__(self, path, table='events'):
        import sqlite3
        self.path, self.table = path, table
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            f'CREATE TABLE IF NOT EXISTS {table} '
            '(seq INTEGER PRIMARY KEY, kind TEXT, row TEXT)')

    def write_batch(self, batch):
        self._connection.executemany(
            f'INSERT OR REPLACE INTO {self.table} (seq, kind, row) VALUES (?, ?, ?)',
            [(seq, kind, json.dumps(row, ensure_ascii=False, sort_keys=True,
                                    default=str))
             for seq, kind, row in batch])
        self._connection.commit()

    def close(self):
        self._connection.close()


class ParquetSink(object):
    """Parquet-файл: один файл на закрытие, батчи копятся в памяти.

    ``pyarrow`` импортируется лениво: без него sink не создаётся, а
    вызывающий получает явную ошибку, а не тихий пропуск вывода.
    """

    def __init__(self, path):
        import pyarrow as pa
        import pyarrow.parquet as pq
        self._pa, self._pq = pa, pq
        self.path = path
        self._rows = []

    def write_batch(self, batch):
        self._rows.extend(json_rows(batch))

    def close(self):
        if not self._rows:
            return
        table = self._pa.Table.from_pylist(self._rows)
        self._pq.write_table(table, self.path)


# ---------------------------------------------------------------------------
# Writer: ограниченная очередь, фон, backpressure и flush.
# ---------------------------------------------------------------------------

class EventWriter(object):
    """Фоновый writer батчей: ограниченная очередь и явный flush при закрытии.

    ``submit`` не блокирует вызывающего ни при каких обстоятельствах: при
    заполненной очереди батч либо принимается с учётом переполнения, либо
    отбрасывается по явной политике. ``flush`` дожидается, пока всё
    отправленное будет записано, и только он (а также ``close``) ждёт диск.
    """

    def __init__(self, sink, max_batches=64, drop_diagnostic=False,
                 name='economy-event-writer'):
        if int(max_batches) < 1:
            raise ValueError('Размер очереди обязан быть больше нуля')
        self.sink = sink
        self.max_batches = int(max_batches)
        self.drop_diagnostic = bool(drop_diagnostic)
        self._queue = queue.Queue(maxsize=self.max_batches)
        self._spill = deque()
        self._condition = threading.Condition()
        self._thread = None
        self._stopped = False
        self.submitted = 0
        self.written = 0
        self.backpressure = 0
        self.dropped = 0
        self.errors = 0
        self.records = 0
        self._name = name

    # --- управление -------------------------------------------------------
    def start(self):
        """Запустить фоновый поток (идемпотентно)."""
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name=self._name,
                                        daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while True:
            item, from_queue = self._next_item()
            if item is None:
                return
            try:
                self.sink.write_batch(item)
            except Exception:
                # Ошибка вывода не должна убивать фон и не должна менять
                # экономику: считаем её и продолжаем, чтобы очередь
                # разгружалась и flush не завис навсегда.
                with self._condition:
                    self.errors += 1
            else:
                with self._condition:
                    self.written += 1
                    self.records += len(item)
            finally:
                if from_queue:
                    self._queue.task_done()
                with self._condition:
                    self._condition.notify_all()

    def _next_item(self):
        """Взять следующий элемент: spill-очередь переполнения идёт первой.

        Порядок батчей при переполнении обязан остаться прежним, поэтому
        ``deque`` (FIFO) хранит одни лишние батчи, а рабочий поток сначала
        разбирает их и только потом читает ``queue.Queue``. Возвращается пара
        ``(batch, from_queue)``: ``task_done`` положен только элементам
        очереди.
        """
        with self._condition:
            if self._spill:
                return self._spill.popleft(), False
        return self._queue.get(), True

    # --- отправка ---------------------------------------------------------
    def submit(self, batches):
        """Отправить батчи в фон, не блокируясь; вернуть число принятых батчей.

        Ни один путь здесь не ждёт диск и не спит: переполнение уходит в
        spill-очередь с явным счётчиком ``backpressure``, поэтому игровой цикл
        не блокируется, а replay-записи не теряются.
        """
        self.start()
        accepted = 0
        for batch in batches:
            try:
                self._queue.put_nowait(batch)
            except queue.Full:
                if self.drop_diagnostic and not self._batch_has_replay(batch):
                    with self._condition:
                        self.dropped += 1
                    continue
                # Simulation не ждёт диск: батч принимается в spill, а
                # переполнение видно в счётчике и требует flush.
                with self._condition:
                    self.backpressure += 1
                self._spill.append(batch)
            with self._condition:
                self.submitted += 1
            accepted += 1
        return accepted

    @staticmethod
    def _batch_has_replay(batch):
        """Есть ли в батче state-affecting запись (replay).

        Запись — это кортеж ``(seq, kind, row)``; приёмник может получить и
        произвольные строки, поэтому разбор защищён: неопознанная запись
        считается replay, то есть не подлежит отбрасыванию.
        """
        for item in batch:
            if isinstance(item, tuple) and len(item) == 3:
                if item[1] == REPLAY:
                    return True
            else:
                return True
        return False

    # --- завершение -------------------------------------------------------
    def flush(self, timeout=None):
        """Дождаться записи всего отправленного; вернуть True при успехе."""
        self.start()
        with self._condition:
            done = self._condition.wait_for(
                lambda: self.written+self.errors+self.dropped >= self.submitted,
                timeout=timeout)
            return done

    def close(self, timeout=None):
        """Опустошить очередь, остановить поток и закрыть приёмник.

        ``close`` — единственное место, где допустимо ждать диск: он вызывается
        при завершении сессии, а не в игровом цикле.
        """
        if self._thread is None:
            if hasattr(self.sink, 'close'):
                self.sink.close()
            return True
        if not self.flush(timeout=timeout):
            return False
        if self._spill:
            # Остаток spill обязан попасть в очередь до стоп-сигнала, иначе
            # порядок закрытия потерял бы принятые при переполнении батчи.
            with self._condition:
                pending = list(self._spill)
                self._spill.clear()
            for batch in pending:
                self._queue.put(batch)
        self._queue.put(None)
        self._thread.join(timeout)
        self._stopped = self._thread is not threading.current_thread()
        if hasattr(self.sink, 'close'):
            try:
                self.sink.close()
            except Exception:
                with self._condition:
                    self.errors += 1
        return self._stopped

    def stats(self):
        """Числовая сводка writer'а: очередь, переполнение и ошибки вывода."""
        return dict(max_batches=self.max_batches,
                    drop_diagnostic=self.drop_diagnostic,
                    submitted=self.submitted, written=self.written,
                    records=self.records, backpressure=self.backpressure,
                    dropped=self.dropped, errors=self.errors,
                    queued=self._queue.qsize())


def drain_recorder(recorder, writer=None):
    """Слить все батчи буфера (включая остаток) в writer или отпустить их.

    Вызывается в конце месяца и при завершении: это дешёвая передача ссылок, а
    не запись на диск. ``flush`` буфера обязателен именно здесь, иначе остаток
    незавершённого батча пропал бы при настройке следующего месяца — а
    replay-событие терять нельзя. Если writer не подключён, батчи
    отпускаются, но их количество остаётся в ``recorder.stats()``: каноническим
    источником остаётся ``report['events']``, поэтому молчаливой потери нет.
    """
    batches = recorder.flush()
    if writer is not None:
        return writer.submit(batches)
    return 0


def close_writer(writer, timeout=None):
    """Завершить сессию вывода: дождаться диска и закрыть приёмник.

    Единственное место, где допустимо ждать диск. Возвращает True, если
    фоновый поток остановился, и False, если writer не был подключён.
    """
    if writer is None:
        return False
    return writer.close(timeout=timeout)
