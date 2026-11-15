"""Focused-тесты stall-отмены проекта: побитовое совпадение с прежним V3.

Проверяется вертикальный срез ``project_material_requests``: прогресс стройки,
порог ``project_stall_months``, причина отмены и результат отмены. Ядро работает
с object-арифметикой намеренно: материалы бывают и int, и float, а встроенный
``sum`` в CPython 3.12 складывает float-слагаемые компенсированно. Поэтому
сравниваются не только значения, но и типы, и порядок вызовов хуков.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
среза гоняет обе реализации на одинаковом состоянии.

Отдельно проверяется, что срез не добавил side effects: stall-отмена не
вызывает ни ``cancel_order``, ни ``mark_market_quotes_dirty``, ни release
sell-заявок — они остаются за ``material_boundary`` и выполняются только для
проектов, которые отменены не были.
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

from modules.economy import support  # noqa: E402
from modules.economy.native import cancel_boundary  # noqa: E402
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


class FakeOwner:
    """Минимальный носитель хуков: ``meta``/``cfg``/``report`` и тела хуков.

    Тела хуков повторяют движок, но с журналом вызовов: так видно не только
    итоговое состояние, но и порядок операций, который обязан совпадать.
    """

    def __init__(self, month=0, stall_months=12, goods=None, cfg=None):
        self.meta = dict(month=month)
        self.goods = goods or {'wood': dict(weight=2), 'stone': dict(weight=5)}
        self.cfg = cfg or dict(minimum_capacity=10, transport_rate=0.01,
                               construction={'wood': 4, 'stone': 2},
                               project_stall_months=stall_months)
        self.cfg['project_stall_months'] = stall_months
        self.report = dict(investment_decisions=[])
        self.accounts = {}
        self.journal = []

    def free_stock(self, account_id, good):
        self.journal.append(('free_stock', account_id, good))
        account = self.accounts[account_id]
        locked = account.get('project', {}).get('materials', {}).get(good, 0)
        return _PY_MAX(0, account['stock'].get(good, 0)
                       -account.get('stock_reserved', {}).get(good, 0)-locked)

    def quoted_cost(self, account, good, quantity, sells):
        self.journal.append(('quoted_cost', account['id'], good, quantity))
        return 0.0, 1.0, True

    def order(self, participant, good, quantity, side, price, purpose):
        self.journal.append(('order', participant, good, quantity, side, price, purpose))

    def mark_market_quotes_dirty(self, good=None):
        self.journal.append(('dirty', good))

    def cancel_order(self, offer, reason):
        self.journal.append(('cancel', offer['id'], reason))


def make_case(materials=None, capacity=10, month=0, created=0, last_progress=None,
              progress_quantity=None, stall_months=12, stock=None, reserved=None,
              offers=None, operating=5, payroll=7, account_id='acc'):
    """Собрать независимую пару (owner, account, sells, store)."""
    owner = FakeOwner(month=month, stall_months=stall_months)
    account = dict(id=account_id, stock=dict(stock or {}),
                   stock_reserved=dict(reserved or {}),
                   balance=1000.0, reserved=0.0, protected=3.0,
                   operating_protected=operating, payroll_protected=payroll)
    project = dict(capacity=capacity, materials=dict(materials if materials is not None else {}),
                   working_cash=11, created=created, good='wood',
                   location=dict(tile_id='t'))
    if last_progress is not None:
        project['last_progress'] = last_progress
    if progress_quantity is not None:
        project['progress_quantity'] = progress_quantity
    account['project'] = project
    owner.accounts[account_id] = account
    store = FakeStore()
    sells = {good: [dict(offer) for offer in rows]
             for good, rows in (offers or {}).items()}
    return dict(owner=owner, account=account, project=project, sells=sells,
                store=store, distance=4)


def run_slice(implementation, case, with_delivery=False):
    """Прогнать срез отмены; при ``with_delivery`` — и прежний путь доставки."""
    owner, account = case['owner'], case['account']
    cancelled = implementation(owner, account, EPS)
    if not cancelled and with_delivery:
        delivery_boundary.request_project_materials(
            owner, account, case['sells'], EPS, case['store'], case['distance'])
    return dict(case, cancelled=cancelled)


def snapshot(case):
    """Наблюдаемое состояние: значения, типы и порядок вызовов хуков."""
    account = case['account']
    project = account.get('project')
    return dict(
        cancelled=case['cancelled'],
        has_project='project' in account,
        project=dict(project) if project else None,
        protected=account.get('protected'),
        protected_type=type(account.get('protected')),
        stock=dict(account['stock']),
        stock_reserved=dict(account['stock_reserved']),
        materials=(dict(project['materials']) if project else None),
        materials_types=({g: type(v) for g, v in project['materials'].items()}
                         if project else None),
        progress_quantity=(project.get('progress_quantity') if project else None),
        progress_type=(type(project.get('progress_quantity')) if project else None),
        sells=case['sells'],
        decisions=case['owner'].report['investment_decisions'],
        owner_journal=case['owner'].journal,
        store_journal=case['store'].journal,
    )


def assert_same(reference, native):
    """Сравнить значения, типы и порядок вызовов хуков."""
    assert native == reference


STALL_CASES = [
    # Порог не достигнут: проект остаётся.
    dict(month=5, created=0, last_progress=0, stall_months=12,
         progress_quantity=4, materials={'wood': 4}),
    # Ровно порог: проект отменяется в этот же месяц.
    dict(month=12, created=0, last_progress=0, stall_months=12,
         progress_quantity=4, materials={'wood': 4}),
    # Порог перешёл: отмена.
    dict(month=13, created=0, last_progress=0, stall_months=12,
         progress_quantity=4, materials={'wood': 4}),
    # За месяц до порога: ещё не отменяется.
    dict(month=11, created=0, last_progress=0, stall_months=12,
         progress_quantity=4, materials={'wood': 4}),
    # last_progress не выставлен: setdefault берёт created.
    dict(month=12, created=0, stall_months=12, materials={}),
    # Прогресс вырос: таймер простоя сбрасывается текущим месяцем.
    dict(month=12, created=0, last_progress=0, stall_months=12,
         progress_quantity=2, materials={'wood': 3}),
    # Прогресс вырос на двух материалах сразу: тоже сброс.
    dict(month=12, created=0, last_progress=0, stall_months=12,
         progress_quantity=1, materials={'wood': 2, 'stone': 2}),
    # int-материалы без прироста: прогресс остаётся int, проект отменяется.
    dict(month=2, stall_months=1, progress_quantity=6, materials={'wood': 4, 'stone': 2}),
    # float-материалы: прогресс остаётся float.
    dict(month=2, stall_months=1, progress_quantity=3.0,
         materials={'wood': 2.5, 'stone': 0.5}),
    # Пустые материалы: прогресс — целый ноль, порог ещё не достигнут.
    dict(month=0, stall_months=1, materials={}),
]


@pytest.mark.parametrize('case_kwargs', STALL_CASES)
def test_native_matches_python_reference(case_kwargs):
    reference = run_slice(cancel_boundary.cancel_stalled_project_python,
                          make_case(**case_kwargs))
    native = run_slice(cancel_boundary.cancel_stalled_project,
                       make_case(**case_kwargs))
    assert_same(snapshot(reference), snapshot(native))


def test_not_cancelled_before_threshold():
    """До порога проект остаётся, но progress-state уже обновлён."""
    case = make_case(month=11, created=0, last_progress=0, stall_months=12,
                     progress_quantity=4, materials={'wood': 4})
    result = run_slice(cancel_boundary.cancel_stalled_project, case)
    assert result['cancelled'] is False
    assert case['account']['project'] is case['project']
    assert case['project']['progress_quantity'] == 4
    assert case['project']['last_progress'] == 0
    assert case['owner'].report['investment_decisions'] == []


def test_cancelled_at_threshold():
    """Порог достигнут: проект снят, protected восстановлен, решение записано."""
    case = make_case(month=12, created=0, last_progress=0, stall_months=12,
                     progress_quantity=4, materials={'wood': 4}, operating=5, payroll=7)
    result = run_slice(cancel_boundary.cancel_stalled_project, case)
    assert result['cancelled'] is True
    assert 'project' not in case['account']
    assert case['account']['protected'] == 5+7
    assert case['owner'].report['investment_decisions'] == [
        dict(kind='investment_decision', investor='acc', outcome='cancelled',
             reason='Стройка не продвигается')]


def test_reason_matches_legacy_literal():
    """Причина — прежний литерал v3, и он же приходит из ядра."""
    assert cancel_boundary.STALL_REASON == 'Стройка не продвигается'
    kernel = cancel_boundary.load_cancel_kernel()
    if kernel is not None:
        assert kernel.STALL_REASON == cancel_boundary.STALL_REASON
    case = make_case(month=12, created=0, last_progress=0, stall_months=12,
                     progress_quantity=4, materials={'wood': 4})
    run_slice(cancel_boundary.cancel_stalled_project, case)
    decision = case['owner'].report['investment_decisions'][0]
    assert decision['reason'] == cancel_boundary.STALL_REASON
    # Порядок ключей решения прежний: kind, investor, outcome, reason.
    assert list(decision) == ['kind', 'investor', 'outcome', 'reason']


def test_cancellation_result_is_prepared_by_core():
    """Результат готовит ядро: те же поля и порядок, без второго состояния."""
    result = cancel_boundary.cancellation_result(cancel_boundary.STALL_REASON)
    assert result == dict(outcome='cancelled', reason='Стройка не продвигается')
    assert list(result) == ['outcome', 'reason']
    assert result == cancel_boundary.cancellation_result_python(
        cancel_boundary.STALL_REASON)


def test_release_hook_called_once():
    """Release sell-оффера — по-прежнему ровно один раз, и не в срезе отмены."""
    offers = {'wood': [dict(id='o1', participant='acc', good='wood', quantity=10,
                            remaining=10, backing=10, status='active')]}
    case = make_case(month=1, created=0, last_progress=1, stall_months=12,
                     materials={}, stock={'wood': 10}, reserved={'wood': 10},
                     offers=offers)
    reference = run_slice(cancel_boundary.cancel_stalled_project_python,
                          make_case(month=1, created=0, last_progress=1, stall_months=12,
                                    materials={}, stock={'wood': 10},
                                    reserved={'wood': 10}, offers=offers),
                          with_delivery=True)
    native = run_slice(cancel_boundary.cancel_stalled_project, case, with_delivery=True)
    assert_same(snapshot(reference), snapshot(native))
    kinds = [row[0] for row in case['owner'].journal]
    # Release идёт через границу заявок: одна мутация remaining на весь срез.
    assert len(case['store'].journal) == 1
    assert case['store'].journal[0][0] == 'set_remaining'
    assert kinds.count('dirty') == 1
    assert case['sells']['wood'][0]['remaining'] == 6


def test_stall_slice_calls_no_order_or_dirty_hooks():
    """Stall-отмена не вызывает cancel/dirty: прежний путь их не трогал."""
    case = make_case(month=12, created=0, last_progress=0, stall_months=12,
                     progress_quantity=4, materials={'wood': 4})
    run_slice(cancel_boundary.cancel_stalled_project, case)
    assert case['owner'].journal == []
    assert case['store'].journal == []
    assert case['sells'] == {}


def test_sell_order_cancelled_through_existing_boundary():
    """Опустевший sell-оффер отменяет прежняя граница резерва, а не ядро."""
    offers = {'wood': [dict(id='o1', participant='acc', good='wood', quantity=4,
                            remaining=4, backing=4, status='active')]}
    case = make_case(month=1, created=0, last_progress=1, stall_months=12,
                     materials={}, stock={'wood': 4}, reserved={'wood': 4},
                     offers=offers)
    result = run_slice(cancel_boundary.cancel_stalled_project, case, with_delivery=True)
    assert result['cancelled'] is False
    assert case['sells']['wood'][0]['remaining'] == 0
    assert ('cancel', 'o1', material_boundary.MATERIAL_RELEASE_REASON) in case['owner'].journal
    assert case['account']['stock_reserved']['wood'] == 0


def test_dirty_goods_and_revisions_match_reference():
    """Порядок dirty goods и число пометок совпадают с эталоном."""
    offers = {'wood': [dict(id='o1', participant='acc', good='wood', quantity=10,
                            remaining=10, backing=10, status='active')]}
    kwargs = dict(month=1, created=0, last_progress=1, stall_months=12,
                  materials={}, stock={'wood': 10}, reserved={'wood': 10},
                  offers=offers)
    reference = run_slice(cancel_boundary.cancel_stalled_project_python,
                          make_case(**kwargs), with_delivery=True)
    native = run_slice(cancel_boundary.cancel_stalled_project,
                       make_case(**kwargs), with_delivery=True)
    ref_dirty = [row for row in reference['owner'].journal if row[0] == 'dirty']
    nat_dirty = [row for row in native['owner'].journal if row[0] == 'dirty']
    assert nat_dirty == ref_dirty == [('dirty', 'wood')]
    assert (native['owner'].report == reference['owner'].report)
    # Срез отмены сам по себе ревизии не двигает.
    stalled = make_case(month=12, created=0, last_progress=0, stall_months=12,
                        progress_quantity=4, materials={'wood': 4})
    run_slice(cancel_boundary.cancel_stalled_project, stalled)
    assert not [row for row in stalled['owner'].journal if row[0] == 'dirty']


def test_queue_and_project_status_match_reference():
    """Очередь инвестиций не затрагивается, поля проекта совпадают."""
    kwargs = dict(month=5, created=0, last_progress=0, stall_months=12,
                  materials={'wood': 4, 'stone': 2}, progress_quantity=1)
    reference = run_slice(cancel_boundary.cancel_stalled_project_python,
                          make_case(**kwargs))
    native = run_slice(cancel_boundary.cancel_stalled_project, make_case(**kwargs))
    assert native['project'] == reference['project']
    assert list(native['project']) == list(reference['project'])
    for key in ('last_progress', 'progress_quantity'):
        assert type(native['project'][key]) is type(reference['project'][key]), key
    # Queue живёт в meta и срезом не читается и не пишется.
    assert native['owner'].meta == reference['owner'].meta == dict(month=5)
    assert 'investment_queue' not in native['owner'].meta


def test_int_float_semantics():
    """Тип прогресса сохраняется: int остаётся int, float — float."""
    integer = make_case(month=0, stall_months=12, materials={'wood': 4, 'stone': 2})
    run_slice(cancel_boundary.cancel_stalled_project, integer)
    assert integer['project']['progress_quantity'] == 6
    assert type(integer['project']['progress_quantity']) is int

    floating = make_case(month=0, stall_months=12,
                         materials={'wood': 2.5, 'stone': 0.5})
    run_slice(cancel_boundary.cancel_stalled_project, floating)
    assert floating['project']['progress_quantity'] == 3.0
    assert type(floating['project']['progress_quantity']) is float


def test_progress_uses_compensated_builtin_sum():
    """Прогресс считается тем же ``sum`` 3.12, а не наивным накоплением."""
    materials = {'a': 1e16, 'b': 1.0, 'c': -1e16}
    assert sum(materials.values()) == 1.0
    case = make_case(month=0, stall_months=12, materials=materials)
    run_slice(cancel_boundary.cancel_stalled_project, case)
    assert case['project']['progress_quantity'] == 1.0
    assert (case['project']['progress_quantity']
            == cancel_boundary.project_progress(case['project']))
    assert cancel_boundary.project_progress({'materials': materials}) == 1.0


def test_deepcopy_does_not_share_state():
    """Копия движка не делит project: отмена в копии не видна в оригинале."""
    case = make_case(month=12, created=0, last_progress=0, stall_months=12,
                     progress_quantity=8, materials={'wood': 8})
    owner = case['owner']
    account = case['account']
    owner.accounts['acc'] = account
    clone = copy.deepcopy(owner)
    assert clone.accounts['acc']['project'] is not account['project']
    assert clone.accounts['acc']['project']['materials'] is not account['project']['materials']
    cancelled = cancel_boundary.cancel_stalled_project(clone, clone.accounts['acc'], EPS)
    assert cancelled is True
    assert 'project' not in clone.accounts['acc']
    # Оригинал не тронут ни решением, ни protected, ни материалами.
    assert account['project'] is case['project']
    assert account['project']['materials'] == {'wood': 8}
    assert account['protected'] == 3.0
    assert owner.report['investment_decisions'] == []


def test_boundary_exposes_native_when_built():
    built = cancel_boundary.load_cancel_kernel() is not None
    assert cancel_boundary.NATIVE_CANCEL_KERNEL is built
    if built:
        assert cancel_boundary.check_stall.__module__.endswith('cancel_kernel')
    else:
        assert cancel_boundary.check_stall is cancel_boundary.check_stall_python


def test_native_entry_points_are_the_compiled_ones():
    """Граница обязана привязать именно скомпилированные функции."""
    kernel = cancel_boundary.load_cancel_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение cancel_kernel не собрано')
    assert cancel_boundary.check_stall is kernel.check_stall
    assert cancel_boundary.cancellation_result is kernel.cancellation_result
    assert cancel_boundary.project_progress is kernel.project_progress
    assert cancel_boundary.check_stall is not cancel_boundary.check_stall_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    """Отсутствие собранного ядра даёт None, а не падает на импорте economy."""
    import importlib

    name = 'modules.economy.native.cancel_kernel'
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name == name or module_name.endswith('cancel_kernel'):
            raise ImportError("cannot import name 'cancel_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert cancel_boundary.load_cancel_kernel() is None


def test_v3_call_site_delegates_to_cancel_boundary():
    """В ``project_material_requests`` один call site среза отмены."""
    source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    start = source.index('def project_material_requests')
    end = source.index('def finish_projects')
    body = source[start:end]
    assert '_cancel.cancel_stalled_project(self, a, EPS)' in body
    assert body.count('cancel_stalled_project') == 1
    # Прогресс, порог и причина не остались в Python-теле.
    assert 'progress_quantity' not in body
    assert 'last_progress' not in body
    assert 'project_stall_months' not in body
    assert 'Стройка не продвигается' not in body
    # Прежние side effects в срезе не появились.
    assert 'cancel_order' not in body
    assert 'mark_market_quotes_dirty' not in body
    assert 'stock_reserved' not in body
    # Отмена по марже/спросу вынесена в свой call site и свою границу.
    assert '_cancel.cancel_margin_demand_project(self, a, sells, due, EPS)' in body
    assert 'Отрицательная маржа или устойчивое отсутствие спроса' not in body
    assert cancel_boundary.MARGIN_DEMAND_REASON == 'Отрицательная маржа или устойчивое отсутствие спроса'


# ---------------------------------------------------------------------------
# Exact-сравнение на реальном движке: seed 17/73129, 3 месяца
# ---------------------------------------------------------------------------

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


def run_engine(seed, months=3, stall_months=None, implementation=None):
    """Прогнать months месяцев, собрав digest, RNG и решения по отменам."""
    original = cancel_boundary.cancel_stalled_project
    if implementation is not None:
        cancel_boundary.cancel_stalled_project = implementation
    decisions = []
    try:
        with workdir('test_cancel') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                if stall_months is not None:
                    engine.cfg['project_stall_months'] = stall_months
                for index in range(months):
                    service.advance_turn(command_id='cancel-%d-%d' % (seed, index),
                                         source='audit')
                    decisions.extend(
                        dict(row) for row in engine.report['investment_decisions'])
                return dict(engine=engine, digest=engine_digest(engine, decisions),
                            rng=digest(engine.rng.getstate()), decisions=decisions)
            finally:
                db.close()
    finally:
        cancel_boundary.cancel_stalled_project = original


@pytest.fixture(scope='module')
def exact_17():
    native = run_engine(17)
    reference = run_engine(17, implementation=cancel_boundary.cancel_stalled_project_python)
    return native, reference


@pytest.fixture(scope='module')
def exact_73129():
    native = run_engine(73129)
    reference = run_engine(73129, implementation=cancel_boundary.cancel_stalled_project_python)
    return native, reference


@pytest.fixture(scope='module')
def forced_stall_17():
    native = run_engine(17, stall_months=1)
    reference = run_engine(17, stall_months=1,
                           implementation=cancel_boundary.cancel_stalled_project_python)
    return native, reference


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_three_months_matches_reference(fixture, request):
    native, reference = request.getfixturevalue(fixture)
    assert native['digest'] == reference['digest']
    assert native['rng'] == reference['rng']


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_rng_equal_and_queue_untouched(fixture, request):
    native, reference = request.getfixturevalue(fixture)
    engine = native['engine']
    assert digest(engine.rng.getstate()) == native['rng']
    assert engine.meta.get('investment_queue', []) == \
        reference['engine'].meta.get('investment_queue', [])


def test_forced_stall_matches_reference(forced_stall_17):
    """При пороге в месяц отмены реально происходят и совпадают с эталоном."""
    native, reference = forced_stall_17
    stopped = [row for row in native['decisions']
               if row.get('reason') == cancel_boundary.STALL_REASON]
    assert stopped, 'stall-отмена не сработала ни разу'
    assert len(stopped) == len([row for row in reference['decisions']
                                if row.get('reason') == cancel_boundary.STALL_REASON])
    assert native['digest'] == reference['digest']
    assert native['rng'] == reference['rng']
