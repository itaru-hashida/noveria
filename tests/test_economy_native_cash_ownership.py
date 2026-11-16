"""Focused-тесты владения ``balance``/``reserved``: прямых записей в native-пути нет.

Проверяется ровно один срез задачи: два прежних прямых писателя поля ``reserved``
в ``Economy`` — месячный сброс в ``step`` и инициализация в ``activate_rules`` —
обязаны идти через ``CashStore``. Доказательство ownership строится не на
чтении кода, а на подмене хранилища: если путь пишет в dict напрямую, подмена
не сработает и шаг пройдёт молча.

Арифметика и порядок операций не проверяются заново: за это отвечает
differential-тест cash_store, здесь важно лишь, что через границу проходят те
же операции в том же порядке и ровно один раз.
"""
import copy
import importlib
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from modules.economy.native import cash_boundary  # noqa: E402
from test_economy_native_cash_store import build_engine  # noqa: E402

KERNEL = cash_boundary.load_cash_store()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение cash_store не собрано')


# --- инструментированное хранилище ---------------------------------------
def instrument(base, log, boom=None):
    """Подкласс хранилища: пишет след операций в ``log``.

    ``boom`` — имя метода, который обязан упасть. Так проверяется, что путь
    действительно идёт через границу, а не мимо неё.
    """
    class LoggedStore(base):
        def _mark(self, op, account_id):
            log.append((op, account_id))
            if boom == op:
                raise AssertionError(f'вызов {op} дошёл до CashStore: {account_id}')

        def set_reserved_cash(self, account_id, value):
            self._mark('set_reserved', account_id)
            return base.set_reserved_cash(self, account_id, value)

        def add_reserved_cash(self, account_id, value):
            self._mark('add_reserved', account_id)
            return base.add_reserved_cash(self, account_id, value)

        def release_reserved_cash(self, account_id, value):
            self._mark('release_reserved', account_id)
            return base.release_reserved_cash(self, account_id, value)

        def move_balance(self, sender, recipient, amount):
            self._mark('move_balance', sender)
            return base.move_balance(self, sender, recipient, amount)

        def add_balance(self, account_id, value):
            self._mark('add_balance', account_id)
            return base.add_balance(self, account_id, value)

        def set_balance(self, account_id, value):
            self._mark('set_balance', account_id)
            return base.set_balance(self, account_id, value)

    return LoggedStore


def rebound(engine, monkeypatch, base, log, boom=None):
    """Подменить класс границы и заставить движок взять новое хранилище."""
    monkeypatch.setattr(cash_boundary, 'CashStore', instrument(base, log, boom))
    engine.__dict__.pop('_cash_store', None)
    return engine.cash_store


# --- 1. Месячный сброс резерва в step ------------------------------------
def test_step_resets_reserved_through_cash_store(monkeypatch):
    """Сброс месяца обязан дойти до CashStore для каждого счёта."""
    engine = build_engine()
    engine.activate_rules()
    log = []
    store = rebound(engine, monkeypatch, cash_boundary.CashStore, log)
    engine.step()
    resets = Counter(a for op, a in log if op == 'set_reserved')
    assert set(resets) == set(engine.accounts)
    assert set(resets.values()) == {1}, 'сброс резерва выполнен не ровно один раз'


def test_step_reset_cannot_bypass_store(monkeypatch):
    """Если сброс пойдёт в dict напрямую, подмена хранилища не заметит его."""
    engine = build_engine()
    engine.activate_rules()
    rebound(engine, monkeypatch, cash_boundary.CashStore, [], boom='set_reserved')
    with pytest.raises(AssertionError, match='set_reserved'):
        engine.step()


def test_step_reset_passes_zero_float(monkeypatch):
    """Литерал ``0.0`` не меняется: в хранилище уходит именно float.

    Смотреть на итоговый ``reserved`` после шага нельзя: его переписывает
    ``release_reserved_cash``, который по контракту возвращает целый ``0`` при
    неположительном остатке. Проверяется значение на самом сбросе.
    """
    engine = build_engine()
    engine.activate_rules()
    engine.accounts[next(iter(engine.accounts))]['reserved'] = 7
    seen = []
    base = cash_boundary.CashStore

    class RecordingStore(base):
        def set_reserved_cash(self, account_id, value):
            if account_id in self.accounts:
                seen.append((account_id, value))
            return base.set_reserved_cash(self, account_id, value)

    monkeypatch.setattr(cash_boundary, 'CashStore', RecordingStore)
    engine.__dict__.pop('_cash_store', None)
    engine.step()
    assert seen, 'сброс резерва не дошёл до хранилища'
    for account_id, value in seen:
        assert value == 0.0, account_id
        assert type(value) is float, account_id


# --- 2. Инициализация резерва в activate_rules ---------------------------
def test_activate_rules_initializes_missing_reserved_through_store(monkeypatch):
    engine = build_engine()
    target = next(iter(engine.accounts))
    engine.accounts[target].pop('reserved')
    log = []
    rebound(engine, monkeypatch, cash_boundary.CashStore, log)
    engine.activate_rules()
    assert ('set_reserved', target) in log
    assert engine.accounts[target]['reserved'] == 0.0
    assert type(engine.accounts[target]['reserved']) is float
    assert engine.cash_store.get_reserved_cash(target) == 0.0


def test_activate_rules_cannot_bypass_store(monkeypatch):
    engine = build_engine()
    target = next(iter(engine.accounts))
    engine.accounts[target].pop('reserved')
    rebound(engine, monkeypatch, cash_boundary.CashStore, [], boom='set_reserved')
    with pytest.raises(AssertionError, match='set_reserved'):
        engine.activate_rules()


def test_activate_rules_keeps_existing_reserved(monkeypatch):
    """Прежнее условие — только отсутствующий ключ: значение не перезаписывается."""
    engine = build_engine()
    target = next(iter(engine.accounts))
    engine.accounts[target]['reserved'] = 7
    log = []
    rebound(engine, monkeypatch, cash_boundary.CashStore, log)
    engine.activate_rules()
    assert ('set_reserved', target) not in log
    assert engine.accounts[target]['reserved'] == 7
    assert type(engine.accounts[target]['reserved']) is int


# --- 3. dict остаётся compatibility view ---------------------------------
def test_dict_mirrors_store_after_step_reset():
    engine = build_engine()
    engine.step()
    store = engine.cash_store
    columns = store.columns()
    assert columns['reserved'] == [a.get('reserved', 0) for a in engine.accounts.values()]
    assert columns['balance'] == [a['balance'] for a in engine.accounts.values()]


def test_direct_dict_write_does_not_create_second_state():
    """Обход границы подхватывается колонкой, второго состояния не возникает."""
    engine = build_engine()
    engine.step()
    target = next(iter(engine.accounts))
    engine.accounts[target]['reserved'] = 3.5
    assert engine.cash_store.get_reserved_cash(target) == 3.5
    engine.cash_store.add_reserved_cash(target, 1.5)
    assert engine.accounts[target]['reserved'] == 5.0
    assert engine.cash_store.get_reserved_cash(target) == 5.0


# --- 4. Порядок reserve/release и режимы ---------------------------------
def _op_sequence(base, monkeypatch):
    engine = build_engine()
    log = []
    rebound(engine, monkeypatch, base, log)
    engine.step()
    return [(op, account) for op, account in log]


def test_native_and_python_store_see_same_operation_order(monkeypatch):
    """Native и эталон получают одинаковую последовательность операций."""
    native_ops = _op_sequence(cash_boundary.CashStore, monkeypatch)
    monkeypatch.undo()
    python_ops = _op_sequence(cash_boundary.CashStorePython, monkeypatch)
    assert native_ops == python_ops
    assert ('set_reserved',) in [(op,) for op, _ in native_ops]


def test_python_mode_step_unchanged():
    """Python-mode не менялся: тот же сброс и те же нули."""
    engine = build_engine()
    engine.step()
    for live in engine.accounts.values():
        assert live['reserved'] == 0.0


def test_deepcopy_does_not_share_cash_store():
    engine = build_engine()
    engine.step()
    clone = copy.deepcopy(engine)
    assert clone.cash_store is not engine.cash_store
    target = next(iter(clone.accounts))
    clone.cash_store.set_reserved_cash(target, 2.5)
    assert clone.accounts[target]['reserved'] == 2.5
    assert engine.cash_store.get_reserved_cash(target) == engine.accounts[target]['reserved']


# --- 5. Fallback без собранного .pyd -------------------------------------
def test_fallback_without_extension_still_steps(monkeypatch):
    monkeypatch.setattr(cash_boundary, 'CashStore', cash_boundary.CashStorePython)
    engine = build_engine()
    engine.__dict__.pop('_cash_store', None)
    engine.step()
    store = engine.cash_store
    for live in engine.accounts.values():
        assert live['reserved'] == 0.0
        assert store.get_reserved_cash(live['id']) == live['reserved']


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('cash_store'):
            raise ImportError("cannot import name 'cash_store'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert cash_boundary.load_cash_store() is None
