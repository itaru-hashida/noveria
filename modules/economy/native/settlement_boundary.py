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


# ---------------------------------------------------------------------------
# Демография и миграция: числовые тела
# ---------------------------------------------------------------------------
#
# Перенесены расчётные срезы ``V3.update_demography`` и ``V3.apply_migration``
# (бывшие ``_province_population``/``_split_people``). Ядро пишет только те же
# поля тех же живых dict-ов: численность группы, ``rounding_carry`` группы,
# ``food_stress``/``last_month`` провинции. Строки отчёта собирает Python из
# возвращённых чисел, переводы и склад двигают прежние ``transfer``/``add_stock``.

def province_population_python(accounts, province_id, population_hook):
    """Эталон ``V3._province_population``: буквальный прежний код."""
    return sum(population_hook(a) for a in accounts
               if a['kind'] == 'household' and a['province_id'] == province_id)


def split_people_python(households, total, population_hook):
    """Эталон ``V3._split_people``: буквальный прежний код."""
    population = sum(population_hook(a) for a in households)
    if total <= 0 or population <= 0:
        return {}
    raw = [(a, total*population_hook(a)/population) for a in households]
    result = {a['id']: math.floor(value) for a, value in raw}
    left = total-sum(result.values())
    for account, value in sorted(raw, key=lambda row: (
            -(row[1]-math.floor(row[1])), row[0]['id']))[:left]:
        result[account['id']] += 1
    return {account: count for account, count in result.items() if count}


def run_demography_python(entities, accounts, province_ids, basic_goods,
                          provinces, groups, population_hook, cfg, month, eps):
    """Эталон числового тела ``V3.update_demography``: буквальный прежний код."""
    rows = []
    for pid in province_ids:
        households = [a for a in accounts
                      if a['kind'] == 'household' and a['province_id'] == pid]
        population = sum(population_hook(a) for a in households)
        needed = sum(a.get('needs', {}).get(good, {}).get('needed', 0)
                     for a in households for good in basic_goods)
        consumed = sum(a.get('needs', {}).get(good, {}).get('consumed', 0)
                       for a in households for good in basic_goods)
        market_shortfall = max(0, needed-consumed)/needed if needed > eps else 0.0
        protected = min(max(0, needed-consumed), needed*cfg['subsistence_share'])
        effective_shortfall = (max(0, needed-consumed-protected)/needed
                              if needed > eps else 0.0)
        province_cache = provinces.setdefault(pid, {})
        prior = province_cache.get('food_stress', 0.0)
        stress = (cfg['stress_decay']*prior
                  +(1-cfg['stress_decay'])*effective_shortfall)
        province_cache.update(food_stress=stress, last_month=month)
        births = baseline_deaths = hunger_deaths = rounding_adjustment = 0.0
        for household in households:
            group = entities[household['group_id']]['properties']
            count = group['population']
            births += count*cfg['birth_rate']
            baseline_deaths += count*cfg['base_death_rate']
            hunger_deaths += count*stress*cfg['hunger_death_rate']
            group_cache = groups.setdefault(household['group_id'], {})
            raw_change = count*(cfg['birth_rate']-cfg['base_death_rate']
                                -stress*cfg['hunger_death_rate'])
            change = raw_change+group_cache.get('rounding_carry', 0.0)
            delta = math.floor(change) if change >= 0 else math.ceil(change)
            group_cache['rounding_carry'] = change-delta
            group['population'] = max(0, count+delta)
            rounding_adjustment += delta-raw_change
        rows.append(dict(province_id=pid, population_before=population,
            population_after=sum(population_hook(a) for a in households),
            births=births, deaths=baseline_deaths+hunger_deaths,
            baseline_deaths=baseline_deaths, hunger_deaths=hunger_deaths,
            food_stress=stress, market_food_shortfall=market_shortfall,
            protected_by_subsistence=protected,
            rounding_adjustment=rounding_adjustment, migration_in=0,
            migration_out=0))
    return rows


class MigrationHooks(object):
    """Живой доступ ядра миграции к движку.

    Ядро считает только числа; RNG (``shuffled``), маршрут (``path``),
    переводы и склад остаются здесь: ``move_share`` — прежний
    ``_move_household_share`` целиком, включая ``transfer`` и ``add_stock``.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    def population(self, province_id):
        return self.engine._province_population(province_id)

    def location_tile(self, province_id):
        return self.engine.location(province_id)['tile_id']

    def owner(self, province_id):
        return self.engine.owners[province_id]

    def distance(self, source_tile, destination_tile):
        return self.engine.path(source_tile, destination_tile)[0]

    def shuffled(self, candidates):
        # RNG месяца: прежний shuffled по ключу id провинции.
        return self.engine.shuffled(candidates, key=lambda row: row[1])

    def households(self, province_id):
        return [a for a in self.engine.accounts.values()
                if a['kind'] == 'household' and a['province_id'] == province_id]

    def split_people(self, people, households):
        return self.engine._split_people(people, households)

    def move_share(self, account_id, destination_id, count):
        return self.engine._move_household_share(
            self.engine.accounts[account_id], destination_id, count)


def migration_rate_python(stress, threshold, maximum, pressure, eps):
    push = max(0, stress-threshold)
    if push <= eps:
        return None
    return min(maximum, pressure*push/(1-threshold))


def migration_score_python(advantage, distance, eps):
    if advantage > eps and distance is not None:
        return advantage/(1+distance)
    return None


def migration_people_python(requested, population, maximum_rate, incoming):
    return min(requested, max(0, math.floor(population*maximum_rate)-incoming))


def _run_migration(rows, hooks, cfg, eps, source_rate, score_candidate, limit_people):
    """Применить каждый переезд до чтения населения для следующего."""
    incoming = {}
    moved_rows = []
    sources = sorted(rows.values(), key=lambda row: (-row['food_stress'],
                                                     row['province_id']))
    for source_row in sources:
        source_id = source_row['province_id']
        if incoming.get(source_id, 0):
            continue
        rate = source_rate(source_row['food_stress'], cfg['stress_threshold'],
                           cfg['max_monthly_rate'], cfg['pressure_rate'], eps)
        if rate is None:
            continue
        requested = math.floor(hooks.population(source_id)*rate)
        if requested <= 0:
            continue
        source_tile = hooks.location_tile(source_id)
        candidates = []
        for destination_id, destination_row in rows.items():
            if destination_id == source_id or hooks.owner(destination_id) != hooks.owner(source_id):
                continue
            advantage = (source_row['food_stress']-destination_row['food_stress']
                         -cfg['minimum_advantage'])
            distance = hooks.distance(source_tile, hooks.location_tile(destination_id))
            score = score_candidate(advantage, distance, eps)
            if score is not None:
                candidates.append((score, destination_id, distance, destination_row))
        if not candidates:
            continue
        _, destination_id, distance, destination_row = max(
            hooks.shuffled(candidates), key=lambda row: row[0])
        people = limit_people(requested, hooks.population(destination_id),
                              cfg['destination_max_rate'], incoming.get(destination_id, 0))
        if people <= 0:
            continue
        households = hooks.households(source_id)
        moved_gold, moved_stock = 0.0, {}
        for account_id, count in hooks.split_people(people, households).items():
            moved = hooks.move_share(account_id, destination_id, count)
            moved_gold += moved['gold']
            for good, quantity in moved['stock'].items():
                moved_stock[good] = moved_stock.get(good, 0.0)+quantity
        source_row['migration_out'] += people
        destination_row['migration_in'] += people
        incoming[destination_id] = incoming.get(destination_id, 0)+people
        moved_rows.append(dict(source_province=source_id,
            destination_province=destination_id, state_id=hooks.owner(source_id),
            people=people, source_stress=source_row['food_stress'],
            destination_stress=destination_row['food_stress'], distance=distance,
            gold=moved_gold, stock=dict(moved_stock), cause='chronic_food_stress'))
    return moved_rows


def run_migration_python(rows, hooks, cfg, eps):
    return _run_migration(rows, hooks, cfg, eps, migration_rate_python,
                          migration_score_python, migration_people_python)


def run_migration(rows, hooks, cfg, eps):
    return _run_migration(rows, hooks, cfg, eps, migration_rate,
                          migration_score, migration_people)


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
    province_population = _kernel.province_population
    split_people = _kernel.split_people
    run_demography = _kernel.run_demography
    NATIVE_KERNEL = True
else:
    run_validate = run_validate_python
    province_population = province_population_python
    split_people = split_people_python
    run_demography = run_demography_python

migration_rate = getattr(_kernel, 'migration_rate', migration_rate_python)
migration_score = getattr(_kernel, 'migration_score', migration_score_python)
migration_people = getattr(_kernel, 'migration_people', migration_people_python)
