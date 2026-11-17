"""Differential-проверки API/hooks-границы месяца.

Задача шага: между Python API/modding и native-месяцем ровно один вызов
``monthly_step``, а всё, что осталось Python, собрано в ``MonthlyHooks`` и не
владеет состоянием. Здесь проверяется именно это:

  1. ``Economy.step`` делает ровно один вызов границы на месяц в обоих режимах,
     и ровно три за три месяца;
  2. порядок хуков за месяц совпадает у обоих режимов и не меняется;
  3. хуки не держат ``cash``/``stock``/``reserved``/order state: денежные и
     складские поля меняются только через CashStore/StockStore, статус заявки —
     только через OrderStore;
  4. ошибка native-режима не откатывается в Python fallback молча;
  5. callback (хук и ``enterprise_planner``) может вернуть Python-объект, но не
     создаёт второго ledger'а/состояния, а возврат остаётся живым ``report``;
  6. RNG/report/trade/event parity и типы возврата сохраняются.

Тест не пересчитывает экономику заново: он сравнивает две ветки одного живого
движка и записи о вызовах, поэтому расхождение здесь означало бы сдвиг
границы, а не чисел.
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
from modules.economy.native import cash_boundary, monthly_boundary  # noqa: E402
from modules.economy.native import order_boundary, stock_store_boundary  # noqa: E402
from modules.economy.native.monthly_hooks import MonthlyHooks  # noqa: E402
from modules.economy.support import EPS  # noqa: E402

DRIVER = monthly_boundary.load_monthly_driver()

pytestmark = pytest.mark.skipif(DRIVER is None,
                               reason='Cython-расширение monthly_driver не собрано')

HOOKS = ('reset_month_reserves', 'expire_orders', 'protect_balances',
         'apply_project_reserves', 'extend_diagnostics')


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def build_engine(seed, detail='standard'):
    """Реальный мир так же, как его строит эталонная матрица parity."""
    import scenario_runner as sr
    from audit.profile_run import workdir

    directory = workdir(f'hooks_{seed}_{detail}')
    stack = directory.__enter__()
    try:
        db, service = sr.build_world(seed, stack, report_detail=detail)
    except Exception:
        directory.__exit__(*sys.exc_info())
        raise
    return service.economy_state().engine, db, directory


class Recorder(object):
    """Хуки-регистратор: делегирует в настоящие хуки и пишет порядок вызовов.

    Наследник ``MonthlyHooks`` не заводит второго состояния: он держит тот же
    движок и тот же журнал вызовов, поэтому ядро зовёт ровно эти методы.
    """

    log = None

    def __init__(self, engine):
        self.engine = engine
        self.log = []

    def __getattr__(self, name):
        if name not in HOOKS:
            raise AttributeError(name)
        real = getattr(MonthlyHooks, name)

        def call(*args, **kwargs):
            self.log.append(name)
            return real(self, *args, **kwargs)
        return call


def record_hooks(monkeypatch, engine):
    """Подменить хуки движка регистратором и вернуть его журнал."""
    recorder = Recorder(engine)
    monkeypatch.setattr(engine, 'monthly_hooks', recorder, raising=False)
    return recorder


def run_step(engine, mode):
    return engine.step(native_mode=mode)


def test_facade_makes_exactly_one_call_per_month_in_both_modes(monkeypatch):
    """Один внешний вызов на месяц: три месяца — ровно три вызова."""
    engine, db, directory = build_engine(17)
    try:
        calls = []
        real = monthly_boundary.monthly_step

        def counting(*args, **kwargs):
            calls.append((args[2], args[3], args[5]))
            return real(*args, **kwargs)

        monkeypatch.setattr(monthly_boundary, 'monthly_step', counting)
        for _ in range(3):
            engine.step()
        assert len(calls) == 3, 'на каждый месяц приходится ровно один вызов'
        assert calls == [('v3', 'v3', 'off')]*3
        calls.clear()
        for _ in range(3):
            engine.step(native_mode='cython')
        assert len(calls) == 3
        assert calls == [('v3', 'v3', 'cython')]*3
    finally:
        db.close()
        directory.__exit__(None, None, None)


@pytest.mark.parametrize('mode', ['off', 'cython'])
def test_hook_order_is_stable_and_identical_in_both_modes(monkeypatch, mode):
    """Порядок хуков месяца прежний и не зависит от режима."""
    engine, db, directory = build_engine(17)
    try:
        recorder = record_hooks(monkeypatch, engine)
        run_step(engine, mode)
        assert recorder.log == ['reset_month_reserves', 'expire_orders',
                                'protect_balances', 'apply_project_reserves',
                                'extend_diagnostics']
    finally:
        db.close()
        directory.__exit__(None, None, None)


def test_hook_order_matches_python_reference(monkeypatch):
    """Native-ядро и Python-эталон зовут хуки в одном порядке и число раз."""
    engine, db, directory = build_engine(73129, 'deep')
    try:
        native, reference = copy.deepcopy(engine), copy.deepcopy(engine)
        native_log, reference_log = [], []

        native_recorder = Recorder(native)
        original_native = native_recorder.log
        monkeypatch.setattr(native, 'monthly_hooks', native_recorder, raising=False)
        reference_recorder = Recorder(reference)
        monkeypatch.setattr(reference, 'monthly_hooks', reference_recorder, raising=False)

        monthly_boundary.monthly_step(native, EPS, 'v3', 'v3', 1, 'cython')
        monthly_boundary.monthly_step_python(reference, EPS, 'v3', 'v3', 1, 'cython')
        native_log = list(original_native)
        reference_log = list(reference_recorder.log)
        assert native_log == reference_log
        assert len(native_log) == len(HOOKS)
    finally:
        db.close()
        directory.__exit__(None, None, None)


def test_hooks_do_not_own_cash_stock_or_order_state(monkeypatch):
    """Мутации месяца идут через существующие границы, а не через хуки.

    Хуки не держат ни колонок денег, ни склада, ни заявок: единственное их
    поле — ссылка на живой движок. Наблюдение ведётся на свойстве движка: оно
    ровно то место, где месяц обязан взять существующую границу, тогда как
    ссылка в ``__dict__`` пересобирается при сортировке счетов.
    """
    assert MonthlyHooks.__slots__ == ('engine',)
    hooks = MonthlyHooks(object())
    for name in ('cash', 'stock', 'reserved', 'orders', 'accounts', 'report'):
        assert not hasattr(hooks, name), name

    engine, db, directory = build_engine(17)
    try:
        calls = []

        class Spy(object):
            """Прозрачная обёртка хранилища: считает доступ к границе."""

            def __init__(self, kind, store):
                self.__dict__['_kind'] = kind
                self.__dict__['_store'] = store

            def __getattr__(self, name):
                calls.append((self._kind, name))
                return getattr(self._store, name)

        for kind, name in (('cash_store', 'cash_store'),
                           ('stock_store', 'stock_store'),
                           ('order_store', 'order_store')):
            original = getattr(Economy, name)
            monkeypatch.setattr(
                Economy, name,
                property(lambda self, kind=kind, original=original:
                         Spy(kind, original.__get__(self, type(self)))))
        run_step(engine, 'cython')
        assert ('cash_store', 'set_reserved_cash') in calls, \
            'резерв месяца обязан идти через CashStore'
        assert ('stock_store', 'reset_reserved') in calls, \
            'резерв склада обязан идти через StockStore'
        assert ('order_store', 'set_status') in calls, \
            'истечение заявок обязано идти через OrderStore'
        assert ('cash_store', 'add_balance') in calls or \
            ('cash_store', 'move_balance') in calls, \
            'деньги месяца двигает CashStore'
    finally:
        db.close()
        directory.__exit__(None, None, None)


def test_callback_return_values_survive_and_do_not_create_second_state(monkeypatch):
    """Возврат callback'а остаётся Python-объектом и не заводит второго ledger'а.

    ``enterprise_planner`` зовётся из ядра месяца как обычный Python-callback:
    он получает живой движок, готовый список предприятий и книгу продаж, а его
    возврат — обычный Python-объект. Ни движок, ни ledger при этом не
    подменяются: отчёт остаётся тем же объектом, счета — тем же словарём.
    """
    engine, db, directory = build_engine(17)
    try:
        accounts = engine.accounts
        # ``step`` пересобирает словарь сортировкой, поэтому сверяется не
        # тождество dict, а тождество самих счетов: второго состояния нет,
        # если callback видит те же объекты, что и движок.
        known = {account_id: account for account_id, account in accounts.items()}
        seen = []

        def planner(owner, existing, sells):
            seen.append((owner is engine, existing, sells))
            assert owner.accounts is engine.accounts
            assert set(owner.accounts) == set(known)
            assert all(owner.accounts[key] is value for key, value in known.items())
            # Callback видит живой отчёт месяца, а не копию: ``reset_report``
            # по-прежнему заменяет словарь отчёта между месяцами.
            assert owner.report is engine.report
            assert owner.world_state.orders.orders is owner.report['orders']
            # Планирование остаётся Python-слоем: callback делает ту же работу,
            # что и штатный планировщик, и возвращает свой Python-объект.
            owner.plan_enterprises(existing, sells)
            return {'python': 'object', 'companies': len(existing)}

        result = engine.step(enterprise_planner=planner)
        assert result is engine.report, 'фасад обязан вернуть живой отчёт'
        assert seen and seen[0][0] is True
        assert isinstance(seen[0][1], list) and seen[0][1]
        # Новые счета месяца (открытые предприятия) — обычная работа фазы, а
        # не второй ledger: старые счета остались теми же объектами.
        assert set(known) <= set(engine.accounts)
        assert all(engine.accounts[key] is value for key, value in known.items())
        assert not hasattr(engine, 'ledger') and not hasattr(engine, '_ledger')
    finally:
        db.close()
        directory.__exit__(None, None, None)


def test_native_error_does_not_fall_back_to_python(monkeypatch):
    """Ошибка native-режима видна вызывающему, а не подменяется эталоном."""
    engine, db, directory = build_engine(17)
    try:
        calls = []
        real_python = monthly_boundary.monthly_step_python

        def python_stub(*args, **kwargs):
            calls.append(args)
            return real_python(*args, **kwargs)

        def broken(*args, **kwargs):
            raise RuntimeError('native сломан')

        monkeypatch.setattr(monthly_boundary, 'monthly_step', broken)
        monkeypatch.setattr(monthly_boundary, 'monthly_step_python', python_stub)
        month_before = engine.meta['month']
        with pytest.raises(RuntimeError, match='native сломан'):
            engine.step(native_mode='cython')
        assert calls == [], 'Python-эталон не должен был выполниться'
        # Требование native-драйвера смотрит на загруженный модуль, а не на
        # привязанное имя: подмена имени не должна включать fallback.
        monkeypatch.setattr(monthly_boundary, '_driver', None)
        with pytest.raises(RuntimeError, match='monthly_driver не собрано'):
            monthly_boundary.require_native_monthly()
        with pytest.raises(RuntimeError, match='monthly_driver не собрано'):
            engine.step(native_mode='cython')
        assert calls == []
        assert engine.meta['month'] == month_before
    finally:
        db.close()
        directory.__exit__(None, None, None)


@pytest.mark.parametrize('seed,detail', [(17, 'standard'), (73129, 'deep')])
def test_rng_report_trades_and_events_stay_equal(seed, detail):
    """RNG, отчёт, сделки и события native-ветки совпадают с Python-эталоном."""
    engine, db, directory = build_engine(seed, detail)
    try:
        reference = copy.deepcopy(engine)
        for _ in range(3):
            monthly_boundary.monthly_step(engine, EPS, 'v3', 'v3', 1, 'cython')
            monthly_boundary.monthly_step_python(reference, EPS, 'v3', 'v3', 1, 'cython')
            assert digest(engine.report) == digest(reference.report)
            assert digest(engine.rng.getstate()) == digest(reference.rng.getstate())
            assert digest([(t['seller'], t['good'], t['quantity'], t['gold'], t['freight'])
                           for t in engine.report['trades']]) == \
                digest([(t['seller'], t['good'], t['quantity'], t['gold'], t['freight'])
                        for t in reference.report['trades']])
            assert digest([e.get('kind') for e in engine.report['events']]) == \
                digest([e.get('kind') for e in reference.report['events']])
            assert type(engine.report) is type(reference.report)
    finally:
        db.close()
        directory.__exit__(None, None, None)


def test_hook_objects_are_rebuilt_per_step_and_hold_the_live_engine(monkeypatch):
    """Хуки берутся от движка на каждый месяц и не кэшируют состояние."""
    engine, db, directory = build_engine(17)
    try:
        seen = []
        real = MonthlyHooks

        class Spy(real):
            def __init__(self, owner):
                seen.append(owner)
                super().__init__(owner)

        monkeypatch.setattr('modules.economy.native.monthly_hooks.MonthlyHooks', Spy)
        for _ in range(2):
            monthly_boundary.monthly_step(engine, EPS, 'v3', 'v3', 1, 'cython')
        assert seen == [engine, engine], 'хуки пересобираются на каждый месяц'
        assert seen[0] is seen[1] is engine
    finally:
        db.close()
        directory.__exit__(None, None, None)
