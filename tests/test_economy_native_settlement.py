"""Focused-тесты settlement-ядра: read-only валидация счетов и населения.

Ядро — чистый обход: ни заявок, ни склада, ни денег, ни отчёта оно не меняет.
Проверяется не только совпадение результата с Python-эталоном, но и то, что
побочных эффектов нет вовсе, а точные тексты ``ValueError`` и момент отказа
(short-circuit) те же.

Эталон — ``settlement_boundary.validate_python``, fallback — та же функция при
отсутствии собранного расширения.
"""
import copy
import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import settlement_boundary  # noqa: E402

FIXTURE = ROOT/'tests/fixtures/economy.json'
EPS = support.EPS


# ---------------------------------------------------------------------------
# Маленький движок на общей фикстуре
# ---------------------------------------------------------------------------

def build_engine(seed=17):
    """Движок на фикстуре: готовые счета и товары без хода."""
    cfg = json.loads(FIXTURE.read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='p2', kind='Province', name='P2', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'p2', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['grain', 'wood', 'ore', 'flax', 'fish'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = 'standard'
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    engine.report['demography'] = []
    engine.report['migration'] = []
    engine.report.setdefault('invariants', {})
    return engine


def empty_engine():
    """Движок без счетов и без строк отчёта: пустой мир."""
    engine = build_engine()
    engine.accounts.clear()
    engine.report['demography'] = []
    engine.report['migration'] = []
    engine.report['invariants'] = {}
    engine.__dict__.pop('_cash_store', None)
    return engine


def demography_row(before=100, after=None, births=1.0, deaths=0.0,
                   migration_in=0, migration_out=0, rounding=0.0):
    """Строка демографии провинции в том виде, в каком её пишет update_demography.

    ``after`` по умолчанию выводится из остальных полей, чтобы строка была
    согласованной: иначе проверка изменения численности сработает раньше той,
    которую проверяет тест.
    """
    if after is None:
        after = before+births-deaths+rounding+migration_in-migration_out
    return dict(province_id='p', population_before=before, population_after=after,
                births=births, deaths=deaths, migration_in=migration_in,
                migration_out=migration_out, rounding_adjustment=rounding,
                food_stress=0.0)


def migration_row(people=5, source='p', destination='p2', state='s'):
    return dict(people=people, source_province=source, destination_province=destination,
                state_id=state)


# ---------------------------------------------------------------------------
# Снимки состояния и побитовое сравнение
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS = ('stock', 'values', 'stock_reserved', 'balance', 'reserved',
                  'protected', 'public_projects', 'project')
REPORT_KEYS = ('orders', 'trades', 'events', 'demography', 'migration', 'invariants')


def rng_digest(engine):
    return hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                     ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def snapshot(engine):
    """Полный снимок того, что validate не вправе менять."""
    return {
        'accounts': {aid: {key: copy.deepcopy(a.get(key)) for key in ACCOUNT_FIELDS}
                     for aid, a in engine.accounts.items()},
        'report': {key: copy.deepcopy(engine.report.get(key)) for key in REPORT_KEYS},
        'entities': copy.deepcopy({e['id']: e for e in engine.entities.values()}),
        'rng': rng_digest(engine),
    }


def mutation_snapshot(engine):
    """Снимок без invariants: только отметки отчёта разрешено писать."""
    state = snapshot(engine)
    state['report'].pop('invariants', None)
    return state


def assert_same(left, right, path='state'):
    """Точное сравнение: значения, типы и порядок ключей."""
    if isinstance(left, dict) and isinstance(right, dict):
        assert list(left.keys()) == list(right.keys()), path+'.keys'
        for key in left:
            assert_same(left[key], right[key], path+'['+repr(key)+']')
        return
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        assert type(left) is type(right), path+'.type'
        assert len(left) == len(right), path+'.length'
        for index, (a, b) in enumerate(zip(left, right)):
            assert_same(a, b, path+'['+str(index)+']')
        return
    if isinstance(left, bool) or isinstance(right, bool):
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        assert type(left) is type(right), path+'.type'
        if isinstance(left, float) and left != left:
            # nan не равен себе: сравниваем класс и «оба nan».
            assert right != right, path
            return
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def outcome(engine):
    """Результат ветки: None либо текст ValueError."""
    try:
        settlement_boundary.validate(engine, EPS)
    except ValueError as error:
        return str(error)
    return None


def outcome_python(engine):
    try:
        settlement_boundary.validate_python(engine, EPS)
    except ValueError as error:
        return str(error)
    return None


def assert_pair(engine):
    """Эталон и ядро на одинаковых копиях: тот же результат и тот же снимок."""
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    assert outcome_python(reference) == outcome(native)
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


# ---------------------------------------------------------------------------
# Пустой мир и один счёт
# ---------------------------------------------------------------------------

def test_empty_world_has_no_checks_to_fail():
    """Пустой мир: нет групп, строк и счетов — проверка проходит."""
    engine = empty_engine()
    assert outcome(engine) is None
    reference, native = assert_pair(engine)
    assert native.report['invariants'] == {'population': True, 'migration': True}


def test_empty_world_writes_invariants_exactly_once():
    """Отметки инвариантов пишутся ровно один раз и ровно в том же месте."""
    engine = empty_engine()
    calls = []
    original = settlement_boundary.SettlementHooks.mark_population_migration

    def traced(self):
        calls.append(len(self.engine.report['invariants']))
        return original(self)

    settlement_boundary.SettlementHooks.mark_population_migration = traced
    try:
        settlement_boundary.validate(engine, EPS)
    finally:
        settlement_boundary.SettlementHooks.mark_population_migration = original
    assert calls == [0]


def test_single_account_passes():
    """Один счёт без резервов: проверка кассы проходит."""
    engine = build_engine()
    engine.accounts = {}
    engine.accounts['only'] = dict(id='only', kind='household', balance=5.0,
        reserved=0.0, stock={}, values={}, stock_reserved={}, state_id='s',
        province_id='p')
    engine.__dict__.pop('_cash_store', None)
    assert outcome(engine) is None
    reference, native = assert_pair(engine)
    assert native.report['invariants']['population'] is True


def test_single_account_reserved_above_balance_fails():
    """Резерв больше баланса — точный прежний текст ошибки."""
    engine = build_engine()
    engine.accounts['h']['reserved'] = engine.accounts['h']['balance']+1.0
    assert outcome(engine) == 'Нарушен резерв средств'
    assert_pair(engine)


def test_single_account_negative_reserved_fails():
    engine = build_engine()
    engine.accounts['h']['reserved'] = -1.0
    assert outcome(engine) == 'Нарушен резерв средств'
    assert_pair(engine)


def test_single_account_non_finite_reserved_fails():
    """nan не является конечным: та же ветка, что и в прежнем any(...)."""
    engine = build_engine()
    engine.accounts['h']['reserved'] = float('nan')
    assert outcome(engine) == 'Нарушен резерв средств'
    assert_pair(engine)


def test_protected_counts_toward_cash_reserve():
    """reserved+protected вместе не могут превысить баланс."""
    engine = build_engine()
    a = engine.accounts['h']
    a['reserved'] = 1.0
    a['protected'] = a['balance']
    assert outcome(engine) == 'Нарушен резерв средств'
    assert_pair(engine)


def test_public_project_locks_are_checked():
    """Материалы публичной стройки входят в проверку резерва товаров."""
    engine = build_engine()
    a = engine.accounts['municipal:c']
    a['public_projects'] = [dict(materials={'planks': 10}, equipment_held={},
                                 support_stock={})]
    a['stock']['planks'] = 4
    a['stock_reserved'] = {'planks': 0}
    assert outcome(engine) == 'Нарушен резерв товаров'
    assert_pair(engine)


# ---------------------------------------------------------------------------
# Несколько счетов
# ---------------------------------------------------------------------------

def test_multiple_accounts_all_checked():
    """Каждый счёт проверяется: отказ на последнем виден так же."""
    engine = build_engine()
    for identity, balance in (('a', 5.0), ('b', 6.0), ('c', 7.0)):
        engine.accounts[identity] = dict(id=identity, kind='household', balance=balance,
            reserved=0.0, stock={}, values={}, stock_reserved={}, state_id='s',
            province_id='p')
    engine.accounts['c']['reserved'] = 100.0
    assert outcome(engine) == 'Нарушен резерв средств'
    assert_pair(engine)


def test_multiple_accounts_first_failure_wins():
    """Порядок обхода: побеждает первая же непрошедшая проверка."""
    engine = build_engine()
    ordered = {}
    for identity in ('a', 'b', 'c'):
        ordered[identity] = dict(id=identity, kind='household', balance=1.0,
            reserved=0.0, stock={}, values={}, stock_reserved={}, state_id='s',
            province_id='p')
    ordered['a']['stock_reserved'] = {'bread': 5.0}
    ordered['a']['stock'] = {'bread': 0.0}
    ordered['b']['reserved'] = 10.0
    engine.accounts = ordered
    engine.__dict__.pop('_cash_store', None)
    # 'a' идёт раньше 'b': отказ по товарам, а не по кассе.
    assert outcome(engine) == 'Нарушен резерв товаров'
    assert_pair(engine)


def test_account_order_follows_dict_iteration():
    """Обратный порядок вставки меняет, какой счёт откажет первым."""
    engine = build_engine()
    ordered = {}
    for identity in ('b', 'a'):
        ordered[identity] = dict(id=identity, kind='household', balance=1.0,
            reserved=0.0, stock={}, values={}, stock_reserved={}, state_id='s',
            province_id='p')
    ordered['a']['stock_reserved'] = {'bread': 5.0}
    ordered['a']['stock'] = {'bread': 0.0}
    ordered['b']['reserved'] = 10.0
    engine.accounts = ordered
    engine.__dict__.pop('_cash_store', None)
    assert outcome(engine) == 'Нарушен резерв средств'
    assert_pair(engine)


# ---------------------------------------------------------------------------
# Population: границы и крайние случаи
# ---------------------------------------------------------------------------

def test_population_must_be_int():
    engine = build_engine()
    engine.entities['h']['properties']['population'] = 1000.5
    assert outcome(engine) == 'Нарушена численность социальной группы'
    assert_pair(engine)


def test_population_bool_is_rejected():
    """bool — подкласс int, но численностью не является."""
    engine = build_engine()
    engine.entities['h']['properties']['population'] = True
    assert outcome(engine) == 'Нарушена численность социальной группы'
    assert_pair(engine)


def test_population_negative_is_rejected():
    engine = build_engine()
    engine.entities['h']['properties']['population'] = -1
    assert outcome(engine) == 'Нарушена численность социальной группы'
    assert_pair(engine)


def test_population_zero_is_allowed():
    engine = build_engine()
    engine.entities['h']['properties']['population'] = 0
    assert outcome(engine) is None
    assert_pair(engine)


def test_entity_without_population_is_skipped():
    """Группа без population и не-группы не проверяются вовсе."""
    engine = build_engine()
    engine.entities['h']['properties'].pop('population')
    engine.entities['x'] = dict(id='x', kind='City', name='X',
                                properties={'population': 'строка'})
    assert outcome(engine) is None
    assert_pair(engine)


# ---------------------------------------------------------------------------
# Переселение и демография
# ---------------------------------------------------------------------------

def test_migration_balance_mismatch_fails():
    """Сумма migration_out не сошлась с суммой people."""
    engine = build_engine()
    engine.report['demography'] = [demography_row(migration_out=5),
                                   demography_row(migration_out=1)]
    engine.report['migration'] = [migration_row(people=5)]
    assert outcome(engine) == 'Нарушен баланс переселения'
    assert_pair(engine)


def test_migration_balance_matches():
    """Переселение сходится: out у источника, in у назначения, people в книге."""
    engine = build_engine()
    engine.report['demography'] = [demography_row(before=100, migration_out=5),
                                   demography_row(before=0, migration_in=5)]
    engine.report['migration'] = [migration_row(people=5)]
    assert outcome(engine) is None
    assert_pair(engine)


def test_demography_change_must_be_explained():
    engine = build_engine()
    engine.report['demography'] = [demography_row(before=100, after=120,
                                                  births=1.0, deaths=0.0)]
    assert outcome(engine) == 'Изменение численности не объяснено'
    assert_pair(engine)


def test_demography_rounding_adjustment_is_used():
    engine = build_engine()
    engine.report['demography'] = [demography_row(before=100, after=101,
                                                  births=0.6, deaths=0.0,
                                                  rounding=0.4)]
    assert outcome(engine) is None
    assert_pair(engine)


def test_invalid_migration_row_fails():
    """people <= 0 — недопустимое переселение."""
    engine = build_engine()
    engine.report['demography'] = [demography_row(migration_out=0)]
    engine.report['migration'] = [migration_row(people=0)]
    assert outcome(engine) == 'Недопустимое переселение'
    assert_pair(engine)


def test_migration_across_states_fails():
    """Переселение между государствами запрещено."""
    engine = build_engine()
    engine.owners['p2'] = 'other'
    engine.report['demography'] = [demography_row(before=100, migration_out=5),
                                   demography_row(before=0, migration_in=5)]
    engine.report['migration'] = [migration_row(people=5, destination='p2')]
    assert outcome(engine) == 'Недопустимое переселение'
    assert_pair(engine)


def test_population_checked_before_migration():
    """Численность проверяется раньше переселения: её отказ побеждает."""
    engine = build_engine()
    engine.entities['h']['properties']['population'] = -1
    engine.report['migration'] = [migration_row(people=0)]
    assert outcome(engine) == 'Нарушена численность социальной группы'


def test_migration_checked_before_cash():
    """Переселение проверяется раньше кассы: его отказ побеждает."""
    engine = build_engine()
    engine.accounts['h']['reserved'] = 10**9
    engine.report['migration'] = [migration_row(people=0)]
    assert outcome(engine) == 'Недопустимое переселение'


def test_invariants_marked_before_cash_failure():
    """При отказе по кассе отметки population/migration уже стоят."""
    engine = build_engine()
    engine.accounts['h']['reserved'] = 10**9
    engine.report['invariants'] = {}
    with pytest.raises(ValueError):
        settlement_boundary.validate(engine, EPS)
    assert engine.report['invariants'] == {'population': True, 'migration': True}


def test_invariants_not_marked_on_migration_failure():
    """При отказе по переселению отметок ещё нет — как в прежнем коде."""
    engine = build_engine()
    engine.report['migration'] = [migration_row(people=0)]
    engine.report['invariants'] = {}
    with pytest.raises(ValueError):
        settlement_boundary.validate(engine, EPS)
    assert engine.report['invariants'] == {}


# ---------------------------------------------------------------------------
# int/float-семантика
# ---------------------------------------------------------------------------

def test_int_reserved_and_int_balance_stay_int():
    """int-поля остаются int: сравнение не приводит их к double."""
    engine = build_engine()
    a = engine.accounts['h']
    a['balance'] = 100
    a['reserved'] = 40
    a['protected'] = 40
    assert isinstance(a['balance'], int)
    assert outcome(engine) is None
    reference, native = assert_pair(engine)
    row = native.accounts['h']
    assert type(row['balance']) is int and type(row['reserved']) is int


def test_float_reserved_keeps_float_arithmetic():
    engine = build_engine()
    a = engine.accounts['h']
    a['balance'] = 100.5
    a['reserved'] = 100.5
    assert outcome(engine) is None
    reference, native = assert_pair(engine)
    assert type(native.accounts['h']['reserved']) is float


def test_float_sum_uses_compensated_builtin_sum():
    """Агрегат переселения считает встроенный sum, а не ручное накопление.

    Слагаемые подобраны так, чтобы ручное ``+=`` и встроенный ``sum`` дали
    разное: ``(1e16+1.0)-1e16`` теряет единицу при наивном накоплении, а
    компенсированный ``sum`` Python 3.12 возвращает 1.0. С наивным накоплением
    баланс переселения не сошёлся бы (0.0 против 1.0), с ``sum`` — сходится.
    """
    engine = build_engine()
    amounts = (1e16, 1.0, -1e16)
    engine.report['demography'] = [
        demography_row(before=100.0, births=0.0, migration_in=value, migration_out=value)
        for value in amounts]
    engine.report['migration'] = [migration_row(people=1.0)]
    naive = 0
    for value in amounts:
        naive += value
    assert naive != 1.0, 'признак компенсации потерян'
    assert sum(amounts) == 1.0
    # С sum() баланс сходится; наивный обход дал бы отказ по балансу.
    assert outcome(engine) is None
    assert_pair(engine)


def test_eps_boundary_is_respected():
    """Допуск EPS: разница ровно на границе ещё проходит, чуть больше — нет."""
    engine = build_engine()
    engine.report['demography'] = [demography_row(migration_out=EPS)]
    engine.report['migration'] = [migration_row(people=0.0+0.0)]
    # people=0 отбраковывается раньше; проверяем именно допуск через demography.
    engine.report['migration'] = []
    engine.report['demography'] = [demography_row(before=1.0, after=1.0+EPS/2,
                                                  births=0.0, deaths=0.0)]
    assert outcome(engine) is None
    engine.report['demography'] = [demography_row(before=1.0, after=1.0+EPS*2,
                                                  births=0.0, deaths=0.0)]
    assert outcome(engine) == 'Изменение численности не объяснено'
    assert_pair(engine)


# ---------------------------------------------------------------------------
# Read-only: side effects отсутствуют
# ---------------------------------------------------------------------------

def test_no_side_effects_on_success():
    """Успешная валидация не трогает счета, отчёт (кроме invariants), RNG."""
    engine = build_engine()
    engine.report['demography'] = [demography_row()]
    engine.report['migration'] = []
    before = mutation_snapshot(engine)
    assert outcome(engine) is None
    after = mutation_snapshot(engine)
    assert_same(before, after)


def test_no_side_effects_on_failure():
    """Отказ тоже ничего не мутирует: только исключение."""
    engine = build_engine()
    engine.accounts['h']['reserved'] = 10**9
    before = mutation_snapshot(engine)
    assert outcome(engine) == 'Нарушен резерв средств'
    assert_same(before, mutation_snapshot(engine))


def test_no_orders_stock_or_trades_created():
    """Ядро не создаёт заявок, не двигает склад и не пишет сделок."""
    engine = build_engine()
    orders, trades = copy.deepcopy(engine.report['orders']), copy.deepcopy(engine.report['trades'])
    stocks = copy.deepcopy({aid: dict(a['stock']) for aid, a in engine.accounts.items()})
    settlement_boundary.validate(engine, EPS)
    assert engine.report['orders'] == orders
    assert engine.report['trades'] == trades
    assert {aid: dict(a['stock']) for aid, a in engine.accounts.items()} == stocks


def test_hooks_called_once():
    """Хуки вызываются ровно по одному разу: total три раза, отметки один."""
    engine = build_engine()
    calls = {'total': 0, 'mark': 0}
    original_total = settlement_boundary.SettlementHooks.total
    original_mark = settlement_boundary.SettlementHooks.mark_population_migration

    def total(self, values):
        calls['total'] += 1
        return original_total(self, values)

    def mark(self):
        calls['mark'] += 1
        return original_mark(self)

    settlement_boundary.SettlementHooks.total = total
    settlement_boundary.SettlementHooks.mark_population_migration = mark
    try:
        settlement_boundary.validate(engine, EPS)
    finally:
        settlement_boundary.SettlementHooks.total = original_total
        settlement_boundary.SettlementHooks.mark_population_migration = original_mark
    assert calls == {'total': 3, 'mark': 1}


# ---------------------------------------------------------------------------
# deepcopy, граница и fallback
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = build_engine()
    engine.accounts['h']['reserved'] = 10**9
    before = snapshot(engine)
    clone = copy.deepcopy(engine)
    clone.accounts['h']['reserved'] = 0.0
    assert outcome(clone) is None
    assert outcome(engine) == 'Нарушен резерв средств'
    assert snapshot(engine)['accounts'] == before['accounts']
    left, right = copy.deepcopy(engine), copy.deepcopy(engine)
    left.accounts['h']['reserved'] = 0.0
    right.accounts['h']['reserved'] = 0.0
    assert outcome_python(left) == outcome(right)
    assert_same(snapshot(left), snapshot(right))


def test_boundary_exposes_native_when_built():
    built = settlement_boundary.load_settlement_kernel() is not None
    assert settlement_boundary.NATIVE_KERNEL is built
    if built:
        assert settlement_boundary.run_validate.__module__.endswith('settlement_kernel')
        assert settlement_boundary.validate.__module__.endswith('settlement_boundary')
    else:
        assert settlement_boundary.run_validate is settlement_boundary.run_validate_python


def test_native_entry_points_are_compiled():
    kernel = settlement_boundary.load_settlement_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение settlement_kernel не собрано')
    assert settlement_boundary.run_validate is kernel.run_validate
    assert settlement_boundary.run_validate is not settlement_boundary.run_validate_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('settlement_kernel'):
            raise ImportError("cannot import name 'settlement_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert settlement_boundary.load_settlement_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = build_engine()
    expected = mutation_snapshot(engine)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('settlement_kernel'):
            raise ImportError("cannot import name 'settlement_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(settlement_boundary)
    try:
        assert settlement_boundary.NATIVE_KERNEL is False
        assert settlement_boundary.run_validate is settlement_boundary.run_validate_python
        assert settlement_boundary.validate.__module__.endswith('settlement_boundary')
        fallback = copy.deepcopy(engine)
        assert settlement_boundary.validate(fallback, EPS) is None
        assert_same(expected, mutation_snapshot(fallback))
        # Отказ тоже совпадает по тексту.
        fallback.accounts['h']['reserved'] = 10**9
        with pytest.raises(ValueError) as error:
            settlement_boundary.validate(fallback, EPS)
        assert str(error.value) == 'Нарушен резерв средств'
    finally:
        monkeypatch.undo()
        importlib.reload(settlement_boundary)


def test_v3_has_single_call_site():
    """В validate один вызов границы, а не вторая копия цикла."""
    source = inspect.getsource(Economy.validate)
    assert '_settlement.validate(self, EPS)' in source
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_settlement.validate(') == 1
    # Прежний цикл проверок в v3 больше не дублируется.
    assert "raise ValueError('Нарушен резерв товаров')" not in module_source
    assert "raise ValueError('Нарушен резерв средств')" not in module_source
    assert "raise ValueError('Недопустимое переселение')" not in module_source


# ---------------------------------------------------------------------------
# RNG и forced-case
# ---------------------------------------------------------------------------

def test_rng_equal_between_branches():
    """Валидация не трогает RNG: генератор одинаков до и после обеих веток."""
    engine = build_engine()
    engine.report['demography'] = [demography_row()]
    before = rng_digest(engine)
    reference, native = copy.deepcopy(engine), copy.deepcopy(engine)
    settlement_boundary.validate_python(reference, EPS)
    settlement_boundary.validate(native, EPS)
    assert rng_digest(reference) == rng_digest(native)
    assert rng_digest(native) == before, 'валидация не должна тратить RNG'


def test_forced_case_detects_real_violation():
    """Явный сценарий: сломанный резерв товаров реально ловится."""
    engine = build_engine()
    a = engine.accounts['h']
    a['stock']['bread'] = 2.0
    a['stock_reserved']['bread'] = 5.0
    with pytest.raises(ValueError) as error:
        settlement_boundary.validate(engine, EPS)
    assert str(error.value) == 'Нарушен резерв товаров'
    reference = copy.deepcopy(engine)
    with pytest.raises(ValueError) as reference_error:
        settlement_boundary.validate_python(reference, EPS)
    assert str(reference_error.value) == str(error.value)


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

REPLAY_17 = (
    ('63c125fe74ded9d8f0e768db932be5ca2932d5eb718fad9803b0f104964ccad2',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('ce582a929bc6b1340af8c3289fe060a38e1fc263d34aab2ac46e1a8cf3b02b63',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('179b96c8a3bf101f403fae810aeed943d8437d3f61f42fee75bd9fd7314f95c6',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('48fa2439a6951ce1e99c079b322150f20a0c2f8d862a90843f23ddc1629bd191',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('f66ae92a98e7fbf25634103cfb5cd0d53c518c28c5007d8a3b7eda7ca689f700',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('611113176efd1800011c2fafda290e6670eff3cdd64c3a6054c3df656a5a990b',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def replay_digest(engine):
    """Digest состояния, отчёта и инвариантов за месяц."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'orders': engine.report['orders'],
        'trades': engine.report['trades'],
        'invariants': engine.report.get('invariants'),
        'demography': engine.report.get('demography'),
        'migration': engine.report.get('migration'),
        'top_up': engine.report.get('household_top_up'),
        'consumed': engine.report.get('consumed'),
        'needs': {a['id']: a.get('needs') for a in engine.accounts.values()},
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_settlement_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='settle-replay-%d-%d' % (seed, index),
                                     source='audit')
                engine = service.economy_state().engine
                rows.append((replay_digest(engine), rng_digest(engine)))
        finally:
            db.close()
    return rows


@pytest.fixture(scope='module')
def replay_seed_17():
    return run_replay(17)


@pytest.fixture(scope='module')
def replay_seed_73129():
    return run_replay(73129)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_17(replay_seed_17, index):
    state, rng = replay_seed_17[index]
    assert (state, rng) == REPLAY_17[index], 'месяц %d' % (index+1)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_73129(replay_seed_73129, index):
    state, rng = replay_seed_73129[index]
    assert (state, rng) == REPLAY_73129[index], 'месяц %d' % (index+1)


def test_replay_is_deterministic(replay_seed_17, replay_seed_73129):
    for rows in (replay_seed_17, replay_seed_73129):
        assert len(rows) == 3
        assert all(rng for _, rng in rows)
    assert run_replay(17) == list(replay_seed_17)


def test_replay_actually_validates_world():
    """Exact replay не должен проходить на пустой проверке."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    counts = {'groups': 0, 'accounts': 0, 'demography': 0}
    original = Economy.validate

    def traced(self, before=None):
        counts['groups'] += sum(1 for e in self.entities.values()
            if e['kind'] == 'SocialGroup' and 'population' in e['properties'])
        counts['accounts'] += len(self.accounts)
        counts['demography'] += len(self.report.get('demography', []))
        return original(self, before)

    Economy.validate = traced
    try:
        with workdir('test_settlement_counts') as directory:
            db, service = sr.build_world(73129, directory, report_detail='standard')
            try:
                for index in range(3):
                    service.advance_turn(command_id='settle-count-%d' % index, source='audit')
            finally:
                db.close()
    finally:
        Economy.validate = original
    assert counts['groups'] > 0, 'группы населения не проверялись'
    assert counts['accounts'] > 0, 'счета не проверялись'
    assert counts['demography'] > 0, 'демография не проверялась'
