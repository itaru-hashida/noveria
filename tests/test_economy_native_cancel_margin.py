"""Focused-тесты отмены проекта по марже/спросу: побитовое совпадение с V3.

Проверяется второй срез ``project_material_requests``: расчёт порога маржи,
расчёт порога отсутствия спроса, условие отмены и её причина. Stall-срез живёт
рядом и здесь не переписывается.

Ядро работает с object-арифметикой намеренно: маржа складывается из ``int`` и
``float`` слагаемых, а порядок операций влияет на последний бит, откуда
значение уехало бы в условие отмены. Поэтому сравниваются не только значения,
но и типы, и порядок вызовов хуков: цена, себестоимость, рыночный сигнал и
``reachable_demand`` остаются методами движка и обязаны вызываться ровно там
же и столько же раз.

Эталонная Python-реализация живёт в самой границе, поэтому каждая проверка
среза гоняет обе реализации на одинаковом состоянии.

Отдельно проверяется, что срез не добавил side effects: отмена по марже/спросу
не вызывает ни ``cancel_order``, ни ``mark_market_quotes_dirty``, ни release
sell-заявок — они остаются за ``material_boundary`` и выполняются только для
проектов, которые отменены не были.
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
from modules.economy.native import cancel_boundary  # noqa: E402

EPS = support.EPS


class FakeOwner:
    """Минимальный носитель хуков цены/себестоимости/сигнала/спроса.

    Тела хуков возвращают заданные числа и пишут журнал: так видно не только
    результат отмены, но и порядок обращений, который обязан совпадать.
    """

    def __init__(self, month=0, net=10.0, cost=4.0, labor=0.5, wage=0.1,
                 overhead=0.02, history=None, volume=0, unmet=0, demand=0.0,
                 goods=None):
        self.meta = dict(month=month)
        if history is not None:
            self.meta['market_history'] = history
        self.cfg = dict(wage=wage, enterprise_overhead_rate=overhead)
        self.goods = goods or {'wood': dict(labor=labor)}
        self.report = dict(investment_decisions=[])
        self.accounts = {}
        self.journal = []
        self.net = net
        self.cost = cost
        self.volume = volume
        self.unmet = unmet
        self.demand = demand

    def net_price(self, e):
        self.journal.append(('net_price', e['id'], e['good'], e['price']))
        return self.net

    def unit_cost(self, e, sells):
        self.journal.append(('unit_cost', e['id'], e['good']))
        return self.cost

    def market_signal(self, history, state, province, good):
        self.journal.append(('market_signal', state, province, good, len(history)))
        return dict(volume=self.volume, unmet=self.unmet)

    def reachable_demand(self, e, sells):
        self.journal.append(('reachable_demand', e['id'], e['good']))
        return self.demand

    def cancel_order(self, offer, reason):
        self.journal.append(('cancel', offer['id'], reason))

    def mark_market_quotes_dirty(self, good=None):
        self.journal.append(('dirty', good))


def make_history(months=3):
    """История рынка нужной длины: её читает прежнее условие ``absent``."""
    return [dict(markets=[]) for _ in range(months)]


def make_case(month=3, created=0, due=True, price=10.0, operating=5, payroll=7,
              account_id='acc', good='wood', state_id='st', province='pr',
              tile='t', history=None, **owner_kwargs):
    """Собрать независимую пару (owner, account) с проектом."""
    owner = FakeOwner(month=month, history=history, **owner_kwargs)
    account = dict(id=account_id, state_id=state_id, balance=1000.0, reserved=0.0,
                   protected=3.0, operating_protected=operating,
                   payroll_protected=payroll)
    project = dict(good=good, capacity=10, materials={}, working_cash=11,
                   created=created, expected_price=price,
                   location=dict(province_id=province, tile_id=tile))
    account['project'] = project
    owner.accounts[account_id] = account
    return dict(owner=owner, account=account, project=project, due=due)


def run_slice(implementation, case, apply=None):
    """Прогнать срез маржи/спроса и вернуть наблюдаемое состояние."""
    owner, account = case['owner'], case['account']
    kwargs = dict(owner=owner, account=account, sells={}, due=case['due'],
                  eps=EPS)
    if implementation is cancel_boundary.cancel_margin_demand_project:
        cancelled = implementation(owner, account, {}, case['due'], EPS,
                                   apply_cancellation=apply)
    else:
        cancelled = implementation(owner, account, {}, case['due'], EPS,
                                   apply_cancellation=apply)
    return dict(case, cancelled=cancelled)


def snapshot(case):
    """Наблюдаемое состояние: значения, типы и порядок вызовов хуков."""
    account = case['account']
    project = account.get('project')
    return dict(
        cancelled=case['cancelled'],
        has_project='project' in account,
        protected=account.get('protected'),
        protected_type=type(account.get('protected')),
        decisions=case['owner'].report['investment_decisions'],
        statement=copy.deepcopy(case['owner'].report),
        meta=dict(case['owner'].meta),
        journal=case['owner'].journal,
        project_keys=list(project) if project else None,
    )


def assert_same(reference, native):
    """Сравнить значения, типы и порядок вызовов хуков."""
    assert native == reference


def run_both(case_kwargs):
    """Прогнать эталон и ядро на одном описании состояния; вернуть snapshots."""
    reference = run_slice(cancel_boundary.cancel_margin_demand_project_python,
                          make_case(**case_kwargs))
    native = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**case_kwargs))
    return snapshot(reference), snapshot(native)


# Маржа = net - cost - labor*wage*overhead. При overhead=0 она равна net-cost,
# поэтому граница ``margin < -EPS`` проверяется точно, без погрешности.
# Спрос по умолчанию высокий: иначе ветка ``absent`` перекрыла бы маржу.
def margin_case(margin, demand=EPS*2, volume=0, unmet=0, due=True, month=3,
                created=0, months=3):
    """Случай с заданной маржой: ``net - cost`` при нулевом overhead."""
    return dict(month=month, created=created, due=due, net=10.0,
                cost=10.0-margin, overhead=0, labor=0.0, wage=0.1,
                history=make_history(months), volume=volume, unmet=unmet,
                demand=demand)


# ---------------------------------------------------------------------------
# Порог маржи
# ---------------------------------------------------------------------------

def test_margin_above_threshold_not_cancelled():
    """Маржа выше порога: проект остаётся, решения об отмене нет."""
    case = make_case(**margin_case(0.5))
    result = run_slice(cancel_boundary.cancel_margin_demand_project, case)
    assert result['cancelled'] is False
    assert case['account']['project'] is case['project']
    assert case['account']['protected'] == 3.0
    assert case['owner'].report['investment_decisions'] == []


def test_margin_below_threshold_cancelled():
    """Маржа ниже порога: проект снят, protected восстановлен, решение есть."""
    case = make_case(**margin_case(-0.5))
    result = run_slice(cancel_boundary.cancel_margin_demand_project, case)
    assert result['cancelled'] is True
    assert 'project' not in case['account']
    assert case['account']['protected'] == 5+7
    assert case['owner'].report['investment_decisions'] == [
        dict(kind='investment_decision', investor='acc', outcome='cancelled',
             reason=cancel_boundary.MARGIN_DEMAND_REASON)]


def test_margin_exactly_on_threshold_not_cancelled():
    """Граница: ``margin == -EPS`` ещё не отмена, условие строгое."""
    result = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**margin_case(-EPS)))
    assert result['cancelled'] is False
    assert 'project' in result['account']


def test_margin_just_below_threshold_cancelled():
    """Граница: ``margin`` чуть ниже ``-EPS`` уже отмена."""
    result = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**margin_case(-EPS*2)))
    assert result['cancelled'] is True


def test_margin_zero_keeps_project():
    """Нулевая маржа не отменяет проект: условие требует строгой отрицательности."""
    result = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**margin_case(0.0)))
    assert result['cancelled'] is False


# ---------------------------------------------------------------------------
# Порог отсутствия спроса
# ---------------------------------------------------------------------------

def test_demand_above_threshold_not_cancelled():
    """Спрос выше порога: проект остаётся даже при пустых наблюдениях рынка."""
    case = make_case(**margin_case(1.0, demand=EPS*2))
    result = run_slice(cancel_boundary.cancel_margin_demand_project, case)
    assert result['cancelled'] is False
    assert 'project' in case['account']
    assert case['owner'].report['investment_decisions'] == []


def test_demand_below_threshold_cancelled():
    """Спрос ниже порога при пустом рынке: проект отменяется."""
    case = make_case(**margin_case(1.0, demand=-1.0))
    result = run_slice(cancel_boundary.cancel_margin_demand_project, case)
    assert result['cancelled'] is True
    assert 'project' not in case['account']
    assert case['owner'].report['investment_decisions'][0]['reason'] == \
        cancel_boundary.MARGIN_DEMAND_REASON


def test_demand_exactly_on_threshold_cancelled():
    """Граница: ``demand == EPS`` попадает в ``<= EPS`` и отменяет проект."""
    result = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**margin_case(1.0, demand=EPS)))
    assert result['cancelled'] is True


def test_demand_requires_positive_margin_absent_volume():
    """Ненулевой объём рынка сам по себе снимает ветку отсутствия спроса."""
    result = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**margin_case(1.0, volume=EPS*2)))
    assert result['cancelled'] is False


def test_absent_requires_history_and_age():
    """``absent`` требует и истории рынка, и возраста проекта."""
    short_history = run_slice(
        cancel_boundary.cancel_margin_demand_project,
        make_case(**margin_case(1.0, demand=0.0, months=2)))
    assert short_history['cancelled'] is False
    young = run_slice(
        cancel_boundary.cancel_margin_demand_project,
        make_case(**margin_case(1.0, demand=0.0, month=2, created=0)))
    assert young['cancelled'] is False


def test_due_false_never_cancels():
    """Без ``due`` не отменяет ни одно из условий."""
    result = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**margin_case(-5.0, demand=-5.0, due=False)))
    assert result['cancelled'] is False
    assert 'project' in result['account']


# ---------------------------------------------------------------------------
# Оба условия одновременно
# ---------------------------------------------------------------------------

def test_both_conditions_same_reason_as_python():
    """Оба условия сразу: причина и результат совпадают с Python-эталоном."""
    kwargs = margin_case(-0.5, demand=-1.0)
    reference, native = run_both(kwargs)
    assert_same(reference, native)
    assert native['cancelled'] is True
    reason = native['decisions'][0]['reason']
    assert reason == reference['decisions'][0]['reason']
    assert reason == cancel_boundary.MARGIN_DEMAND_REASON
    # Одно решение на проект, а не два: приоритет маржи не дублирует отмену.
    assert len(native['decisions']) == 1


def test_both_conditions_reason_matches_legacy_literal():
    """Причина — прежний литерал v3, и он же приходит из ядра."""
    assert cancel_boundary.MARGIN_DEMAND_REASON == \
        'Отрицательная маржа или устойчивое отсутствие спроса'
    kernel = cancel_boundary.load_cancel_kernel()
    if kernel is not None:
        assert kernel.MARGIN_DEMAND_REASON == cancel_boundary.MARGIN_DEMAND_REASON
    case = make_case(**margin_case(-0.5, demand=-1.0))
    run_slice(cancel_boundary.cancel_margin_demand_project, case)
    decision = case['owner'].report['investment_decisions'][0]
    assert decision['reason'] == cancel_boundary.MARGIN_DEMAND_REASON
    # Порядок ключей решения прежний: kind, investor, outcome, reason.
    assert list(decision) == ['kind', 'investor', 'outcome', 'reason']


def test_margin_takes_priority_when_both_fire():
    """Маржа проверяется первой: при обоих условиях результат тот же и один."""
    only_margin = run_slice(cancel_boundary.cancel_margin_demand_project,
                            make_case(**margin_case(-0.5, demand=EPS*2)))
    both = run_slice(cancel_boundary.cancel_margin_demand_project,
                     make_case(**margin_case(-0.5, demand=-1.0)))
    assert only_margin['owner'].report['investment_decisions'] == \
        both['owner'].report['investment_decisions']
    # Оба случая дошли до ``reachable_demand``: маржа считается всегда.
    assert [row[0] for row in only_margin['owner'].journal] == \
        [row[0] for row in both['owner'].journal]


# ---------------------------------------------------------------------------
# Совпадение с Python-эталоном на сетке состояний
# ---------------------------------------------------------------------------

CASES = [
    # Маржа выше порога: проект остаётся.
    margin_case(0.25),
    # Маржа ниже порога: отмена.
    margin_case(-0.25),
    # Ровно граница.
    margin_case(-EPS),
    margin_case(-EPS*2),
    margin_case(0.0),
    # Отсутствие спроса срабатывает.
    margin_case(1.0, demand=0.0),
    margin_case(1.0, demand=EPS),
    margin_case(1.0, demand=EPS*2),
    # Спрос не наблюдается, но объём рынка ненулевой.
    margin_case(1.0, volume=EPS*2),
    # Неполная история рынка.
    margin_case(1.0, months=2),
    # Слишком молодой проект.
    margin_case(1.0, month=2, created=0),
    # Не месяц инвестиций.
    margin_case(-1.0, due=False),
    margin_case(1.0, demand=-1.0, due=False),
    # Оба условия.
    margin_case(-0.5, demand=-1.0),
    # int-маржа.
    dict(month=3, created=0, due=True, net=10, cost=4, overhead=0,
         labor=0, wage=0.1, history=make_history(3), volume=0, unmet=0,
         demand=EPS*2),
    # Нулевая float-маржа: условие строгое, проект остаётся.
    dict(month=3, created=0, due=True, net=10.0, cost=10.0, overhead=0,
         labor=0.0, wage=0.1, history=make_history(3), volume=0, unmet=0,
         demand=EPS*2),
    # Ненулевые labor/wage/overhead: прежний порядок произведений.
    dict(month=3, created=0, due=True, net=10.0, cost=4.0, overhead=0.02,
         labor=0.5, wage=0.12, history=make_history(3), volume=0, unmet=0,
         demand=EPS*2),
    # Дробные значения с погрешностью: побитовое совпадение обязательно.
    dict(month=3, created=0, due=True, net=0.3, cost=0.1, overhead=0.0,
         labor=0.0, wage=0.1, history=make_history(3), volume=0, unmet=0,
         demand=EPS*2),
]


@pytest.mark.parametrize('case_kwargs', CASES)
def test_native_matches_python_reference(case_kwargs):
    reference, native = run_both(case_kwargs)
    assert_same(reference, native)


# ---------------------------------------------------------------------------
# Side effects: ровно как в старом пути
# ---------------------------------------------------------------------------

def test_side_effects_called_exactly_as_old_path():
    """Хуки зовутся в прежнем порядке и столько же раз."""
    kwargs = margin_case(-0.5)
    reference, native = run_both(kwargs)
    kinds = [row[0] for row in native['journal']]
    assert kinds == [row[0] for row in reference['journal']]
    # ``absent`` доходит до ``reachable_demand``: история и возраст набраны,
    # volume/unmet пусты. Результат проверки отменяет ветку, но не вызов.
    assert kinds == ['net_price', 'unit_cost', 'market_signal',
                     'reachable_demand']
    # Отмена применяется один раз: снятие проекта, protected и отчёт — один шаг.
    assert native['has_project'] is False
    assert len(native['decisions']) == 1


def test_absent_path_calls_reachable_demand_once():
    """Ветка отсутствия спроса доходит до ``reachable_demand`` ровно один раз."""
    kwargs = margin_case(1.0, demand=0.0)
    reference, native = run_both(kwargs)
    kinds = [row[0] for row in native['journal']]
    assert kinds == [row[0] for row in reference['journal']]
    assert kinds.count('reachable_demand') == 1
    assert kinds == ['net_price', 'unit_cost', 'market_signal', 'reachable_demand']


def test_positive_volume_skips_reachable_demand():
    """Ненулевой объём рынка останавливает ``absent`` до ``reachable_demand``."""
    kwargs = margin_case(1.0, volume=EPS*2)
    reference, native = run_both(kwargs)
    kinds = [row[0] for row in native['journal']]
    assert kinds == [row[0] for row in reference['journal']]
    assert 'reachable_demand' not in kinds


def test_no_order_or_dirty_hooks_in_slice():
    """Срез не вызывает cancel/dirty: маржа/спрос их прежде не трогали."""
    for kwargs in (margin_case(-0.5), margin_case(1.0, demand=0.0),
                   margin_case(0.5)):
        case = make_case(**kwargs)
        run_slice(cancel_boundary.cancel_margin_demand_project, case)
        kinds = [row[0] for row in case['owner'].journal]
        assert 'cancel' not in kinds
        assert 'dirty' not in kinds


def test_application_hook_is_the_shared_cancellation_hook():
    """Оба среза применяют отмену одним и тем же хуком границы."""
    calls = []

    def apply(owner, account, reason, result):
        calls.append((reason, dict(result)))

    case = make_case(**margin_case(-0.5))
    run_slice(cancel_boundary.cancel_margin_demand_project, case, apply=apply)
    assert calls == [(cancel_boundary.MARGIN_DEMAND_REASON,
                      dict(outcome='cancelled',
                           reason=cancel_boundary.MARGIN_DEMAND_REASON))]
    # Хук получил управление, поэтому проект снимает он, а не ядро.
    assert 'project' in case['account']


def test_protected_report_and_queue_match_reference():
    """protected, отчёт и очередь совпадают с Python-эталоном."""
    kwargs = margin_case(-0.5, demand=-1.0)
    reference, native = run_both(kwargs)
    assert native['protected'] == reference['protected']
    assert type(native['protected']) is type(reference['protected'])
    assert native['decisions'] == reference['decisions']
    assert native['statement'] == reference['statement']
    # Queue срез не читает и не пишет.
    assert native['meta'] == reference['meta']
    assert 'investment_queue' not in native['meta']


def test_int_float_semantics():
    """Тип маржи сохраняется: int остаётся int, float — float."""
    # int-маржа: ``10-4-0*...`` остаётся целым и выше порога.
    integer = make_case(**dict(month=3, created=0, due=True, net=10, cost=4,
                               overhead=0, labor=0, wage=0.1, volume=0,
                               unmet=0, demand=EPS*2, history=make_history(3)))
    run_slice(cancel_boundary.cancel_margin_demand_project, integer)
    # int-маржа выше нуля: проект не отменён.
    assert 'project' in integer['account']

    # float-маржа: 0.1+0.2-0.3 на границе нуля ведёт себя как в Python.
    floating_kwargs = dict(month=3, created=0, due=True, net=0.1+0.2, cost=0.3,
                           overhead=0.0, labor=0.0, wage=0.1, volume=0,
                           unmet=0, demand=EPS*2, history=make_history(3))
    reference = run_slice(cancel_boundary.cancel_margin_demand_project_python,
                          make_case(**floating_kwargs))
    native = run_slice(cancel_boundary.cancel_margin_demand_project,
                       make_case(**floating_kwargs))
    assert native['cancelled'] == reference['cancelled']
    # 0.1+0.2-0.3 > 0, поэтому проект остаётся в обеих реализациях.
    assert native['cancelled'] is False


def test_margin_arithmetic_order_preserved():
    """Порядок ``(net-cost)-labor*wage*overhead`` сохраняется побитово."""
    kwargs = dict(month=3, created=0, due=True, net=0.1+0.2, cost=0.3,
                  overhead=0.03, labor=0.7, wage=0.11, volume=0, unmet=0,
                  demand=EPS*2, history=make_history(3))
    reference, native = run_both(kwargs)
    assert_same(reference, native)
    # Явная проверка формулы: ядро не переставило множители.
    expected = ((kwargs['net']-kwargs['cost'])
                -kwargs['labor']*kwargs['wage']*kwargs['overhead'])
    assert native['cancelled'] is (expected < -EPS)


def test_price_and_cost_hooks_receive_legacy_request_dict():
    """В хуки приходит прежний ``e``: id, good, price и поля локации."""
    case = make_case(**margin_case(1.0))
    run_slice(cancel_boundary.cancel_margin_demand_project, case)
    net_call = case['owner'].journal[0]
    assert net_call == ('net_price', 'acc', 'wood', 10.0)
    assert case['owner'].journal[1] == ('unit_cost', 'acc', 'wood')
    # ``e`` — локальный словарь запроса, в проект он не попадает.
    assert 'id' not in case['project']
    assert case['project']['location'] == dict(province_id='pr', tile_id='t')


def test_project_state_not_mutated_by_check():
    """Проверка маржи/спроса не пишет проект: он остаётся прежним."""
    case = make_case(**margin_case(1.0, demand=1.0))
    before = dict(case['project'])
    run_slice(cancel_boundary.cancel_margin_demand_project, case)
    assert case['project'] == before
    assert list(case['project']) == list(before)


def test_deepcopy_does_not_share_state():
    """Копия не делит project: отмена в копии не видна в оригинале."""
    case = make_case(**margin_case(-0.5))
    owner, account = case['owner'], case['account']
    clone = copy.deepcopy(owner)
    assert clone.accounts['acc']['project'] is not account['project']
    cancelled = cancel_boundary.cancel_margin_demand_project(
        clone, clone.accounts['acc'], {}, True, EPS)
    assert cancelled is True
    assert 'project' not in clone.accounts['acc']
    # Оригинал не тронут ни решением, ни protected.
    assert account['project'] is case['project']
    assert account['protected'] == 3.0
    assert owner.report['investment_decisions'] == []
    assert owner.journal == []


def test_missing_project_is_noop():
    """Без проекта срез ничего не делает и не зовёт хуки."""
    owner = FakeOwner(month=3)
    account = dict(id='acc', state_id='st', protection=0, operating_protected=5,
                   payroll_protected=7)
    assert cancel_boundary.cancel_margin_demand_project(
        owner, account, {}, True, EPS) is False
    assert owner.journal == []
    assert owner.report['investment_decisions'] == []


# ---------------------------------------------------------------------------
# Привязка native-реализации
# ---------------------------------------------------------------------------

def test_boundary_exposes_native_when_built():
    built = cancel_boundary.load_cancel_kernel() is not None
    assert cancel_boundary.NATIVE_CANCEL_KERNEL is built
    if built:
        assert cancel_boundary.check_margin_demand.__module__.endswith(
            'cancel_kernel')
    else:
        assert cancel_boundary.check_margin_demand is \
            cancel_boundary.check_margin_demand_python


def test_native_entry_point_is_the_compiled_one():
    """Граница обязана привязать именно скомпилированную функцию."""
    kernel = cancel_boundary.load_cancel_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение cancel_kernel не собрано')
    assert cancel_boundary.check_margin_demand is kernel.check_margin_demand
    assert cancel_boundary.check_margin_demand is not \
        cancel_boundary.check_margin_demand_python


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
    """В ``project_material_requests`` ровно один call site маржи/спроса."""
    source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    start = source.index('def project_material_requests')
    end = source.index('def finish_projects')
    body = source[start:end]
    assert ('_cancel.cancel_margin_demand_project(self, a, sells, due, EPS)'
            in body)
    assert body.count('cancel_margin_demand_project') == 1
    # Пороги, условие и причина не остались в Python-теле.
    assert 'Отрицательная маржа или устойчивое отсутствие спроса' not in body
    assert 'net_price' not in body
    assert 'unit_cost' not in body
    assert 'reachable_demand' not in body
    assert 'market_signal' not in body
    assert 'enterprise_overhead_rate' not in body
    # Прежние side effects в срезе не появились и не потеряны.
    assert 'cancel_order' not in body
    assert 'mark_market_quotes_dirty' not in body
    # Stall-срез остался на месте и не переписан.
    assert '_cancel.cancel_stalled_project(self, a, EPS)' in body
    # Доставка/резерв остались единственным вызовом.
    assert body.count('request_project_materials') == 1


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


def run_engine(seed, months=3, implementation=None):
    """Прогнать months месяцев, собрав digest, RNG и решения по отменам."""
    original = cancel_boundary.cancel_margin_demand_project
    if implementation is not None:
        cancel_boundary.cancel_margin_demand_project = implementation
    decisions = []
    try:
        with workdir('test_cancel_margin') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                for index in range(months):
                    service.advance_turn(
                        command_id='cancel-margin-%d-%d' % (seed, index),
                        source='audit')
                    decisions.extend(
                        dict(row) for row in engine.report['investment_decisions'])
                return dict(engine=engine, digest=engine_digest(engine, decisions),
                            rng=digest(engine.rng.getstate()), decisions=decisions)
            finally:
                db.close()
    finally:
        cancel_boundary.cancel_margin_demand_project = original


@pytest.fixture(scope='module')
def exact_17():
    native = run_engine(17)
    reference = run_engine(
        17, implementation=cancel_boundary.cancel_margin_demand_project_python)
    return native, reference


@pytest.fixture(scope='module')
def exact_73129():
    native = run_engine(73129)
    reference = run_engine(
        73129, implementation=cancel_boundary.cancel_margin_demand_project_python)
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
    # Решения по отменам совпадают построчно.
    assert native['decisions'] == reference['decisions']


def run_engine_forced(seed, months=6, implementation=None):
    """Тот же прогон, но с гарантированно отрицательной маржой при отмене.

    Сжатие ``net_price`` включается только внутри ``project_material_requests``:
    отбор кандидатов и старт проектов видят настоящие цены, а проверка отмены —
    сдвинутые. Иначе проекты не появлялись бы вовсе и отменять было бы нечего.
    Хук общий для обеих реализаций, поэтому exact-сравнение остаётся честным,
    а путь отмены — реально исполненным, а не пропущенным.
    """
    from modules.economy import Economy

    real_net_price = Economy.net_price
    real_requests = Economy.project_material_requests
    active = {'squeeze': False}

    def squeezed(self, e):
        if active['squeeze']:
            return real_net_price(self, e)-1e6
        return real_net_price(self, e)

    def wrapped(self, sells, due, investors):
        active['squeeze'] = True
        try:
            return real_requests(self, sells, due, investors)
        finally:
            active['squeeze'] = False

    original = cancel_boundary.cancel_margin_demand_project
    if implementation is not None:
        cancel_boundary.cancel_margin_demand_project = implementation
    Economy.net_price = squeezed
    Economy.project_material_requests = wrapped
    decisions = []
    try:
        with workdir('test_cancel_margin_forced') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                for index in range(months):
                    service.advance_turn(
                        command_id='cancel-margin-forced-%d-%d' % (seed, index),
                        source='audit')
                    decisions.extend(
                        dict(row) for row in engine.report['investment_decisions'])
                return dict(engine=engine, digest=engine_digest(engine, decisions),
                            rng=digest(engine.rng.getstate()), decisions=decisions)
            finally:
                db.close()
    finally:
        Economy.net_price = real_net_price
        Economy.project_material_requests = real_requests
        cancel_boundary.cancel_margin_demand_project = original


@pytest.fixture(scope='module')
def forced_margin_17():
    native = run_engine_forced(17)
    reference = run_engine_forced(
        17, implementation=cancel_boundary.cancel_margin_demand_project_python)
    return native, reference


@pytest.fixture(scope='module')
def forced_margin_73129():
    native = run_engine_forced(73129)
    reference = run_engine_forced(
        73129, implementation=cancel_boundary.cancel_margin_demand_project_python)
    return native, reference


@pytest.mark.parametrize('fixture', ['forced_margin_17', 'forced_margin_73129'])
def test_forced_margin_matches_reference(fixture, request):
    """Форсированная маржа: отмена реально происходит и совпадает с эталоном."""
    native, reference = request.getfixturevalue(fixture)
    cancelled = [row for row in native['decisions']
                 if row.get('reason') == cancel_boundary.MARGIN_DEMAND_REASON]
    assert cancelled, 'отмена по марже/спросу не сработала ни разу'
    reference_cancelled = [row for row in reference['decisions']
                           if row.get('reason') ==
                           cancel_boundary.MARGIN_DEMAND_REASON]
    assert len(cancelled) == len(reference_cancelled)
    assert native['digest'] == reference['digest']
    assert native['rng'] == reference['rng']
    assert native['decisions'] == reference['decisions']
