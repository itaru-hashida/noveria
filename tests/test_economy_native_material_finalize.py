"""Focused-тесты финализации material request: побитовое совпадение с эталоном.

Проверяется последний участок ``project_material_requests``: затронутые товары,
запись ревизий и готовые numeric diagnostics/journal-строки. К моменту
финализации смета, доставка, резерв и строительные закупки уже посчитаны
существующими срезами, поэтому ядро только читает готовые числа и возвращает
новый dict.

Ядро работает с object-арифметикой намеренно: остатки бывают и ``int``, и
``float``, а встроенный ``max(0, x)`` сохраняет тип аргумента, и этот тип виден
дальше в отчёте. Поэтому сравниваются не только значения, но и типы, и порядок
строк, и порядок обновлений dirty/revisions.

Отдельно проверяется, что срез не добавил side effects: ядро не вызывает
``mark_market_quotes_dirty`` и не двигает ``_demand_revision`` второй раз —
их уже выполнили release- и cancel-срезы. Python-хук применяет отчёт ровно
один раз.
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
from modules.economy.native import material_finalize_boundary as finalize  # noqa: E402

EPS = support.EPS
_PY_MAX = builtins.max


class FakeOwner:
    """Минимальный носитель хуков: ``report``, ``report_detail`` и ревизии.

    Тела хуков повторяют движок, но с журналом вызовов: так видно не только
    итоговое состояние, но и то, что ядро не пометило котировки и не сдвинуло
    ревизии повторно.
    """

    def __init__(self, detail='standard', revisions=None, quote_versions=None):
        self.report = dict(material_requests=[])
        self._detail = detail
        self._demand_revision = dict(revisions or {})
        self._market_quote_versions = dict(quote_versions or {})
        self.journal = []

    def report_detail(self):
        self.journal.append(('report_detail',))
        return self._detail

    def mark_market_quotes_dirty(self, good=None):
        self.journal.append(('dirty', good))
        if good is not None:
            self._market_quote_versions[good] = \
                self._market_quote_versions.get(good, 0)+1

    def order(self, participant, good, quantity, side, price, purpose):
        self.journal.append(('order', participant, good, quantity, side, price, purpose))
        if side == 'buy':
            self._demand_revision[good] = self._demand_revision.get(good, 0)+1


def make_case(materials=None, capacity=10, delivery=0.72, revisions=None,
              quote_versions=None, detail='standard', account_id='acc',
              project_id=None, created=0, last_progress=None):
    """Собрать независимый (owner, account, project, bill)."""
    owner = FakeOwner(detail=detail, revisions=revisions,
                      quote_versions=quote_versions)
    account = dict(id=account_id, stock={}, stock_reserved={},
                   balance=1000.0, reserved=0.0)
    project = dict(capacity=capacity, materials=dict(materials or {}),
                   delivery=delivery, created=created)
    if last_progress is not None:
        project['last_progress'] = last_progress
    if project_id is not None:
        project['project_id'] = project_id
    account['project'] = project
    bill = {'wood': 4.0*capacity/10, 'stone': 2.0*capacity/10}
    return dict(owner=owner, account=account, project=project, bill=bill)


def run(implementation, case, reason=None):
    """Прогнать одну реализацию финализации и вернуть результат."""
    return implementation(case['account'], case['project'], case['bill'],
                          case['project'].get('delivery', 0), EPS, reason,
                          case['owner']._demand_revision,
                          case['owner']._market_quote_versions)


def assert_same(reference, native):
    """Сравнить значения, типы и порядок строк и полей."""
    assert native == reference
    assert list(native) == list(reference)
    assert [row['good'] for row in native['rows']] == \
        [row['good'] for row in reference['rows']]
    for native_row, reference_row in zip(native['rows'], reference['rows']):
        assert list(native_row) == list(reference_row), reference_row['good']
        for field, value in reference_row.items():
            assert type(native_row[field]) is type(value), field
        assert list(native_row['progress']) == list(reference_row['progress'])


CASE_CASES = [
    # Один товар, склад пуст: недостача равна смете.
    dict(materials={}),
    # Материал закреплён полностью: недостача нулевая.
    dict(materials={'wood': 4.0, 'stone': 2.0}),
    # Материал закреплён частично: недостача — остаток сметы.
    dict(materials={'wood': 1.5}),
    # int-материалы: тип недостачи обязан остаться int.
    dict(materials={'wood': 2, 'stone': 1}),
    # float-материалы: тип обязан остаться float.
    dict(materials={'wood': 2.5, 'stone': 0.25}),
    # Мощность не кратна minimum_capacity: дробная смета.
    dict(materials={}, capacity=7),
    # Нулевая мощность: пустая по количеству смета.
    dict(materials={}, capacity=0),
    # Причина отмены уже рассчитана: она попадает в строки как есть.
    dict(materials={'wood': 1.0}),
]


@pytest.mark.parametrize('case_kwargs', CASE_CASES)
def test_native_matches_python_reference(case_kwargs):
    reason = 'Отрицательная маржа или устойчивое отсутствие спроса' \
        if case_kwargs.get('materials') == {'wood': 1.0} else None
    reference = run(finalize.finalize_material_request_python,
                    make_case(**case_kwargs), reason)
    native = run(finalize.finalize_material_request,
                 make_case(**case_kwargs), reason)
    assert_same(reference, native)


def test_dirty_goods_for_one_good():
    """Один товар сметы: одна запись dirty, порядок сохранён."""
    case = make_case(quote_versions={'wood': 3})
    result = run(finalize.finalize_material_request, case)
    assert result['dirty_goods'] == ['wood']
    assert result['goods'] == ['wood', 'stone']


def test_dirty_goods_for_several_goods():
    """Несколько товаров: dirty идёт в порядке сметы, а не множеством."""
    case = make_case(quote_versions={'stone': 1, 'wood': 2})
    result = run(finalize.finalize_material_request, case)
    assert result['dirty_goods'] == ['wood', 'stone']
    assert result['dirty_goods'] == finalize.dirty_goods_python(
        result['goods'], case['owner']._market_quote_versions)


def test_no_duplicates_when_python_path_had_none():
    """Смета — словарь: каждый товар в журнале ровно один раз, как и прежде."""
    case = make_case(quote_versions={'wood': 1, 'stone': 1})
    result = run(finalize.finalize_material_request, case)
    assert len(result['rows']) == len(set(result['goods'])) == 2
    assert [row['good'] for row in result['rows']] == ['wood', 'stone']
    assert len(result['dirty_goods']) == len(set(result['dirty_goods']))
    assert len(result['revisions']) == len(result['goods'])


def test_revisions_updated_exactly_once():
    """Ядро не двигает ревизии: значение читается, а не инкрементируется."""
    case = make_case(revisions={'wood': 7, 'stone': 2})
    before = dict(case['owner']._demand_revision)
    result = run(finalize.finalize_material_request, case)
    assert case['owner']._demand_revision == before
    assert result['revisions'] == [dict(good='wood', revision=7),
                                   dict(good='stone', revision=2)]
    assert not [row for row in case['owner'].journal if row[0] == 'order']
    assert not [row for row in case['owner'].journal if row[0] == 'dirty']


def test_diagnostics_match_reference_on_totals():
    """Итоги месяца считаются тем же встроенным ``sum`` в порядке строк."""
    case = make_case(materials={'wood': 1.5, 'stone': 0.5}, quote_versions={'wood': 1})
    reference = run(finalize.finalize_material_request_python, case)
    native = run(finalize.finalize_material_request, make_case(
        materials={'wood': 1.5, 'stone': 0.5}, quote_versions={'wood': 1}))
    assert_same(reference, native)
    assert native['requested_total'] == 4.0+2.0
    assert native['delivered_total'] == 1.5+0.5
    assert native['missing_total'] == _PY_MAX(0, 4.0-1.5)+_PY_MAX(0, 2.0-0.5)
    assert type(native['requested_total']) is float
    assert type(native['missing_total']) is float


def test_report_rows_match_and_keep_order():
    """Report-строки совпадают и идут в том же порядке: Python-хук один."""
    case = make_case(materials={'wood': 1.0}, quote_versions={'wood': 1})
    owner = case['owner']
    result = finalize.apply_material_request(
        owner, case['account'], case['project'], case['bill'], EPS)
    rows = owner.report['material_requests']
    assert rows == result['rows']
    assert [row['good'] for row in rows] == ['wood', 'stone']
    assert [row['project_id'] for row in rows] == ['acc', 'acc']
    # Отчётный детальный режим спрошен ровно один раз: второго хука нет.
    assert [row[0] for row in owner.journal].count('report_detail') == 1
    # Строки — копии: правка отчёта не меняет результат ядра.
    assert rows[0] is not result['rows'][0]
    rows[0]['good'] = 'mutated'
    assert result['rows'][0]['good'] == 'wood'


def test_missing_quantity():
    """Недостача — остаток сметы, целый ноль остаётся целым."""
    case = make_case(materials={'wood': 4.0, 'stone': 2.0}, capacity=10)
    result = run(finalize.finalize_material_request, case)
    assert [row['missing'] for row in result['rows']] == [0.0, 0.0]
    assert result['missing_total'] == 0.0

    partial = run(finalize.finalize_material_request,
                  make_case(materials={'wood': 1.0}))
    assert partial['rows'][0]['missing'] == 3.0
    assert partial['rows'][1]['missing'] == 2.0


def test_delivered_quantity():
    """Доставлено ровно то, что закреплено за проектом."""
    case = make_case(materials={'wood': 2.5, 'stone': 0})
    result = run(finalize.finalize_material_request, case)
    assert [row['delivered'] for row in result['rows']] == [2.5, 0]
    assert result['delivered_total'] == 2.5
    assert case['project']['materials'] == {'wood': 2.5, 'stone': 0}


def test_reserved_quantity():
    """Зарезервировано за проектом то же, что доставлено; склад не трогается."""
    case = make_case(materials={'wood': 3.0})
    before = copy.deepcopy(case['account'])
    result = run(finalize.finalize_material_request, case)
    assert [row['reserved'] for row in result['rows']] == [3.0, 0]
    assert result['reserved_total'] == 3.0
    assert case['account'] == before


def test_cancellation_reason():
    """Причина отмены переносится как есть и помечает строку отменённой."""
    case = make_case(materials={'wood': 1.0})
    reason = 'Стройка не продвигается'
    result = run(finalize.finalize_material_request, case, reason)
    assert all(row['reason'] == reason for row in result['rows'])
    assert all(row['cancelled'] is True for row in result['rows'])
    assert result['reason'] == reason
    active = run(finalize.finalize_material_request, make_case())
    assert all(row['reason'] is None for row in active['rows'])
    assert all(row['cancelled'] is False for row in active['rows'])


def test_int_float_semantics():
    """Тип сметы и остатка сохраняется: int остаётся int, float — float."""
    integer = make_case(materials={'wood': 2, 'stone': 1}, capacity=10)
    integer_case = dict(integer, bill={'wood': 4, 'stone': 2})
    result = run(finalize.finalize_material_request, integer_case)
    assert [type(row['requested']) for row in result['rows']] == [int, int]
    assert [type(row['delivered']) for row in result['rows']] == [int, int]
    assert [type(row['missing']) for row in result['rows']] == [int, int]

    floating = make_case(materials={'wood': 2.0, 'stone': 1.0}, capacity=10)
    float_case = dict(floating, bill={'wood': 4.0, 'stone': 2.0})
    result = run(finalize.finalize_material_request, float_case)
    assert [type(row['requested']) for row in result['rows']] == [float, float]
    assert [type(row['delivered']) for row in result['rows']] == [float, float]
    assert [type(row['missing']) for row in result['rows']] == [float, float]


def test_empty_material_result():
    """Пустая смета: ни строк, ни ревизий, ни dirty; итоги — целый ноль."""
    case = make_case(materials={})
    case['bill'] = {}
    result = run(finalize.finalize_material_request, case)
    assert result['rows'] == []
    assert result['goods'] == []
    assert result['dirty_goods'] == []
    assert result['revisions'] == []
    assert result['requested_total'] == 0
    assert type(result['requested_total']) is int
    assert result['missing_total'] == 0


def test_several_projects_keep_their_order():
    """Несколько проектов: порядок проектов и строк задаёт вызывающий список."""
    records = [
        (dict(id='a'), dict(materials={'wood': 1.0}, delivery=0.5), {'wood': 4.0}, 0.5, None),
        (dict(id='b'), dict(materials={}, delivery=0.25), {'stone': 2.0}, 0.25, None),
        (dict(id='c'), dict(materials={'wood': 4.0}, delivery=0.0), {'wood': 4.0}, 0.0,
         'Строка проекта не продвигается'),
    ]
    reference = finalize.finalize_material_requests_python(
        copy.deepcopy(records), EPS, {'wood': 2}, {'wood': 1})
    native = finalize.finalize_material_requests(
        copy.deepcopy(records), EPS, {'wood': 2}, {'wood': 1})
    assert native == reference
    assert [project['project_id'] for project in native['projects']] == ['a', 'b', 'c']
    assert [row['good'] for row in native['rows']] == ['wood', 'stone', 'wood']
    assert native['goods'] == ['wood', 'stone', 'wood']
    assert native['dirty_goods'] == ['wood', 'wood']
    assert native['requested_total'] == 4.0+2.0+4.0
    assert native['delivered_total'] == 1.0+0+4.0


def test_deepcopy_does_not_share_state():
    """Копия движка не делит проект и отчёт: финализация в копии не видна."""
    case = make_case(materials={'wood': 1.0})
    case['owner'].accounts = {'acc': case['account']}
    clone = copy.deepcopy(case)
    assert clone['owner'].accounts['acc']['project'] is not case['project']
    result = finalize.apply_material_request(
        clone['owner'], clone['account'], clone['project'], clone['bill'], EPS)
    assert result['rows'][0]['delivered'] == 1.0
    assert case['owner'].report['material_requests'] == []
    assert case['project']['materials'] == {'wood': 1.0}


def test_report_not_written_in_none_detail():
    """``detail == 'none'`` не хранит тяжёлые диагностические слои, как и прежде."""
    case = make_case(detail='none', materials={'wood': 1.0})
    result = finalize.apply_material_request(
        case['owner'], case['account'], case['project'], case['bill'], EPS)
    assert result['rows']
    assert case['owner'].report['material_requests'] == []


def test_rows_are_a_fresh_copy_of_live_dicts():
    """Строки — готовые копии: живой проект после финализации не меняется."""
    case = make_case(materials={'wood': 2.0})
    project = case['project']
    before = copy.deepcopy(project)
    result = run(finalize.finalize_material_request, case)
    assert project == before
    assert result['rows'][0]['progress']['quantity'] == 2.0
    assert type(result['rows'][0]['progress']['quantity']) is float


def test_progress_uses_compensated_builtin_sum():
    """Прогресс считается тем же ``sum`` 3.12, а не наивным накоплением."""
    materials = {'wood': 1e16, 'stone': 1.0, 'clay': -1e16}
    case = make_case(materials=materials)
    case['bill'] = {'wood': 1.0}
    result = run(finalize.finalize_material_request, case)
    assert result['rows'][0]['progress']['quantity'] == 1.0
    assert result['rows'][0]['progress']['quantity'] == sum(materials.values())


def test_boundary_exposes_native_when_built():
    built = finalize.load_material_finalize_kernel() is not None
    assert finalize.NATIVE_MATERIAL_FINALIZE_KERNEL is built
    if built:
        assert finalize.finalize_material_request.__module__.endswith(
            'material_finalize_kernel')
    else:
        assert finalize.finalize_material_request is \
            finalize.finalize_material_request_python


def test_native_entry_points_are_the_compiled_ones():
    """Граница обязана привязать именно скомпилированные функции."""
    kernel = finalize.load_material_finalize_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение material_finalize_kernel не собрано')
    assert finalize.finalize_material_request is kernel.finalize_material_request
    assert finalize.finalize_material_requests is kernel.finalize_material_requests
    assert finalize.material_rows is kernel.material_rows
    assert finalize.finalize_material_request is not \
        finalize.finalize_material_request_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    """Отсутствие собранного ядра даёт None, а не падает на импорте economy."""
    import importlib

    name = 'modules.economy.native.material_finalize_kernel'
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name == name or module_name.endswith('material_finalize_kernel'):
            raise ImportError("cannot import name 'material_finalize_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert finalize.load_material_finalize_kernel() is None


def test_v3_call_site_has_one_finalize_call():
    """В ``project_material_requests`` ровно один call site финализации."""
    source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    start = source.index('def project_material_requests')
    end = source.index('def finish_projects')
    body = source[start:end]
    assert body.count('apply_material_request') == 1
    assert '_material_finalize.apply_material_request(self, a, project, bill, EPS)' in body
    # Логика журнала не осталась в Python-теле: ни ключа отчёта, ни ревизий.
    assert "'material_requests'" not in body
    assert 'progress_quantity' not in body
    assert 'mark_market_quotes_dirty' not in body
    assert '_demand_revision' not in body


# ---------------------------------------------------------------------------
# Exact-сравнение на реальном движке: seed 17/73129, 3 месяца
# ---------------------------------------------------------------------------

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def engine_digest(engine, rows):
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'queue': engine.meta.get('investment_queue', []),
        'rows': rows,
        'quote_versions': dict(getattr(engine, '_market_quote_versions', {}) or {}),
        'demand_revision': dict(getattr(engine, '_demand_revision', {}) or {}),
    })


def run_engine(seed, months=3, implementation=None):
    """Прогнать months месяцев, собрав digest, RNG и journal-строки."""
    saved = finalize.finalize_material_request
    saved_apply = finalize.apply_material_request
    v3 = sys.modules['modules.economy.v3']
    # Сохраняется именно модуль границы: v3 разрешает имя при каждом вызове,
    # поэтому подстановка функции вернула бы не тот объект.
    saved_module = v3._material_finalize
    if implementation is not None:
        finalize.finalize_material_request = implementation
        finalize.apply_material_request = apply_reference
    rows = []
    original_apply = finalize.apply_material_request

    def counted_apply(owner, account, project, bill, eps, reason=None):
        result = original_apply(owner, account, project, bill, eps, reason)
        rows.extend(dict(row) for row in result['rows'])
        return result

    finalize.apply_material_request = counted_apply
    v3._material_finalize = finalize
    try:
        with workdir('test_material_finalize') as directory:
            db, service = sr.build_world(seed, directory, report_detail='standard')
            try:
                engine = service.economy_state().engine
                for index in range(months):
                    service.advance_turn(
                        command_id='material-finalize-%d-%d' % (seed, index),
                        source='audit')
                report_rows = [dict(row) for row in
                               engine.report.get('material_requests', [])]
                return dict(engine=engine,
                            digest=engine_digest(engine, rows),
                            rng=digest(engine.rng.getstate()),
                            rows=rows, report_rows=report_rows)
            finally:
                db.close()
    finally:
        v3._material_finalize = saved_module
        finalize.finalize_material_request = saved
        finalize.apply_material_request = saved_apply


def apply_reference(owner, account, project, bill, eps, reason=None):
    """Эталонный хук применения: то же, но через эталонную реализацию."""
    result = finalize.finalize_material_request_python(
        account, project, bill, project.get('delivery', 0), eps, reason,
        getattr(owner, '_demand_revision', None),
        getattr(owner, '_market_quote_versions', None))
    finalize.apply_rows_hook(owner, result)
    return result


@pytest.fixture(scope='module')
def exact_17():
    native = run_engine(17)
    reference = run_engine(17, implementation=finalize.finalize_material_request_python)
    return native, reference


@pytest.fixture(scope='module')
def exact_73129():
    native = run_engine(73129)
    reference = run_engine(73129,
                           implementation=finalize.finalize_material_request_python)
    return native, reference


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_three_months_matches_reference(fixture, request):
    native, reference = request.getfixturevalue(fixture)
    assert native['digest'] == reference['digest']
    assert native['rng'] == reference['rng']


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_rows_really_built(fixture, request):
    """Срез действительно исполнялся: строки есть и совпадают побитово."""
    native, reference = request.getfixturevalue(fixture)
    assert native['rows']
    assert native['rows'] == reference['rows']
    assert len(native['rows']) == sum(
        1 for row in native['rows'] if 'missing' in row)


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_report_rows_are_copies_in_order(fixture, request):
    """Report-строки последнего месяца идут в том же порядке, что журнал."""
    native, reference = request.getfixturevalue(fixture)
    assert native['report_rows'] == reference['report_rows']
    if native['report_rows']:
        assert [row['good'] for row in native['report_rows']] == \
            [row['good'] for row in reference['report_rows']]


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_rng_equal_and_queue_untouched(fixture, request):
    native, reference = request.getfixturevalue(fixture)
    engine = native['engine']
    assert digest(engine.rng.getstate()) == native['rng']
    assert engine.meta.get('investment_queue', []) == \
        reference['engine'].meta.get('investment_queue', [])


@pytest.mark.parametrize('fixture', ['exact_17', 'exact_73129'])
def test_exact_revisions_not_doubled(fixture, request):
    """Ревизии и версии котировок совпадают: ядро не пометило их второй раз."""
    native, reference = request.getfixturevalue(fixture)
    native_engine = native['engine']
    reference_engine = reference['engine']
    assert dict(getattr(native_engine, '_demand_revision', {}) or {}) == \
        dict(getattr(reference_engine, '_demand_revision', {}) or {})
    assert dict(getattr(native_engine, '_market_quote_versions', {}) or {}) == \
        dict(getattr(reference_engine, '_market_quote_versions', {}) or {})
