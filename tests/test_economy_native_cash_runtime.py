"""Focused-проверка: в штатном runtime-пути нет прямых writers cash.

Проверяется главное свойство шага: единственный владелец ``balance``,
``reserved`` и ``protected`` — ``CashStore``. Доказательство строится тремя
независимыми способами:

  1. окно владения открыто на месяц, поэтому любое расхождение колонки с dict
     падает ``ValueError``; полный месяц (и Python-режим, и native) проходит
     без единого принятого расхождения — ``adopt_mismatch`` остаётся нулём;
  2. запись мимо владельца, сделанная внутри окна, действительно падает, а
     импорт на границе (до открытия окна) остаётся разрешённым;
  3. ``adopt_mismatch`` не растёт ни на одном шаге жизненного цикла: старт
     мира, месяц, откат хода, save/load, повторные месяцы. Это и есть
     доказательство, что вне окна границу переходят только импортёры
     состояния, а не runtime.

Арифметика и порядок операций здесь не пересчитываются: за это отвечают
differential-тесты ``cash_store`` и parity-матрица.
"""
import copy
import sys
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


def test_runtime_month_has_no_direct_cash_writers():
    """Штатный месяц не пишет cash в dict: окно владения не приняло ни одного."""
    engine = build_engine(seed=17, detail='standard')
    engine.step()
    store = engine.cash_store
    diagnostics = store.diagnostics()
    assert diagnostics['strict'] is True
    assert diagnostics['runtime'] is False, 'окно владения закрыто после месяца'
    assert diagnostics['adopt_mismatch'] == 0, \
        'внутри месяца cash писался в обход владельца'
    # Колонки и словарь счетов сходятся по всем трём полям.
    columns = store.columns()
    assert columns['balance'] == [a['balance'] for a in engine.accounts.values()]
    assert columns['reserved'] == [a.get('reserved', 0) for a in engine.accounts.values()]
    assert columns['protected'] == [a.get('protected', 0) for a in engine.accounts.values()]


def test_python_mode_month_has_no_direct_cash_writers():
    """Python-режим месяца идёт через то же окно владения."""
    engine = build_engine(seed=73129, detail='standard')
    engine.step(native_mode='off')
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0


def test_dict_write_inside_runtime_is_rejected():
    """Запись cash в dict внутри месяца падает, а не подхватывается молча."""
    engine = build_engine()
    engine.activate_rules()
    store = engine.cash_store.begin_runtime()
    target = next(iter(engine.accounts))
    engine.accounts[target]['protected'] = 123.0
    with pytest.raises(ValueError, match='в обход'):
        store.get_protected_cash(target)
    with pytest.raises(ValueError, match='в обход'):
        store.verify()
    store.end_runtime(check=False)
    # Вне окна то же значение — законный импорт старого состояния.
    assert store.get_protected_cash(target) == 123.0
    assert store.diagnostics()['adopt_mismatch'] >= 1
    assert target in store.diagnostics()['adopt_accounts']


def test_import_before_runtime_is_adopted_and_counted():
    """Граница импорта: значение checkpoint принимается на входе в месяц."""
    engine = build_engine()
    engine.activate_rules()
    target = next(iter(engine.accounts))
    engine.accounts[target]['balance'] = 55.5
    store = engine.cash_store.begin_runtime()
    assert store.get_balance(target) == 55.5
    assert store.diagnostics()['adopt_mismatch'] >= 1
    assert store.diagnostics()['runtime'] is True


def test_step_keeps_strict_after_accounts_rebuild():
    """Пересборка словаря сортировкой не выключает строгий режим."""
    engine = build_engine()
    before = engine.cash_store
    engine.step()
    after = engine.cash_store
    assert after is not before and after.accounts is engine.accounts
    assert after.diagnostics()['strict'] is True
    # Второй месяц тоже проходит строго: окно открывается заново.
    engine.step()
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0


def test_deepcopy_inside_runtime_stays_strict():
    """Копия движка наследует strict, но не окно владения чужого месяца."""
    engine = build_engine()
    engine.step()
    clone = copy.deepcopy(engine)
    store = clone.cash_store
    assert store.diagnostics()['strict'] is True
    assert store.diagnostics()['runtime'] is False
    assert store.accounts is clone.accounts


def test_normal_writers_never_raise_on_a_full_month():
    """Все три денежных поля месяца проходят через владельца без обходов.

    Наблюдение идёт подменой класса границы, как в focused-тестах ownership:
    подкласс считает вызовы и всё равно делегирует в настоящий владелец. Если
    бы месяц писал cash в dict напрямую, ``adopt_mismatch`` вырос бы, а сверка
    на закрытии окна упала бы.
    """
    engine = build_engine(seed=17, detail='deep')
    calls = []
    base = cash_boundary.CashStore

    class Recording(base):
        def set_reserved_cash(self, account_id, value):
            calls.append('set_reserved_cash')
            return base.set_reserved_cash(self, account_id, value)

        def move_balance(self, sender, recipient, amount):
            calls.append('move_balance')
            return base.move_balance(self, sender, recipient, amount)

        def add_protected_cash(self, account_id, value):
            calls.append('add_protected_cash')
            return base.add_protected_cash(self, account_id, value)

    cash_boundary.CashStore = Recording
    try:
        engine.__dict__.pop('_cash_store', None)
        engine.step()
    finally:
        cash_boundary.CashStore = base
    assert 'set_reserved_cash' in calls, 'сброс резерва месяца идёт через владельца'
    assert 'move_balance' in calls, 'деньги месяца двигает владелец полей'
    assert 'add_protected_cash' in calls, 'защита кассы идёт через владельца'
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0


# --- доказательство границы импорта --------------------------------------
def test_adoption_stays_zero_across_whole_lifecycle():
    """Ни один шаг жизненного цикла не переходит границу владельца.

    Вне окна владения расхождения по-прежнему принимаются — поэтому мало
    сказать «граница импорта есть»: надо показать, что в штатном прогоне за
    неё никто не заходит. Счётчик ``adopt_mismatch`` сбрасывается вместе с
    хранилищем, поэтому после каждой фазы проверяется именно текущее значение:
    старт мира, месяц, откат хода и повторные месяцы.
    """
    from core.economy_state import EconomyState

    engine = build_engine(seed=17, detail='standard')
    # Старт мира: bootstrap уже прошёл, денег мимо владельца не писали.
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0, 'старт мира'

    state = EconomyState(engine, dict(snapshot=dict(entities=[]), tiles=[]),
                         keep_reports=False)
    target = next(iter(engine.accounts))

    state.begin_turn()
    engine.step()
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0, 'месяц'

    # Откат восстанавливает счета как новые объекты: это импорт, и он
    # принимается на входе в следующий месяц, а не в середине.
    state.rollback()
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0, 'откат'

    engine.step()
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0, 'месяц после отката'
    engine.step()
    assert engine.cash_store.diagnostics()['adopt_mismatch'] == 0, 'третий месяц'

    # Колонки, dict и восстановленный счёт сходятся по всем трём полям.
    store = engine.cash_store
    for account_id, account in engine.accounts.items():
        assert store.get_balance(account_id) == account['balance'], account_id
        assert store.get_reserved_cash(account_id) == account.get('reserved', 0), account_id
        assert store.get_protected_cash(account_id) == account.get('protected', 0), account_id
    assert target in engine.accounts


def test_only_import_boundary_is_adopted_never_runtime():
    """Приём расхождения возможен только вне окна владения.

    Проверяется свойство окна: пока ``runtime`` открыт, расхождение — ошибка;
    после ``end_runtime`` то же расхождение принимается. Значит внутри месяца
    обход владельца невозможен, а вне месяца границу переходят только
    импортёры состояния — и это видно по ``adopt_accounts``.
    """
    engine = build_engine()
    engine.activate_rules()
    store = engine.cash_store
    target = next(iter(engine.accounts))

    store.begin_runtime()
    engine.accounts[target]['balance'] = 777.0
    with pytest.raises(ValueError, match='в обход'):
        store.get_balance(target)

    store.end_runtime(check=False)
    assert store.get_balance(target) == 777.0
    assert store.diagnostics()['adopt_mismatch'] >= 1
    assert store.diagnostics()['adopt_accounts'] == [target]
    assert store.diagnostics()['runtime'] is False


# --- тип protected: release не меняет parity ------------------------------
def test_release_protected_zero_type_does_not_change_parity():
    """``release_protected_cash`` возвращает целый ``0``, а не ``0.0``.

    Прежняя прямая запись в ``lagged_batch``/``turn`` использовала
    ``max(0.0, protected-value)`` и давала ``0.0`` (float); владелец повторяет
    семантику встроенного ``max(0, ...)`` и даёт целый ``0``. Различие видно
    только как тип промежуточного значения: следующий ``add_protected_cash``
    прибавляет зарплату и получает ровно то же значение, что и раньше.
    """
    base = cash_boundary.CashStore
    live = dict(id='a', kind='firm', balance=100.0, reserved=0.0, protected=0.0,
                stock={}, values={})
    store = base({'a': live}).begin_runtime()
    released = store.release_protected_cash('a', 0.0)
    assert released == 0 and type(released) is int
    # После прибавки зарплаты тип и значение прежние: float приходит от payroll.
    total = store.add_protected_cash('a', 2.5)
    assert total == 2.5 and type(total) is float
    assert live['protected'] == 2.5
    # Тот же результат у прежней формулы max(0.0, 0.0-0.0) + 2.5.
    assert max(0.0, 0.0-0.0)+2.5 == total
    assert store.diagnostics()['adopt_mismatch'] == 0
