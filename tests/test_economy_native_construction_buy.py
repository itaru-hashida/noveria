"""Focused-тесты construction buy orders: побитовое совпадение с прежним V3.

Проверяется вертикальный срез «закупка недостающего материала проекта»: цена
закупки из ``quoted_cost`` и создание buy order через существующий ``V3.order``.
Ядро работает с object-арифметикой намеренно: ``missing`` бывает и ``int``, и
``float``, а тип аргумента виден в заявке и дальше в отчёте. Поэтому
сравниваются не только значения, но и типы, и порядок вызовов хуков.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
гоняет обе реализации на одинаковом состоянии. Отдельно проверяется, что
недостающий объём считает существующий ``material_kernel``: второй копии
расчёта количества в срезе закупки нет.
"""
import builtins
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
from modules.economy import support  # noqa: E402
from modules.economy.native import construction_buy_boundary  # noqa: E402
from modules.economy.native import delivery_boundary  # noqa: E402
from modules.economy.native import material_boundary  # noqa: E402

EPS = support.EPS
_PY_MAX = builtins.max
PURPOSE = '\u0421\u0442\u0440\u043e\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u043c\u0430\u0442\u0435\u0440\u0438\u0430\u043b\u044b'


class FakeQuoter:
    """Хук котировки: журнал вызовов и та же тройка, что у ``V3.quoted_cost``."""

    def __init__(self, ceiling=None, exact=True):
        self.ceiling = ceiling
        self.exact = exact
        self.journal = []
        self.account = None

    def __call__(self, owner, account, good, missing, sells):
        self.journal.append(('quoted_cost', account['id'], good, missing))
        # Прежний ``quoted_cost`` возвращает (value, max(ceiling, EPS), exact).
        ceiling = self.ceiling if self.ceiling is not None else 2.5
        return missing*ceiling, _PY_MAX(ceiling, EPS), self.exact


class FakeOrderHook:
    """Хук заявки: тело ``V3.order`` buy-ветки, но с журналом вызовов.

    Повторяет движок: урезание количества по свободным деньгам, отбрасывание
    пустой заявки, ``reserved = quantity*price`` и рост ``CashStore``. Так видно
    не только аргументы вызова, но и результат, который прежний путь давал.
    """

    def __init__(self, accounts, free_cash=None, closed=()):
        self.accounts = accounts
        self.free_cash = free_cash
        self.closed = set(closed)
        self.journal = []
        self.orders = []
        self.demand_revision = {}

    def _free_cash(self, participant):
        if self.free_cash is not None:
            return self.free_cash
        account = self.accounts[participant]
        return _PY_MAX(0.0, account['balance']-account.get('reserved', 0)
                       -account.get('protected', 0))

    def __call__(self, owner, participant, good, quantity, side, price, purpose):
        self.journal.append(('order', participant, good, quantity, side, price, purpose))
        assert side == construction_buy_boundary.SIDE == 'buy'
        assert purpose == construction_buy_boundary.PURPOSE == PURPOSE
        if participant in self.closed:
            return None
        quantity = min(quantity, self._free_cash(participant)/price)
        if quantity <= EPS:
            return None
        account = self.accounts[participant]
        reserved = quantity*price
        account['reserved'] = account.get('reserved', 0)+reserved
        row = dict(id='%d' % len(self.orders), participant=participant, good=good,
                   quantity=quantity, remaining=quantity, price=price, side='buy',
                   purpose=purpose, status='active', reserved=reserved)
        self.orders.append(row)
        self.demand_revision[good] = self.demand_revision.get(good, 0)+1
        return row


class FakeOwner:
    """Минимальный носитель хуков: котировка, заявка и ``accounts``."""

    def __init__(self, quoter, order_hook):
        self._quoter = quoter
        self._order_hook = order_hook
        self.journal = []

    def quoted_cost(self, account, good, quantity, sells):
        return self._quoter(self, account, good, quantity, sells)

    def order(self, participant, good, quantity, side, price, purpose):
        return self._order_hook(self, participant, good, quantity, side, price, purpose)


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


class HookOwner(FakeOwner):
    """Носитель хуков резерва: ядро ``material_kernel`` зовёт их напрямую."""

    def __init__(self, quoter, order_hook, accounts, free=None, goods=None, cfg=None):
        super().__init__(quoter, order_hook)
        self.accounts = accounts
        self.free = free
        self.goods = goods or {'wood': dict(weight=2), 'stone': dict(weight=5)}
        self.cfg = cfg or dict(minimum_capacity=10, transport_rate=0.01,
                               construction={'wood': 4, 'stone': 2})

    def free_stock(self, account_id, good):
        self.journal.append(('free_stock', account_id, good))
        if self.free is not None:
            return self.free.get(good, 0)
        account = self.accounts[account_id]
        locked = account.get('project', {}).get('materials', {}).get(good, 0)
        return _PY_MAX(0, account['stock'].get(good, 0)
                       -account.get('stock_reserved', {}).get(good, 0)-locked)

    def mark_market_quotes_dirty(self, good=None):
        self.journal.append(('dirty', good))

    def cancel_order(self, offer, reason):
        self.journal.append(('cancel', offer['id'], reason))


def make_state(stock=None, reserved=None, materials=None, balance=1000.0,
               cash_reserved=0.0, capacity=10, account_id='acc', free_cash=None,
               ceiling=None, exact=True):
    """Собрать независимую пару (owner, account, project, sells, store)."""
    account = dict(id=account_id, stock=dict(stock or {}),
                   stock_reserved=dict(reserved or {}),
                   balance=balance, reserved=cash_reserved)
    project = dict(capacity=capacity, materials=dict(materials or {}),
                   working_cash=11)
    account['project'] = project
    accounts = {account_id: account}
    quoter = FakeQuoter(ceiling=ceiling, exact=exact)
    order_hook = FakeOrderHook(accounts, free_cash=free_cash)
    owner = HookOwner(quoter, order_hook, accounts)
    store = FakeStore()
    return dict(owner=owner, account=account, project=project, sells={},
                store=store, quoter=quoter, order_hook=order_hook)


def run_buy(implementation, case, missing, purpose=construction_buy_boundary.PURPOSE):
    """Прогнать одну реализацию закупки на общем состоянии."""
    owner, account = case['owner'], case['account']
    implementation(owner, account, 'wood', missing, case['sells'], purpose,
                   construction_buy_boundary.quoted_cost_hook,
                   construction_buy_boundary.order_hook)
    return case


def snapshot(case):
    """Наблюдаемое состояние: значения, типы и порядок вызовов хуков."""
    account = case['account']
    orders = case['order_hook'].orders
    return dict(
        quote_journal=case['quoter'].journal,
        order_journal=case['order_hook'].journal,
        orders=copy.deepcopy(orders),
        order_types=[{key: type(value) for key, value in row.items()} for row in orders],
        reserved=account.get('reserved'),
        reserved_type=type(account.get('reserved')),
        demand_revision=dict(case['order_hook'].demand_revision),
        owner_journal=case['owner'].journal,
        store_journal=case['store'].journal,
    )


def assert_same(reference, native):
    assert snapshot(native) == snapshot(reference)


BUY_CASES = [
    # Материал нужен целиком: закупка на всю смету.
    dict(stock={}, reserved={}, materials={}),
    # Материал частично доступен: закупка только на недостающую часть.
    dict(stock={'wood': 1.5}, reserved={'wood': 1.5}, materials={}),
    # Материал полностью доступен: недостача нулевая.
    dict(stock={'wood': 4}, reserved={'wood': 4}, materials={}),
    # Проект уже укомплектован: недостача нулевая и int.
    dict(stock={'wood': 4, 'stone': 2}, reserved={'wood': 4, 'stone': 2},
         materials={'wood': 4, 'stone': 2}),
    # Дробная смета: мощность не кратна minimum_capacity.
    dict(stock={'wood': 1}, reserved={'wood': 1}, materials={}, capacity=7),
    # Денег меньше, чем нужно: заявка урезается по свободному остатку.
    dict(stock={}, reserved={}, materials={}, balance=10.0, ceiling=4.0),
    # Совсем нет свободных денег: пустая заявка отбрасывается ``order``.
    dict(stock={}, reserved={}, materials={}, balance=0.0),
    # Котировка не покрывает весь объём: цена всё равно из потолка.
    dict(stock={}, reserved={}, materials={}, exact=False),
]


@pytest.mark.parametrize('case_kwargs', BUY_CASES)
def test_native_matches_python_reference(case_kwargs):
    """Срез закупки побитово равен эталону на одинаковом состоянии."""
    missing = case_kwargs.get('missing', 4.0)
    reference = run_buy(construction_buy_boundary.place_order_python,
                        make_state(**case_kwargs), missing)
    native = run_buy(construction_buy_boundary.place_order,
                     make_state(**case_kwargs), missing)
    assert_same(reference, native)


def test_missing_material_is_fully_ordered():
    """Материала нет: закупка на полный недостающий объём."""
    case = make_state(stock={}, reserved={}, materials={}, ceiling=3.0)
    run_buy(construction_buy_boundary.place_order, case, 4.0)
    orders = case['order_hook'].orders
    assert len(orders) == 1
    assert orders[0]['good'] == 'wood'
    assert orders[0]['quantity'] == 4.0
    assert orders[0]['purpose'] == PURPOSE


def test_partially_available_material_orders_only_the_shortfall():
    """Материал частично доступен: закупается ровно недостача, а не вся смета."""
    materials = {'wood': 1.0}
    case = make_state(stock={'wood': 2.5}, reserved={'wood': 1.0},
                      materials=materials, capacity=10)
    bill = dict(wood=4, stone=2)
    material_boundary.reserve_materials(
        case['owner'], case['account'], case['project'], bill, case['sells'], EPS,
        case['store'], material_boundary.free_stock_hook,
        material_boundary.detach_offer_hook,
        material_boundary.settle_release_hook,
        construction_buy_boundary.place_order_hook)
    # Свободный склад 2.5-1.0-1.0=0.5 закрепляется за проектом, поэтому
    # материалов становится 1.5, а недостача — 4-1.5=2.5 (не вся смета).
    assert case['project']['materials']['wood'] == 1.5
    assert case['order_hook'].orders[0]['quantity'] == 2.5
    assert case['order_hook'].orders[0]['quantity'] != bill['wood']


def test_fully_available_material_creates_no_order():
    """Материал полностью доступен: ``V3.order`` отбрасывает пустую заявку."""
    case = make_state(stock={'wood': 4}, reserved={'wood': 4}, materials={},
                      ceiling=3.0)
    run_buy(construction_buy_boundary.place_order, case, 0)
    # Хук заявки вызван (ядро резерва зовёт его на каждый материал), но сама
    # заявка не создана: её отбрасывает прежняя проверка ``quantity <= EPS``.
    assert case['order_hook'].journal == [
        ('order', 'acc', 'wood', 0, 'buy', 3.0, PURPOSE)]
    assert case['order_hook'].orders == []
    assert case['account']['reserved'] == 0.0


def test_zero_quantity_uses_int_zero_and_places_no_order():
    """Нулевой недостаток остаётся ``int``, как прежний ``max(0, ...)``."""
    case = make_state(ceiling=2.0)
    run_buy(construction_buy_boundary.place_order, case, 0)
    call = case['order_hook'].journal[0]
    assert call[3] == 0
    assert type(call[3]) is int
    assert case['order_hook'].orders == []


def test_negative_quantity_is_rejected_by_existing_order_hook():
    """Отрицательный объём — ошибка прежнего ``V3.order``, а не тихий ноль."""
    case = make_state(ceiling=2.0)
    with pytest.raises(ValueError):
        construction_buy_boundary.place_order(
            case['owner'], case['account'], 'wood', -1.0, case['sells'], PURPOSE,
            lambda owner, account, good, missing, sells: (0.0, -2.0, True),
            lambda owner, participant, good, quantity, side, price, purpose:
                _raise_invalid_order(quantity, price))


def _raise_invalid_order(quantity, price):
    """Повторить проверку ``V3.order``: неотрицательное количество и цена."""
    import math
    if not math.isfinite(quantity) or not math.isfinite(price) or price <= 0 or quantity < 0:
        raise ValueError('Некорректная заявка')
    return None


def test_ceiling_price_becomes_the_order_price():
    """Цена закупки — второй элемент тройки ``quoted_cost``, то есть потолок."""
    case = make_state(stock={}, reserved={}, materials={}, ceiling=7.5)
    run_buy(construction_buy_boundary.place_order, case, 2.0)
    assert case['quoter'].journal == [('quoted_cost', 'acc', 'wood', 2.0)]
    assert case['order_hook'].journal[0][5] == 7.5
    assert case['order_hook'].orders[0]['price'] == 7.5


def test_ceiling_is_floored_by_eps_in_reference_quote():
    """Нулевой потолок движок поднимает до EPS: ядро берёт готовое значение."""
    case = make_state(ceiling=0.0)
    owner = case['owner']

    def quote(owner_, account, good, missing, sells):
        # Прежний ``quoted_cost`` возвращает ``max(ceiling, EPS)``.
        return 0.0, _PY_MAX(0.0, EPS), True

    construction_buy_boundary.place_order(
        owner, case['account'], 'wood', 1.0, case['sells'], PURPOSE, quote,
        construction_buy_boundary.order_hook)
    assert case['order_hook'].orders[0]['price'] == EPS


def test_insufficient_cash_trims_the_order():
    """Недостаточно денег: заявка урезается по ``free_cash/price``."""
    case = make_state(stock={}, reserved={}, materials={}, balance=10.0,
                      cash_reserved=2.0, ceiling=4.0)
    run_buy(construction_buy_boundary.place_order, case, 4.0)
    # Свободно 10-2=8, цена 4, поэтому доступно 2 единицы из 4.
    assert case['order_hook'].orders[0]['quantity'] == 2.0
    assert case['order_hook'].orders[0]['reserved'] == 8.0
    # Резерв заявки добавляется к уже занятому резерву счёта: 2+8.
    assert case['account']['reserved'] == 10.0


def test_no_cash_means_no_order_but_the_hook_is_called():
    """Нет свободных денег: заявка отброшена, заявки в списке нет."""
    case = make_state(stock={}, reserved={}, materials={}, balance=4.0,
                      cash_reserved=4.0, ceiling=2.0)
    run_buy(construction_buy_boundary.place_order, case, 3.0)
    assert case['order_hook'].journal == [
        ('order', 'acc', 'wood', 3.0, 'buy', 2.0, PURPOSE)]
    assert case['order_hook'].orders == []
    assert case['account']['reserved'] == 4.0


def test_reserved_cash_and_order_reserved_agree():
    """``reserved`` счёта и ``order['reserved']`` считает прежний ``V3.order``."""
    case = make_state(stock={}, reserved={}, materials={}, ceiling=2.5)
    run_buy(construction_buy_boundary.place_order, case, 4.0)
    order = case['order_hook'].orders[0]
    assert order['reserved'] == order['quantity']*order['price'] == 10.0
    assert case['account']['reserved'] == order['reserved']
    assert case['order_hook'].demand_revision == {'wood': 1}


def test_cash_store_reserved_matches_the_live_account():
    """Резерв пишется в живой счёт, а не в копию: ``CashStore`` видит его."""
    case = make_state(stock={}, reserved={}, materials={}, ceiling=2.0)
    reserved_before = dict(case['account'])
    run_buy(construction_buy_boundary.place_order, case, 2.5)
    assert list(case['account']) == list(reserved_before)
    assert case['account']['reserved'] == 5.0


def test_several_materials_keep_their_bill_order():
    """Несколько материалов: порядок закупок — порядок сметы."""
    materials = {'wood': 4, 'stone': 2}
    case = make_state(stock={}, reserved={}, materials={}, capacity=10)
    case['owner'].cfg['construction'] = dict(materials)
    bill = material_boundary.construction_bill(
        case['owner'].cfg['construction'], case['owner'].cfg['minimum_capacity'],
        case['project']['capacity'])
    material_boundary.reserve_materials(
        case['owner'], case['account'], case['project'], bill, case['sells'], EPS,
        case['store'], material_boundary.free_stock_hook,
        material_boundary.detach_offer_hook,
        material_boundary.settle_release_hook,
        construction_buy_boundary.place_order_hook)
    goods = [row[2] for row in case['order_hook'].journal]
    assert goods == list(bill)
    assert goods == ['wood', 'stone']


def test_several_projects_keep_their_order_and_reserve_separately():
    """Несколько проектов: порядок инвесторов сохранён, резервы не смешаны."""
    first = make_state(stock={}, reserved={}, materials={}, account_id='acc-a')
    second = make_state(stock={}, reserved={}, materials={}, account_id='acc-b')
    # Общий носитель хуков: заявки обоих проектов идут в один журнал.
    owner = first['owner']
    owner.accounts.update(second['owner'].accounts)
    owner._order_hook.accounts = owner.accounts
    # Своя цена-потолок у каждого проекта: видно, что котировка адресная.
    ceilings = {'acc-a': 2.0, 'acc-b': 3.0}

    def quote(owner_, account, good, missing, sells):
        ceiling = ceilings[account['id']]
        owner_.journal.append(('quoted_cost', account['id'], good, missing))
        return missing*ceiling, _PY_MAX(ceiling, EPS), True

    owner._quoter = quote
    projects = [('acc-a', first, 2.0), ('acc-b', second, 1.0)]
    for account_id, case, missing in projects:
        material_boundary.reserve_materials(
            owner, owner.accounts[account_id], case['project'], dict(wood=missing),
            {}, EPS, case['store'], material_boundary.free_stock_hook,
            material_boundary.detach_offer_hook,
            material_boundary.settle_release_hook,
            construction_buy_boundary.place_order_hook)

    journal = owner._order_hook.journal
    assert [(row[1], row[3], row[5]) for row in journal] == [
        ('acc-a', 2.0, 2.0), ('acc-b', 1.0, 3.0)]
    # Резерв каждого счёта свой: суммы не переехали между проектами.
    assert owner.accounts['acc-a']['reserved'] == 4.0
    assert owner.accounts['acc-b']['reserved'] == 3.0
    # Склада нет, поэтому за проект ничего не закреплено, а закупка — полная.
    assert first['project']['materials'] == {'wood': 0}
    assert second['project']['materials'] == {'wood': 0}


def test_reserve_calls_the_native_hook_for_every_material():
    """Хук закупки вызывается на каждый материал сметы, включая нулевую недостачу."""
    case = make_state(stock={}, reserved={}, materials={}, capacity=10)
    bill = dict(wood=4, stone=2)
    calls = []

    def place(owner, account, good, missing, sells):
        calls.append((good, missing))

    material_boundary.reserve_materials(
        case['owner'], case['account'], case['project'], bill, case['sells'], EPS,
        case['store'], material_boundary.free_stock_hook,
        material_boundary.detach_offer_hook,
        material_boundary.settle_release_hook, place)
    assert calls == [('wood', 4), ('stone', 2)]


def test_missing_quantity_comes_from_material_kernel_not_from_buy_slice():
    """Недостачу считает нативный резерв: срез закупки её не пересчитывает."""
    case = make_state(stock={'wood': 2.5}, reserved={'wood': 1.0}, materials={},
                      capacity=10)
    bill = dict(wood=4, stone=2)
    seen = []

    def place(owner, account, good, missing, sells):
        seen.append((good, missing))

    material_boundary.reserve_materials(
        case['owner'], case['account'], case['project'], bill, case['sells'], EPS,
        case['store'], material_boundary.free_stock_hook,
        material_boundary.detach_offer_hook,
        material_boundary.settle_release_hook, place)
    # Свободный склад 2.5-1.0=1.5 закрепляется за проектом, недостача 4-1.5=2.5.
    assert seen[0] == ('wood', 2.5)
    assert type(seen[0][1]) is float


def test_materials_and_stock_reserved_are_not_touched_by_the_buy_slice():
    """Закупка не правит проект и склад: это делает резерв до неё."""
    materials = {'wood': 1.5}
    case = make_state(stock={'wood': 2.5}, reserved={'wood': 2.5},
                      materials=materials, ceiling=2.0)
    project_ref = case['project']['materials']
    stock_ref = case['account']['stock']
    reserved_ref = case['account']['stock_reserved']
    run_buy(construction_buy_boundary.place_order, case, 2.5)
    assert case['project']['materials'] is project_ref
    assert case['account']['stock'] is stock_ref
    assert case['account']['stock_reserved'] is reserved_ref
    assert case['project']['materials'] == {'wood': 1.5}
    assert case['account']['stock'] == {'wood': 2.5}


def test_buy_slice_marks_no_dirty_goods():
    """Прежний путь не помечал котировки: buy-ветка двигает ревизию спроса."""
    case = make_state(stock={}, reserved={}, materials={}, ceiling=2.0)
    run_buy(construction_buy_boundary.place_order, case, 1.0)
    assert case['owner'].journal == []
    assert not [row for row in case['owner'].journal if row[0] == 'dirty']
    assert case['order_hook'].demand_revision == {'wood': 1}


def test_quote_triple_is_unpacked_exactly_like_the_old_body():
    """Кортику распаковывает тройная распаковка: другая длина — та же ошибка."""
    case = make_state()
    with pytest.raises(ValueError):
        construction_buy_boundary.place_order(
            case['owner'], case['account'], 'wood', 1.0, case['sells'], PURPOSE,
            lambda owner, account, good, missing, sells: (0.0, 2.0),
            construction_buy_boundary.order_hook)
    assert case['order_hook'].orders == []


def test_order_side_and_purpose_are_the_legacy_literals():
    """Сторона и назначение — прежние литералы, общие с материал-границей."""
    assert construction_buy_boundary.SIDE == 'buy'
    assert construction_buy_boundary.PURPOSE == PURPOSE
    assert construction_buy_boundary.PURPOSE == material_boundary.MATERIAL_PURPOSE


def test_deepcopy_does_not_share_state():
    """Копия не делит состояние: закупка в копии не видна в оригинале."""
    case = make_state(stock={}, reserved={}, materials={}, ceiling=3.0)
    clone = copy.deepcopy(case)
    run_buy(construction_buy_boundary.place_order, clone, 4.0)
    assert len(clone['order_hook'].orders) == 1
    assert clone['account']['reserved'] == 12.0
    # Оригинал не тронут ни заявкой, ни резервом, ни журналами.
    assert case['order_hook'].orders == []
    assert case['account']['reserved'] == 0.0
    assert case['order_hook'].journal == []
    assert case['quoter'].journal == []


# --- привязка границы и проводка в прежний call site -----------------------

def test_boundary_exposes_native_when_built():
    built = construction_buy_boundary.load_construction_buy_kernel() is not None
    assert construction_buy_boundary.NATIVE_CONSTRUCTION_BUY_KERNEL is built
    if built:
        assert construction_buy_boundary.place_order.__module__.endswith(
            'construction_buy_kernel')
    else:
        assert construction_buy_boundary.place_order is \
            construction_buy_boundary.place_order_python


def test_native_entry_points_are_the_compiled_ones():
    """Граница обязана привязать именно скомпилированную функцию."""
    kernel = construction_buy_boundary.load_construction_buy_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение construction_buy_kernel не собрано')
    assert construction_buy_boundary.place_order is kernel.place_order
    assert construction_buy_boundary.place_order is not \
        construction_buy_boundary.place_order_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    """Отсутствие собранного ядра даёт None, а не падает на импорте economy."""
    import importlib

    name = 'modules.economy.native.construction_buy_kernel'
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name == name or module_name.endswith('construction_buy_kernel'):
            raise ImportError("cannot import name 'construction_buy_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert construction_buy_boundary.load_construction_buy_kernel() is None


def test_boundary_installs_hook_into_the_legacy_call_site():
    """Граница подставляет хук в прежний call site резерва при импорте."""
    assert material_boundary.place_order_hook is construction_buy_boundary.place_order_hook
    assert (construction_buy_boundary.PYTHON_PLACE_ORDER_HOOK
            is not construction_buy_boundary.place_order_hook)


def test_hook_can_be_restored_and_reinstalled():
    """Прежняя реализация возвращается явно и ставится обратно."""
    saved = material_boundary.place_order_hook
    try:
        construction_buy_boundary.restore_hook()
        assert material_boundary.place_order_hook is \
            construction_buy_boundary.PYTHON_PLACE_ORDER_HOOK
        # Эталонная ветка хука остаётся рабочей.
        case = make_state(ceiling=2.0)
        material_boundary.place_order_hook(case['owner'], case['account'], 'wood',
                                           1.5, case['sells'])
        assert case['order_hook'].journal[0][3] == 1.5
    finally:
        construction_buy_boundary.install_hook()
    assert material_boundary.place_order_hook is construction_buy_boundary.place_order_hook
    assert material_boundary.place_order_hook is saved


def test_python_fallback_matches_native_when_kernel_is_bypassed():
    """Без ядра участок не меняется: эталон даёт то же состояние."""
    case = make_state(stock={}, reserved={}, materials={}, ceiling=2.5)
    # Хук зовёт ``place_order`` из границы, поэтому подменяем только его.
    saved = construction_buy_boundary.place_order
    construction_buy_boundary.place_order = \
        construction_buy_boundary.place_order_python
    try:
        construction_buy_boundary.place_order_hook(
            case['owner'], case['account'], 'wood', 3.0, case['sells'])
        fallback = snapshot(case)
    finally:
        construction_buy_boundary.place_order = saved
    case = make_state(stock={}, reserved={}, materials={}, ceiling=2.5)
    construction_buy_boundary.place_order_hook(
        case['owner'], case['account'], 'wood', 3.0, case['sells'])
    assert snapshot(case) == fallback


def test_v3_call_site_references_the_construction_buy_boundary():
    """В v3 ровно один call site участка закупок, без копии тела закупки."""
    source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    start = source.index('def project_material_requests')
    end = source.index('def finish_projects')
    body = source[start:end]
    # Литерал назначения не остался в Python: он живёт в границе.
    assert PURPOSE not in body
    # Тело закупки не осталось в Python: ни котировки, ни заявки.
    assert 'quoted_cost' not in body
    assert 'self.order(' not in body
    # Прежний единственный call site участка остался один и тот же.
    assert body.count('request_project_materials') == 1
    assert 'construction_buy_kernel' in body
    assert source.count('construction_buy_boundary as _construction_buy') == 1


def test_v3_import_is_wired_and_hook_installed():
    """Импорт границы в v3 ставит нативный хук в прежний call site."""
    import modules.economy.v3 as v3
    assert v3._construction_buy is construction_buy_boundary
    assert material_boundary.place_order_hook is construction_buy_boundary.place_order_hook


# --- exact на реальном движке: seed 17/73129, 3 месяца --------------------

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def engine_digest(engine, decisions):
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'queue': engine.meta.get('investment_queue', []),
        'decisions': decisions,
        'demand_revision': dict(getattr(engine, '_demand_revision', {}) or {}),
    })


def run_engine(seed, months=3, implementation=None, native_mode='cython'):
    """Прогнать months месяцев и вернуть digest, RNG и счётчики закупок."""
    saved = construction_buy_boundary.place_order
    if implementation is not None:
        construction_buy_boundary.place_order = implementation
    counted = [0, 0]
    original = construction_buy_boundary.place_order

    def counting_hook(owner, account, good, missing, sells):
        counted[0] += 1
        if missing > EPS:
            counted[1] += 1
        return construction_buy_boundary.place_order(
            owner, account, good, missing, sells, construction_buy_boundary.PURPOSE,
            construction_buy_boundary.quoted_cost_hook,
            construction_buy_boundary.order_hook)

    construction_buy_boundary.place_order_hook = counting_hook
    material_boundary.place_order_hook = counting_hook
    decisions = []
    try:
        with workdir('test_construction_buy') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                for index in range(months):
                    service.advance_turn(
                        command_id='buy-%d-%d' % (seed, index), source='audit')
                    decisions.extend(
                        dict(row) for row in engine.report['investment_decisions'])
                orders = [o for o in engine.report['orders']
                          if o['side'] == 'buy' and o['purpose'] == PURPOSE]
                return dict(engine=engine,
                            digest=engine_digest(engine, decisions),
                            rng=digest(engine.rng.getstate()),
                            calls=counted[0], non_zero=counted[1],
                            buy_orders=len(orders),
                            buy_reserved=sum(o['reserved'] for o in orders),
                            reserved_total=sum(a.get('reserved', 0)
                                               for a in engine.accounts.values()))
            finally:
                db.close()
    finally:
        construction_buy_boundary.place_order = saved
        construction_buy_boundary.install_hook()


@pytest.fixture(scope='module')
def exact_17():
    native = run_engine(17)
    reference = run_engine(
        17, implementation=construction_buy_boundary.place_order_python)
    return native, reference


@pytest.fixture(scope='module')
def exact_73129():
    native = run_engine(73129)
    reference = run_engine(
        73129, implementation=construction_buy_boundary.place_order_python)
    return native, reference


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_three_months_matches_reference(fixture, request):
    """Три месяца на реальном движке побитово совпадают с эталоном."""
    native, reference = request.getfixturevalue(fixture)
    assert native['digest'] == reference['digest']
    assert native['rng'] == reference['rng']
    assert native['buy_orders'] == reference['buy_orders']
    assert native['buy_reserved'] == reference['buy_reserved']
    assert native['reserved_total'] == reference['reserved_total']


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_construction_buy_really_happened(fixture, request):
    """Срез действительно исполнялся: вызовы были и закупки создавались."""
    native, reference = request.getfixturevalue(fixture)
    assert native['calls'] > 0
    assert native['calls'] == reference['calls']
    assert native['non_zero'] > 0
    assert native['buy_orders'] > 0


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_rng_equal_and_queue_untouched(fixture, request):
    native, reference = request.getfixturevalue(fixture)
    engine = native['engine']
    assert digest(engine.rng.getstate()) == native['rng']
    assert engine.meta.get('investment_queue', []) == \
        reference['engine'].meta.get('investment_queue', [])


def test_exact_seeds_differ():
    """Разные seed дают разные траектории: сравнение не вырождено."""
    assert run_engine(17)['digest'] != run_engine(73129)['digest']
