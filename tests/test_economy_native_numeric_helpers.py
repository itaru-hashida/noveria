"""Object-level numeric helpers: склад, касса, оброк и финансирование.

Блок 1 переноса: прежние Python-тела ``locked_material``, ``free_stock``,
``total_stock``, ``gold``, ``enterprise_overhead`` и ``project_finances``
переехали в ``stock_kernel``/``money_kernel``. Ядро ничего не мутирует и не
заводит второго ledger'а: ядро получает живые dict-ы счетов и готовые числа.

Тест держит то, что легко потерять при переносе:

  * побитовое совпадение native и Python-эталона;
  * типы: int-остаток остаётся int, ``max(0, ...)`` даёт именно 0;
  * компенсированные суммы ``sum`` в Python 3.12 (Neumaier) для float;
  * порядок обхода и отсутствие побочных эффектов;
  * fallback на эталон без собранного ``.pyd``;
  * exact-отпечатки траектории 3 месяца для seed 17 и 73129.
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

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy.native import money_boundary  # noqa: E402
from modules.economy.native import stock_boundary  # noqa: E402

# Exact-отпечатки траектории, снятые на Python-коде V3 до переноса блока 1.
GOLDEN = {
    17: {
        'engine': '077b775c565b045cb020ca88996723603c290d5b5c20cdcf3b5edd3b9f078f1b',
        'report': '89e82f08689800986581fa2ad62fee18faa04c5a5195f1cf59439740efeb2a55',
        'rng': '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377',
    },
    73129: {
        'engine': 'd8446744ebd07232f7995c4035f5f1dae298d0373132d54e972f7411ef76f531',
        'report': '32ea25765f5057144b8c0b976e323b23e3ef34948615b5f15df7b61cd0e545bc',
        'rng': 'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b',
    },
}


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def engine_digest(engine):
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'queue': engine.meta.get('investment_queue', []),
    })


def account(**overrides):
    row = dict(id='a', kind='enterprise', balance=10.0, stock={}, values={},
               stock_reserved={})
    row.update(overrides)
    return row


def project(materials=None, equipment=None, support=None):
    return dict(materials=dict(materials or {}), equipment_held=dict(equipment or {}),
                support_stock=dict(support or {}))


# --- 1. free_stock и locked_material ---------------------------------------

def test_free_stock_matches_python_reference():
    row = account(stock={'planks': 7}, stock_reserved={'planks': 2},
                  project=project({'planks': 1}),
                  public_projects=[project({'planks': 1}, {'planks': 1},
                                           {'planks': 0.5})])
    assert stock_boundary.free_stock(row, 'planks') == \
        stock_boundary.free_stock_python(row, 'planks')


def test_locked_material_sums_project_builds_equipment_and_support():
    row = account(stock={'planks': 7},
                  project=project({'planks': 1}),
                  public_projects=[project({'planks': 1}, {'planks': 1},
                                           {'planks': 0.5})])
    assert stock_boundary.locked_material(row, 'planks') == 3.5
    assert stock_boundary.locked_material_python(row, 'planks') == 3.5


def test_free_stock_keeps_int_type_for_integral_stock():
    """int-склад, int-резерв и int-закрепление дают int, а не float."""
    row = account(stock={'planks': 5}, stock_reserved={'planks': 2})
    assert stock_boundary.free_stock(row, 'planks') == 3
    assert type(stock_boundary.free_stock(row, 'planks')) is int


def test_free_stock_clamps_to_zero_int_on_negative_remainder():
    row = account(stock={'planks': 1}, stock_reserved={'planks': 4})
    assert stock_boundary.free_stock(row, 'planks') == 0
    assert type(stock_boundary.free_stock(row, 'planks')) is int


def test_free_stock_of_absent_goods_is_zero():
    row = account()
    assert stock_boundary.free_stock(row, 'planks') == 0
    assert stock_boundary.free_stock_python(row, 'planks') == 0


def test_locked_material_ignores_missing_buckets():
    row = account(stock={'planks': 3})
    assert stock_boundary.locked_material(row, 'planks') == 0
    assert type(stock_boundary.locked_material(row, 'planks')) is int


def test_locked_material_preserves_float_sum_order():
    """Порядок «проект, затем стройки» сохранён: 0.1+0.2+0.3 в том же порядке."""
    row = account(stock={'x': 1.0}, project=project({'x': 0.1}),
                  public_projects=[project({'x': 0.2}, {'x': 0.3})])
    assert stock_boundary.locked_material(row, 'x') == 0.1+0.2+0.3
    assert (stock_boundary.locked_material(row, 'x')
            == stock_boundary.locked_material_python(row, 'x'))


def test_free_stock_does_not_mutate_the_account():
    row = account(stock={'planks': 5}, stock_reserved={'planks': 1})
    before = copy.deepcopy(row)
    stock_boundary.free_stock(row, 'planks')
    stock_boundary.locked_material(row, 'planks')
    assert row == before


# --- 2. total_stock и gold: компенсированные суммы --------------------------

def test_total_stock_matches_builtin_sum_bitwise():
    accounts = [account(id='a', stock={'g': 0.1}), account(id='b', stock={'g': 0.2}),
                account(id='c', stock={'g': 0.3})]
    native = stock_boundary.total_stock(accounts, ['g'])
    reference = stock_boundary.total_stock_python(accounts, ['g'])
    assert native == reference
    assert native['g'] == sum(a['stock'].get('g', 0) for a in accounts)


def test_total_stock_keeps_int_type_for_integral_stock():
    accounts = [account(id='a', stock={'g': 2}), account(id='b', stock={'g': 3})]
    result = stock_boundary.total_stock(accounts, ['g'])
    assert result['g'] == 5
    assert type(result['g']) is int


def test_total_stock_covers_every_good_in_order():
    accounts = [account(id='a', stock={'g': 1, 'h': 2}),
                account(id='b', stock={'g': 3})]
    result = stock_boundary.total_stock(accounts, ['h', 'g'])
    assert list(result) == ['h', 'g']
    assert result == {'h': 2, 'g': 4}


def test_gold_matches_builtin_sum_bitwise():
    accounts = [account(id='a', balance=0.1), account(id='b', balance=0.2),
                account(id='c', balance=0.7)]
    assert stock_boundary.gold(accounts) == stock_boundary.gold_python(accounts)
    assert stock_boundary.gold(accounts) == sum(a['balance'] for a in accounts)


def test_total_stock_and_gold_do_not_mutate_accounts():
    accounts = [account(id='a', stock={'g': 1}, balance=2.0)]
    before = copy.deepcopy(accounts)
    stock_boundary.total_stock(accounts, ['g'])
    stock_boundary.gold(accounts)
    assert accounts == before


# --- 3. enterprise_overhead ------------------------------------------------

def test_enterprise_overhead_matches_python_reference():
    for capacity, labor, wage, rate in ((10, 2, 3.0, 0.02), (7, 0.5, 4.0, 0.0),
                                        (3, 1, 5, 0.1)):
        assert money_boundary.enterprise_overhead(capacity, labor, wage, rate) == \
            money_boundary.enterprise_overhead_python(capacity, labor, wage, rate)


def test_enterprise_overhead_preserves_multiplication_order():
    """Произведение считается слева направо, без перегруппировки."""
    capacity, labor, wage, rate = 7, 0.1, 3.0, 0.7
    assert money_boundary.enterprise_overhead(capacity, labor, wage, rate) == \
        capacity*labor*wage*rate


def test_enterprise_overhead_zero_rate_is_zero():
    assert money_boundary.enterprise_overhead(10, 2, 3.0, 0) == 0


# --- 4. project_finances ---------------------------------------------------

FINANCE_CASES = [
    # capacity, available, startup, material, delivery, unit, fixed, equipment, margin, expansion
    (100, 50, 10, 2, 1, 3.0, 1.0, 4.0, 0.5, False),
    (100, 50, 10, 2, 1, 3.0, 1.0, 4.0, 0.5, True),
    (5, 1000, 10, 2, 1, 3.0, 1.0, 4.0, 0.5, False),
    (0, 0, 10, 0, 0, 0, 0, 0, 1.0, False),
    (12.5, 3.25, 0, 0.5, 0.25, 1.5, 0.5, 2.5, 0.125, True),
]


@pytest.mark.parametrize('case', FINANCE_CASES)
def test_project_finances_matches_python_reference(case):
    native = money_boundary.project_finances(*case, 1e-7)
    reference = money_boundary.project_finances_python(*case, 1e-7)
    assert native == reference


@pytest.mark.parametrize('case', FINANCE_CASES)
def test_project_finances_types_match(case):
    native = money_boundary.project_finances(*case, 1e-7)
    reference = money_boundary.project_finances_python(*case, 1e-7)
    assert [type(v) for v in native] == [type(v) for v in reference]


def test_project_finances_clamps_to_zero_budget():
    """Нулевая касса даёт нулевую мощность, но вложения остаются прежними."""
    capacity, working, investment, profit = money_boundary.project_finances(
        100, 10, 10, 2, 1, 3.0, 1.0, 4.0, 0.5, False, 1e-7)
    assert capacity == 0
    assert working == max(10, 0*(3.0+1.0))
    assert investment == working
    assert profit == 0


def test_project_finances_uses_eps_floor_for_unit_need():
    """Нулевая удельная потребность не делится на ноль: работает пол EPS."""
    capacity, working, investment, profit = money_boundary.project_finances(
        10, 5, 0, 0, 0, 0, 0, 0, 1.0, True, 1e-7)
    assert capacity == 10
    assert investment == 0


# --- 5. Fallback без .pyd ---------------------------------------------------

def test_stock_helpers_fall_back_to_python(monkeypatch):
    assert stock_boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    row = account(stock={'planks': 7}, stock_reserved={'planks': 2},
                  public_projects=[project({'planks': 1})])
    native_value = stock_boundary.free_stock(row, 'planks')
    monkeypatch.setattr(stock_boundary, 'free_stock', stock_boundary.free_stock_python)
    monkeypatch.setattr(stock_boundary, 'NATIVE_KERNEL', False)
    assert stock_boundary.free_stock(row, 'planks') == native_value
    monkeypatch.undo()
    assert stock_boundary.NATIVE_KERNEL is True


def test_money_helpers_fall_back_to_python(monkeypatch):
    assert money_boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    native_value = money_boundary.enterprise_overhead(10, 2, 3.0, 0.02)
    monkeypatch.setattr(money_boundary, 'enterprise_overhead',
                        money_boundary.enterprise_overhead_python)
    monkeypatch.setattr(money_boundary, 'NATIVE_KERNEL', False)
    assert money_boundary.enterprise_overhead(10, 2, 3.0, 0.02) == native_value
    monkeypatch.undo()
    assert money_boundary.NATIVE_KERNEL is True


def test_kernel_loaders_return_none_when_extension_is_missing(monkeypatch):
    import importlib
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith(('stock_kernel', 'money_kernel')):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert stock_boundary.load_stock_kernel() is None
    assert money_boundary.load_money_kernel() is None


def test_boundaries_expose_both_implementations():
    for name in ('locked_material', 'locked_material_python', 'free_stock',
                 'free_stock_python', 'total_stock', 'total_stock_python',
                 'gold', 'gold_python'):
        assert callable(getattr(stock_boundary, name)), name
    for name in ('enterprise_overhead', 'enterprise_overhead_python',
                 'project_finances', 'project_finances_python'):
        assert callable(getattr(money_boundary, name)), name


# --- 6. Реальный движок: exact ---------------------------------------------

def build_world(seed, months, detail='standard'):
    tmp = workdir('test_numeric_helpers')
    with tmp as directory:
        db, service = sr.build_world(seed, directory, report_detail=detail)
        try:
            for month in range(months):
                service.advance_turn(
                    command_id='nh-test-%d-%d' % (seed, month),
                    source='audit')
            return service.economy_state().engine
        finally:
            db.close()


@pytest.fixture(scope='module')
def world_17():
    return build_world(17, 3)


@pytest.fixture(scope='module')
def world_73129():
    return build_world(73129, 3)


def test_engine_helpers_match_python_on_live_world(world_17):
    engine = world_17
    for account_id in list(engine.accounts)[:40]:
        for good in list(engine.goods)[:6]:
            assert engine.free_stock(account_id, good) == \
                stock_boundary.free_stock_python(engine.accounts[account_id], good)
            assert engine.locked_material(account_id, good) == \
                stock_boundary.locked_material_python(engine.accounts[account_id], good)
    assert engine.total_stock() == stock_boundary.total_stock_python(
        list(engine.accounts.values()), engine.goods)
    assert engine.gold() == stock_boundary.gold_python(list(engine.accounts.values()))


def test_engine_overhead_matches_python_on_live_world(world_17):
    engine = world_17
    for enterprise in list(engine.enterprises.values())[:40]:
        expected = money_boundary.enterprise_overhead_python(
            enterprise['capacity'], engine.goods[enterprise['good']]['labor'],
            engine.cfg['wage'], engine.cfg.get('enterprise_overhead_rate', 0))
        assert engine.enterprise_overhead(enterprise) == expected


@pytest.mark.parametrize('seed,fixture_name', [(17, 'world_17'),
                                               (73129, 'world_73129')])
def test_exact_three_months_are_unchanged(seed, fixture_name, request):
    engine = request.getfixturevalue(fixture_name)
    assert engine.meta['month'] == 3
    assert engine_digest(engine) == GOLDEN[seed]['engine']
    assert digest(engine.report) == GOLDEN[seed]['report']
    assert digest(engine.rng.getstate()) == GOLDEN[seed]['rng']
