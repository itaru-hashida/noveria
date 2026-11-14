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


def account(identity='h', balance=100.0, reserved=0.0):
    """Живой счёт: обычный словарь, как у настоящего движка."""
    return dict(id=identity, kind='household', balance=balance, reserved=reserved,
                stock={}, values={}, state_id='s', province_id='p', city_id=None,
                tile_id='t')


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


# --- 5. Обход границы: dict новее колонки -------------------------------
def test_direct_dict_write_is_adopted_by_column():
    """native-ветки пишут balance в dict напрямую: колонка обязана догнать."""
    live = account('a', balance=10.0)
    store = make_store(live)
    live['balance'] = 4.0
    assert store.get_balance('a') == 4.0
    assert store.columns()['balance'] == [4.0]


def test_direct_reserved_write_is_adopted_by_column():
    live = account('a', balance=10.0, reserved=2.0)
    store = make_store(live)
    live['reserved'] = 6.0
    assert store.get_reserved_cash('a') == 6.0


def test_new_account_outside_boundary_is_picked_up():
    """Счёт открывается прямо в engine.accounts: колонки обязаны догнать."""
    live = account('a')
    store = make_store(live)
    fresh = account('firm-1', balance=50.0)
    store.accounts['firm-1'] = fresh          # в обход границы
    assert store.get_balance('firm-1') == 50.0
    assert store.columns()['keys'] == ['a', 'firm-1']
    store.add_balance('firm-1', 5.0)
    assert fresh['balance'] == 55.0


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
    """Копия ``transfer`` обязана совпадать с эталоном на тех же данных.

    ``Economy.transfer`` дублирует тело ``EconomyServices.transfer``, потому
    что эталон пишет в dict напрямую. Расхождение тел сдвинуло бы экономику,
    поэтому равенство проверяется явно.
    """
    from modules.economy.support import EconomyServices

    def run(method):
        engine = build_engine()
        engine.activate_rules()
        engine.accounts['carrier:s']['balance'] = 10.0
        engine.accounts['treasury:s']['balance'] = 0.0
        method(engine, 'carrier:s', 'treasury:s', 2.5, 'Проверка')
        return (engine.accounts['carrier:s']['balance'],
                engine.accounts['treasury:s']['balance'],
                engine.report['flows'][-1]['gold'])

    assert run(Economy.transfer) == run(EconomyServices.transfer)


def test_transfer_to_same_account_is_exact():
    engine = build_engine()
    engine.activate_rules()
    engine.accounts['carrier:s']['balance'] = 0.1+0.2
    cash = engine.cash_store
    cash.get_balance('carrier:s')
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
