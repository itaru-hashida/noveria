# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Project completion kernel: numeric writers завершения и урезания проектов.

Два среза:

  * ``run_finish`` — числовой цикл ``finish_projects``: проверка сметы и
    кассы, накопление ``asset`` (доставка плюс списания со склада в порядке
    сметы) и запись ``e['capacity']``/``e['asset_value']`` при расширении;
  * ``limit_project_capacity`` — пересчёт числового состояния ещё не начатого
    проекта при урезании мощности.

Границы среза:

  * список счетов готовит ``v3.finish_projects``: порядок обхода и выбор
    проектов ядро не трогает;
  * снятие проекта, защита денег и перевод доставки — хуки: деньги двигает
    прежний ``Economy.transfer`` через ``cash_store``, второго ledger нет;
  * склад уменьшает прежний ``use_stock`` (существующая stock boundary) —
    хук, второй склад не заводится;
  * создание предприятия, квитанции, отчёты, события и lifecycle — хуки:
    это выбор/создание и отчётность, а не numeric-записи;
  * текстовая причина ``capacity_limit`` приходит аргументом из границы.

Арифметика намеренно ``object``: смета, доставка и остатки бывают и int,
и float, и тип результата виден дальше в отчёте. Cython порождает те же
``PyNumber_*``, что и интерпретатор, поэтому значения и типы не приводятся.
Порядок сложений ``asset`` и порядок ключей ``project.update(...)``
сохранены буквально, включая чтение уже записанных полей.
"""


def run_finish(object engine, object accounts, object hooks, object eps):
    """Пройти счета и завершить готовые проекты: числа здесь, эффекты — хуки."""
    cdef object a, p, bill, need_cash, asset, g, q, e
    cdef Py_ssize_t index, count
    cdef list rows = accounts

    count = len(rows)
    for index in range(count):
        a = rows[index]
        p = a.get('project')
        if not p:
            continue
        bill = hooks.construction_bill(p['capacity'])
        need_cash = p['working_cash']+p.get('delivery', 0)
        # Порядок проверок прежний: сначала смета (короткое замыкание any),
        # затем касса. Текст причины не появляется: pending-отчёт — хук.
        if any(p['materials'].get(g, 0)+eps < q for g, q in bill.items()) \
                or hooks.free_cash(a['id'])+a.get('protected', 0)+eps < need_cash:
            hooks.pending(a, p)
            continue
        hooks.detach_project(a)
        hooks.pay_delivery(a, p)
        asset = p.get('delivery', 0)
        # Накопление asset — в порядке сметы; склад списывает хук, поэтому
        # прежний use_stock и его проверки остаются у движка.
        for g, q in bill.items():
            asset += hooks.use_stock(a['id'], g, q)
            hooks.construction_used(g, q)
        if p['expansion']:
            e = engine.enterprises[a['id']]
            # Только эти две записи numeric: порядок и типы прежние.
            e['capacity'] += p['capacity']
            e['asset_value'] += asset
            hooks.expanded(e, p)
        else:
            # Создание предприятия — выбор и создание, остаётся хуком.
            e = hooks.built(a, p, bill, asset)
        hooks.decision(a, p, e)
    return None


def limit_project_capacity(object project, object capacity, object eps,
                           object hooks, object reason):
    """Уменьшить ещё не начатый проект без изменения экономики на единицу."""
    cdef object previous, previous_working, construction, working
    previous = project['capacity']
    if capacity+eps >= previous:
        return False
    previous_working = project['working_cash']
    construction = max(0, project['expected_investment']-previous_working)
    working = 0 if project['expansion'] else max(hooks.startup_cash(),
        capacity*project['expected_unit_cost'])
    # Порядок ключей — прежний: dict сохраняет его при update. Числа считает
    # ядро; chain_requirements — хук движка, причина — аргумент границы.
    project.update(requested_capacity=round(previous, 12), capacity=capacity,
        working_cash=working,
        expected_profit=project['expected_profit']*capacity/previous,
        expected_investment=construction*capacity/previous+working,
        chain_requirements=hooks.recipe_requirements(project['good'], capacity),
        capacity_limit=reason)
    return True
