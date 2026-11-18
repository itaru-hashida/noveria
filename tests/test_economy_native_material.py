"""Focused-тесты резерва строительных материалов: побитовое совпадение с V3.

Проверяется вертикальный срез ``project_material_requests``: смета, проверка
доступного склада, резерв материалов и обновление ``stock_reserved``. Ядро
работает с object-арифметикой намеренно: остатки V3 бывают и int, и float, а
встроенные ``min``/``max`` сохраняют тип аргумента, и этот тип виден в отчёте.
Поэтому сравниваются не только значения, но и типы, и порядок вызовов хуков.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
резерва гоняет обе реализации на одинаковом состоянии.
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
    """Минимальный носитель хуков: ядру нужны только они и ``cfg``/``goods``.

    Тела хуков повторяют движок, но с журналом вызовов: так видно не только
    итоговое состояние, но и порядок операций, который обязан совпадать.
    ``free_stock`` по умолчанию считается как ``V3.free_stock`` — от живого
    склада счёта, его ``stock_reserved`` и материалов проекта, — иначе
    освобождение собственных офферов не было бы видно следующему шагу.
    """

    def __init__(self, free=None, goods=None, cfg=None, accounts=None):
        self.free = None if free is None else dict(free)
        self.goods = goods or {}
        self.cfg = cfg or dict(minimum_capacity=10, transport_rate=0.01,
                               construction={'wood': 4, 'stone': 2})
        self.accounts = accounts or {}
        self.journal = []

    def free_stock(self, account_id, good):
        self.journal.append(('free_stock', account_id, good))
        if self.free is not None:
            return self.free.get(good, 0)
        account = self.accounts[account_id]
        locked = account.get('project', {}).get('materials', {}).get(good, 0)
        return _PY_MAX(0, account['stock'].get(good, 0)
                       -account.get('stock_reserved', {}).get(good, 0)-locked)

    def quoted_cost(self, account, good, quantity, sells):
        self.journal.append(('quoted_cost', account['id'], good, quantity))
        return 0.0, 1.0, True

    def order(self, participant, good, quantity, side, price, purpose):
        self.journal.append(('order', participant, good, quantity, side, price, purpose))

    def mark_market_quotes_dirty(self, good):
        self.journal.append(('dirty', good))

    def cancel_order(self, offer, reason):
        self.journal.append(('cancel', offer['id'], reason))


def make_case(stock=None, reserved=None, materials=None, capacity=10,
              free=None, offers=None, account_id='acc'):
    """Собрать независимую пару (owner, account, project, sells, store)."""
    owner = FakeOwner(free=free, goods={'wood': dict(weight=2), 'stone': dict(weight=5)})
    account = dict(id=account_id, stock=dict(stock or {}),
                   stock_reserved=dict(reserved or {}),
                   balance=1000.0, reserved=0.0)
    project = dict(capacity=capacity, materials=dict(materials or {}))
    account['project'] = project
    owner.accounts[account_id] = account
    store = FakeStore()
    sells = {good: [dict(offer) for offer in rows]
             for good, rows in (offers or {}).items()}
    return owner, account, project, sells, store


def run(implementation, case, bill=None):
    """Прогнать одну реализацию резерва и вернуть наблюдаемое состояние."""
    owner, account, project, sells, store = case
    bill = bill if bill is not None else material_boundary.construction_bill(
        owner.cfg['construction'], owner.cfg['minimum_capacity'], project['capacity'])
    implementation(owner, account, project, bill, sells, EPS, store,
                   material_boundary.free_stock_hook,
                   material_boundary.detach_offer_hook,
                   material_boundary.settle_release_hook,
                   material_boundary.place_order_hook)
    return dict(owner=owner, account=account, project=project, sells=sells,
                store=store, bill=bill)


def assert_same(reference, native):
    """Сравнить значения, типы и порядок вызовов хуков."""
    assert native['bill'] == reference['bill']
    for good, quantity in reference['bill'].items():
        assert type(native['bill'][good]) is type(quantity), good
    for field in ('stock', 'stock_reserved'):
        assert native['account'][field] == reference['account'][field], field
        for good, value in reference['account'][field].items():
            assert type(native['account'][field][good]) is type(value), (field, good)
    assert native['project']['materials'] == reference['project']['materials']
    for good, value in reference['project']['materials'].items():
        assert type(native['project']['materials'][good]) is type(value), good
    assert native['sells'] == reference['sells']
    assert native['store'].journal == reference['store'].journal
    assert native['owner'].journal == reference['owner'].journal


RESERVE_CASES = [
    # Пустой склад: всё недостающее уходит в закупку.
    dict(stock={}, reserved={}, materials={}),
    # Склад покрывает половину: int-остаток остаётся int.
    dict(stock={'wood': 2}, reserved={'wood': 2}, materials={}),
    # Часть материалов уже закреплена за проектом.
    dict(stock={'wood': 10}, reserved={'wood': 10}, materials={'wood': 2}),
    # Проект уже укомплектован: assign=0, закупка нулевая.
    dict(stock={'wood': 4, 'stone': 2}, reserved={'wood': 4, 'stone': 2},
         materials={'wood': 4, 'stone': 2}),
    # float-склад и float-резерв: тип результата обязан сохраниться.
    dict(stock={'wood': 2.5}, reserved={'wood': 2.5}, materials={}),
    # Мощность не кратна minimum_capacity: смета дробная.
    dict(stock={'wood': 1}, reserved={'wood': 1}, materials={}, capacity=7),
]


@pytest.mark.parametrize('case_kwargs', RESERVE_CASES)
def test_native_matches_python_reference(case_kwargs):
    reference = run(material_boundary.reserve_materials_python, make_case(**case_kwargs))
    native = run(material_boundary.reserve_materials, make_case(**case_kwargs))
    assert_same(reference, native)


def test_construction_bill_matches_v3_exactly():
    """Смета ядра побитово равна ``V3.construction_bill`` и сохраняет типы."""
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
        expected = engine.construction_bill(capacity)
        packed = material_boundary.construction_bill(
            engine.cfg['construction'], engine.cfg['minimum_capacity'], capacity)
        assert list(packed) == list(expected)
        for good, quantity in expected.items():
            assert packed[good] == quantity, (capacity, good)
            assert type(packed[good]) is type(quantity), (capacity, good)


def test_release_takes_only_reserved_part_and_keeps_mode():
    """Снимается только закреплённая часть; свободный остаток остаётся в продаже."""
    offers = {'wood': [dict(id='o1', participant='acc', good='wood', quantity=10,
                            remaining=10, backing=10, status='active')]}
    case = make_case(stock={'wood': 10}, reserved={'wood': 10}, materials={},
                     capacity=10, offers=offers)
    # Смета на мощность 10 требует wood=4; свободного остатка 0, значит релиз 4.
    result = run(material_boundary.reserve_materials, case)
    offer = result['sells']['wood'][0]
    assert offer['quantity'] == 6
    assert offer['remaining'] == 6
    assert offer['backing'] == 6
    assert result['account']['stock_reserved']['wood'] == 6
    assert result['project']['materials']['wood'] == 4
    assert ('set_quantity', 'o1', 6) in result['store'].journal
    assert ('set_remaining', 'o1', 6) in result['store'].journal
    assert ('set_metadata', 'o1', 'backing', 6) in result['store'].journal
    assert ('dirty', 'wood') in result['owner'].journal


def test_release_cancels_emptied_offer_with_the_same_reason():
    offers = {'wood': [dict(id='o1', participant='acc', good='wood', quantity=4,
                            remaining=4, backing=4, status='active')]}
    case = make_case(stock={'wood': 4}, reserved={'wood': 4}, materials={},
                     capacity=10, offers=offers)
    result = run(material_boundary.reserve_materials, case)
    offer = result['sells']['wood'][0]
    assert offer['remaining'] == 0
    assert ('cancel', 'o1', material_boundary.MATERIAL_RELEASE_REASON) in result['owner'].journal
    assert material_boundary.MATERIAL_RELEASE_REASON == 'Материалы закреплены за собственной стройкой'


def test_foreign_and_closed_offers_are_skipped():
    """Чужой оффер и отменённый не трогаются, но закупка всё равно считается."""
    offers = {'wood': [
        dict(id='foreign', participant='other', good='wood', quantity=9,
             remaining=9, backing=9, status='active'),
        dict(id='dead', participant='acc', good='wood', quantity=9,
             remaining=9, backing=9, status='cancelled')]}
    case = make_case(stock={'wood': 9}, reserved={'wood': 9}, materials={},
                     capacity=10, offers=offers)
    reference = run(material_boundary.reserve_materials_python, case)
    native = run(material_boundary.reserve_materials, make_case(
        stock={'wood': 9}, reserved={'wood': 9}, materials={}, capacity=10, offers=offers))
    assert_same(reference, native)
    assert native['sells']['wood'][0]['remaining'] == 9
    assert native['sells']['wood'][1]['remaining'] == 9
    assert not [row for row in native['store'].journal
                if row[0] in ('set_remaining', 'set_quantity', 'set_metadata')]


def test_release_is_not_taken_twice_for_one_material():
    """Два оффера одного товара делят один release: второй получает остаток."""
    offers = {'wood': [
        dict(id='o1', participant='acc', good='wood', quantity=3,
             remaining=3, backing=3, status='active'),
        dict(id='o2', participant='acc', good='wood', quantity=3,
             remaining=3, backing=3, status='active')]}
    case = make_case(stock={'wood': 6}, reserved={'wood': 6}, materials={},
                     capacity=10, offers=offers)
    result = run(material_boundary.reserve_materials, case)
    # Смета wood=4: первый оффер отдаёт 3, второй — оставшийся 1.
    assert result['sells']['wood'][0]['remaining'] == 0
    assert result['sells']['wood'][1]['remaining'] == 2
    assert result['account']['stock_reserved']['wood'] == 2
    assert result['project']['materials']['wood'] == 4


def test_missing_material_places_order_even_when_zero():
    """Закупка зовётся на каждый материал, пустую отбрасывает сам ``order``."""
    case = make_case(stock={'wood': 4, 'stone': 2}, reserved={'wood': 4, 'stone': 2},
                     materials={'wood': 4, 'stone': 2})
    result = run(material_boundary.reserve_materials, case)
    orders = [row for row in result['owner'].journal if row[0] == 'order']
    assert [row[2] for row in orders] == list(result['bill'])
    assert all(type(row[3]) is int and row[3] == 0 for row in orders)
    assert all(row[6] == material_boundary.MATERIAL_PURPOSE for row in orders)


def test_stock_reserved_is_the_same_dict_not_a_second_state():
    """Ядро меняет живой ``stock_reserved`` счёта, а не свою копию."""
    case = make_case(stock={'wood': 10}, reserved={'wood': 10}, materials={},
                     capacity=10,
                     offers={'wood': [dict(id='o1', participant='acc', good='wood',
                                           quantity=10, remaining=10, backing=10,
                                           status='active')]})
    owner, account, project, sells, store = case
    reserved = account['stock_reserved']
    stock = account['stock']
    run(material_boundary.reserve_materials, case)
    assert account['stock_reserved'] is reserved
    assert account['stock'] is stock
    # Физический склад резерв не меняет: материалы остаются на месте.
    assert account['stock']['wood'] == 10


def test_construction_bill_zero_capacity():
    bill = material_boundary.construction_bill({'wood': 4, 'stone': 2}, 10, 0)
    assert bill == {'wood': 0.0, 'stone': 0.0}
    assert all(type(value) is float for value in bill.values())


def test_boundary_exposes_native_when_built():
    built = hasattr(material_boundary.load_material_kernel(), 'assignable_stock')
    assert material_boundary.NATIVE_MATERIAL_KERNEL is built
    if built:
        assert material_boundary.assignable_stock.__module__.endswith('material_kernel')
    else:
        assert material_boundary.assignable_stock is material_boundary.assignable_stock_python


def test_native_entry_points_are_the_compiled_ones():
    """Граница обязана привязать именно скомпилированные функции, а не эталон."""
    kernel = material_boundary.load_material_kernel()
    if not hasattr(kernel, 'assignable_stock'):
        pytest.skip('Cython-расширение material_kernel не собрано')
    assert material_boundary.assignable_stock is kernel.assignable_stock
    assert material_boundary.assign_from_free_stock is kernel.assign_from_free_stock
    assert material_boundary.construction_bill is kernel.construction_bill
    assert not hasattr(kernel, 'reserve_materials')


def test_load_returns_none_when_extension_absent(monkeypatch):
    """Отсутствие собранного ядра даёт None, а не падает на импорте economy."""
    import importlib

    name = 'modules.economy.native.material_kernel'
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name == name or module_name.endswith('material_kernel'):
            raise ImportError("cannot import name 'material_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert material_boundary.load_material_kernel() is None


def test_v3_call_site_delegates_to_delivery_boundary():
    """В ``project_material_requests`` остался один call site, без копии цикла.

    Доставка вынесена в ``delivery_boundary`` (она считает объём, стоимость и
    ``protected``), поэтому v3 зовёт её, а резерв материалов остаётся за
    ``material_boundary`` внутри этого вызова.
    """
    source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    start = source.index('def project_material_requests')
    end = source.index('def finish_projects')
    body = source[start:end]
    assert '_delivery.request_project_materials(self, a, sells, EPS, store, distance)' in body
    assert 'stock_reserved' not in body
    assert 'self.free_stock' not in body
    assert 'self.order(' not in body
    assert 'cancel_order' not in body
