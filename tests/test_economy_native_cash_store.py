"""Тесты Cython-owned денежных полей: колонки — источник истины.

Проверяется главное свойство шага: ``balance`` и ``reserved`` счетов живут в
колонках ``CashStore``, а Python-dict остаётся compatibility view. Поэтому
часть проверок смотрит на колонки, а не на словарь, и отдельно проверяет, что
dict не может разойтись с колонкой: запись идёт только через границу.

Отдельный блок — устойчивость к обходу границы. Счета создаются прямо в
``engine.accounts`` при открытии предприятия, а native-ветки восстановления
предприятий пишут ``balance`` в dict напрямую. Колонки обязаны это подхватить,
иначе получилось бы второе независимое состояние.

Последний блок — exact replay реального мира: прогон с native-хранилищем
сравнивается с прогоном на Python-эталоне той же границы. Совпадать обязаны
состояние, отчёт и RNG на обоих seed.
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
from modules.economy.native import cash_boundary  # noqa: E402

KERNEL = cash_boundary.load_cash_store()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение cash_store не собрано')


def account(identity='h', balance=100.0, reserved=0.0, protected=0.0):
    """Живой счёт: обычный словарь, как у настоящего движка."""
    return dict(id=identity, kind='household', balance=balance, reserved=reserved,
                protected=protected, stock={}, values={}, state_id='s',
                province_id='p', city_id=None, tile_id='t')


def make_store(*accounts):
    return cash_boundary.CashStore({a['id']: a for a in accounts})


# --- 1. Импорт текущих значений ------------------------------------------
def test_store_imports_current_balances():
    first, second = account('a', balance=7.5), account('b', balance=3)
    store = make_store(first, second)
    assert store.accounts['a'] is first and store.accounts['b'] is second
    assert store.columns()['balance'] == [7.5, 3]


def test_import_preserves_int_and_float_types():
    """int остаётся int, float — float: приведение сломало бы точность."""
    live = account('a', balance=3, reserved=0)
    store = make_store(live)
    assert type(store.columns()['balance'][0]) is int
    assert type(store.get_balance('a')) is int
    store.set_balance('a', 0.1+0.2)
    assert type(store.get_balance('a')) is float
    # Последний разряд сохраняется побитово.
    assert store.get_balance('a').hex() == (0.1+0.2).hex()


def test_missing_reserved_gets_default():
    live = dict(id='a', kind='household', balance=5.0, stock={}, values={})
    store = cash_boundary.CashStore({'a': live})
    assert store.get_reserved_cash('a') == 0


# --- 2. get/set/add balance ----------------------------------------------
def test_get_set_add_balance():
    live = account('a', balance=10.0)
    store = make_store(live)
    assert store.get_balance('a') == 10.0
    assert store.set_balance('a', 4.0) == 4.0
    assert store.get_balance('a') == 4.0
    assert store.add_balance('a', 2.5) == 6.5
    assert store.get_balance('a') == 6.5


def test_columns_are_independent_per_account():
    first, second = account('a', balance=8.0), account('b', balance=9.0)
    store = make_store(first, second)
    store.set_balance('b', 1.0)
    assert store.columns()['balance'] == [8.0, 1.0]
    assert first['balance'] == 8.0


# --- 3. reserved cash ----------------------------------------------------
def test_reserved_cash_operations():
    live = account('a', balance=10.0, reserved=4.0)
    store = make_store(live)
    assert store.get_reserved_cash('a') == 4.0
    assert store.add_reserved_cash('a', 1.5) == 5.5
    assert store.set_reserved_cash('a', 2.0) == 2.0
    assert store.get_reserved_cash('a') == 2.0


def test_release_reserved_never_goes_negative():
    """Семантика ``max(0, reserved-value)``: отсечение нулём, а не минус."""
    live = account('a', balance=10.0, reserved=3.0)
    store = make_store(live)
    assert store.release_reserved_cash('a', 5.0) == 0
    assert store.get_reserved_cash('a') == 0
    store.set_reserved_cash('a', 3.0)
    assert store.release_reserved_cash('a', 1.25) == 1.75


# --- 3b. protected cash --------------------------------------------------
def test_protected_cash_operations():
    """``protected`` — отдельная колонка со своим set/read/reset."""
    live = account('a', balance=10.0, protected=4.0)
    store = make_store(live)
    assert store.get_protected_cash('a') == 4.0
    assert store.set_protected_cash('a', 2.5) == 2.5
    assert store.get_protected_cash('a') == 2.5
    assert live['protected'] == 2.5
    assert store.add_protected_cash('a', 1.5) == 4.0
    assert live['protected'] == 4.0
    # Сброс: ``set_protected_cash(0)`` — прежняя запись ``protected = 0``.
    assert store.set_protected_cash('a', 0) == 0
    assert store.get_protected_cash('a') == 0
    assert type(store.get_protected_cash('a')) is int


def test_release_protected_never_goes_negative():
    """Семантика ``max(0, protected-value)``: отсечение нулём, а не минус."""
    live = account('a', balance=10.0, protected=3.0)
    store = make_store(live)
    assert store.release_protected_cash('a', 5.0) == 0
    assert type(store.release_protected_cash('a', 1.0)) is int
    store.set_protected_cash('a', 3.0)
    assert store.release_protected_cash('a', 1.25) == 1.75


def test_protected_and_reserved_are_independent():
    """Два разных понятия: запись одного не трогает второе."""
    live = account('a', balance=100.0, reserved=7.0, protected=11.0)
    store = make_store(live)
    store.set_protected_cash('a', 21.0)
    assert store.get_reserved_cash('a') == 7.0
    store.set_reserved_cash('a', 3.0)
    assert store.get_protected_cash('a') == 21.0
    assert (live['reserved'], live['protected']) == (3.0, 21.0)


def test_missing_protected_gets_default_zero():
    live = dict(id='a', kind='household', balance=5.0, stock={}, values={})
    store = cash_boundary.CashStore({'a': live})
    assert store.get_protected_cash('a') == 0


def test_protected_type_preserved_across_operations():
    """int остаётся int, float — float: приведение сломало бы точность."""
    live = account('a', balance=100.0, protected=3)
    store = make_store(live)
    assert type(store.get_protected_cash('a')) is int
    store.add_protected_cash('a', 0.1+0.2)
    assert type(store.get_protected_cash('a')) is float
    assert store.get_protected_cash('a') == 3+(0.1+0.2)


# --- 3c. единая формула свободной кассы ----------------------------------
def test_free_cash_normal_case():
    live = account('a', balance=100.0, reserved=0.0, protected=0.0)
    store = make_store(live)
    assert store.free_cash('a') == 100.0


def test_free_cash_subtracts_reserved():
    store = make_store(account('a', balance=100.0, reserved=30.0, protected=0.0))
    assert store.free_cash('a') == 70.0


def test_free_cash_subtracts_protected():
    store = make_store(account('a', balance=100.0, reserved=0.0, protected=25.0))
    assert store.free_cash('a') == 75.0


def test_free_cash_subtracts_reserved_and_protected_in_order():
    """Порядок тот же, что в прежней формуле: сначала reserved, затем protected."""
    live = account('a', balance=100.0, reserved=30.0, protected=25.0)
    store = make_store(live)
    assert store.free_cash('a') == (100.0-30.0)-25.0


def test_free_cash_clamps_to_zero_float():
    """Отсечение нулём возвращает именно ``0.0``, как прежний ``max(0.0, ...)``."""
    store = make_store(account('a', balance=10.0, reserved=30.0, protected=0.0))
    assert store.free_cash('a') == 0.0
    assert type(store.free_cash('a')) is float


def test_free_cash_matches_python_owner_exactly():
    """Python-эталон и native-владелец дают побитово одно значение."""
    cases = [
        dict(balance=100.0, reserved=0.0, protected=0.0),
        dict(balance=0.1+0.2, reserved=0.1, protected=0.0),
        dict(balance=1e-9, reserved=0.0, protected=0.0),
        dict(balance=7, reserved=2, protected=1),
        dict(balance=100.0, reserved=30.0, protected=95.0),
    ]
    for values in cases:
        live = account('a', **values)
        native = cash_boundary.CashStore({'a': live})
        reference_live = account('a', **values)
        reference = cash_boundary.CashStorePython({'a': reference_live})
        assert native.free_cash('a') == reference.free_cash('a'), values
        assert type(native.free_cash('a')) is type(reference.free_cash('a')), values


def test_free_cash_module_helper_uses_owner():
    """``cash_boundary.free_cash`` — адаптер, а не вторая копия формулы."""
    store = make_store(account('a', balance=100.0, reserved=30.0, protected=25.0))
    assert cash_boundary.free_cash(store, 'a') == store.free_cash('a')


# --- 3d. явная политика adoption -----------------------------------------
def test_normal_mutation_does_not_adopt_foreign_dict_value():
    """Обычная запись пишет и колонку, и dict: расхождений не появляется."""
    live = account('a', balance=10.0, protected=1.0)
    store = make_store(live)
    store.add_protected_cash('a', 2.0)
    assert store.diagnostics()['adopt_mismatch'] == 0
    assert store.get_protected_cash('a') == 3.0


def test_direct_dict_mismatch_is_counted_in_diagnostics():
    """Вне окна владения обход границы подхватывается, но виден в диагностике.

    Так выглядит граница импорта: счёт выставил checkpoint, откат хода или
    фикстура теста. Штатный прогон закрывает окно владения только на время
    месяца, и там та же правка падает.
    """
    live = account('a', balance=10.0, protected=1.0)
    store = make_store(live)
    assert store.diagnostics()['adopt_mismatch'] == 0
    live['protected'] = 9.0
    assert store.get_protected_cash('a') == 9.0
    assert store.diagnostics()['adopt_mismatch'] == 1
    assert store.diagnostics()['strict'] is True
    assert store.diagnostics()['runtime'] is False


def test_adopt_mismatch_reports_offending_accounts():
    """Диагностика называет счета, по которым границу обошли.

    Без списка ``adopt_mismatch`` был бы неотличим от счётчика случайных
    расхождений: непонятно, какой именно writer писал мимо владельца.
    """
    first, second = account('a', balance=10.0), account('b', balance=20.0)
    store = make_store(first, second)
    first['balance'] = 5.0
    assert store.get_balance('a') == 5.0
    diagnostics = store.diagnostics()
    assert diagnostics['adopt_mismatch'] == 1
    assert diagnostics['adopt_accounts'] == ['a']


def test_strict_mode_rejects_dict_write_inside_runtime():
    """Внутри окна владения обычный путь не принимает правку dict."""
    live = account('a', balance=10.0, protected=1.0)
    store = cash_boundary.CashStore({'a': live}, True).begin_runtime()
    assert store.diagnostics()['strict'] is True
    live['protected'] = 9.0
    with pytest.raises(ValueError, match='в обход'):
        store.get_protected_cash('a')


def test_explicit_adopt_imports_dict_values():
    """Явный импорт (load/new-account) принимает значения dict в колонки."""
    live = account('a', balance=10.0, reserved=1.0, protected=2.0)
    store = make_store(live)
    live.update(balance=50.0, reserved=5.0, protected=6.0)
    store.adopt()
    assert store.columns()['balance'] == [50.0]
    assert store.columns()['reserved'] == [5.0]
    assert store.columns()['protected'] == [6.0]


def test_python_owner_exposes_the_same_adoption_surface():
    """Эталон повторяет владельца: те же методы, тот же strict и счётчик."""
    live = account('a', balance=10.0, protected=1.0)
    reference = cash_boundary.CashStorePython({'a': live})
    assert reference.diagnostics() == dict(version=reference.version, accounts=1,
                                           adopt_mismatch=0, adopt_accounts=[],
                                           strict=True, runtime=False)
    live['protected'] = 4.0
    assert reference.get_protected_cash('a') == 4.0
    assert reference.diagnostics()['adopt_mismatch'] == 1
    assert reference.diagnostics()['adopt_accounts'] == ['a']


def test_python_owner_rejects_dict_write_inside_runtime():
    """Окно владения у эталона то же: внутри месяца правка dict — ошибка."""
    live = account('a', balance=10.0, protected=1.0)
    reference = cash_boundary.CashStorePython({'a': live}).begin_runtime()
    live['protected'] = 4.0
    with pytest.raises(ValueError, match='в обход'):
        reference.get_protected_cash('a')


# --- 4. Python compatibility view ---------------------------------------
def test_dict_is_mirrored_by_every_write():
    live = account('a', balance=10.0, reserved=1.0)
    store = make_store(live)
    store.set_balance('a', 7.0)
    store.add_balance('a', 1.0)
    store.set_reserved_cash('a', 2.0)
    store.add_reserved_cash('a', 0.5)
    store.release_reserved_cash('a', 0.5)
    # Истина в колонке, dict — её точное отражение.
    assert (live['balance'], live['reserved']) == (8.0, 2.0)
    assert store.get_balance('a') == live['balance']
    assert store.get_reserved_cash('a') == live['reserved']


def test_move_balance_matches_two_separate_writes():
    left, right = account('a', balance=10.0), account('b', balance=1.0)
    store = make_store(left, right)
    store.move_balance('a', 'b', 2.5)
    assert (left['balance'], right['balance']) == (7.5, 3.5)


def test_move_balance_to_same_account_keeps_operation_order():
    """Перевод самому себе: вторая операция считается от результата первой."""
    live = account('a', balance=0.1+0.2)
    store = make_store(live)
    store.move_balance('a', 'a', 0.1)
    expected = (live['balance']-0.1)+0.1
    assert live['balance'] == expected


# --- 5. Обход границы: dict разошёлся с колонкой -------------------------
def test_direct_dict_write_is_adopted_outside_runtime():
    """Вне окна правка ``balance`` — импорт: колонка принимает и считает."""
    live = account('a', balance=10.0)
    store = make_store(live)
    live['balance'] = 4.0
    assert store.get_balance('a') == 4.0
    assert store.columns()['balance'] == [4.0]
    assert store.diagnostics()['adopt_accounts'] == ['a']


def test_direct_reserved_write_is_adopted_outside_runtime():
    live = account('a', balance=10.0, reserved=2.0)
    store = make_store(live)
    live['reserved'] = 6.0
    assert store.get_reserved_cash('a') == 6.0


def test_direct_dict_write_is_rejected_inside_runtime():
    """Внутри окна та же правка — обход владельца и ошибка."""
    live = account('a', balance=10.0)
    store = make_store(live).begin_runtime()
    live['balance'] = 4.0
    with pytest.raises(ValueError, match='в обход'):
        store.get_balance('a')


def test_new_account_outside_boundary_is_picked_up():
    """Новый счёт в ``engine.accounts`` импортируется: это рост раскладки.

    Заведение счёта — не мутация денежного поля существующего счёта, поэтому
    ``_sync`` добавляет его в хвост колонок со значениями из словаря.
    """
    live = account('a')
    store = make_store(live)
    fresh = account('firm-1', balance=50.0)
    store.accounts['firm-1'] = fresh          # новый счёт, а не правка поля
    assert store.get_balance('firm-1') == 50.0
    assert store.columns()['keys'] == ['a', 'firm-1']
    store.add_balance('firm-1', 5.0)
    assert fresh['balance'] == 55.0
    assert store.diagnostics()['adopt_mismatch'] == 0


# --- 6. Явные ошибки -----------------------------------------------------
def test_unknown_account_raises_key_error():
    store = make_store(account('a'))
    with pytest.raises(KeyError):
        store.get_balance('ghost')
    with pytest.raises(KeyError):
        store.set_balance('ghost', 1.0)
    with pytest.raises(KeyError):
        store.get_reserved_cash('ghost')


# --- 7. deepcopy и pickle ------------------------------------------------
def test_deepcopy_creates_independent_store():
    live = account('a', balance=6.0, reserved=1.0)
    store = make_store(live)
    clone = copy.deepcopy(store)
    assert clone.accounts is not store.accounts
    assert clone.accounts['a'] is not live
    assert clone.columns() == store.columns()
    clone.set_balance('a', 0.0)
    assert store.get_balance('a') == 6.0
    assert live['balance'] == 6.0


def test_pickle_round_trip_restores_columns():
    import pickle

    live = account('a', balance=6.0, reserved=1.0)
    store = make_store(live)
    restored = pickle.loads(pickle.dumps(store))
    assert restored.get_balance('a') == 6.0
    assert restored.get_reserved_cash('a') == 1.0


# --- 8. Layout invalidation ---------------------------------------------
def test_reordered_accounts_rebuild_columns():
    """Словарь пересобрали сортировкой: слоты недействительны."""
    first, second = account('a', balance=1.0), account('b', balance=2.0)
    store = make_store(first, second)
    store.accounts = {'b': second, 'a': first}
    assert store.get_balance('a') == 1.0
    assert store.columns()['keys'] == ['b', 'a']


def test_shrunk_accounts_rebuild_columns():
    first, second = account('a'), account('b')
    store = make_store(first, second)
    store.accounts.pop('a')
    assert store.get_balance('b') == 100.0
    assert store.columns()['keys'] == ['b']


# --- 9. Реальный движок --------------------------------------------------
def build_engine(seed=17, detail='none'):
    cfg = json.loads((ROOT/'tests/fixtures/economy.json').read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H',
             properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def test_engine_store_binds_accounts_dict():
    engine = build_engine()
    store = engine.cash_store
    assert store.accounts is engine.accounts
    for live in engine.accounts.values():
        assert store.get_balance(live['id']) == live['balance']
        assert store.get_reserved_cash(live['id']) == live.get('reserved', 0)


def test_month_keeps_columns_in_sync():
    engine = build_engine()
    engine.step()
    store = engine.cash_store
    assert store.accounts is engine.accounts
    for live in engine.accounts.values():
        assert store.get_balance(live['id']) == live['balance'], live['id']
        assert store.get_reserved_cash(live['id']) == live.get('reserved', 0), live['id']


def test_step_rebinds_store_after_accounts_rebuild():
    """``step`` пересобирает словарь сортировкой: граница обязана смениться."""
    engine = build_engine()
    before = engine.cash_store
    engine.step()
    after = engine.cash_store
    assert after is not before
    assert after.accounts is engine.accounts
    assert before.accounts is not engine.accounts


def test_engine_deepcopy_gets_its_own_store():
    engine = build_engine()
    engine.step()
    clone = copy.deepcopy(engine)
    assert clone.cash_store is not engine.cash_store
    assert clone.cash_store.accounts is clone.accounts
    for live in clone.accounts.values():
        assert clone.cash_store.get_balance(live['id']) == live['balance']


def test_transfer_matches_reference_semantics():
    """``Economy.transfer`` и ``EconomyServices.transfer`` дают одно состояние.

    Тело живёт в ``EconomyServices``; ``Economy`` добавляет только guard
    свободной кассы. Тест фиксирует, что деньги двигает один владелец и
    результат у обеих точек входа совпадает.
    """
    from modules.economy.support import EconomyServices

    def run(method):
        engine = build_engine()
        engine.activate_rules()
        cash = engine.cash_store
        cash.set_balance('carrier:s', 10.0)
        cash.set_balance('treasury:s', 0.0)
        method(engine, 'carrier:s', 'treasury:s', 2.5, 'Проверка')
        return (engine.accounts['carrier:s']['balance'],
                engine.accounts['treasury:s']['balance'],
                engine.report['flows'][-1]['gold'])

    assert run(Economy.transfer) == run(EconomyServices.transfer)


def test_transfer_to_same_account_is_exact():
    engine = build_engine()
    engine.activate_rules()
    engine.cash_store.set_balance('carrier:s', 0.1+0.2)
    engine.transfer('carrier:s', 'carrier:s', 0.1, 'Проверка')
    assert engine.accounts['carrier:s']['balance'] == (0.1+0.2)


# --- 10. Exact replay реального мира -------------------------------------
def state_digest(engine):
    payload = {
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'trades': engine.report['trades'],
        'flows': engine.report['flows'],
        'rng': repr(engine.rng.getstate()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def run_world(seed, months, monkeypatch=None, python_store=False):
    """Прогон мира; при ``python_store`` граница берёт Python-эталон."""
    import scenario_runner as sr
    from audit.profile_run import workdir

    if monkeypatch is not None and python_store:
        monkeypatch.setattr(cash_boundary, 'CashStore', cash_boundary.CashStorePython)
    try:
        with workdir(f'cash_store_{seed}_{python_store}') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                digests = []
                for _ in range(months):
                    engine.step()
                    engine.validate()
                    digests.append(state_digest(engine))
                return digests, repr(engine.rng.getstate())
            finally:
                db.close()
    finally:
        if monkeypatch is not None:
            monkeypatch.undo()


@pytest.mark.parametrize('seed', [17, 73129])
def test_exact_replay_matches_python_reference(seed, monkeypatch):
    """native-хранилище и Python-эталон дают одно состояние и один RNG."""
    native_state, native_rng = run_world(seed, 3)
    python_state, python_rng = run_world(seed, 3, monkeypatch, python_store=True)
    assert native_state == python_state
    assert native_rng == python_rng


def test_reference_store_has_the_same_surface():
    for name in ('get_balance', 'set_balance', 'add_balance', 'move_balance',
                 'get_reserved_cash', 'set_reserved_cash', 'add_reserved_cash',
                 'release_reserved_cash', 'sync', 'columns'):
        assert callable(getattr(cash_boundary.CashStorePython, name))
        assert callable(getattr(cash_boundary.CashStore, name))


def test_boundary_binds_compiled_class():
    assert cash_boundary.NATIVE_CASH_STORE is True
    assert cash_boundary.CashStore is KERNEL.CashStore
    assert cash_boundary.CashStore is not cash_boundary.CashStorePython
    assert cash_boundary.CashStore.__module__.endswith('cash_store')


def test_fallback_used_when_extension_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('cash_store'):
            raise ImportError("cannot import name 'cash_store'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert cash_boundary.load_cash_store() is None
