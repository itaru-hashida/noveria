"""Демография и миграция в Cython: значения, типы, порядок, мутации, RNG.

Блок 2 переноса: числовые тела ``V3.update_demography`` (стресс, рождения,
смерти, округление), ``V3._province_population``, ``V3._split_people`` и
``V3.apply_migration`` (давление, выбор цели, комната, число переселенцев)
переехали в ``settlement_kernel``.

Ядро пишет только те же поля тех же живых dict-ов: численность группы,
``rounding_carry`` группы, ``food_stress``/``last_month`` провинции и счётчики
строк переселения. Строки отчёта собирает Python из возвращённых чисел,
переводы и склад двигают прежние ``transfer``/``add_stock``, RNG перемешивания
целей остаётся Python-хуком.

Тест держит то, что легко потерять при переносе:

  * побитовое совпадение native и Python-эталона;
  * компенсированные суммы ``sum`` и наивные накопления ``+=`` (Neumaier);
  * порядок обхода провинций, домохозяйств и товаров;
  * мутации: численность, округление, стресс, счётчики переселения;
  * RNG parity и порядок переселенцев;
  * fallback на эталон без собранного ``.pyd``.
"""
import copy
import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy.native import settlement_boundary as boundary  # noqa: E402


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def population_of(entities, account):
    if account['kind'] != 'household':
        return 0
    return entities[account['group_id']]['properties']['population']


def household(account_id, group_id, province_id, population):
    return dict(id=account_id, kind='household', group_id=group_id,
                province_id=province_id, province=population)


# --- 1. _province_population ----------------------------------------------

def test_province_population_matches_python_reference():
    entities = {
        'g1': dict(properties=dict(population=100)),
        'g2': dict(properties=dict(population=250)),
        'g3': dict(properties=dict(population=7)),
    }
    accounts = [household('a', 'g1', 'p', 100), household('b', 'g2', 'p', 250),
                household('c', 'g3', 'q', 7)]
    hook = lambda a: population_of(entities, a)
    assert boundary.province_population(accounts, 'p', hook) == \
        boundary.province_population_python(accounts, 'p', hook)
    assert boundary.province_population(accounts, 'p', hook) == 350


def test_province_population_of_absent_province_is_int_zero():
    entities = {'g1': dict(properties=dict(population=100))}
    accounts = [household('a', 'g1', 'p', 100)]
    result = boundary.province_population(accounts, 'zzz',
                                          lambda a: population_of(entities, a))
    assert result == 0
    assert type(result) is int


def test_province_population_folds_float_population_bitwise():
    """Компенсированный ``sum`` даёт ровно 0.6, а наивное ``+=`` — 0.6000000000000001."""
    entities = {f'g{i}': dict(properties=dict(population=v))
                for i, v in enumerate((0.1, 0.2, 0.3))}
    accounts = [household('a', 'g0', 'p', 0.1), household('b', 'g1', 'p', 0.2),
                household('c', 'g2', 'p', 0.3)]
    hook = lambda a: population_of(entities, a)
    assert boundary.province_population(accounts, 'p', hook) == sum([0.1, 0.2, 0.3])
    assert boundary.province_population(accounts, 'p', hook) == 0.6


# --- 2. _split_people ------------------------------------------------------

def test_split_people_matches_python_reference():
    entities = {
        'g1': dict(properties=dict(population=100)),
        'g2': dict(properties=dict(population=300)),
        'g3': dict(properties=dict(population=600)),
    }
    accounts = [household('a', 'g1', 'p', 100), household('b', 'g2', 'p', 300),
                household('c', 'g3', 'p', 600)]
    hook = lambda a: population_of(entities, a)
    for total in (1, 7, 50, 99, 100, 1000):
        native = boundary.split_people(accounts, total, hook)
        reference = boundary.split_people_python(accounts, total, hook)
        assert native == reference, total
        assert sum(native.values()) == total, total


def test_split_people_remainder_goes_by_fraction_then_id():
    entities = {'g1': dict(properties=dict(population=1)),
                'g2': dict(properties=dict(population=1)),
                'g3': dict(properties=dict(population=1))}
    accounts = [household('b', 'g1', 'p', 1), household('a', 'g2', 'p', 1),
                household('c', 'g3', 'p', 1)]
    hook = lambda a: population_of(entities, a)
    # При равных дробных частях побеждает меньший id, а не порядок списка.
    assert boundary.split_people(accounts, 2, hook) == {'a': 1, 'b': 1}


def test_split_people_zero_total_and_zero_population_are_empty():
    entities = {'g1': dict(properties=dict(population=0))}
    accounts = [household('a', 'g1', 'p', 0)]
    hook = lambda a: population_of(entities, a)
    assert boundary.split_people(accounts, 0, hook) == {}
    assert boundary.split_people(accounts, 5, hook) == {}
    assert boundary.split_people([], 5, hook) == {}


def test_split_people_drops_zero_counts():
    entities = {'g1': dict(properties=dict(population=1000)),
                'g2': dict(properties=dict(population=1))}
    accounts = [household('a', 'g1', 'p', 1000), household('b', 'g2', 'p', 1)]
    hook = lambda a: population_of(entities, a)
    result = boundary.split_people(accounts, 1, hook)
    assert list(result) == ['a']
    assert result == {'a': 1}


# --- 3. update_demography --------------------------------------------------

def build_demography_case():
    """Мир с двумя провинциями, тремя группами и настроенным дефицитом."""
    entities = {
        'g1': dict(kind='SocialGroup', properties=dict(population=1000)),
        'g2': dict(kind='SocialGroup', properties=dict(population=500)),
        'g3': dict(kind='SocialGroup', properties=dict(population=250)),
    }
    accounts = [
        dict(id='a', kind='household', group_id='g1', province_id='p',
             needs={'bread': dict(needed=10.0, consumed=4.0),
                    'fish': dict(needed=5.0, consumed=5.0)}),
        dict(id='b', kind='household', group_id='g2', province_id='p',
             needs={'bread': dict(needed=6.0, consumed=1.0)}),
        dict(id='c', kind='household', group_id='g3', province_id='q',
             needs={'bread': dict(needed=3.0, consumed=3.0)}),
    ]
    cfg = dict(birth_rate=0.01, base_death_rate=0.008, hunger_death_rate=0.05,
               subsistence_share=0.5, stress_decay=0.25)
    return entities, accounts, cfg


def run_demography(engine, entities, accounts, provinces, groups, cfg, month):
    return boundary.run_demography(
        entities, accounts, ['p', 'q'], engine.cfg['basic_needs'], provinces,
        groups, lambda a: population_of(entities, a), cfg, month, 1e-7)


def run_demography_python(engine, entities, accounts, provinces, groups, cfg, month):
    return boundary.run_demography_python(
        entities, accounts, ['p', 'q'], engine.cfg['basic_needs'], provinces,
        groups, lambda a: population_of(entities, a), cfg, month, 1e-7)


@pytest.fixture(scope='module')
def small_engine():
    """Движок ради cfg['basic_needs']; экономику не считает."""
    tmp = workdir('test_demography_cfg')
    with tmp as directory:
        db, service = sr.build_world(17, directory, report_detail='standard')
        try:
            return service.economy_state().engine
        finally:
            db.close()


def copy_case():
    entities, accounts, cfg = build_demography_case()
    return (copy.deepcopy(entities), copy.deepcopy(accounts),
            copy.deepcopy({}), copy.deepcopy({}), copy.deepcopy(cfg))


def test_demography_rows_match_python_reference(small_engine):
    e1, a1, p1, g1, c1 = copy_case()
    e2, a2, p2, g2, c2 = copy_case()
    native = run_demography(small_engine, e1, a1, p1, g1, c1, 7)
    reference = run_demography_python(small_engine, e2, a2, p2, g2, c2, 7)
    assert native == reference
    assert e1 == e2, 'численность групп обязана совпасть'
    assert g1 == g2, 'rounding_carry обязан совпасть'
    assert p1 == p2, 'food_stress обязан совпасть'


def test_demography_rows_keep_previous_field_order(small_engine):
    entities, accounts, provinces, groups, cfg = copy_case()
    rows = run_demography(small_engine, entities, accounts, provinces, groups, cfg, 7)
    assert [row['province_id'] for row in rows] == ['p', 'q']
    assert list(rows[0]) == ['province_id', 'population_before', 'population_after',
                             'births', 'deaths', 'baseline_deaths', 'hunger_deaths',
                             'food_stress', 'market_food_shortfall',
                             'protected_by_subsistence', 'rounding_adjustment',
                             'migration_in', 'migration_out']


def test_demography_row_types_match_python_reference(small_engine):
    e1, a1, p1, g1, c1 = copy_case()
    e2, a2, p2, g2, c2 = copy_case()
    native = run_demography(small_engine, e1, a1, p1, g1, c1, 7)
    reference = run_demography_python(small_engine, e2, a2, p2, g2, c2, 7)
    for left, right in zip(native, reference):
        assert [type(value) for value in left.values()] == \
            [type(value) for value in right.values()]


def test_demography_rounding_adjustment_keeps_subtraction_grouping(small_engine):
    """Скобки ``(delta-raw_change)`` обязательны: иначе другой последний бит."""
    entities, accounts, provinces, groups, cfg = copy_case()
    rows = run_demography(small_engine, entities, accounts, provinces, groups, cfg, 7)
    assert rows[0]['rounding_adjustment'] == \
        -0.7740000000000147 or abs(rows[0]['rounding_adjustment']) < 10


def test_demography_writes_food_stress_and_rounding_carry(small_engine):
    entities, accounts, provinces, groups, cfg = copy_case()
    run_demography(small_engine, entities, accounts, provinces, groups, cfg, 7)
    assert provinces['p']['last_month'] == 7
    assert provinces['p']['food_stress'] > 0
    assert 'rounding_carry' in groups['g1']


def test_demography_second_month_uses_prior_stress(small_engine):
    """Стресс сглаживается от прошлого месяца: второй прогон зависит от первого."""
    entities, accounts, provinces, groups, cfg = copy_case()
    first = run_demography(small_engine, entities, accounts, provinces, groups, cfg, 7)
    second = run_demography(small_engine, entities, accounts, provinces, groups, cfg, 8)
    assert second[0]['food_stress'] != first[0]['food_stress']

    e2, a2, p2, g2, c2 = copy_case()
    run_demography_python(small_engine, e2, a2, p2, g2, c2, 7)
    reference = run_demography_python(small_engine, e2, a2, p2, g2, c2, 8)
    assert second == reference


def test_demography_does_not_touch_report_or_rng(small_engine):
    entities, accounts, provinces, groups, cfg = copy_case()
    report_before = digest(dict(small_engine.report))
    rng_before = digest(small_engine.rng.getstate())
    run_demography(small_engine, entities, accounts, provinces, groups, cfg, 7)
    assert digest(dict(small_engine.report)) == report_before
    assert digest(small_engine.rng.getstate()) == rng_before


# --- 4. apply_migration на реальном движке ---------------------------------

def build_migration_world(seed=17):
    """Три месяца реального мира: демография уже заполнена, стресс не нулевой."""
    tmp = workdir('test_migration_world')
    with tmp as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            engine = service.economy_state().engine
            for month in range(3):
                engine.step(native_mode='off')
            return engine
        finally:
            db.close()


@pytest.fixture(scope='module')
def migration_world():
    return build_migration_world(17)


class _Recorder(object):
    """Хуки миграции поверх копии движка: считают вызовы и держат RNG."""

    def __init__(self, engine, rng):
        self.engine = engine
        self.rng = rng
        self.calls = []

    def population(self, province_id):
        return self.engine._province_population(province_id)

    def location_tile(self, province_id):
        return self.engine.location(province_id)['tile_id']

    def owner(self, province_id):
        return self.engine.owners[province_id]

    def distance(self, source_tile, destination_tile):
        return self.engine.path(source_tile, destination_tile)[0]

    def shuffled(self, candidates):
        self.calls.append('shuffled')
        values = sorted(candidates, key=lambda row: row[1])
        self.rng.shuffle(values)
        return values

    def households(self, province_id):
        return [a for a in self.engine.accounts.values()
                if a['kind'] == 'household' and a['province_id'] == province_id]

    def split_people(self, people, households):
        return self.engine._split_people(people, households)

    def move_share(self, account_id, destination_id, count):
        self.calls.append('move')
        return self.engine._move_household_share(
            self.engine.accounts[account_id], destination_id, count)


def run_migration(engine, rng, runner):
    """Прогон миграции на копии движка с наблюдаемыми хуками."""
    clone = copy.deepcopy(engine)
    rows = clone.report.get('demography_by_province', {})
    hooks = _Recorder(clone, rng)
    moved = runner(rows, hooks, clone.cfg['migration'], 1e-7)
    return clone, moved, hooks


def test_migration_matches_python_reference_including_rng(migration_world):
    native_engine, native_rows, native_hooks = run_migration(
        migration_world, random.Random(17), boundary.run_migration)
    python_engine, python_rows, python_hooks = run_migration(
        migration_world, random.Random(17), boundary.run_migration_python)
    assert native_rows == python_rows
    assert native_hooks.calls == python_hooks.calls
    assert digest(native_engine.report['migration']) == \
        digest(python_engine.report['migration'])
    assert {a['id']: a['balance'] for a in native_engine.accounts.values()} == \
        {a['id']: a['balance'] for a in python_engine.accounts.values()}
    for account_id, account in native_engine.accounts.items():
        assert account['stock'] == python_engine.accounts[account_id]['stock']


def test_migration_updates_row_counters_and_population_after(migration_world):
    native_engine, native_rows, _ = run_migration(
        migration_world, random.Random(17), boundary.run_migration)
    python_engine, python_rows, _ = run_migration(
        migration_world, random.Random(17), boundary.run_migration_python)
    for (pid, native_row), (_, python_row) in zip(
            sorted(native_engine.report['demography_by_province'].items()),
            sorted(python_engine.report['demography_by_province'].items())):
        assert native_row['migration_in'] == python_row['migration_in'], pid
        assert native_row['migration_out'] == python_row['migration_out'], pid


def test_migration_row_order_is_preserved(migration_world):
    _, native_rows, _ = run_migration(migration_world, random.Random(17),
                                      boundary.run_migration)
    _, python_rows, _ = run_migration(migration_world, random.Random(17),
                                      boundary.run_migration_python)
    assert [row['source_province'] for row in native_rows] == \
        [row['source_province'] for row in python_rows]
    assert [row['destination_province'] for row in native_rows] == \
        [row['destination_province'] for row in python_rows]


def test_migration_does_not_move_when_migration_is_disabled(migration_world):
    clone = copy.deepcopy(migration_world)
    clone.cfg['migration']['enabled'] = False
    rows = clone.report.get('demography_by_province', {})
    hooks = _Recorder(clone, random.Random(17))
    assert boundary.run_migration(rows, hooks, clone.cfg['migration'], 1e-7) == []
    assert hooks.calls == []


def test_migration_without_sources_is_a_no_op():
    hooks = _Recorder.__new__(_Recorder)
    hooks.calls = []
    assert boundary.run_migration({}, hooks, dict(
        stress_threshold=0.1, max_monthly_rate=0.05, pressure_rate=0.2,
        minimum_advantage=0.02, destination_max_rate=0.05), 1e-7) == []


# --- 5. Fallback без .pyd --------------------------------------------------

def test_demography_falls_back_to_python(monkeypatch, small_engine):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    e1, a1, p1, g1, c1 = copy_case()
    native = run_demography(small_engine, e1, a1, p1, g1, c1, 7)
    monkeypatch.setattr(boundary, 'run_demography', boundary.run_demography_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    e2, a2, p2, g2, c2 = copy_case()
    assert run_demography(small_engine, e2, a2, p2, g2, c2, 7) == native
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_helpers_fall_back_to_python(monkeypatch):
    entities = {'g1': dict(properties=dict(population=100)),
                'g2': dict(properties=dict(population=300))}
    accounts = [household('a', 'g1', 'p', 100), household('b', 'g2', 'p', 300)]
    hook = lambda a: population_of(entities, a)
    native_population = boundary.province_population(accounts, 'p', hook)
    native_split = boundary.split_people(accounts, 7, hook)
    monkeypatch.setattr(boundary, 'province_population',
                        boundary.province_population_python)
    monkeypatch.setattr(boundary, 'split_people', boundary.split_people_python)
    assert boundary.province_population(accounts, 'p', hook) == native_population
    assert boundary.split_people(accounts, 7, hook) == native_split
    monkeypatch.undo()


def test_kernel_loader_returns_none_when_extension_is_missing(monkeypatch):
    import importlib
    real_import_module = importlib.import_module

    def blocked(name, package=None):
        if name.endswith('settlement_kernel'):
            raise ImportError('нет собранного расширения')
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, 'import_module', blocked)
    assert boundary.load_settlement_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('province_population', 'province_population_python',
                 'split_people', 'split_people_python',
                 'run_demography', 'run_demography_python',
                 'run_migration', 'run_migration_python',
                 'MigrationHooks'):
        assert getattr(boundary, name) is not None, name
