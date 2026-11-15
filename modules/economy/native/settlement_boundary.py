"""Граница settlement-ядра: read-only валидация счетов и населения.

В Cython уходит только чистый read-only цикл проверок ``Economy.validate``:

  * численность социальных групп;
  * агрегаты переселения и сверка изменения численности;
  * допустимость строк переселения;
  * инварианты кассы счёта и резерва товаров.

Ядро ничего не мутирует: ни заявок, ни склада, ни денег, ни отчёта. Ядро лишь
возвращает код первой неудачи; исключение с прежним текстом поднимает эта
граница, поэтому точные причины и момент отказа (short-circuit) те же.
Отметки ``report['invariants']`` — side effect отчёта, он тоже остался здесь и
выполняется хуком в тот же момент, что и в прежнем коде.

Агрегаты считает встроенный ``sum`` через хук ``total``: в Python 3.12 ``sum``
суммирует float с компенсацией погрешности, и ручное накопление в ядре дало бы
расхождение в последнем знаке.
"""
import importlib
import math

# Точные тексты прежнего кода: их видит игрок и тесты, поэтому они остаются
# здесь, а не в ядре.
REASONS = {
    'population': 'Нарушена численность социальной группы',
    'migration_balance': 'Нарушен баланс переселения',
    'demography': 'Изменение численности не объяснено',
    'migration': 'Недопустимое переселение',
    'cash_reserve': 'Нарушен резерв средств',
    'stock_reserve': 'Нарушен резерв товаров',
}


class SettlementHooks(object):
    """Побочные эффекты валидации, которые обязаны остаться в Python."""

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    def total(self, values):
        """Встроенный ``sum`` без начального значения — прежняя семантика."""
        return sum(values)

    def mark_population_migration(self):
        """Отметить population/migration сразу после их проверок."""
        self.engine.report['invariants'].update(population=True, migration=True)


def run_validate_python(engine, eps, hooks):
    """Эталон read-only цикла — буквальный прежний код ``Economy.validate``."""
    for entity in engine.entities.values():
        if entity['kind'] != 'SocialGroup' or 'population' not in entity['properties']:
            continue
        population = entity['properties']['population']
        if isinstance(population, bool) or not isinstance(population, int) or population < 0:
            raise ValueError('Нарушена численность социальной группы')
    demography = engine.report.get('demography', [])
    migration = engine.report.get('migration', [])
    moved_out = sum(row.get('migration_out', 0) for row in demography)
    moved_in = sum(row.get('migration_in', 0) for row in demography)
    moved = sum(row.get('people', 0) for row in migration)
    if abs(moved_out-moved) > eps or abs(moved_in-moved) > eps:
        raise ValueError('Нарушен баланс переселения')
    for row in demography:
        actual = row['population_after']-row['population_before']
        expected = (row['births']-row['deaths']+row.get('rounding_adjustment', 0)
                    +row.get('migration_in', 0)-row.get('migration_out', 0))
        if abs(actual-expected) > eps:
            raise ValueError('Изменение численности не объяснено')
    for row in migration:
        if (row['people'] <= 0 or engine.owners[row['source_province']] != row['state_id']
                or engine.owners[row['destination_province']] != row['state_id']):
            raise ValueError('Недопустимое переселение')
    hooks.mark_population_migration()
    for a in engine.accounts.values():
        if any(not _isfinite(a.get(k, 0)) or a.get(k, 0) < -eps
               for k in ('reserved', 'protected')) \
                or a.get('reserved', 0)+a.get('protected', 0) > a['balance']+eps:
            raise ValueError('Нарушен резерв средств')
        public_locks = set()
        for project in a.get('public_projects', []):
            public_locks |= (project.get('materials', {}).keys()
                | project.get('equipment_held', {}).keys()
                | project.get('support_stock', {}).keys())
        for g in a.get('stock_reserved', {}).keys() | a.get('project', {}).get('materials', {}).keys() | public_locks:
            q = a.get('stock_reserved', {}).get(g, 0)
            if q < -eps or q+engine.locked_material(a['id'], g) > a['stock'].get(g, 0)+eps:
                raise ValueError('Нарушен резерв товаров')


def _isfinite(value):
    """Ссылка на math.isfinite вынесена, чтобы не искать модуль в цикле."""
    return math.isfinite(value)


def validate(engine, eps):
    """Единственный вход из ``Economy.validate``: native-цикл или эталон.

    Отрицательный код причины превращается в ``ValueError`` с точным прежним
    текстом, поэтому вызывающий код и тесты видят то же исключение.
    """
    code = run_validate(engine, eps, SettlementHooks(engine))
    if code is not None:
        raise ValueError(REASONS[code])


def validate_python(engine, eps):
    """Эталонная Python-ветка целиком: те же аргументы и те же хуки."""
    run_validate_python(engine, eps, SettlementHooks(engine))


def load_settlement_kernel():
    """Вернуть native-модуль settlement_kernel или None, если он не собран.

    Отсутствие собранного расширения даёт ImportError, а не
    ModuleNotFoundError: пакет ``modules.economy.native`` существует, а
    подмодуля нет. Обе ситуации означают одно — ядро не собрано.
    """
    try:
        return importlib.import_module('.settlement_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_settlement_kernel()
if _kernel is not None:
    run_validate = _kernel.run_validate
    NATIVE_KERNEL = True
else:
    run_validate = run_validate_python
