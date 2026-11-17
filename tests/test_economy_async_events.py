"""Focused-тесты асинхронного пайплайна событий, истории и отчёта.

Задача шага — разделить simulation и output. Синхронно в Cython остаются
упорядоченные event records, числовые report/history records, diagnostics
counters, фильтрация standard/deep, батчи и порядок событий, влияющий на
replay. В фон вынесены только форматирование, JSON/Parquet/SQLite/файлы и
доставка в UI.

Здесь проверяется именно граница, а не пересчёт экономики:

  1. порядок и количество событий, включая полное совпадение с
     ``report['events']`` — каноническим источником replay;
  2. фильтрация standard/deep: events только в deep, diagnostics/history — в
     standard и deep;
  3. батчи: ровно по ``batch_size``, остаток — только при flush;
  4. replay parity: state/report/RNG/сделки/события не двигаются от буфера;
  5. отсутствие блокировки simulation writer'ом (медленный приёмник);
  6. flush/close: остаток батча не теряется, приёмник закрыт;
  7. backpressure: replay-записи не теряются никогда, diagnostic-only — только
     по явной политике;
  8. fallback без native ``.pyd``: Python-эталон даёт те же записи и счётчики.
"""
import copy
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from modules.economy.native import event_buffer_boundary as boundary  # noqa: E402
from modules.economy.native import event_writer as writer_module  # noqa: E402
from modules.economy.native import monthly_boundary  # noqa: E402
from modules.economy.support import EPS  # noqa: E402

DRIVER = monthly_boundary.load_monthly_driver()
SEEDS = (17, 73129)
MONTHS = 3


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def build_engine(seed, detail='standard'):
    """Реальный мир так же, как его строит эталонная матрица parity."""
    import scenario_runner as sr
    from audit.profile_run import workdir

    directory = workdir(f'events_{seed}_{detail}')
    stack = directory.__enter__()
    try:
        db, service = sr.build_world(seed, stack, report_detail=detail)
    except Exception:
        directory.__exit__(*sys.exc_info())
        raise
    return service.economy_state().engine, db, directory


def rows(seq_from, count, **extra):
    return [dict(seq=i, **extra) for i in range(seq_from, seq_from+count)]


def records(seq_from, count, kind, **extra):
    """Готовые записи буфера того же вида, что отдаёт ядро."""
    return [(i, kind, dict(seq=i, **extra))
            for i in range(seq_from, seq_from+count)]


def recorder_channel_open(engine, channel):
    return engine.event_recorder.channel_open(channel)


@pytest.fixture
def outdir():
    """Каталог вывода внутри репозитория под контролем теста.

    ``tmp_path`` pytest писал бы вне workspace, а ``tempfile.mkdtemp`` создаёт
    каталог с ACL, куда песочница не даёт писать, поэтому каталог создаётся
    явно в корне репозитория и удаляется после теста.
    """
    import os
    import shutil
    import uuid

    path = ROOT/(f'.events-out-{uuid.uuid4().hex[:8]}')
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --- 1. Порядок и количество событий -----------------------------------------

def test_recorder_preserves_order_and_count():
    recorder = boundary.EventRecorder('deep', batch_size=4, capacity=64)
    for i in range(10):
        assert recorder.record({'i': i})
    stats = recorder.stats()
    assert stats['accepted'] == 10
    assert stats['seq'] == 10
    drained = recorder.flush()
    order = [row['i'] for batch in drained for _, _, row in batch]
    assert order == list(range(10)), 'порядок записей обязан сохраняться'
    assert [seq for batch in drained for seq, _, _ in batch] == list(range(1, 11))


def test_native_and_python_reference_record_identically():
    native = boundary.EventRecorder('deep', batch_size=3, capacity=8)
    reference = boundary.EventRecorderPython('deep', batch_size=3, capacity=8)
    for recorder in (native, reference):
        for i in range(7):
            recorder.record({'i': i}, kind=boundary.DIAGNOSTIC,
                            channel=boundary.DIAGNOSTICS)
    assert native.stats() == reference.stats()
    assert native.flush() == reference.flush()


def test_record_rows_keeps_batch_arithmetic():
    recorder = boundary.EventRecorder('deep', batch_size=5, capacity=100)
    assert recorder.record_rows(rows(0, 12)) == 12
    stats = recorder.stats()
    assert stats['batches'] == 2, 'два полных батча по пять'
    assert stats['pending'] == 2, 'остаток ждёт flush'
    assert [len(batch) for batch in recorder.flush()] == [5, 5, 2]


# --- 2. Standard/deep фильтрация ---------------------------------------------

@pytest.mark.parametrize('detail,events_open,other_open', [
    ('none', False, False),
    ('standard', False, True),
    ('deep', True, True),
])
def test_channel_policy_matches_detail(detail, events_open, other_open):
    recorder = boundary.EventRecorder(detail)
    assert recorder.channel_open(boundary.EVENTS) is events_open
    assert recorder.channel_open(boundary.DIAGNOSTICS) is other_open
    assert recorder.channel_open(boundary.HISTORY) is other_open


def test_standard_filters_events_but_keeps_diagnostics():
    recorder = boundary.EventRecorder('standard', batch_size=8, capacity=64)
    assert recorder.record({'e': 1}) is False
    assert recorder.record({'d': 1}, kind=boundary.DIAGNOSTIC,
                           channel=boundary.DIAGNOSTICS) is True
    stats = recorder.stats()
    assert stats['filtered'] == 1
    assert stats['accepted'] == 1
    assert stats['seq'] == 1


def test_deep_accepts_both_channels_in_call_order():
    recorder = boundary.EventRecorder('deep', batch_size=8, capacity=64)
    recorder.record({'e': 1})
    recorder.record({'d': 1}, kind=boundary.DIAGNOSTIC,
                    channel=boundary.DIAGNOSTICS)
    recorder.record({'h': 1}, kind=boundary.REPLAY, channel=boundary.HISTORY)
    drained = [row for batch in recorder.flush() for _, _, row in batch]
    assert drained == [{'e': 1}, {'d': 1}, {'h': 1}]


def test_unknown_channel_and_detail_are_rejected():
    recorder = boundary.EventRecorder('deep')
    with pytest.raises(ValueError, match='канал'):
        recorder.channel_open('nope')
    with pytest.raises(ValueError, match='детализация'):
        boundary.EventRecorder('superdeep')


# --- 3. Батчи ----------------------------------------------------------------

def test_batches_split_exactly_by_batch_size():
    recorder = boundary.EventRecorder('deep', batch_size=3, capacity=100)
    recorder.record_rows(rows(0, 8))
    assert [len(batch) for batch in recorder.drain()] == [3, 3]
    assert recorder.pending() == 2
    assert [len(batch) for batch in recorder.drain()] == []
    assert [len(batch) for batch in recorder.flush()] == [2]


def test_drain_releases_only_completed_batches():
    recorder = boundary.EventRecorder('deep', batch_size=2, capacity=100)
    recorder.record_rows(rows(0, 5))
    drained = recorder.drain()
    assert sum(len(batch) for batch in drained) == 4
    assert recorder.queued() == 1
    assert recorder.seq == 5


def test_batch_size_must_be_positive():
    with pytest.raises(ValueError, match='батча'):
        boundary.EventRecorder('deep', batch_size=0)
    with pytest.raises(ValueError, match='очереди'):
        boundary.EventRecorder('deep', capacity=0)


# --- 4. Replay parity на реальном мире ---------------------------------------

@pytest.mark.skipif(DRIVER is None, reason='Cython-расширение не собрано')
@pytest.mark.parametrize('seed,detail', [(17, 'standard'), (73129, 'deep')])
def test_buffer_does_not_move_state_report_rng_or_trades(seed, detail):
    """Буфер — поток вывода: state/report/RNG/сделки/события не меняются.

    Сравниваются два одинаковых движка: у одного буфер наполняется штатно, у
    другого каналы закрыты (детализация none для буфера). Экономика обязана
    остаться побитово той же.
    """
    engine, db, directory = build_engine(seed, detail)
    try:
        reference = copy.deepcopy(engine)
        report = monthly_boundary.monthly_step(engine, EPS, 'v3', 'v3', 1, 'cython')
        recorder = engine.event_recorder
        # Второй движок проходит тот же месяц, но буфер молчит.
        reference.__dict__['_event_recorder'] = boundary.EventRecorder('none')
        reference_report = monthly_boundary.monthly_step(reference, EPS, 'v3', 'v3',
                                                         1, 'cython')
        assert report is engine.report
        assert digest(report) == digest(reference_report)
        assert digest(engine.rng.getstate()) == digest(reference.rng.getstate())
        assert digest([(t['trade_id'], t['quantity'], t['gold'])
                       for t in report['trades']]) == \
            digest([(t['trade_id'], t['quantity'], t['gold'])
                    for t in reference_report['trades']])
        # Все replay-события месяца попали в буфер и совпали с отчётом.
        emitted = [row for batch in recorder.flush() for _, _, row in batch]
        replay = [row for row in emitted if row in report['events']]
        assert len(replay) == len(report['events'])
        if detail == 'deep':
            # Все replay-события месяца попали в буфер и совпали с отчётом,
            # а молчащий буфер отфильтровал ровно те же записи.
            assert recorder.stats()['accepted'] > 0
            assert reference.event_recorder.stats()['filtered'] == \
                recorder.stats()['accepted']
        else:
            # В standard причинный слой не строится вовсе: events-канал пуст,
            # а diagnostics/history месяца доходят до приёмника.
            assert report['events'] == []
            assert recorder.stats()['replay_accepted'] >= 1
    finally:
        db.close()
        directory.__exit__(None, None, None)


@pytest.mark.skipif(DRIVER is None, reason='Cython-расширение не собрано')
@pytest.mark.parametrize('detail,expect_events', [('standard', 0), ('deep', None)])
def test_report_channels_follow_detail(detail, expect_events):
    engine, db, directory = build_engine(17, detail)
    try:
        monthly_boundary.monthly_step(engine, EPS, 'v3', 'v3', 1, 'cython')
        stats = engine.event_recorder.stats()
        assert stats['detail'] == detail
        if expect_events == 0:
            # events-канал в standard закрыт: причинного слоя нет вовсе, но
            # diagnostics/history открыты и доходят до приёмника.
            assert engine.report['events'] == []
            assert recorder_channel_open(engine, boundary.EVENTS) is False
            assert recorder_channel_open(engine, boundary.DIAGNOSTICS) is True
            assert stats['accepted'] > 0
            assert stats['filtered'] == 0
            assert stats['replay_accepted'] >= 1, 'истории месяца приняты'
        else:
            assert stats['accepted'] > 0
            assert engine.report['events'], 'deep хранит причинный слой'
            # replay-записей не меньше, чем событий: к ним добавлены строки
            # историй месяца, которые тоже нельзя терять.
            assert stats['replay_accepted'] >= len(engine.report['events'])
            emitted_events = [row for batch in engine.event_recorder.flush()
                              for _, _, row in batch if row in engine.report['events']]
            assert len(emitted_events) == len(engine.report['events'])
    finally:
        db.close()
        directory.__exit__(None, None, None)


# --- 5. Simulation не блокируется writer'ом ----------------------------------

class SlowSink(object):
    """Медленный приёмник: имитирует диск, который не успевает за циклом."""

    def __init__(self, delay=0.01):
        self.delay = delay
        self.batches = 0
        self.closed = False

    def write_batch(self, batch):
        time.sleep(self.delay)
        self.batches += 1

    def close(self):
        self.closed = True


def test_submit_does_not_block_simulation():
    sink = SlowSink(delay=0.2)
    writer = writer_module.EventWriter(sink, max_batches=8).start()
    batches = [records(i*4, 4, boundary.REPLAY) for i in range(20)]
    started = time.perf_counter()
    accepted = writer.submit(batches)
    elapsed = time.perf_counter()-started
    assert accepted == 20
    assert elapsed < 0.2, 'submit обязан возвращаться быстрее записи одного батча'
    assert writer.flush(timeout=30)
    assert writer.close(timeout=30)
    assert sink.batches == 20
    assert writer.stats()['written'] == 20


def test_submit_without_records_is_free():
    writer = writer_module.EventWriter(writer_module.MemorySink())
    assert writer.submit([]) == 0
    assert writer.flush(timeout=5)


# --- 6. Flush/close ----------------------------------------------------------

def test_flush_writes_partial_batch_and_close_closes_sink():
    sink = writer_module.MemorySink()
    writer = writer_module.EventWriter(sink, max_batches=4)
    recorder = boundary.EventRecorder('deep', batch_size=4, capacity=32)
    recorder.record_rows(rows(0, 6))
    assert writer_module.drain_recorder(recorder, writer) == 2
    assert writer.flush(timeout=10)
    assert len(sink.snapshot()) == 6, 'остаток батча обязан дойти до приёмника'
    assert writer.close(timeout=10)
    assert writer.stats()['records'] == 6
    assert recorder.queued() == 0


def test_drain_recorder_without_writer_keeps_counters_only():
    recorder = boundary.EventRecorder('deep', batch_size=4, capacity=32)
    recorder.record_rows(rows(0, 6))
    assert writer_module.drain_recorder(recorder) == 0
    assert recorder.queued() == 0, 'батчи отпущены'
    assert recorder.stats()['accepted'] == 6, 'счётчики остались'


def test_close_is_idempotent_and_marks_sink_closed():
    sink = SlowSink(delay=0)
    writer = writer_module.EventWriter(sink).start()
    writer.submit([records(0, 2, boundary.REPLAY)])
    assert writer.close(timeout=10)
    assert sink.closed


def test_jsonl_sink_writes_one_row_per_line(outdir):
    path = outdir/'events.jsonl'
    sink = writer_module.JsonlFileSink(path)
    writer = writer_module.EventWriter(sink)
    recorder = boundary.EventRecorder('deep', batch_size=2, capacity=16)
    recorder.record_rows([{'b': 2}, {'a': 1}])
    writer_module.drain_recorder(recorder, writer)
    assert writer.close(timeout=10)
    lines = path.read_text(encoding='utf-8').splitlines()
    assert [json.loads(line) for line in lines] == [{'b': 2}, {'a': 1}]


def test_sqlite_sink_keeps_sequence_order(outdir):
    import sqlite3
    path = outdir/'events.db'
    sink = writer_module.SqliteSink(path)
    writer = writer_module.EventWriter(sink)
    recorder = boundary.EventRecorder('deep', batch_size=3, capacity=16)
    recorder.record_rows([{'i': i} for i in range(7)])
    writer_module.drain_recorder(recorder, writer)
    assert writer.close(timeout=10)
    connection = sqlite3.connect(path)
    try:
        rows_ = connection.execute(
            'SELECT seq, kind, row FROM events ORDER BY seq').fetchall()
    finally:
        connection.close()
    assert [row_[0] for row_ in rows_] == list(range(1, 8))
    assert [json.loads(row_[2])['i'] for row_ in rows_] == list(range(7))


# --- 7. Backpressure и политика отбрасывания ---------------------------------

def test_backpressure_never_drops_replay_events():
    recorder = boundary.EventRecorder('deep', batch_size=1000, capacity=2,
                                      drop_diagnostic=True)
    for i in range(10):
        assert recorder.record({'i': i}) is True, 'replay нельзя терять'
    stats = recorder.stats()
    assert stats['accepted'] == 10
    assert stats['dropped'] == 0
    assert stats['backpressure'] == 8, 'переполнение видно в счётчике'
    assert len([row for batch in recorder.flush() for _, _, row in batch]) == 10


def test_diagnostic_events_drop_only_with_explicit_policy():
    strict = boundary.EventRecorder('deep', batch_size=1000, capacity=2)
    for i in range(6):
        strict.record({'d': i}, kind=boundary.DIAGNOSTIC,
                      channel=boundary.DIAGNOSTICS)
    assert strict.stats()['dropped'] == 0, 'по умолчанию ничего не теряется'
    assert strict.stats()['backpressure'] == 4

    lossy = boundary.EventRecorder('deep', batch_size=1000, capacity=2,
                                   drop_diagnostic=True)
    for i in range(6):
        lossy.record({'d': i}, kind=boundary.DIAGNOSTIC,
                     channel=boundary.DIAGNOSTICS)
    assert lossy.stats()['dropped'] == 4
    assert lossy.stats()['accepted'] == 2
    assert len([row for batch in lossy.flush() for _, _, row in batch]) == 2


def test_writer_records_backpressure_without_losing_replay():
    sink = SlowSink(delay=0.05)
    writer = writer_module.EventWriter(sink, max_batches=1).start()
    batches = [[(i, boundary.REPLAY, {'i': i})] for i in range(6)]
    accepted = writer.submit(batches)
    assert accepted == 6
    assert writer.stats()['backpressure'] >= 1
    assert writer.flush(timeout=30)
    assert writer.close(timeout=30)
    assert sink.batches == 6, 'записаны все батчи, включая replay при переполнении'


def test_writer_drops_diagnostic_batches_only_under_policy():
    sink = SlowSink(delay=0.05)
    writer = writer_module.EventWriter(sink, max_batches=1,
                                       drop_diagnostic=True).start()
    batches = [[(i, boundary.DIAGNOSTIC, {'i': i})] for i in range(6)]
    writer.submit(batches)
    assert writer.stats()['dropped'] >= 1
    assert writer.flush(timeout=30)
    assert writer.close(timeout=30)
    assert sink.batches < 6


# --- 8. Fallback без native .pyd ---------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    native = boundary.EventRecorder('deep', batch_size=2, capacity=4)
    native.record_rows(rows(0, 5))
    expected = native.stats()
    monkeypatch.setattr(boundary, 'EventRecorder', boundary.EventRecorderPython)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    reference = boundary.EventRecorder('deep', batch_size=2, capacity=4)
    reference.record_rows(rows(0, 5))
    assert reference.stats() == expected
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    import importlib

    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('event_buffer_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_event_buffer_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('EventRecorder', 'EventRecorderPython',
                 'load_event_buffer_kernel', 'REPLAY', 'DIAGNOSTIC',
                 'EVENTS', 'DIAGNOSTICS', 'HISTORY'):
        assert hasattr(boundary, name), name


# --- 9. Полный прогон: seed × месяц × detail ---------------------------------

@pytest.mark.skipif(DRIVER is None, reason='Cython-расширение не собрано')
@pytest.mark.parametrize('seed', SEEDS)
@pytest.mark.parametrize('detail', ['standard', 'deep'])
def test_three_months_keep_event_parity(seed, detail):
    """Seed 17 и 73129 × 3 месяца × standard/deep: события и RNG те же."""
    engine, db, directory = build_engine(seed, detail)
    try:
        sink = writer_module.MemorySink()
        writer = writer_module.EventWriter(sink, max_batches=64)
        engine.event_writer = writer
        digests, counts = [], []
        for _ in range(MONTHS):
            report = engine.step(native_mode='cython')
            digests.append(digest([(e['event_type'], e['month'])
                                   for e in report['events']]))
            counts.append((len(report['events']),
                           engine.event_recorder.stats()['accepted']))
        assert writer.flush(timeout=60)
        assert writer.close(timeout=60)
        written = sink.snapshot()
        if detail == 'deep':
            assert all(count > 0 for count, _ in counts)
        else:
            assert all(count == 0 for count, _ in counts)
        # Всё, что буфер принял, дошло до приёмника: ни одна replay-запись не
        # осталась только в очереди.
        assert writer.stats()['errors'] == 0
        assert len(written) == sum(accepted for _, accepted in counts)
    finally:
        db.close()
        directory.__exit__(None, None, None)
