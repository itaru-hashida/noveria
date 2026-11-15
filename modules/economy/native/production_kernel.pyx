# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Production kernel: числовой цикл выпуска V3.produce над ``object``-значениями.

Ядро сознательно работает с ``object``-арифметикой, а не с ``double``.
Остатки V3 бывают и int, и float: ``free_stock`` возвращает остаток склада
как есть, а ``q`` получается из ``max(0, constraints[reason])``. Арифметика
над ``object`` в Cython порождает те же PyNumber_*, что и интерпретатор,
поэтому значения и типы совпадают побитово, включая int + int -> int и
int/float -> float.

Порядок операций сохранён буквально:
  * ``reason = min(constraints, key=constraints.get)`` — при равных значениях
    побеждает первый ключ в порядке вставки, как у встроенного min;
  * ``q = max(0, constraints[reason])`` — встроенный max, а не тернарник:
    при равенстве он возвращает ``0``, сохраняя его тип;
  * ``q*ratio`` считается один раз и используется и для ``use_stock``, и для
    строки отчёта, как в прежнем коде;
  * ``cost`` накапливается в том же порядке: зарплата, затем сырьё в порядке
    вставки рецепта, затем износ оборудования.

RNG ядро не трогает: готовый порядок предприятий приходит из Python
(``engine.shuffled``), перемешивание и расход RNG остаются за границей.
Side effects (transfer/wages, report, dirty-поля, обновление записи фирмы)
вызываются как Python-хуки в том же порядке, что и раньше.
"""


cdef inline object _free_stock(object engine, object e, object good):
    return engine.free_stock(e['id'], good)


def run_enterprises(object order, object engine, object labor, object resources,
                    bint observe, object hooks, object eps):
    """Пройти перемешанный список предприятий, не меняя порядок и RNG."""
    cdef object month = engine.meta['month']
    cdef object e, plan, recipe, account, project
    cdef object planned, project_hold, constraints, reason, q, workers
    cdef object resource, wages, cost, ratio, quantity, worn, good
    cdef Py_ssize_t index, count
    cdef list rows = order

    count = len(rows)
    for index in range(count):
        e = rows[index]
        if e['status'] == 'closed' or e.get('suspended') or e['available_from'] > month:
            continue
        plan = e.get('production_plan', {})
        # plan['output'] берётся без get: месяц плана обязан иметь output.
        planned = plan['output'] if plan.get('month') == month else 0.0
        e['planned_output'] = planned
        recipe = engine.goods[e['good']]
        account = engine.accounts[e['id']]
        # Закупочные резервы уже сняты, проектный капитал остаётся защищён.
        project = account.get('project', {})
        project_hold = project.get('working_cash', 0)+project.get('delivery', 0)
        account['protected'] = project_hold
        # dict сохраняет порядок вставки: он задаёт как порядок вызовов
        # free_cash/free_stock, так и порядок ключей в report-наблюдении.
        constraints = {'План': planned, 'Мощность': e['capacity'],
            'Труд': labor[e['state_id'], e['province_id']]/recipe['labor'],
            'Деньги на зарплату': engine.free_cash(e['id'])/(recipe['labor']*engine.cfg['wage'])}
        resource = recipe['resource']
        if resource:
            constraints['Природный ресурс'] = resources[e['province_id'], resource]
        inputs = recipe['inputs']
        for good, ratio in inputs.items():
            constraints['Сырьё: '+good] = _free_stock(engine, e, good)/ratio
        equipment = recipe.get('equipment', {})
        for good, ratio in equipment.items():
            constraints['Оборудование: '+good] = _free_stock(engine, e, good)/ratio
        if observe:
            hooks.observe(e, account, constraints, plan, month)
        reason = min(constraints, key=constraints.get)
        q = max(0, constraints[reason])
        e['input_limited'] = any(key.startswith('Сырьё') and value+eps < planned
                                 for key, value in constraints.items())
        if planned > eps:
            e['production_limit'] = reason
        e['reason'] = e['production_limit']
        if q <= eps:
            e['status'] = 'idle'
            continue
        workers = q*recipe['labor']
        labor[e['state_id'], e['province_id']] -= workers
        if resource:
            resources[e['province_id'], resource] -= q
        wages = workers*engine.cfg['wage']
        engine.distribute(e['id'], engine.workforce(e), wages, 'Заработная плата')
        cost = wages
        for good, ratio in inputs.items():
            quantity = q*ratio
            cost += engine.use_stock(e['id'], good, quantity)
            hooks.input_used(good, quantity)
        worn = engine.equipment_wear(recipe, q)
        for good, quantity in worn.items():
            cost += engine.use_stock(e['id'], good, quantity)
            hooks.equipment_worn(e, good, quantity)
        # Цена нового выпуска заменяет цену распродажи только у пустого склада.
        if account['stock'].get(e['good'], 0) <= eps:
            e['price'] = plan.get('sale_price', e['price'])
        engine.add_stock(e['id'], e['good'], q, cost)
        e.update(status='active', workers=workers, output=q, production_cost=cost,
                 unit_cost=cost/q, wages=wages)
        hooks.production(e['good'], q)
    return None
