# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Settlement kernel: read-only цикл валидации счетов и населения.

Перенесён ровно один участок — числовые проверки ``Economy.validate``:

  * численность социальных групп (int и неотрицательная);
  * агрегаты переселения ``sum(migration_out)``/``sum(migration_in)``/``sum(people)``
    и их сверка между собой;
  * построчная сверка изменения численности провинции;
  * допустимость каждой строки переселения;
  * инварианты кассы счёта: ``reserved``/``protected`` конечны и неотрицательны,
    их сумма не выше ``balance``;
  * инварианты резерва товаров: ``stock_reserved`` неотрицателен и вместе с
    ``locked_material`` не выше склада.

Ядро ничего не меняет и ничего не создаёт: это чистый обход тех же объектов
движка. Заявки, склад, деньги, RNG, события и отчёт остаются за Python:

  * отметки ``report['invariants']`` — побочный эффект отчёта, ядро лишь
    вызывает хук в тот момент, когда прежний код их писал;
  * сообщения об ошибках живут в границе: ядро возвращает код причины, а
    ``ValueError`` с прежним текстом поднимает Python. Поэтому тексты и
    момент отказа (short-circuit) не могут разойтись с прежним кодом.

Порядок обхода и порядок проверок сохранены буквально, включая короткое
замыкание: ищется первая же непрошедшая проверка, дальше обход не идёт.
``reserved``/``protected``/``stock`` бывают и int, и float, поэтому арифметика
остаётся ``object``-арифметикой: Cython порождает те же ``PyNumber_*``, что и
интерпретатор, и типы значений не приводятся к ``double``.

Суммы ``sum(...)`` ядро не накапливает вручную: в Python 3.12 встроенный
``sum`` суммирует float с компенсацией погрешности, и простое ``+=`` дало бы
расхождение в последнем знаке. Поэтому агрегат считает тот же встроенный
``sum`` через хук ``total`` — семантика прежнего кода сохраняется побитово.
"""

import math


cdef inline tuple _fold(object total, object comp, object value):
    """Шаг суммирования ровно как встроенный ``sum`` в CPython 3.12.

    ``sum`` складывает float компенсированно (Neumaier); наивное ``+=`` дало бы
    другой последний бит, а он меняет численность и траекторию. Целые
    складываются обычным ``+``, чтобы тип остался ``int``.
    """
    cdef object new_total
    if type(total) is float and type(value) is float:
        new_total = total+value
        if abs(total) >= abs(value):
            comp = comp+((total-new_total)+value)
        else:
            comp = comp+((value-new_total)+total)
        return new_total, comp
    return total+value, comp


cdef inline object _finish(object total, object comp):
    """Итог компенсированной суммы: ``total+comp`` только при ненулевой поправке."""
    if comp != 0.0:
        return total+comp
    return total


cdef inline object _max0(object value):
    """``max(0, value)``: при равенстве и для отрицательных побеждает ``0``."""
    if value > 0:
        return value
    return 0


cdef inline object _min2(object first, object second):
    """``min(first, second)``: второй побеждает только при строгом «меньше»."""
    if second < first:
        return second
    return first


def province_population(object accounts, object province_id, object population_hook):
    """Прежний ``V3._province_population``: компенсированная сумма населения.

    Обход идёт в порядке ``accounts.values()`` и отбирает домохозяйства той же
    провинции — ровно как прежний генератор внутри ``sum``.
    """
    cdef object total = 0
    cdef object comp = 0.0
    cdef object account
    for account in accounts:
        if account['kind'] == 'household' and account['province_id'] == province_id:
            total, comp = _fold(total, comp, population_hook(account))
    return _finish(total, comp)


def split_people(object households, object total, object population_hook):
    """Прежний ``V3._split_people``: точное целочисленное деление переселенцев.

    Порядок тот же: сначала эталонный ``dict`` из ``floor``, затем остаток
    раздаётся по убыванию дробной части и по ``id`` при равенстве. Сумма
    населения считается компенсированно, как прежний встроенный ``sum``.
    """
    cdef object population = 0
    cdef object comp = 0.0
    cdef object account, value, left
    cdef list raw = []
    cdef dict result = {}
    cdef dict ordered = {}
    for account in households:
        population, comp = _fold(population, comp, population_hook(account))
    population = _finish(population, comp)
    if total <= 0 or population <= 0:
        return {}
    for account in households:
        value = total*population_hook(account)/population
        raw.append((account, value))
        result[account['id']] = math.floor(value)
    left = total-sum(result.values())
    for account, value in sorted(raw, key=lambda row: (-(row[1]-math.floor(row[1])),
                                                       row[0]['id']))[:left]:
        result[account['id']] += 1
    for account in households:
        if result[account['id']]:
            ordered[account['id']] = result[account['id']]
    return ordered


def run_demography(object entities, object accounts, object province_ids,
                   object basic_goods, object provinces, object groups,
                   object population_hook, object cfg, object month,
                   object eps):
    """Числовое тело ``V3.update_demography``: стресс, рождения, смерти.

    Порядок прежний: провинции по возрастанию ``id``, домохозяйства в порядке
    ``accounts.values()``, товары в порядке ``basic_needs``. Суммы ``needed``,
    ``consumed`` и населения считаются компенсированно (встроенный ``sum``
    CPython 3.12), а накопления ``births``/``deaths``/``rounding`` остаются
    наивными: прежний код складывал их через ``+=`` от ``0.0``.

    Ядро пишет только те же поля, что и прежний код: численность группы,
    ``rounding_carry`` группы и ``food_stress``/``last_month`` провинции.
    Второго ledger'а нет — это живые dict-ы ``meta['demography']`` и
    ``entities``; строки отчёта возвращаются списком и добавляет их Python.
    """
    cdef list rows = []
    cdef object pid, account, good, household, group, group_cache, province_cache
    cdef object population, needed, consumed, total, comp
    cdef object market_shortfall, protected, effective_shortfall, prior, stress
    cdef object births, baseline_deaths, hunger_deaths, rounding_adjustment
    cdef object count, raw_change, change, delta, row
    cdef list households = []
    for pid in province_ids:
        households = []
        for account in accounts:
            if account['kind'] == 'household' and account['province_id'] == pid:
                households.append(account)
        population = 0
        comp = 0.0
        for account in households:
            population, comp = _fold(population, comp, population_hook(account))
        population = _finish(population, comp)
        needed = 0
        comp = 0.0
        for account in households:
            for good in basic_goods:
                need_row = account.get('needs', {}).get(good)
                if need_row is not None:
                    needed, comp = _fold(needed, comp, need_row.get('needed', 0))
        needed = _finish(needed, comp)
        consumed = 0
        comp = 0.0
        for account in households:
            for good in basic_goods:
                need_row = account.get('needs', {}).get(good)
                if need_row is not None:
                    consumed, comp = _fold(consumed, comp, need_row.get('consumed', 0))
        consumed = _finish(consumed, comp)
        market_shortfall = (max(0, needed-consumed)/needed
                            if needed > eps else 0.0)
        protected = min(max(0, needed-consumed), needed*cfg['subsistence_share'])
        effective_shortfall = (max(0, needed-consumed-protected)/needed
                              if needed > eps else 0.0)
        province_cache = provinces.setdefault(pid, {})
        prior = province_cache.get('food_stress', 0.0)
        stress = cfg['stress_decay']*prior+(1-cfg['stress_decay'])*effective_shortfall
        province_cache.update(food_stress=stress, last_month=month)
        births = baseline_deaths = hunger_deaths = rounding_adjustment = 0.0
        for household in households:
            group = entities[household['group_id']]['properties']
            count = group['population']
            births = births+count*cfg['birth_rate']
            baseline_deaths = baseline_deaths+count*cfg['base_death_rate']
            hunger_deaths = hunger_deaths+count*stress*cfg['hunger_death_rate']
            group_cache = groups.setdefault(household['group_id'], {})
            raw_change = count*(cfg['birth_rate']-cfg['base_death_rate']
                                -stress*cfg['hunger_death_rate'])
            change = raw_change+group_cache.get('rounding_carry', 0.0)
            delta = math.floor(change) if change >= 0 else math.ceil(change)
            group_cache['rounding_carry'] = change-delta
            group['population'] = max(0, count+delta)
            # Это не потеря населения: население целочисленно, а темп рождения
            # и смертности дробный. Поправка нужна инварианту, чтобы отличать
            # округление от необъяснённой мутации. Скобки обязательны: прежний
            # ``+= delta-raw_change`` складывал именно ``delta-raw_change``.
            rounding_adjustment = rounding_adjustment+(delta-raw_change)
        population_after = 0
        comp = 0.0
        for account in households:
            population_after, comp = _fold(population_after, comp,
                                           population_hook(account))
        population_after = _finish(population_after, comp)
        row = dict(province_id=pid, population_before=population,
            population_after=population_after, births=births,
            deaths=baseline_deaths+hunger_deaths, baseline_deaths=baseline_deaths,
            hunger_deaths=hunger_deaths, food_stress=stress,
            market_food_shortfall=market_shortfall,
            protected_by_subsistence=protected,
            rounding_adjustment=rounding_adjustment,
            migration_in=0, migration_out=0)
        rows.append(row)
    return rows


def run_migration(object rows, object hooks, object cfg, object eps):
    """Числовое тело ``V3.apply_migration``: давление, выбор цели, комната.

    Порядок прежний: провинции по убыванию ``food_stress`` и по ``id``, цели —
    в порядке ``rows``. RNG (перемешивание кандидатов), маршруты, переводы и
    склад остаются за хуками границы; ядро считает push, rate, requested,
    advantage, room и people и обновляет ``incoming``/счётчики строк.

    ``move_share`` вызывается внутри цикла там же, где прежний код звал
    ``_move_household_share``: чередование расчёта и мутации сохранено, поэтому
    следующий источник видит уже изменённое население.
    """
    cdef dict incoming = {}
    cdef list sources = []
    cdef list candidates = []
    cdef list moved_rows = []
    cdef object source_row, destination_row, source_id, destination_id, row
    cdef object push, rate, requested, advantage, distance, room, people
    cdef object source_tile, households, moved_gold, moved_stock, good, quantity
    cdef object best, count, account_id
    for row in rows.values():
        sources.append(row)
    sources = sorted(sources, key=lambda row: (-row['food_stress'], row['province_id']))
    for source_row in sources:
        source_id = source_row['province_id']
        # Прибывшие в этом месяце не продолжают путь через полкарты за тот же ход.
        if incoming.get(source_id, 0):
            continue
        push = _max0(source_row['food_stress']-cfg['stress_threshold'])
        if push <= eps:
            continue
        rate = _min2(cfg['max_monthly_rate'],
                     cfg['pressure_rate']*push/(1-cfg['stress_threshold']))
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
            if advantage > eps and distance is not None:
                candidates.append((advantage/(1+distance), destination_id,
                                   distance, destination_row))
        if not candidates:
            continue
        # Порядок перемешивания прежний: ключ сортировки — id провинции.
        candidates = hooks.shuffled(candidates)
        best = candidates[0]
        for row in candidates:
            if row[0] > best[0]:
                best = row
        destination_id = best[1]
        distance = best[2]
        destination_row = best[3]
        room = _max0(math.floor(hooks.population(destination_id)
                                *cfg['destination_max_rate'])
                     -incoming.get(destination_id, 0))
        people = _min2(requested, room)
        if people <= 0:
            continue
        households = hooks.households(source_id)
        moved_gold = 0.0
        moved_stock = {}
        for account_id, count in hooks.split_people(people, households).items():
            moved = hooks.move_share(account_id, destination_id, count)
            moved_gold = moved_gold+moved['gold']
            for good, quantity in moved['stock'].items():
                moved_stock[good] = moved_stock.get(good, 0.0)+quantity
        source_row['migration_out'] = source_row['migration_out']+people
        destination_row['migration_in'] = destination_row['migration_in']+people
        incoming[destination_id] = incoming.get(destination_id, 0)+people
        moved_rows.append(dict(source_province=source_id,
            destination_province=destination_id, state_id=hooks.owner(source_id),
            people=people, source_stress=source_row['food_stress'],
            destination_stress=destination_row['food_stress'], distance=distance,
            gold=moved_gold, stock=dict(moved_stock), cause='chronic_food_stress'))
    return moved_rows


def run_validate(object engine, object eps, object hooks):
    """Пройти проверки validate и вернуть код первой неудачи или None.

    Коды причин: ``population``, ``migration_balance``, ``demography``,
    ``migration``, ``cash_reserve``, ``stock_reserve``.
    """
    cdef object entity, properties, population
    cdef object demography, migration, row, actual, expected
    cdef object a, project, good, quantity, value
    cdef object moved_out, moved_in, moved
    cdef object public_locks

    # 1. Численность социальных групп: только существующие SocialGroup.
    for entity in engine.entities.values():
        properties = entity['properties']
        if entity['kind'] != 'SocialGroup' or 'population' not in properties:
            continue
        population = properties['population']
        # bool — подкласс int, но численностью не является.
        if (isinstance(population, bool) or not isinstance(population, int)
                or population < 0):
            return 'population'

    # 2. Агрегаты переселения. Встроенный sum через хук: компенсация
    # погрешности float обязана остаться прежней.
    demography = engine.report.get('demography', [])
    migration = engine.report.get('migration', [])
    moved_out = hooks.total([row.get('migration_out', 0) for row in demography])
    moved_in = hooks.total([row.get('migration_in', 0) for row in demography])
    moved = hooks.total([row.get('people', 0) for row in migration])
    if abs(moved_out-moved) > eps or abs(moved_in-moved) > eps:
        return 'migration_balance'

    # 3. Изменение численности провинции обязано быть объяснено.
    for row in demography:
        actual = row['population_after']-row['population_before']
        expected = (row['births']-row['deaths']+row.get('rounding_adjustment', 0)
                    +row.get('migration_in', 0)-row.get('migration_out', 0))
        if abs(actual-expected) > eps:
            return 'demography'

    # 4. Строка переселения: положительное число людей и то же государство.
    for row in migration:
        if (row['people'] <= 0
                or engine.owners[row['source_province']] != row['state_id']
                or engine.owners[row['destination_province']] != row['state_id']):
            return 'migration'

    # Прежний код писал отметки инвариантов сразу после этих проверок и до
    # обхода счетов: при отказе по кассе они уже стояли. Хук сохраняет момент.
    hooks.mark_population_migration()

    # 5. Касса счёта и резерв товаров.
    for a in engine.accounts.values():
        # Порядок полей и короткое замыкание — как в прежнем any(...).
        for good in ('reserved', 'protected'):
            value = a.get(good, 0)
            if not math.isfinite(value) or value < -eps:
                return 'cash_reserve'
        if a.get('reserved', 0)+a.get('protected', 0) > a['balance']+eps:
            return 'cash_reserve'
        public_locks = set()
        for project in a.get('public_projects', []):
            public_locks |= (project.get('materials', {}).keys()
                | project.get('equipment_held', {}).keys()
                | project.get('support_stock', {}).keys())
        # Товары проекта, публичных строек и уже зарезервированные: объединение
        # множеств сохраняет прежний состав и прежний порядок обхода.
        for good in (a.get('stock_reserved', {}).keys()
                     | a.get('project', {}).get('materials', {}).keys()
                     | public_locks):
            quantity = a.get('stock_reserved', {}).get(good, 0)
            if (quantity < -eps
                    or quantity+engine.locked_material(a['id'], good)
                    > a['stock'].get(good, 0)+eps):
                return 'stock_reserve'
    return None
