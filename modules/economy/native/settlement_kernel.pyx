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
