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

from modules.economy import Economy  # noqa: E402
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

        def set_protected_cash(self, account_id, value):
            self._mark('set_protected', account_id)
            return base.set_protected_cash(self, account_id, value)

        def add_protected_cash(self, account_id, value):
            self._mark('add_protected', account_id)
            return base.add_protected_cash(self, account_id, value)

        def release_protected_cash(self, account_id, value):
            self._mark('release_protected', account_id)
            return base.release_protected_cash(self, account_id, value)

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


# --- 6. protected: единственный владелец колонки -------------------------
def test_reset_month_reserves_writes_protected_through_store(monkeypatch):
    """Месячный сброс пишет ``protected`` через владельца, а не в dict."""
    engine = build_engine()
    engine.activate_rules()
    seen = []
    base = cash_boundary.CashStore

    class RecordingStore(base):
        def set_protected_cash(self, account_id, value):
            seen.append((account_id, value))
            return base.set_protected_cash(self, account_id, value)

    monkeypatch.setattr(cash_boundary, 'CashStore', RecordingStore)
    engine.__dict__.pop('_cash_store', None)
    engine.step()
    assert {account_id for account_id, _ in seen} == set(engine.accounts)
    # Итоговое значение колонки совпадает с dict: зеркало не разошлось.
    for live in engine.accounts.values():
        assert engine.cash_store.get_protected_cash(live['id']) == live['protected']


def test_reset_month_reserves_cannot_bypass_store(monkeypatch):
    """Если сброс ``protected`` пойдёт в dict напрямую, подмена не заметит его."""
    engine = build_engine()
    engine.activate_rules()
    log = []
    rebound(engine, monkeypatch, cash_boundary.CashStore, log, boom='set_protected')
    with pytest.raises(AssertionError, match='set_protected'):
        engine.step()


def test_protect_balances_writes_through_store(monkeypatch):
    engine = build_engine()
    engine.activate_rules()
    seen = []
    base = cash_boundary.CashStore

    class RecordingStore(base):
        def set_protected_cash(self, account_id, value):
            seen.append(account_id)
            return base.set_protected_cash(self, account_id, value)

    monkeypatch.setattr(cash_boundary, 'CashStore', RecordingStore)
    engine.__dict__.pop('_cash_store', None)
    engine.step()
    assert seen, 'защита кассы не дошла до владельца колонки'


def test_balance_reserved_protected_share_one_owner():
    """Все три денежных поля живут в колонках одного владельца."""
    engine = build_engine()
    engine.step()
    store = engine.cash_store
    columns = store.columns()
    assert columns['protected'] == [a.get('protected', 0) for a in engine.accounts.values()]
    assert store.diagnostics()['accounts'] == len(engine.accounts)


def test_free_cash_adapter_is_the_owner(monkeypatch):
    """``Economy.free_cash`` — адаптер владельца, второй формулы нет."""
    engine = build_engine()
    engine.step()
    for live in engine.accounts.values():
        assert engine.free_cash(live['id']) == engine.cash_store.free_cash(live['id'])


# --- 7. баланс: общий guard вместо bypass --------------------------------
def _funded_engine():
    """Движок с чистыми карманными счетами: без резерва и защиты."""
    engine = build_engine()
    engine.activate_rules()
    for identity, balance in (('carrier:s', 10.0), ('treasury:s', 0.0)):
        engine.cash_store.set_reserved_cash(identity, 0)
        engine.cash_store.set_protected_cash(identity, 0)
        engine.cash_store.set_balance(identity, balance)
    return engine


def test_transfer_guard_matches_reference_on_all_outcomes():
    """Кредит, дебет, self-transfer, нехватка, ноль и отрицательная сумма."""
    from modules.economy.support import EconomyServices

    def prepare():
        engine = build_engine()
        engine.activate_rules()
        for identity, balance in (('carrier:s', 10.0), ('treasury:s', 0.0)):
            engine.cash_store.set_reserved_cash(identity, 0)
            engine.cash_store.set_protected_cash(identity, 0)
            engine.cash_store.set_balance(identity, balance)
        return engine

    def outcome(method, sender, recipient, amount):
        engine = prepare()
        try:
            method(engine, sender, recipient, amount, 'Проверка')
        except ValueError as error:
            # Текст отказа у границы и у ``Economy`` разный по построению:
            # ``Economy.transfer`` получает дополнительный guard свободной
            # кассы и падает раньше. Сравнивается тип отказа, а не текст.
            return ('error', str(error).split(':')[0])
        return (engine.accounts[sender]['balance'],
                engine.accounts[recipient]['balance'],
                engine.report['flows'][-1]['gold'] if engine.report['flows'] else None)

    cases = [('carrier:s', 'treasury:s', 2.5),     # обычный перевод
             ('carrier:s', 'carrier:s', 0.1),      # самому себе
             ('carrier:s', 'treasury:s', 0),       # ноль
             ('carrier:s', 'treasury:s', -1),      # отрицательная сумма
             ('carrier:s', 'treasury:s', 1000.0)]  # нехватка средств
    for sender, recipient, amount in cases:
        native = outcome(Economy.transfer, sender, recipient, amount)
        reference = outcome(EconomyServices.transfer, sender, recipient, amount)
        if native[0] == 'error' or reference[0] == 'error':
            # Нехватка и отрицательная сумма отвергаются обеими реализациями.
            assert native[0] == reference[0] == 'error', (sender, recipient, amount)
            continue
        assert native == reference, (sender, recipient, amount)


def test_transfer_int_amount_keeps_int_type():
    """Тип результата берётся из слагаемых: int остаётся int."""
    from modules.economy.support import EconomyServices

    def run(method):
        engine = build_engine()
        engine.activate_rules()
        for identity, balance in (('carrier:s', 10), ('treasury:s', 0)):
            engine.cash_store.set_reserved_cash(identity, 0)
            engine.cash_store.set_protected_cash(identity, 0)
            engine.cash_store.set_balance(identity, balance)
        method(engine, 'carrier:s', 'treasury:s', 3, 'Проверка')
        return (type(engine.accounts['carrier:s']['balance']),
                type(engine.accounts['treasury:s']['balance']))

    assert run(Economy.transfer) == run(EconomyServices.transfer)


def test_one_transfer_makes_exactly_one_flow():
    engine = _funded_engine()
    before = len(engine.report['flows'])
    engine.transfer('carrier:s', 'treasury:s', 1.0, 'Проверка')
    assert len(engine.report['flows']) == before+1
    assert engine.report['flows'][-1]['gold'] == 1.0


def test_recovery_path_uses_the_same_guard_and_one_flow(monkeypatch):
    """Восстановление не обходит guard: перевод идёт общей точкой владельца.

    Прямой ``move_balance`` + ручная запись flow давали второго writer'а;
    теперь сумма обязана пройти ту же проверку и оставить ровно одну запись.
    """
    from modules.economy.native import update_prices_boundary as update

    engine = _funded_engine()
    calls = []
    real = engine.transfer

    def counting(sender, recipient, amount, reason):
        calls.append((sender, recipient, amount, reason))
        return real(sender, recipient, amount, reason)

    monkeypatch.setattr(engine, 'transfer', counting)
    before = len(engine.report['flows'])
    update._recovery_transfer(engine, 'carrier:s', 'treasury:s', 1.0)
    assert calls == [('carrier:s', 'treasury:s', 1.0,
                      update.RECOVERY_TRANSFER_REASON)]
    assert len(engine.report['flows']) == before+1


def test_recovery_transfer_rejects_amount_above_balance():
    """Тот же guard, что у ``Economy.transfer``: нехватка — ошибка, не запись."""
    from modules.economy.native import update_prices_boundary as update

    engine = _funded_engine()
    engine.cash_store.set_balance('carrier:s', 5.0)
    before = len(engine.report['flows'])
    with pytest.raises(ValueError):
        update._recovery_transfer(engine, 'carrier:s', 'treasury:s', 500.0)
    assert len(engine.report['flows']) == before


def test_recovery_transfer_keeps_total_gold():
    """Сумма золота сохраняется: перевод не создаёт и не теряет деньги."""
    from modules.economy.native import update_prices_boundary as update

    engine = _funded_engine()
    before = engine.gold()
    update._recovery_transfer(engine, 'carrier:s', 'treasury:s', 1.0)
    assert engine.gold() == pytest.approx(before)


# --- 8. Rollback: следующий ход идёт на восстановленных деньгах ----------
def test_rollback_restores_cash_and_store_is_rebound():
    """После отката владелец указывает на восстановленные счета, а не на старые."""
    from core.economy_state import EconomyState

    engine = build_engine(seed=17, detail='standard')
    engine.activate_rules()
    state = EconomyState(engine, dict(snapshot=dict(entities=[]), tiles=[]),
                         keep_reports=False)
    target = next(iter(engine.accounts))
    engine.cash_store.set_balance(target, 123.0)
    engine.cash_store.set_protected_cash(target, 12.0)
    engine.cash_store.set_reserved_cash(target, 3.0)
    before = (engine.accounts[target]['balance'],
              engine.accounts[target]['protected'],
              engine.accounts[target]['reserved'])
    state.begin_turn()
    engine.cash_store.set_balance(target, 999.0)
    engine.cash_store.set_protected_cash(target, 99.0)
    state.rollback()
    after = (engine.accounts[target]['balance'],
             engine.accounts[target]['protected'],
             engine.accounts[target]['reserved'])
    assert after == before
    # Колонки перепривязаны к восстановленному словарю: stale-ссылки нет.
    store = engine.cash_store
    assert store.accounts is engine.accounts
    assert store.get_balance(target) == before[0]
    assert store.get_protected_cash(target) == before[1]
    assert store.get_reserved_cash(target) == before[2]
    # Следующая денежная операция работает на восстановленных значениях.
    store.add_reserved_cash(target, 1.5)
    assert engine.accounts[target]['reserved'] == before[2]+1.5


def test_checkpoint_keeps_protected_and_types(tmp_path):
    """``protected`` переживает save/load, и int/float не подменяются."""
    from core.economy_state import EconomyState

    engine = build_engine(seed=17, detail='none')
    engine.activate_rules()
    target = next(iter(engine.accounts))
    engine.cash_store.set_protected_cash(target, 7)
    engine.cash_store.set_balance(target, 25.5)
    state = EconomyState(engine, dict(snapshot=dict(entities=[]), tiles=[]),
                         keep_reports=False)
    state.save_checkpoint(tmp_path / 'cash')
    values = (state.engine.accounts[target]['protected'],
              state.engine.accounts[target]['balance'],
              type(state.engine.accounts[target]['protected']),
              type(state.engine.accounts[target]['balance']))
    assert values == (7, 25.5, int, float)
    engine.cash_store.set_protected_cash(target, 11)
    assert state.engine.accounts[target]['protected'] == 11
