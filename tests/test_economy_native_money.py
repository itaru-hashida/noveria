"""Focused-тесты ядра денежных и трудовых расчётов.

Проверяются ``project_cash_reserve``, ``working_cash_reserve``,
``cash_after_commitments``, ``reserve_expedited_construction_labor``,
``distribute`` и распределение доходов carrier/treasury в ``settle``:
значения, типы, порядок получателей, округления, ограничения кассы и труда.

Эталон — ``*_python`` функции в границе, fallback — те же эквиваленты при
отсутствии собранного расширения. ``working_cash_reserve`` и
``cash_after_commitments`` переиспользуют ``dividend_kernel`` и здесь
сверяются с прежним Python-телом.
"""
import copy
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy, support, v3  # noqa: E402
from modules.economy import support as support_module  # noqa: E402
from modules.economy.native import money_boundary as boundary  # noqa: E402

pytest.importorskip('modules.economy.native.money_kernel')

EPS = support.EPS
FIXTURE = ROOT/'tests/fixtures/economy.json'


def build_engine(seed=17, detail='standard'):
    cfg = json.loads(FIXTURE.read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H',
             properties={'owner_id': 'p', 'population': 100000}),
        dict(id='h2', kind='SocialGroup', name='H2',
             properties={'owner_id': 'p', 'population': 40000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'лён', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def clear(engine):
    engine.report['orders'] = []
    engine._order_store = None
    for a in engine.accounts.values():
        a.update(stock={}, values={}, stock_reserved={})
    engine.reset_report()
    return engine


def firm(engine, good='bread'):
    return next(e for e in engine.enterprises.values() if e['good'] == good)


def typed(value):
    return (type(value).__name__, value)


# --- 1. project_cash_reserve ---------------------------------------------------

def project(engine, capacity=10.0, working_cash=100.0, delivery=0.0):
    return dict(capacity=capacity, working_cash=working_cash, delivery=delivery,
                location=dict(tile_id='t', province_id='p', state_id='s'),
                materials={}, equipment_held={}, support_stock={},
                material_bill={}, equipment={}, equipment_wear={},
                work_required=10.0, work_done=0.0, max_workers=5.0, mode='normal')


def test_project_reserve_without_project_is_zero():
    engine = build_engine()
    clear(engine)
    assert engine.project_cash_reserve('private:s', {}) == 0.0


def test_project_reserve_counts_working_cash_and_missing_materials():
    engine = build_engine()
    clear(engine)
    account = engine.accounts['private:s']
    account['project'] = project(engine, capacity=engine.cfg['minimum_capacity'])
    engine.add_stock('private:s', 'planks', 1, 2)
    reserve = engine.project_cash_reserve('private:s', {})
    bill = engine.construction_bill(account['project']['capacity'])
    # Рабочая касса плюс недостача каждого материала по котировке.
    assert reserve > 100.0
    assert reserve == pytest.approx(boundary.project_cash_reserve_python(
        engine, 'private:s', {}))
    assert bill


def test_project_reserve_subtracts_paid_construction_orders():
    engine = build_engine()
    clear(engine)
    account = engine.accounts['private:s']
    account['project'] = project(engine, capacity=engine.cfg['minimum_capacity'])
    before = engine.project_cash_reserve('private:s', {})
    good = next(iter(engine.construction_bill(account['project']['capacity'])))
    engine.add_stock('private:s', 'planks', 100, 100)
    order = engine.order('private:s', good, 1, 'buy', 1.0, 'Строительные материалы')
    order['reserved'] = 1.0
    after = engine.project_cash_reserve('private:s', {})
    # Купленное и обеспеченная заявка повторного резерва не требуют.
    assert after < before


def test_project_reserve_uses_delivery_fallback_without_route(monkeypatch):
    """Без маршрута резерв берёт прежнюю ``delivery`` проекта.

    ``path`` подменяется, а не зовётся на несвязанном узле: кэш маршрутов
    процесса (``ROUTE_TREE_CACHE``) общий и ключуется только набором тайлов,
    поэтому запись в него здесь испортила бы прогоны соседних тестов.
    """
    engine = build_engine()
    clear(engine)
    account = engine.accounts['private:s']
    account['project'] = project(engine, capacity=engine.cfg['minimum_capacity'],
                                 delivery=7.5)
    monkeypatch.setattr(engine, 'path', lambda start, end: (None, []))
    reserve = engine.project_cash_reserve('private:s', {})
    reference = boundary.project_cash_reserve_python(engine, 'private:s', {})
    assert reserve == pytest.approx(reference)
    # Недостача материалов считается и без маршрута: смета непустая.
    assert engine.construction_bill(account['project']['capacity'])


def test_project_reserve_matches_reference_types():
    engine = build_engine()
    clear(engine)
    engine.accounts['private:s']['project'] = project(
        engine, capacity=engine.cfg['minimum_capacity'])
    native = engine.project_cash_reserve('private:s', {})
    reference = boundary.project_cash_reserve_python(engine, 'private:s', {})
    assert typed(native) == typed(reference)


# --- 2. working_cash_reserve / cash_after_commitments ---------------------------

def test_working_cash_reserve_reuses_dividend_kernel():
    engine = build_engine()
    clear(engine)
    bakery = firm(engine)
    native = engine.working_cash_reserve(bakery, {})
    reference = boundary.working_cash_reserve_python(engine, bakery, {})
    assert typed(native) == typed(reference)
    wage = (bakery['capacity']*engine.cfg['working_reserve_months']
            *engine.goods[bakery['good']]['labor']*engine.cfg['wage'])
    assert native >= wage


def test_cash_after_commitments_uses_max_of_protected_and_reserves():
    engine = build_engine()
    clear(engine)
    account = engine.accounts['private:s']
    account.update(balance=1000.0, reserved=25.0, protected=0.0)
    engine.cash_store.set_reserved_cash('private:s', 25.0)
    assert engine.cash_after_commitments('private:s', 25, 10) == pytest.approx(
        1000-25-35)
    assert engine.cash_after_commitments('private:s', 25, 10) == pytest.approx(
        boundary.cash_after_commitments_python(engine, 'private:s', 25, 10))


def test_cash_after_commitments_never_negative_and_keeps_int():
    engine = build_engine()
    clear(engine)
    account = engine.accounts['private:s']
    account.update(balance=10, reserved=100, protected=0.0)
    engine.cash_store.set_reserved_cash('private:s', 100)
    assert engine.cash_after_commitments('private:s', 0) == 0
    assert type(engine.cash_after_commitments('private:s', 0)) is int


# --- 3. reserve_expedited_construction_labor -----------------------------------

def accelerated_engine(seed=17):
    engine = build_engine(seed)
    clear(engine)
    engine.goods['tools']['durable'] = {'wear_rate': .04}
    engine.cfg.update(labor_share=10/100000,
        institutions={'academy': dict(name='Академия', materials={'planks': 1},
                                      equipment={'tools': 1},
                                      equipment_wear={'tools': 0},
                                      work_required=10, max_workers=10)},
        construction_modes={
            'normal': dict(wage_multiplier=1, lodging_allowance=0, support_goods={}),
            'accelerated': dict(wage_multiplier=2, lodging_allowance=.04,
                                support_goods={})})
    engine.start_public_project('c', 'academy', mode='accelerated')
    municipal = engine.accounts['municipal:c']
    project_row = municipal['public_projects'][0]
    engine.add_stock(municipal['id'], 'planks', 1, 2)
    engine.add_stock(municipal['id'], 'tools', 1, 5.5)
    project_row['materials']['planks'] = 1
    project_row['equipment_held']['tools'] = 1
    return engine, project_row


def test_expedited_labor_reserves_workers_and_matches_reference():
    engine, project_row = accelerated_engine()
    engine.reserve_expedited_construction_labor()
    workers = engine._expedited_construction_workers[project_row['id']]
    assert workers == pytest.approx(10)

    reference = copy.deepcopy(engine)
    reference.__dict__.pop('_expedited_construction_workers', None)
    boundary.reserve_expedited_construction_labor_python(reference)
    assert engine._expedited_construction_workers == \
           reference._expedited_construction_workers
    assert dict(engine._expedited_construction_reservations) == \
           dict(reference._expedited_construction_reservations)


def test_expedited_labor_limited_by_support_stock():
    engine, project_row = accelerated_engine()
    engine.cfg['construction_modes']['accelerated']['support_goods'] = {'tools': 2.0}
    project_row['support_stock']['tools'] = 3.0
    engine.reserve_expedited_construction_labor()
    # 3 единицы поддержки при норме 2 на работника — это 1.5 работника.
    assert engine._expedited_construction_workers[project_row['id']] == pytest.approx(1.5)


def test_expedited_labor_limited_by_cash():
    engine, project_row = accelerated_engine()
    cost = engine.cfg['wage']*2+.04
    engine.accounts['municipal:c']['balance'] = cost*2
    engine.reserve_expedited_construction_labor()
    assert engine._expedited_construction_workers[project_row['id']] == pytest.approx(2)


def test_expedited_labor_skips_unready_projects():
    engine, project_row = accelerated_engine()
    project_row['materials']['planks'] = 0
    engine.reserve_expedited_construction_labor()
    assert engine._expedited_construction_workers == {}


def test_expedited_labor_consumes_no_rng():
    engine, _ = accelerated_engine()
    before = repr(engine.rng.getstate())
    engine.reserve_expedited_construction_labor()
    assert repr(engine.rng.getstate()) == before


# --- 4. distribute -------------------------------------------------------------

def test_distribute_splits_by_population_and_orders_recipients():
    engine = build_engine()
    clear(engine)
    sender = engine.accounts['treasury:s']
    sender['balance'] = 1000.0
    engine.cash_store.set_balance('treasury:s', 1000.0)
    recipients = [engine.accounts['h'], engine.accounts['h2']]
    before = {a['id']: a['balance'] for a in recipients}
    engine.distribute('treasury:s', recipients, 300.0, 'Тест')
    assert engine.accounts['h']['balance']-before['h'] == pytest.approx(
        300*100000/140000)
    assert engine.accounts['h2']['balance']-before['h2'] == pytest.approx(
        300*40000/140000)
    assert [f['recipient'] for f in engine.report['flows']] == ['h', 'h2']


def test_distribute_clips_amount_by_live_balance():
    engine = build_engine()
    clear(engine)
    sender = engine.accounts['treasury:s']
    sender['balance'] = 50.0
    engine.cash_store.set_balance('treasury:s', 50.0)
    recipients = [engine.accounts['h']]
    before = recipients[0]['balance']
    engine.distribute('treasury:s', recipients, 500.0, 'Тест')
    assert recipients[0]['balance']-before == pytest.approx(50.0)
    assert engine.cash_store.get_balance('treasury:s') == pytest.approx(0.0)


def test_distribute_does_nothing_without_population():
    engine = build_engine()
    clear(engine)
    before = copy.deepcopy(engine.report['flows'])
    engine.distribute('treasury:s', [engine.accounts['private:s']], 100.0, 'Тест')
    assert engine.report['flows'] == before


def test_distribute_matches_python_reference():
    engine = build_engine()
    clear(engine)
    engine.accounts['treasury:s']['balance'] = 1000.0
    engine.cash_store.set_balance('treasury:s', 1000.0)
    recipients = [engine.accounts['h'], engine.accounts['h2']]
    reference = copy.deepcopy(engine)
    engine.distribute('treasury:s', recipients, 123.456, 'Тест')
    boundary.distribute_python(reference, 'treasury:s', recipients, 123.456, 'Тест')
    assert {(f['recipient'], typed(f['gold'])) for f in engine.report['flows']} == \
           {(f['recipient'], typed(f['gold'])) for f in reference.report['flows']}


def test_distribute_consumes_no_rng():
    engine = build_engine()
    clear(engine)
    engine.accounts['treasury:s']['balance'] = 1000.0
    engine.cash_store.set_balance('treasury:s', 1000.0)
    before = repr(engine.rng.getstate())
    engine.distribute('treasury:s', [engine.accounts['h']], 10.0, 'Тест')
    assert repr(engine.rng.getstate()) == before


# --- 5. settle: carrier/treasury ----------------------------------------------

def test_settle_distributes_carrier_and_treasury_income():
    engine = build_engine()
    clear(engine)
    for account in engine.accounts.values():
        if account['kind'] in ('carrier', 'treasury'):
            account['balance'] = 10000.0
            engine.cash_store.set_balance(account['id'], 10000.0)
    households = [a for a in engine.accounts.values() if a['kind'] == 'household']
    before = {h['id']: h['balance'] for h in households}
    boundary.settle(engine, [], households)
    carried = {f['reason'] for f in engine.report['flows']}
    assert carried <= {'Оплата общественных услуг',
                       'Оплата транспортных услуг населению'}
    for household in households:
        assert household['balance'] >= before[household['id']]


def test_settle_matches_python_reference_with_same_shuffle():
    engine = build_engine()
    clear(engine)
    for account in engine.accounts.values():
        if account['kind'] in ('carrier', 'treasury'):
            account['balance'] = 10000.0
            engine.cash_store.set_balance(account['id'], 10000.0)
    households = [a for a in engine.accounts.values() if a['kind'] == 'household']

    native = copy.deepcopy(engine)
    native.rng.seed(99)
    boundary.settle(native, [], households)
    reference = copy.deepcopy(engine)
    reference.rng.seed(99)
    boundary.settle_python(reference, [], households)
    assert [(f['sender'], f['recipient'], typed(f['gold']), f['reason'])
            for f in native.report['flows']] == \
           [(f['sender'], f['recipient'], typed(f['gold']), f['reason'])
            for f in reference.report['flows']]


# --- 6. Fallback и call site ---------------------------------------------------

def test_fallback_switches_to_the_python_reference(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    engine, project_row = accelerated_engine()
    engine.reserve_expedited_construction_labor()
    native_workers = dict(engine._expedited_construction_workers)

    monkeypatch.setattr(boundary, 'project_cash_reserve',
                        boundary.project_cash_reserve_python)
    monkeypatch.setattr(boundary, 'reserve_expedited_construction_labor',
                        boundary.reserve_expedited_construction_labor_python)
    monkeypatch.setattr(boundary, 'distribute', boundary.distribute_python)
    monkeypatch.setattr(boundary, 'settle', boundary.settle_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    engine.reserve_expedited_construction_labor()
    assert engine._expedited_construction_workers == native_workers
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('money_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_money_kernel() is None


def test_v3_and_support_delegate_to_money_boundary():
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    support_source = (ROOT/'modules/economy/support.py').read_text(encoding='utf-8')
    assert module_source.count('_money.project_cash_reserve(') == 1
    assert module_source.count('_money.working_cash_reserve(') == 1
    assert module_source.count('_money.cash_after_commitments(') == 1
    assert module_source.count('_money.reserve_expedited_construction_labor(') == 1
    assert module_source.count('_money.settle(') == 1
    assert support_source.count('_money.distribute(') == 1
    # Прежние Python-тела не остались ни там, ни там.
    for body in (inspect.getsource(Economy.project_cash_reserve),
                 inspect.getsource(Economy.working_cash_reserve),
                 inspect.getsource(Economy.cash_after_commitments),
                 inspect.getsource(Economy.reserve_expedited_construction_labor),
                 inspect.getsource(Economy.settle),
                 inspect.getsource(support.EconomyServices.distribute)):
        assert '_money.' in body
    assert 'sum(self.population(a) for a in recipients)' not in support_source
    assert "self.cfg['construction_modes']['accelerated']" not in module_source
    # wage/dividend/settlement ядра повторно не переносились.
    assert 'working_reserve(' in (ROOT/'modules/economy/native/dividend_kernel.pyx').read_text(encoding='utf-8')


def test_native_kernel_is_used_on_the_standard_path():
    engine = build_engine()
    clear(engine)
    engine.accounts['private:s']['project'] = project(
        engine, capacity=engine.cfg['minimum_capacity'])
    seen = []
    real = boundary.run_project_reserve
    boundary.run_project_reserve = lambda *args: seen.append(args) or real(*args)
    try:
        engine.project_cash_reserve('private:s', {})
    finally:
        boundary.run_project_reserve = real
    assert seen, 'штатный путь обязан звать ядро money_kernel'
