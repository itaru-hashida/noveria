"""Focused-тесты release sell orders: побитовое совпадение с прежним V3.

Проверяется вертикальный срез ``project_material_requests``: снятие
закреплённой части собственного sell-оффера (``quantity``/``remaining``/
``backing``) и окончание release (dirty-котировки и отмена опустевшего
оффера). Обход заявок, проверки ``participant``/``status``, ``take``,
``stock_reserved`` и ``project['materials']`` по-прежнему живут в
``material_kernel``, поэтому изменение проверяется на двух уровнях:

  * ``release_boundary`` — ядро против эталона на одинаковом состоянии:
    значения, типы и порядок вызовов хуков;
  * ``material_boundary.reserve_materials`` — весь release-участок на
    ``FakeOwner``/``FakeStore``, где видно порядок dirty/cancel и ревизии.

Арифметика намеренно ``object``: остатки бывают и ``int``, и ``float``, и тип
виден дальше в отчёте. Поэтому сравниваются не только значения, но и типы.
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

from modules.economy import support  # noqa: E402
from modules.economy.native import material_boundary  # noqa: E402
from modules.economy.native import release_boundary  # noqa: E402

EPS = support.EPS
REASON = 'Материалы закреплены за собственной стройкой'


class FakeStore:
    """Граница заявок: журнал вызовов и те же значения в тех же dict-ах."""

    def __init__(self):
        self.journal = []

    def set_remaining(self, offer, value):
        self.journal.append(('set_remaining', offer['id'], value))
        offer['remaining'] = value


class FakeOwner:
    """Минимальный носитель хуков release: журнал и тела хуков движка.

    ``mark_market_quotes_dirty`` повторяет движок: общая версия, версия по
    товару и сброс кэшей котировок. ``cancel_order`` повторяет ветку sell:
    ``stock_reserved`` уменьшается на ``remaining``, статус становится
    ``cancelled``, причина пишется в заявку.
    """

    def __init__(self, accounts=None):
        self.goods = {'wood': dict(weight=2), 'stone': dict(weight=5)}
        self.cfg = dict(minimum_capacity=10, transport_rate=0.01,
                        construction={'wood': 4, 'stone': 2})
        self.accounts = accounts or {}
        self.quote_version = 0
        self.quote_versions = {}
        self.demand_revision = {}
        self.quoted_cost_cache = {'stale': True}
        self.reference_price_cache = {'stale': True}
        self.journal = []

    def mark_market_quotes_dirty(self, good=None):
        self.journal.append(('dirty', good))
        self.quote_version += 1
        if good is None:
            self.quote_versions = {}
        else:
            self.quote_versions[good] = self.quote_versions.get(good, 0)+1
        self.quoted_cost_cache = None
        self.reference_price_cache = None

    def cancel_order(self, order, reason='Отмена заявки'):
        if order['status'] in ('cancelled', 'expired'):
            self.journal.append(('cancel-skipped', order['id']))
            return
        filled = order['status'] == 'filled'
        account = self.accounts.get(order['participant'])
        if order['side'] == 'buy':
            self.demand_revision[order['good']] = \
                self.demand_revision.get(order['good'], 0)+1
        elif account is not None:
            account['stock_reserved'][order['good']] = max(
                0, account['stock_reserved'].get(order['good'], 0)-order['remaining'])
            self.mark_market_quotes_dirty(order['good'])
        self.journal.append(('cancel', order['id'], reason))
        if not filled:
            order['status'] = 'cancelled'
            order['reason'] = reason

    def free_stock(self, account_id, good):
        self.journal.append(('free_stock', account_id, good))
        account = self.accounts[account_id]
        locked = account.get('project', {}).get('materials', {}).get(good, 0)
        return max(0, account['stock'].get(good, 0)
                   -account.get('stock_reserved', {}).get(good, 0)-locked)

    def quoted_cost(self, account, good, quantity, sells):
        self.journal.append(('quoted_cost', account['id'], good, quantity))
        return 0.0, 1.0, True

    def order(self, participant, good, quantity, side, price, purpose):
        self.journal.append(('order', participant, good, quantity, side, price, purpose))


def make_offer(offer_id='o1', participant='acc', good='wood', quantity=10,
               remaining=None, backing=None, status='active', side='sell'):
    """Живой sell-оффер в том же наборе полей, что строит ``V3.order``."""
    remaining = quantity if remaining is None else remaining
    backing = quantity if backing is None else backing
    return dict(id=offer_id, participant=participant, good=good,
                quantity=quantity, remaining=remaining, backing=backing,
                side=side, status=status, reason='')


def make_case(stock=None, reserved=None, materials=None, capacity=10,
              offers=None, account_id='acc'):
    """Собрать независимую пару (owner, account, project, sells, store)."""
    owner = FakeOwner()
    account = dict(id=account_id, stock=dict(stock or {}),
                   stock_reserved=dict(reserved or {}),
                   balance=1000.0, reserved=0.0)
    project = dict(capacity=capacity, materials=dict(materials or {}))
    account['project'] = project
    owner.accounts[account_id] = account
    store = FakeStore()
    sells = {good: [dict(offer) for offer in rows]
             for good, rows in (offers or {}).items()}
    return dict(owner=owner, account=account, project=project, sells=sells,
                store=store)


def run_release(implementation, case, bill=None):
    """Прогнать весь release-участок резерва и вернуть наблюдаемое состояние."""
    owner, account, project = case['owner'], case['account'], case['project']
    bill = bill if bill is not None else material_boundary.construction_bill(
        owner.cfg['construction'], owner.cfg['minimum_capacity'], project['capacity'])
    implementation(owner, account, project, bill, case['sells'], EPS, case['store'],
                   material_boundary.free_stock_hook,
                   material_boundary.detach_offer_hook,
                   material_boundary.settle_release_hook,
                   material_boundary.place_order_hook)
    return case


def snapshot(case):
    """Наблюдаемое состояние: значения, типы и порядок вызовов хуков."""
    owner = case['owner']
    return dict(
        sells=copy.deepcopy(case['sells']),
        remaining_types={offer['id']: type(offer['remaining'])
                         for rows in case['sells'].values() for offer in rows},
        quantity_types={offer['id']: type(offer['quantity'])
                        for rows in case['sells'].values() for offer in rows},
        backing_types={offer['id']: type(offer['backing'])
                       for rows in case['sells'].values() for offer in rows},
        stock=copy.deepcopy(case['account']['stock']),
        stock_reserved=copy.deepcopy(case['account']['stock_reserved']),
        materials=copy.deepcopy(case['project']['materials']),
        owner_journal=owner.journal,
        store_journal=case['store'].journal,
        quote_version=owner.quote_version,
        quote_versions=dict(owner.quote_versions),
        demand_revision=dict(owner.demand_revision),
    )


def assert_same(reference, native):
    assert snapshot(native) == snapshot(reference)


# --- само ядро: ядро против эталона ---------------------------------------

DETACH_CASES = [
    # Обычный частичный release: остаётся 6 из 10.
    dict(quantity=10, take=4),
    # Полный release: остаток обнуляется.
    dict(quantity=4, take=4),
    # Снятие больше остатка: минуса не появляется, потому что take ограничен.
    dict(quantity=3, take=3),
    # float-значения: тип обязан сохраниться.
    dict(quantity=2.5, take=1.25),
    # int-снятие с float-оферта и наоборот: тип результата прежний.
    dict(quantity=10.0, take=3),
    # Нулевое снятие: арифметика не меняет ни одного поля.
    dict(quantity=7, take=0),
]


@pytest.mark.parametrize('case_kwargs', DETACH_CASES)
def test_detach_offer_matches_python_reference(case_kwargs):
    take = case_kwargs['take']

    def build():
        offer = make_offer(quantity=case_kwargs['quantity'],
                           remaining=case_kwargs['quantity'],
                           backing=case_kwargs['quantity'])
        return FakeOwner(), FakeStore(), offer

    ref_owner, ref_store, ref_offer = build()
    release_boundary.detach_offer_python(ref_owner, ref_store, ref_offer, take)
    nat_owner, nat_store, nat_offer = build()
    release_boundary.detach_offer(nat_owner, nat_store, nat_offer, take)
    assert nat_offer == ref_offer
    for field in ('quantity', 'remaining', 'backing'):
        assert type(nat_offer[field]) is type(ref_offer[field]), field
    assert nat_store.journal == ref_store.journal


def test_detach_offer_keeps_field_order():
    """Порядок полей прежний: quantity, remaining, backing."""
    offer = make_offer(quantity=10, backing=10)
    store = FakeStore()
    release_boundary.detach_offer(None, store, offer, 4)
    assert list(offer) == ['id', 'participant', 'good', 'quantity', 'remaining',
                           'backing', 'side', 'status', 'reason']
    assert (offer['quantity'], offer['remaining'], offer['backing']) == (6, 6, 6)
    assert store.journal == [('set_remaining', 'o1', 6)]


def test_detach_offer_writes_remaining_through_store():
    """``remaining`` идёт через границу заявок: колонка и dict не расходятся."""
    offer = make_offer(quantity=10, backing=10)
    store = FakeStore()
    release_boundary.detach_offer(None, store, offer, 4)
    assert store.journal == [('set_remaining', 'o1', 6)]
    assert offer['remaining'] == 6


def test_settle_release_marks_dirty_on_partial_release():
    """Частичный release всё равно двигает ревизию котировок, но не отменяет."""
    owner, offer = FakeOwner(), make_offer(quantity=6, backing=6)
    release_boundary.settle_release(owner, offer, 'wood', EPS)
    assert owner.journal == [('dirty', 'wood')]
    assert owner.quote_version == 1
    assert owner.quote_versions == {'wood': 1}
    assert offer['status'] == 'active'
    assert offer['remaining'] == 6


def test_settle_release_cancels_emptied_offer():
    """Опустевший оффер отменяется с прежней причиной, dirty идёт первым."""
    owner = FakeOwner(accounts={'acc': dict(id='acc', stock={},
                                            stock_reserved={'wood': 0})})
    offer = make_offer(quantity=0, backing=0)
    release_boundary.settle_release(owner, offer, 'wood', EPS)
    # Первый dirty — от самого release, второй — из ``cancel_order`` sell-ветки.
    assert [row[0] for row in owner.journal] == ['dirty', 'dirty', 'cancel']
    assert owner.journal[2] == ('cancel', 'o1', REASON)
    assert offer['status'] == 'cancelled'
    assert offer['reason'] == REASON


def test_settle_release_reason_is_the_legacy_literal():
    """Причина — прежний литерал v3, один для границы, ядра и material_boundary."""
    assert release_boundary.RELEASE_REASON == REASON
    assert material_boundary.MATERIAL_RELEASE_REASON == REASON
    kernel = release_boundary.load_release_kernel()
    if kernel is not None:
        assert kernel.RELEASE_REASON == REASON


# --- весь release-участок через material_boundary -------------------------

RELEASE_CASES = [
    # Обычный release: снимается только закреплённая часть.
    dict(stock={'wood': 10}, reserved={'wood': 10}, materials={},
         offers={'wood': [dict(id='o1', quantity=10)]}),
    # Ровно хватает склада: оффер опустевает и отменяется.
    dict(stock={'wood': 4}, reserved={'wood': 4}, materials={},
         offers={'wood': [dict(id='o1', quantity=4)]}),
    # Свободный остаток уже есть: release не нужен, поля не меняются.
    dict(stock={'wood': 10}, reserved={'wood': 2}, materials={},
         offers={'wood': [dict(id='o1', quantity=8)]}),
    # Уже отменённый order: пропускается, ревизия не двигается.
    dict(stock={'wood': 10}, reserved={'wood': 10}, materials={},
         offers={'wood': [dict(id='o1', quantity=10, status='cancelled')]}),
    # Order с remaining=0: take=0, оффер не трогается.
    dict(stock={'wood': 10}, reserved={'wood': 10}, materials={},
         offers={'wood': [dict(id='o1', quantity=10, remaining=0, backing=10)]}),
    # Истёкший order: тоже пропускается.
    dict(stock={'wood': 10}, reserved={'wood': 10}, materials={},
         offers={'wood': [dict(id='o1', quantity=10, status='expired')]}),
    # Два оффера одного товара: порядок обхода и делёж release.
    dict(stock={'wood': 6}, reserved={'wood': 6}, materials={},
         offers={'wood': [dict(id='o1', quantity=3), dict(id='o2', quantity=3)]}),
    # Чужой оффер идёт первым: он пропускается, свой получает весь release.
    dict(stock={'wood': 10}, reserved={'wood': 10}, materials={},
         offers={'wood': [dict(id='foreign', participant='other', quantity=9),
                          dict(id='o2', quantity=10)]}),
    # float-остатки: тип сохраняется.
    dict(stock={'wood': 2.5}, reserved={'wood': 2.5}, materials={}, capacity=7,
         offers={'wood': [dict(id='o1', quantity=2.5)]}),
]


def build_case(case_kwargs):
    """Собрать независимый case из параметризованного описания офферов."""
    rows = case_kwargs.get('offers', {})
    offers = {}
    for good, items in rows.items():
        built = []
        for item in items:
            item = dict(item)
            offer_id = item.pop('id')
            built.append(make_offer(offer_id, good=good, **item))
        offers[good] = built
    kwargs = {key: value for key, value in case_kwargs.items() if key != 'offers'}
    return make_case(offers=offers, **kwargs)


@pytest.mark.parametrize('case_kwargs', RELEASE_CASES)
def test_release_matches_python_reference(case_kwargs):
    reference = run_release(material_boundary.reserve_materials_python,
                            build_case(case_kwargs))
    native = run_release(material_boundary.reserve_materials,
                         build_case(case_kwargs))
    assert_same(reference, native)


def test_normal_release_of_sell_order():
    """Обычный sell order: quantity/remaining/backing уменьшаются на take."""
    case = make_case(stock={'wood': 10}, reserved={'wood': 10},
                     offers={'wood': [make_offer(quantity=10, backing=10)]})
    run_release(material_boundary.reserve_materials, case)
    offer = case['sells']['wood'][0]
    assert (offer['quantity'], offer['remaining'], offer['backing']) == (6, 6, 6)
    assert case['store'].journal == [('set_remaining', 'o1', 6)]
    assert [row[0] for row in case['owner'].journal if row[0] == 'dirty'] == ['dirty']


def test_partial_release_keeps_offer_alive():
    """Частичный release: оффер остаётся активным, причина не пишется."""
    case = make_case(stock={'wood': 10}, reserved={'wood': 10},
                     offers={'wood': [make_offer(quantity=10, backing=10)]})
    run_release(material_boundary.reserve_materials, case)
    offer = case['sells']['wood'][0]
    assert offer['remaining'] == 6
    assert offer['status'] == 'active'
    assert offer['reason'] == ''
    assert not [row for row in case['owner'].journal if row[0] == 'cancel']


def test_full_release_cancels_offer_with_reason():
    """Полный release: remaining=0, оффер отменён прежней причиной."""
    case = make_case(stock={'wood': 4}, reserved={'wood': 4},
                     offers={'wood': [make_offer(quantity=4, backing=4)]})
    run_release(material_boundary.reserve_materials, case)
    offer = case['sells']['wood'][0]
    assert offer['remaining'] == 0
    assert offer['status'] == 'cancelled'
    assert offer['reason'] == REASON
    assert ('cancel', 'o1', REASON) in case['owner'].journal


def test_already_cancelled_order_is_skipped():
    """Отменённый order не трогается: ни мутаций, ни dirty, ни cancel."""
    case = make_case(stock={'wood': 10}, reserved={'wood': 10},
                     offers={'wood': [make_offer(quantity=10, status='cancelled')]})
    run_release(material_boundary.reserve_materials, case)
    offer = case['sells']['wood'][0]
    assert offer['remaining'] == 10
    assert offer['quantity'] == 10
    assert case['store'].journal == []
    assert not [row for row in case['owner'].journal if row[0] == 'dirty']
    assert case['owner'].quote_version == 0


def test_expired_order_is_skipped():
    case = make_case(stock={'wood': 10}, reserved={'wood': 10},
                     offers={'wood': [make_offer(quantity=10, status='expired')]})
    run_release(material_boundary.reserve_materials, case)
    assert case['sells']['wood'][0]['remaining'] == 10
    assert case['store'].journal == []
    assert case['owner'].quote_version == 0


def test_order_with_zero_remaining_is_not_touched():
    """remaining=0: take=0, оффер не мутируется и не отменяется."""
    case = make_case(stock={'wood': 10}, reserved={'wood': 10},
                     offers={'wood': [make_offer(quantity=10, remaining=0,
                                                 backing=10)]})
    run_release(material_boundary.reserve_materials, case)
    offer = case['sells']['wood'][0]
    assert offer['quantity'] == 10
    assert offer['remaining'] == 0
    assert offer['backing'] == 10
    assert offer['status'] == 'active'
    assert case['store'].journal == []
    assert case['owner'].quote_version == 0


def test_reserved_backing_and_quantity_stay_consistent():
    """После release: backing==remaining==quantity, stock_reserved сходится."""
    case = make_case(stock={'wood': 10}, reserved={'wood': 10},
                     offers={'wood': [make_offer(quantity=10, backing=10)]})
    run_release(material_boundary.reserve_materials, case)
    offer = case['sells']['wood'][0]
    assert offer['backing'] == offer['remaining'] == offer['quantity']
    assert case['account']['stock_reserved']['wood'] == 6
    assert case['account']['stock']['wood'] == 10


def test_market_quote_revision_grows_once_per_release():
    """Ревизия котировок растёт на каждое снятие, а не на каждый оффер."""
    offers = {'wood': [make_offer('o1', quantity=10, backing=10)]}
    case = make_case(stock={'wood': 10}, reserved={'wood': 10}, offers=offers)
    run_release(material_boundary.reserve_materials, case)
    assert case['owner'].quote_versions == {'wood': 1}
    assert case['owner'].quote_version == 1


def test_market_quote_revision_grows_for_cancel_too():
    """Полный release: dirty от снятия плюс dirty из ``cancel_order``."""
    offers = {'wood': [make_offer('o1', quantity=4, backing=4)]}
    case = make_case(stock={'wood': 4}, reserved={'wood': 4}, offers=offers)
    run_release(material_boundary.reserve_materials, case)
    assert case['owner'].quote_versions == {'wood': 2}
    assert case['owner'].quote_version == 2


def test_several_orders_keep_their_order():
    """Порядок обхода нескольких orders сохраняется: первый берёт свой остаток."""
    offers = {'wood': [make_offer('o1', quantity=3, backing=3),
                       make_offer('o2', quantity=3, backing=3),
                       make_offer('o3', quantity=3, backing=3)]}
    case = make_case(stock={'wood': 9}, reserved={'wood': 9}, offers=offers)
    run_release(material_boundary.reserve_materials, case)
    rows = case['sells']['wood']
    # Смета wood=4: o1 отдаёт 3, o2 — оставшийся 1, o3 не трогается.
    assert [row['remaining'] for row in rows] == [0, 2, 3]
    assert [row['id'] for row in rows] == ['o1', 'o2', 'o3']
    assert case['store'].journal == [('set_remaining', 'o1', 0),
                                     ('set_remaining', 'o2', 2)]
    assert [row[1] for row in case['owner'].journal if row[0] == 'cancel'] == ['o1']


def test_foreign_and_dead_offers_do_not_block_own_release():
    """Чужой и отменённый оффер пропускаются, свой получает весь release."""
    offers = {'wood': [make_offer('foreign', participant='other', quantity=9),
                       make_offer('dead', quantity=9, status='cancelled'),
                       make_offer('own', quantity=10, backing=10)]}
    case = make_case(stock={'wood': 10}, reserved={'wood': 10}, offers=offers)
    run_release(material_boundary.reserve_materials, case)
    rows = {row['id']: row for row in case['sells']['wood']}
    assert rows['foreign']['remaining'] == 9
    assert rows['dead']['remaining'] == 9
    assert rows['own']['remaining'] == 6


def test_release_does_not_touch_other_goods_or_accounts():
    """Release одного товара не трогает соседний товар и чужой счёт.

    По смете нужны и wood=4, и stone=2. По stone свободного остатка хватает
    (склад 10, резерв 8), поэтому release там нулевой, и оффер обязан остаться
    нетронутым: снимается только wood.
    """
    offers = {'wood': [make_offer('w', quantity=10, backing=10)],
              'stone': [make_offer('s', good='stone', quantity=10, backing=10)]}
    case = make_case(stock={'wood': 10, 'stone': 10},
                     reserved={'wood': 10, 'stone': 8}, offers=offers)
    run_release(material_boundary.reserve_materials, case)
    assert case['sells']['wood'][0]['remaining'] == 6
    assert case['sells']['stone'][0]['remaining'] == 10
    assert case['sells']['stone'][0]['status'] == 'active'
    assert case['store'].journal == [('set_remaining', 'w', 6)]
    assert case['account']['stock_reserved'] == {'wood': 6, 'stone': 8}


def test_release_does_not_create_second_state():
    """Ядро меняет живые объекты движка, а не свои копии."""
    offers = {'wood': [make_offer('o1', quantity=10, backing=10)]}
    case = make_case(stock={'wood': 10}, reserved={'wood': 10}, offers=offers)
    rows = case['sells']['wood']
    reserved = case['account']['stock_reserved']
    run_release(material_boundary.reserve_materials, case)
    assert case['sells']['wood'] is rows
    assert case['sells']['wood'][0] is rows[0]
    assert case['account']['stock_reserved'] is reserved
    assert case['account']['stock']['wood'] == 10


# --- привязка границы -----------------------------------------------------

def test_boundary_exposes_native_when_built():
    built = release_boundary.load_release_kernel() is not None
    assert release_boundary.NATIVE_RELEASE_KERNEL is built
    if built:
        assert release_boundary.detach_offer.__module__.endswith('release_kernel')
    else:
        assert release_boundary.detach_offer is release_boundary.detach_offer_python


def test_native_entry_points_are_the_compiled_ones():
    """Граница обязана привязать именно скомпилированные функции."""
    kernel = release_boundary.load_release_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение release_kernel не собрано')
    assert release_boundary.detach_offer is kernel.detach_offer
    assert release_boundary.settle_release is kernel.settle_release
    assert release_boundary.detach_offer is not release_boundary.detach_offer_python


def test_material_boundary_binds_release_hooks_to_release_boundary():
    """Прежние хуки резерва остались, но их тела приходят из release-границы."""
    assert material_boundary.detach_offer_hook(
        FakeOwner(), FakeStore(), make_offer(quantity=10, backing=10), 4) is None
    owner = FakeOwner()
    store = FakeStore()
    offer = make_offer(quantity=10, backing=10)
    material_boundary.detach_offer_hook(owner, store, offer, 4)
    assert (offer['quantity'], offer['remaining'], offer['backing']) == (6, 6, 6)
    assert store.journal == [('set_remaining', 'o1', 6)]


def test_load_returns_none_when_extension_absent(monkeypatch):
    """Отсутствие собранного ядра даёт None, а не падает на импорте economy."""
    import importlib

    name = 'modules.economy.native.release_kernel'
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name == name or module_name.endswith('release_kernel'):
            raise ImportError("cannot import name 'release_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert release_boundary.load_release_kernel() is None


def test_release_falls_back_to_python_when_kernel_absent():
    """Без расширения граница берёт эталон, и release-участок не меняется."""
    saved = (release_boundary.detach_offer, release_boundary.settle_release)
    release_boundary.detach_offer = release_boundary.detach_offer_python
    release_boundary.settle_release = release_boundary.settle_release_python
    try:
        offers = {'wood': [make_offer('o1', quantity=4, backing=4)]}
        case = make_case(stock={'wood': 4}, reserved={'wood': 4}, offers=offers)
        run_release(material_boundary.reserve_materials_python, case)
        reference = snapshot(case)
        offers = {'wood': [make_offer('o1', quantity=4, backing=4)]}
        case = make_case(stock={'wood': 4}, reserved={'wood': 4}, offers=offers)
        run_release(material_boundary.reserve_materials, case)
        assert snapshot(case) == reference
    finally:
        release_boundary.detach_offer, release_boundary.settle_release = saved


# --- deepcopy -------------------------------------------------------------

def test_deepcopy_does_not_share_state():
    """Копия движка не делит оффер: release в копии не виден в оригинале."""
    offers = {'wood': [make_offer('o1', quantity=10, backing=10)]}
    case = make_case(stock={'wood': 10}, reserved={'wood': 10}, offers=offers)
    owner = case['owner']
    clone = copy.deepcopy(owner)
    clone_account = copy.deepcopy(case['account'])
    clone_project = clone_account['project']
    clone_sells = copy.deepcopy(case['sells'])
    clone_store = FakeStore()
    material_boundary.reserve_materials(
        clone, clone_account, clone_project, {'wood': 4}, clone_sells, EPS,
        clone_store, material_boundary.free_stock_hook,
        material_boundary.detach_offer_hook, material_boundary.settle_release_hook,
        material_boundary.place_order_hook)
    assert clone_sells['wood'][0]['remaining'] == 6
    # Оригинал не тронут ни оффером, ни резервом, ни котировками.
    assert case['sells']['wood'][0]['remaining'] == 10
    assert case['account']['stock_reserved']['wood'] == 10
    assert case['owner'].quote_version == 0
    assert case['owner'].journal == []


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
        'quote_versions': dict(getattr(engine, '_market_quote_versions', {}) or {}),
        'demand_revision': dict(getattr(engine, '_demand_revision', {}) or {}),
    })


def run_engine(seed, months=3, implementation=None):
    """Прогнать months месяцев и вернуть digest, RNG и release-наблюдения."""
    saved = (release_boundary.detach_offer, release_boundary.settle_release)
    reference_mode = implementation is not None
    if reference_mode:
        release_boundary.detach_offer = release_boundary.detach_offer_python
        release_boundary.settle_release = release_boundary.settle_release_python
    detaches = [0]
    original_detach = release_boundary.detach_offer

    def counted_detach(owner, store, offer, take):
        detaches[0] += 1
        return original_detach(owner, store, offer, take)

    release_boundary.detach_offer = counted_detach
    decisions = []
    try:
        with workdir('test_release') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                for index in range(months):
                    service.advance_turn(command_id='release-%d-%d' % (seed, index),
                                         source='audit')
                    decisions.extend(dict(row) for row in engine.report['investment_decisions'])
                return dict(engine=engine,
                            digest=engine_digest(engine, decisions),
                            rng=digest(engine.rng.getstate()),
                            detaches=detaches[0],
                            cancelled=sum(1 for o in engine.report['orders']
                                          if o['status'] == 'cancelled'),
                            sells=sum(1 for o in engine.report['orders']
                                      if o['side'] == 'sell'))
            finally:
                db.close()
    finally:
        release_boundary.detach_offer, release_boundary.settle_release = saved


@pytest.fixture(scope='module')
def exact_17():
    native = run_engine(17)
    reference = run_engine(17, implementation=release_boundary.detach_offer_python)
    return native, reference


@pytest.fixture(scope='module')
def exact_73129():
    native = run_engine(73129)
    reference = run_engine(73129, implementation=release_boundary.detach_offer_python)
    return native, reference


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_three_months_matches_reference(fixture, request):
    native, reference = request.getfixturevalue(fixture)
    assert native['digest'] == reference['digest']
    assert native['rng'] == reference['rng']
    assert native['cancelled'] == reference['cancelled']


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_release_really_happened(fixture, request):
    """Срез действительно исполнялся: снятия были, порядок и число совпали."""
    native, reference = request.getfixturevalue(fixture)
    assert native['detaches'] > 0
    assert native['detaches'] == reference['detaches']


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
