"""Focused-тесты доставки строительных материалов: побитовое совпадение с V3.

Проверяется вертикальный срез доставки внутри ``project_material_requests``:
объём перевозки, её стоимость, запись ``project['delivery']``, защита денег
(``protected``) и порядок «смета -> доставка -> резерв материалов». Ядро
работает с object-арифметикой намеренно: остатки и смета бывают и int, и
float, а тип результата виден дальше в отчёте. Поэтому сравниваются не только
значения, но и типы, и порядок вызовов хуков.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
гоняет обе реализации на одинаковом состоянии.
"""
import builtins
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import delivery_boundary  # noqa: E402
from modules.economy.native import material_boundary  # noqa: E402

EPS = support.EPS
_PY_MAX = builtins.max


class FakeStore:
    """Граница заявок: журнал вызовов и те же значения в тех же dict-ах."""

    def __init__(self):
        self.journal = []

    def set_remaining(self, offer, value):
        self.journal.append(('set_remaining', offer['id'], value))
        offer['remaining'] = value

    def set_quantity(self, offer, value):
        self.journal.append(('set_quantity', offer['id'], value))
        offer['quantity'] = value

    def set_metadata(self, offer, key, value):
        self.journal.append(('set_metadata', offer['id'], key, value))
        offer[key] = value


class FakeOwner:
    """Минимальный носитель хуков: ядру нужны только они и ``cfg``/``goods``."""

    def __init__(self, free=None, goods=None, cfg=None):
        self.free = None if free is None else dict(free)
        self.goods = goods or {}
        self.cfg = cfg or dict(minimum_capacity=10, transport_rate=0.01,
                               construction={'wood': 4, 'stone': 2})
        self.accounts = {}
        self.journal = []

    def free_stock(self, account_id, good):
        self.journal.append(('free_stock', account_id, good))
        if self.free is not None:
            return self.free.get(good, 0)
        account = self.accounts[account_id]
        locked = account.get('project', {}).get('materials', {}).get(good, 0)
        return _PY_MAX(0, account['stock'].get(good, 0)
                       -account.get('stock_reserved', {}).get(good, 0)-locked)

    def use_stock(self, account_id, good, quantity):
        self.journal.append(('use_stock', account_id, good, quantity))
        return 0.0

    def quoted_cost(self, account, good, quantity, sells):
        self.journal.append(('quoted_cost', account['id'], good, quantity))
        return 0.0, 1.0, True

    def order(self, participant, good, quantity, side, price, purpose):
        self.journal.append(('order', participant, good, quantity, side, price, purpose))

    def mark_market_quotes_dirty(self, good):
        self.journal.append(('dirty', good))

    def cancel_order(self, offer, reason):
        self.journal.append(('cancel', offer['id'], reason))

    def transfer(self, source, target, amount, reason):
        self.journal.append(('transfer', source, target, amount, reason))


def make_case(stock=None, reserved=None, materials=None, capacity=10,
              balance=1000.0, working_cash=11, operating=5, payroll=7,
              distance=4, offers=None, account_id='acc'):
    """Собрать независимый owner/account/project/sells/store."""
    owner = FakeOwner(free={'wood': 0, 'stone': 0},
                      goods={'wood': dict(weight=2), 'stone': dict(weight=5)})
    account = dict(id=account_id, stock=dict(stock or {}),
                   stock_reserved=dict(reserved or {}),
                   balance=balance, reserved=0.0,
                   operating_protected=operating, payroll_protected=payroll)
    project = dict(capacity=capacity, materials=dict(materials or {}),
                   working_cash=working_cash)
    account['project'] = project
    owner.accounts[account_id] = account
    store = FakeStore()
    sells = {good: [dict(offer) for offer in rows]
             for good, rows in (offers or {}).items()}
    return dict(owner=owner, account=account, project=project, sells=sells,
                store=store, distance=distance)


def run(implementation, case):
    """Прогнать одну реализацию ``request_project_materials``."""
    bill = implementation(case['owner'], case['account'], case['sells'], EPS,
                          case['store'], case['distance'])
    return dict(case, bill=bill)


def assert_same(reference, native):
    """Сравнить значения, типы и порядок вызовов хуков."""
    assert native['bill'] == reference['bill']
    assert list(native['bill']) == list(reference['bill'])
    for good, quantity in reference['bill'].items():
        assert type(native['bill'][good]) is type(quantity), good
    assert native['project']['delivery'] == reference['project']['delivery']
    assert (type(native['project']['delivery'])
            is type(reference['project']['delivery']))
    assert native['account']['protected'] == reference['account']['protected']
    assert (type(native['account']['protected'])
            is type(reference['account']['protected']))
    assert native['project']['materials'] == reference['project']['materials']
    assert list(native['project']['materials']) == list(reference['project']['materials'])
    for good, value in reference['project']['materials'].items():
        assert type(native['project']['materials'][good]) is type(value), good
    for field in ('stock', 'stock_reserved'):
        assert native['account'][field] == reference['account'][field], field
    assert native['sells'] == reference['sells']
    assert native['store'].journal == reference['store'].journal
    assert native['owner'].journal == reference['owner'].journal


DELIVERY_CASES = [
    # Базовый случай: смета wood=4, stone=2 при весах 2 и 5.
    dict(),
    # Мощность не кратна minimum_capacity: дробная смета и дробный объём.
    dict(capacity=7, distance=3),
    # Нулевая мощность: пустая по весу перевозка.
    dict(capacity=0),
    # Целые остатки склада: тип объёма и стоимости обязан сохраниться.
    dict(stock={'wood': 4, 'stone': 2}, reserved={'wood': 4, 'stone': 2},
         materials={'wood': 4, 'stone': 2}),
    # Баланс меньше защиты: ``protected`` урезается по balance-reserved.
    dict(balance=100.0, working_cash=90),
    # Дробный склад: материал доезжает частично.
    dict(stock={'wood': 2.5}, reserved={'wood': 2.5}),
    # Собственный оффер: доставка идёт до резерва, release виден дальше.
    dict(stock={'wood': 10}, reserved={'wood': 10},
         offers={'wood': [dict(id='o1', participant='acc', good='wood',
                               quantity=10, remaining=10, backing=10,
                               status='active')]}),
]


@pytest.mark.parametrize('case_kwargs', DELIVERY_CASES)
def test_native_matches_python_reference(case_kwargs):
    reference = run(delivery_boundary.request_project_materials_python,
                    make_case(**case_kwargs))
    native = run(delivery_boundary.request_project_materials,
                 make_case(**case_kwargs))
    assert_same(reference, native)


def test_delivery_volume_matches_builtin_sum_order():
    """Объём считается тем же ``sum`` и в том же порядке сметы."""
    cases = [
        {'wood': 4.0, 'stone': 2.0},
        {'wood': 1, 'stone': 2},
        {'wood': 0, 'stone': 0},
        {},
        {'wood': 1e16, 'stone': 1.0},
        {'wood': 7/3, 'stone': 5/7},
    ]
    goods = {'wood': dict(weight=2), 'stone': dict(weight=5)}
    for bill in cases:
        expected = sum(q*goods[g]['weight'] for g, q in bill.items())
        packed = delivery_boundary.delivery_volume(bill, goods)
        assert packed == expected, bill
        assert type(packed) is type(expected), (bill, type(packed), type(expected))


def test_delivery_volume_matches_python_on_compensated_cancellation():
    """Компенсация CPython 3.12: объём не должен считаться наивно."""
    goods = {'a': dict(weight=1), 'b': dict(weight=1), 'c': dict(weight=1)}
    bill = {'a': 1e16, 'b': 1.0, 'c': -1e16}
    # Наивное накопление даёт 0.0, встроенный sum 3.12 — 1.0.
    assert sum(bill.values()) == 1.0
    assert delivery_boundary.delivery_volume(bill, goods) == 1.0
    assert (delivery_boundary.delivery_volume(bill, goods)
            == delivery_boundary.delivery_volume_python(bill, goods))


def test_delivery_cost_keeps_operation_order():
    """Стоимость — ``volume*distance*transport_rate`` прежним порядком."""
    goods = {'wood': dict(weight=2), 'stone': dict(weight=5)}
    bill = {'wood': 4.0, 'stone': 2.0}
    volume = delivery_boundary.delivery_volume(bill, goods)
    expected = volume*3*0.01
    assert delivery_boundary.delivery_cost(bill, goods, 3, 0.01) == expected
    assert delivery_boundary.delivery_cost(bill, goods, 3, 0.01) == (
        delivery_boundary.delivery_cost_python(bill, goods, 3, 0.01))


def test_plan_delivery_writes_delivery_before_protection():
    """``project['delivery']`` записан до хука защиты денег."""
    owner = FakeOwner(goods={'wood': dict(weight=2), 'stone': dict(weight=5)})
    account = dict(id='acc', stock={}, stock_reserved={}, balance=1000.0,
                   reserved=0.0, operating_protected=5, payroll_protected=7)
    project = dict(capacity=10, materials={}, working_cash=11)
    seen = {}

    def set_protection(owner_, account_, project_, delivery_, eps_):
        seen['delivery'] = project_['delivery']
        seen['arg'] = delivery_
        delivery_boundary.set_project_protection(
            owner_, account_, project_, delivery_, eps_)

    bill = {'wood': 4.0, 'stone': 2.0}
    delivery = delivery_boundary.plan_delivery(
        owner, account, project, bill, 4, EPS, set_protection)
    # Вес 4*2+2*5=18; 18*4*0.01=0.72.
    assert delivery == 18*4*owner.cfg['transport_rate']
    assert project['delivery'] == delivery
    assert seen == dict(delivery=delivery, arg=delivery)
    assert account['protected'] == 5+7+11+delivery


def test_protection_clamps_by_balance_and_reserved():
    """Урезание ``protected`` по ``balance-reserved`` сохранено."""
    owner = FakeOwner(goods={'wood': dict(weight=1), 'stone': dict(weight=1)})
    account = dict(id='acc', stock={}, stock_reserved={}, balance=100.0,
                   reserved=30.0, operating_protected=0, payroll_protected=0)
    project = dict(capacity=10, materials={}, working_cash=90)
    delivery_boundary.plan_delivery(owner, account, project,
                                    {'wood': 0.0, 'stone': 0.0}, 1, EPS,
                                    delivery_boundary.set_project_protection)
    assert account['protected'] == _PY_MAX(0, 100.0-30.0)


def test_delivery_runs_before_material_reservation():
    """Порядок прежний: доставка и ``protected`` -> резерв -> закупки."""
    case = make_case()
    result = run(delivery_boundary.request_project_materials, case)
    journal = result['owner'].journal
    kinds = [row[0] for row in journal]
    assert 'free_stock' in kinds
    assert kinds[-1] == 'order'
    # Офферов нет, поэтому release не состоялся; материалы не закрепились.
    assert result['project']['materials'] == {'wood': 0.0, 'stone': 0.0}
    assert ('order', 'acc', 'wood', 4.0, 'buy', 1.0,
            material_boundary.MATERIAL_PURPOSE) in journal
    assert ('order', 'acc', 'stone', 2.0, 'buy', 1.0,
            material_boundary.MATERIAL_PURPOSE) in journal


def test_material_and_delivery_share_the_same_state():
    """Доставка не создаёт второй проект, склад или резерв."""
    case = make_case(stock={'wood': 10}, reserved={'wood': 10})
    account, project = case['account'], case['project']
    materials = project['materials']
    stock, reserved = account['stock'], account['stock_reserved']
    run(delivery_boundary.request_project_materials, case)
    assert project['materials'] is materials
    assert account['stock'] is stock
    assert account['stock_reserved'] is reserved
    # Доставка материал не списывает: это делает прежний use_stock.
    assert account['stock']['wood'] == 10


def test_delivery_does_not_touch_rng_report_or_queue():
    """Срез не трогает RNG, отчёт и очередь инвестиций."""
    case = make_case()
    result = run(delivery_boundary.request_project_materials, case)
    keys = {row[0] for row in result['owner'].journal}
    # Никаких отчётных или очередных хуков срез не вызывает вовсе.
    assert keys <= {'free_stock', 'quoted_cost', 'order', 'dirty', 'cancel'}


def test_construction_bill_comes_from_material_boundary():
    """Смета одна на резерв и доставку — из существующей границы."""
    case = make_case(capacity=7)
    project = case['project']
    expected = material_boundary.construction_bill(
        case['owner'].cfg['construction'],
        case['owner'].cfg['minimum_capacity'], project['capacity'])
    result = run(delivery_boundary.request_project_materials, case)
    assert result['bill'] == expected
    assert list(result['bill']) == list(expected)


def test_native_bill_matches_v3_exactly():
    """Смета по-прежнему побитово равна ``V3.construction_bill``."""
    cfg = json.loads((ROOT/'tests/fixtures/economy.json').read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = 'none'
    engine.seed = 17
    engine.rng.seed(17)
    engine.bootstrap()
    for capacity in (1, 3, 7, 7.5, 100, 0):
        bill = material_boundary.construction_bill(
            engine.cfg['construction'], engine.cfg['minimum_capacity'], capacity)
        volume = delivery_boundary.delivery_volume(bill, engine.goods)
        expected = sum(q*engine.goods[g]['weight'] for g, q in bill.items())
        assert volume == expected, capacity
        assert type(volume) is type(expected), capacity
        cost = delivery_boundary.delivery_cost(bill, engine.goods, 5,
                                               engine.cfg['transport_rate'])
        assert cost == expected*5*engine.cfg['transport_rate'], capacity


def test_boundary_exposes_native_when_built():
    built = delivery_boundary.load_delivery_kernel() is not None
    assert delivery_boundary.NATIVE_DELIVERY_KERNEL is built
    if built:
        assert delivery_boundary.plan_delivery.__module__.endswith('delivery_kernel')
    else:
        assert delivery_boundary.plan_delivery is delivery_boundary.plan_delivery_python


def test_native_entry_points_are_the_compiled_ones():
    """Граница обязана привязать именно скомпилированные функции."""
    kernel = delivery_boundary.load_delivery_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение delivery_kernel не собрано')
    assert delivery_boundary.delivery_volume is kernel.delivery_volume
    assert delivery_boundary.delivery_cost is kernel.delivery_cost
    assert delivery_boundary.plan_delivery is kernel.plan_delivery
    assert delivery_boundary.plan_delivery is not delivery_boundary.plan_delivery_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    """Отсутствие собранного ядра даёт None, а не падает на импорте economy."""
    import importlib

    name = 'modules.economy.native.delivery_kernel'
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name == name or module_name.endswith('delivery_kernel'):
            raise ImportError("cannot import name 'delivery_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert delivery_boundary.load_delivery_kernel() is None


def test_v3_call_site_delegates_to_delivery_boundary():
    """В ``project_material_requests`` один delivery call site, без копии логики."""
    source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    start = source.index('def project_material_requests')
    end = source.index('def finish_projects')
    body = source[start:end]
    assert '_delivery.request_project_materials(self, a, sells, EPS, store, distance)' in body
    assert body.count('request_project_materials') == 1
    # Ни смета, ни доставка, ни защита денег не остались в Python-теле.
    assert 'construction_bill' not in body
    assert 'transport_rate' not in body
    assert "project['delivery']" not in body
    assert '_material.' not in body
    assert 'stock_reserved' not in body
    assert 'self.free_stock' not in body
    assert 'self.order(' not in body
    assert 'cancel_order' not in body
