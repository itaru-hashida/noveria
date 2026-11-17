"""Differential-проверки единственного native-entrypoint месяца.

Задача шага: Python делает один внешний вызов ``monthly_step`` на месяц, а все
фазы месяца выполняет Cython. Здесь проверяется ровно это и ничего больше:

  1. фасад ``Economy.step`` делает ровно один вызов границы за месяц;
  2. ``monthly_step`` возвращает живой ``report``, а не копию;
  3. native-оркестрация и Python-эталон дают один state/report/RNG digest,
     одно число и порядок сделок и событий, одни типы возвращаемых значений;
  4. обе ветки (``off`` и ``cython``) воспроизводятся эталоном побитово.

Эталон (``monthly_step_python``) — не второй движок: он работает с тем же
живым ``Economy`` и его хранилищами. Поэтому сравнение идёт на двух глубоких
копиях одного исходного состояния, а не на двух независимо построенных мирах.
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from modules.economy import Economy  # noqa: E402
from modules.economy.native import monthly_boundary  # noqa: E402
from modules.economy.support import EPS  # noqa: E402

DRIVER = monthly_boundary.load_monthly_driver()

pytestmark = pytest.mark.skipif(DRIVER is None,
                                reason='Cython-расширение monthly_driver не собрано')

SEEDS = (17, 73129)
DETAILS = ('standard', 'deep')
MONTHS = 3


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def state_digest(engine):
    """Тот же payload, что у эталонной матрицы parity."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'rng': repr(engine.rng.getstate()),
    })


def type_signature(value):
    """Рекурсивная подпись типов: int и float различаются, порядок сохранён."""
    if isinstance(value, dict):
        return 'dict{' + ','.join(f'{k}:{type_signature(v)}'
                                  for k, v in value.items()) + '}'
    if isinstance(value, (list, tuple)):
        return ('list' if isinstance(value, list) else 'tuple') + '[' + \
            ','.join(type_signature(item) for item in value) + ']'
    return type(value).__name__


def build_engine(seed, detail):
    """Реальный мир так же, как его строит эталонная матрица parity."""
    import scenario_runner as sr
    from audit.profile_run import workdir

    directory = workdir(f'monthly_{seed}_{detail}')
    stack = directory.__enter__()
    try:
        db, service = sr.build_world(seed, stack, report_detail=detail)
    except Exception:
        directory.__exit__(*sys.exc_info())
        raise
    engine = service.economy_state().engine
    return engine, db, directory


def run_both(engine_native, engine_reference, detail, native_mode, months=MONTHS):
    """Прогнать обе реализации на одинаковых копиях и вернуть наблюдения."""
    rows = []
    for _ in range(months):
        monthly_boundary.monthly_step(engine_native, EPS, 'v3', 'v3', 1, native_mode)
        monthly_boundary.monthly_step_python(engine_reference, EPS, 'v3', 'v3', 1, native_mode)
        engine_native.validate()
        engine_reference.validate()
        rows.append(dict(
            month=engine_native.meta['month'],
            native_state=state_digest(engine_native),
            reference_state=state_digest(engine_reference),
            native_report=digest(engine_native.report),
            reference_report=digest(engine_reference.report),
            native_rng=digest(engine_native.rng.getstate()),
            reference_rng=digest(engine_reference.rng.getstate()),
            native_trades=len(engine_native.report['trades']),
            reference_trades=len(engine_reference.report['trades']),
            native_trade_order=digest([t['trade_id'] for t in engine_native.report['trades']]),
            reference_trade_order=digest([t['trade_id'] for t in engine_reference.report['trades']]),
            native_trade_pairs=digest([(t['seller'], t['good'], t['quantity'], t['gold'], t['freight'])
                                       for t in engine_native.report['trades']]),
            reference_trade_pairs=digest([(t['seller'], t['good'], t['quantity'], t['gold'], t['freight'])
                                          for t in engine_reference.report['trades']]),
            native_events=len(engine_native.report['events']),
            reference_events=len(engine_reference.report['events']),
            native_event_order=digest([e.get('kind') for e in engine_native.report['events']]),
            reference_event_order=digest([e.get('kind') for e in engine_reference.report['events']]),
            native_types=digest(type_signature(engine_native.report)),
            reference_types=digest(type_signature(engine_reference.report)),
        ))
    return rows


def test_driver_is_the_native_extension():
    """Граница действительно берёт собранное расширение, а не fallback."""
    assert monthly_boundary.NATIVE_MONTHLY_DRIVER is True
    assert monthly_boundary.monthly_step is DRIVER.monthly_step


def test_monthly_step_returns_live_report_not_a_copy():
    """Возвращается тот же объект отчёта: копии состояния не появилось."""
    engine = object.__new__(Economy)
    engine.meta = dict(month=0)
    engine.report = {'orders': []}

    class Probe:
        def __init__(self):
            self.calls = []

        def monthly_step(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return engine.report

    probe = Probe()
    import modules.economy.native as native
    saved = monthly_boundary.monthly_step
    monthly_boundary.monthly_step = probe.monthly_step
    try:
        result = monthly_boundary.monthly_step(engine, EPS, 'v3', 'v3', 1, 'off')
    finally:
        monthly_boundary.monthly_step = saved
    assert result is engine.report
    assert len(probe.calls) == 1
    assert native is not None


def test_facade_makes_exactly_one_native_call_per_month(monkeypatch):
    """``Economy.step`` — тонкий фасад: один вызов границы на месяц."""
    engine, db, directory = build_engine(17, 'standard')
    try:
        calls = []
        real = monthly_boundary.monthly_step

        def counting(*args, **kwargs):
            calls.append((args[2], args[3], args[5]))
            return real(*args, **kwargs)

        monkeypatch.setattr(monthly_boundary, 'monthly_step', counting)
        engine.step()
        assert len(calls) == 1, 'на месяц должен приходиться ровно один вызов'
        assert calls[0] == ('v3', 'v3', 'off')
        calls.clear()
        engine.step(native_mode='cython')
        assert len(calls) == 1
        assert calls[0] == ('v3', 'v3', 'cython')
    finally:
        db.close()
        directory.__exit__(None, None, None)


def test_facade_rejects_unknown_modes_before_mutation():
    """Проверки режимов остались в фасаде и падают до мутации пролога."""
    engine = object.__new__(Economy)
    engine.meta = dict(month=5)
    with pytest.raises(ValueError, match='native-режим'):
        engine.step(native_mode='nope')
    with pytest.raises(ValueError, match='планирования'):
        engine.step(planning_mode='nope')
    with pytest.raises(ValueError, match='инвестиционный'):
        engine.step(investment_mode='nope')
    with pytest.raises(ValueError, match='только с планированием v3'):
        engine.step(planning_mode='lagged_batch', investment_mode='snapshot_batch')
    assert engine.meta['month'] == 5, 'пролог не должен был выполниться'


def test_missing_driver_fails_instead_of_silently_using_reference(monkeypatch):
    """Собранный serial_step не должен скрывать отсутствие оркестрации."""
    monkeypatch.setattr(monthly_boundary, '_driver', None)
    with pytest.raises(RuntimeError, match='monthly_driver не собрано'):
        monthly_boundary.require_native_monthly()

    engine = object.__new__(Economy)
    engine.meta = dict(month=7)
    import modules.economy.native as native
    monkeypatch.setattr(native, 'load_serial_step', lambda: object())
    with pytest.raises(RuntimeError, match='monthly_driver не собрано'):
        engine.step(native_mode='cython')
    assert engine.meta['month'] == 7, 'пролог не должен был выполниться'


@pytest.mark.parametrize('seed,detail', [(17, 'standard'), (73129, 'deep')])
@pytest.mark.parametrize('native_mode', ['off', 'cython'])
def test_native_orchestration_matches_python_reference(seed, detail, native_mode):
    """Native-оркестрация и Python-эталон совпадают по всем наблюдаемым полям."""
    engine, db, directory = build_engine(seed, detail)
    try:
        reference = copy.deepcopy(engine)
        rows = run_both(engine, reference, detail, native_mode)
        for row in rows:
            assert row['native_state'] == row['reference_state'], row['month']
            assert row['native_report'] == row['reference_report'], row['month']
            assert row['native_rng'] == row['reference_rng'], row['month']
            assert row['native_trades'] == row['reference_trades'], row['month']
            assert row['native_trade_order'] == row['reference_trade_order'], row['month']
            assert row['native_trade_pairs'] == row['reference_trade_pairs'], row['month']
            assert row['native_events'] == row['reference_events'], row['month']
            assert row['native_event_order'] == row['reference_event_order'], row['month']
            assert row['native_types'] == row['reference_types'], row['month']
    finally:
        db.close()
        directory.__exit__(None, None, None)


def test_native_entrypoint_does_not_create_a_second_world_state():
    """Оркестрация работает с существующим контейнером, а не заводит свой."""
    engine, db, directory = build_engine(17, 'standard')
    try:
        first = engine.world_state
        monthly_boundary.monthly_step(engine, EPS, 'v3', 'v3', 1, 'cython')
        assert engine.world_state is first, 'контейнер обязан остаться тем же объектом'
        assert engine.world_state.orders.orders is engine.report['orders']
        assert engine.world_state.cash.accounts is engine.accounts
    finally:
        db.close()
        directory.__exit__(None, None, None)


@pytest.mark.parametrize('seed', SEEDS)
def test_phase_timings_are_still_reported(seed):
    """Тайминги фаз по-прежнему заполняет ход, а не только цикл Python."""
    engine, db, directory = build_engine(seed, 'standard')
    try:
        engine.step()
        timings = engine.phase_timings
        for key in ('preparation', 'planning_execution', 'market_tail',
                    'production_settlement', 'report', 'wall'):
            assert key in timings, key
            assert timings[key] >= 0
    finally:
        db.close()
        directory.__exit__(None, None, None)
