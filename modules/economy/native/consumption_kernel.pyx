# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Consumption kernel: числовой цикл потребления домохозяйств из V3.step.

Перенесён ровно один участок — цикл, который идёт в ``Economy.step`` сразу
после ``consume_owner_purchases``: обход домохозяйств, обход их ``needs`` и
расчёт ``consumed = min(needed, free_stock)``. Списание со склада, запись
``consumed``/``deficit`` и строка ``report['consumed']`` остаются
Python-хуками границы и выполняются ровно в прежнем порядке.

Ядро сознательно работает с ``object``-значениями, а не с ``double``.
``needed`` берётся из need-словаря, а остаток — из ``free_stock``: оба бывают
и int, и float. Встроенный ``min`` при равенстве возвращает первый аргумент,
поэтому тип результата зависит от входа, и он виден дальше — в ``deficit``
(``needed-consumed``) и в накоплении ``report['consumed']``. Арифметика над
``object`` в Cython порождает те же PyNumber_*, что и интерпретатор, поэтому
значения и типы совпадают побитово, включая int + int -> int.

Порядок обхода сохранён буквально:
  * домохозяйства идут в переданном порядке (список готовит Python:
    ``native_households`` в cython-режиме либо ``engine.shuffled``);
  * внутри — ``a.get('needs', {}).items()``, то есть порядок вставки товаров;
  * ``needed`` читается до вызова ``free_stock``, как в прежнем коде;
  * ``consumed = min(needed, free_stock)`` — встроенный ``min``: Cython
    встраивает его в одно сравнение (``free_stock < needed``), что даёт ту же
    семантику и тот же объект-результат, включая тип при равенстве.

RNG ядро не трогает: перемешивание и расход RNG остаются за границей.
Ни склада, ни денег, ни ledger ядро не копирует: единственная мутация склада
делается хуком через существующую stock boundary движка.
"""


def run_households(object households, object engine, object hooks):
    """Пройти домохозяйства и их нужды, отдав каждую норму потребления хуку.

    Ядро считает только числовое ``consumed``; порядок вызовов совпадает с
    прежним циклом V3.step.
    """
    cdef object rows = households
    cdef object a, needs, good, need, needed, consumed

    for a in rows:
        needs = a.get('needs', {})
        # dict сохраняет порядок вставки: он же задаёт порядок списаний.
        for good, need in needs.items():
            needed = need['needed']
            consumed = min(needed, engine.free_stock(a['id'], good))
            hooks.consume(a, good, need, consumed)
    return None
