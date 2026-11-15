# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Зарплатный срез публичных строек: числовой цикл бригад и их выплат.

Ядро считает только числа: готовность стройки, число работников, wage bill и
надбавку за жильё. Всё, что пишет словари движка (склад, ``report``, поля
проекта, завершение стройки) и всё, что двигает деньги, вызывается как
Python-хуки границы ``wage_boundary``. Второго cash ledger нет: выплата идёт
прежним ``Economy.distribute``, а он — прежним ``transfer``/``cash_store``.

Арифметика — ``object``, не ``double``. Значения здесь бывают и ``int``, и
``float``: ``max_workers``/``work_required`` приходят из контента, ``labor`` и
``free_cash`` — из расчётов месяца. Приведение к ``double`` сменило бы тип и
последний разряд, а от них зависят суммы выплат и траектория мира.

Порядок операций сохранён буквально:

  * готовность считается до чтения ``location`` — прежние два ``all()`` с тем
    же short-circuit: сначала материалы, затем оборудование;
  * ``workers = min(max_workers, work_required-work_done, labor[key],
    free_cash/wage)`` — аргументы вычисляются слева направо, и при равных
    значениях ``min`` возвращает первый, вместе с его типом;
  * ``wages`` читается как ``workers*wage*wage_multiplier``: оба множителя
    берутся из ``cfg`` в том же месте, что и раньше;
  * short-circuit ``workers > eps`` и ``lodging > eps`` решает ядро, а
    граница лишь выполняет уже разрешённый side effect;
  * порядок обхода — счета, отсортированные по ``id``, и их
    ``public_projects`` в порядке вставки; ``labor[key]`` перечитывается на
    каждой стройке, потому что предыдущая уменьшает остаток труда.

RNG ядро не трогает: перемешивания в этом участке нет, порядок задаёт Python.
"""


cdef inline bint _ready(object project, object eps):
    """Готовность стройки: прежние два ``all()`` с тем же short-circuit."""
    cdef object good, quantity
    for good, quantity in project['material_bill'].items():
        if not project['materials'].get(good, 0)+eps >= quantity:
            return False
    for good, quantity in project['equipment'].items():
        if not project['equipment_held'].get(good, 0)+eps >= quantity:
            return False
    return True


def run_advance(object engine, object accounts, object labor, object expedited,
                object hooks, object eps):
    """Обойти счета и их стройки, выплатив зарплату и жильё через хуки.

    ``accounts`` уже отсортирован Python-стороной; ``labor`` — живой
    ``defaultdict(float)`` месяца, который уменьшают хвосты прошлых строек;
    ``expedited`` — снимок уже забронированных ускоренных бригад.
    """
    cdef object account, project, mode, workers, wages, lodging
    cdef object key, location, ready, advanced
    cdef Py_ssize_t index, inner, count, total
    cdef list rows = accounts
    cdef list projects

    total = len(rows)
    for index in range(total):
        account = rows[index]
        hooks.begin(account)
        projects = account.get('public_projects', [])
        count = len(projects)
        for inner in range(count):
            project = projects[inner]
            ready = _ready(project, eps)
            location = project['location']
            key = (location['state_id'], location['province_id'])
            # Ноль объявлен float, как в прежнем коде: при неготовой стройке
            # отчёт обязан получить именно ``0.0``, а не ``0``.
            workers = 0.0
            advanced = False
            if ready:
                mode = engine.cfg['construction_modes'][project['mode']]
                if project['mode'] == 'accelerated':
                    workers = expedited.get(project['id'], 0)
                else:
                    # Порядок аргументов min сохраняется: он же порядок
                    # вычислений и правило выбора при равных значениях.
                    workers = min(project['max_workers'],
                        project['work_required']-project['work_done'],
                        labor[key], hooks.free_cash(account['id'])/engine.cfg['wage'])
                advanced = workers > eps
                if advanced:
                    # Два независимых short-circuit: сначала зарплата, затем
                    # жильё. Нулевая надбавка не создаёт второго перевода.
                    wages = workers*engine.cfg['wage']*mode['wage_multiplier']
                    hooks.pay_wages(account['id'], project, wages)
                    lodging = workers*mode['lodging_allowance']
                    if lodging > eps:
                        hooks.pay_lodging(account['id'], project, lodging)
            # Хвост стройки — Python: склад, износ, отчёт и завершение.
            hooks.tail(account, project, ready, workers, advanced)
        hooks.end(account)
    return None
