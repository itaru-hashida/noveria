# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Stall-отмена проекта: progress, порог простоя и причина отмены.

Ядро — только вертикальный срез ``project_material_requests``: проверка
продвижения стройки, порог ``project_stall_months``, определение причины
отмены и подготовка результата отмены. Всё, что вокруг, остаётся у Python:

  * проект выбирает ``v3.project_material_requests``: сюда приходит уже
    выбранный проект и его progress-state, порядок проектов задаёт
    перемешанный список инвесторов;
  * снятие проекта со счёта (``del a['project']``), восстановление
    ``protected`` и запись ``report['investment_decisions']`` — хук
    ``apply_cancellation``: тела остаются у движка, ядро задаёт только момент
    вызова и аргументы;
  * ``cancel_order``, освобождение sell-заявок, ``mark_market_quotes_dirty``,
    dirty goods, отчёт, события и очередь в этом срезе не вызываются вовсе:
    прежний stall-путь их не трогал, и ядро не добавляет новых side effects;
  * отмена по марже/спросу живёт целиком в v3 после этого вызова и не
    затрагивается;
  * доставка, смета, резерв материалов и строительные закупки остаются за
    ``delivery_boundary``/``material_boundary``;
  * второго project/material state нет: ``project`` — живой словарь движка,
    ядро пишет в него ``last_progress``/``progress_quantity`` теми же
    значениями, что и прежнее тело;
  * rollback не добавляется.

Арифметика намеренно ``object``, а не ``double``. Прогресс — ``sum`` по
значениям ``project['materials']``: материалы бывают и ``int``, и ``float``, а
встроенный ``sum`` в CPython 3.12 складывает float-слагаемые компенсированно.
Поэтому используется тот же встроенный ``sum``, что и прежде, а не наивное
накопление.

Порядок операций сохранён буквально, в том числе:

  * ``progress > project.get('progress_quantity', 0)+EPS`` — сначала сравнение,
    и только потом запись ``progress_quantity``;
  * ``project['last_progress']`` пишется только при реальном продвижении, а
    ``setdefault`` берёт ``project['created']`` и потому не перезаписывает
    ранее выставленный месяц;
  * порог проверяется как ``month-last_progress >= project_stall_months``, то
    есть ровно в месяц достижения порога, а не раньше;
  * причина отмены — прежний литерал ``Стройка не продвигается``.
"""

import builtins

# Встроенный sum: float-слагаемые CPython 3.12 складывает компенсированно,
# поэтому прогресс обязан считаться той же функцией, что и прежде.
_PY_SUM = builtins.sum

# Причина stall-отмены: тот же текст, что и в прежнем теле v3.
STALL_REASON = 'Стройка не продвигается'


def project_progress(object project):
    """Прогресс стройки: ``sum`` по значениям материалов проекта."""
    return _PY_SUM(project['materials'].values())


def check_stall(object project, object month, object stall_months, object eps):
    """Обновить progress-state проекта и решить, пора ли отменять по простою.

    Возвращает признак отмены: ``True`` — порог простоя достигнут. Состояние
    проекта пишется ровно там же, где писало прежнее тело: ``progress_quantity``
    — всегда, ``last_progress`` — только при продвижении и только через
    ``setdefault``, поэтому ранее выставленный месяц не перезаписывается.
    """
    cdef object progress = _PY_SUM(project['materials'].values())
    if progress > project.get('progress_quantity', 0)+eps:
        project['last_progress'] = month
    project['progress_quantity'] = progress
    project.setdefault('last_progress', project['created'])
    return month-project['last_progress'] >= stall_months


def cancellation_result(object reason):
    """Подготовить результат отмены: поля и их порядок — как в прежнем отчёте.

    Второй записи состояния здесь нет: ``project`` в результат не попадает,
    ``reason`` приходит уже определённым. Python-хук добавляет к результату
    ``kind``/``investor`` и вставляет его в ``report['investment_decisions']``,
    сохраняя прежний порядок ключей.
    """
    return dict(outcome='cancelled', reason=reason)
