# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Отмены проекта: простой стройки, маржа и устойчивое отсутствие спроса.

Ядро — вертикальные срезы ``project_material_requests``. Первый срез — простой:
проверка продвижения стройки, порог ``project_stall_months``, определение
причины отмены и подготовка результата отмены. Второй срез — маржа/спрос:
порог маржи, порог отсутствия спроса, условие отмены и её причина. Всё, что
вокруг, остаётся у Python:

  * проект выбирает ``v3.project_material_requests``: сюда приходит уже
    выбранный проект и его progress-state, порядок проектов задаёт
    перемешанный список инвесторов;
  * снятие проекта со счёта (``del a['project']``), восстановление
    ``protected`` и запись ``report['investment_decisions']`` — хук
    ``apply_cancellation``: тела остаются у движка, ядро задаёт только момент
    вызова и аргументы;
  * ``cancel_order``, освобождение sell-заявок, ``mark_market_quotes_dirty``,
    dirty goods, отчёт, события и очередь в этих срезах не вызываются вовсе:
    прежние пути их не трогали, и ядро не добавляет новых side effects;
  * цена, себестоимость и рыночный сигнал остаются за движком: сюда приходят
    уже посчитанные ``margin`` и флаги наблюдений, поэтому срез маржи/спроса
    не читает рынок повторно и не заводит второго состояния;
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

Срез маржи/спроса повторяет прежний участок буквально, в том числе порядок:

  * ``margin`` считается всегда, ещё до проверки ``due``:
    ``(net_price-unit_cost)-labor*wage*overhead`` — левое вычитание, затем
    произведение слева направо, как в прежнем теле;
  * рыночный сигнал запрашивается тоже всегда, до проверки ``due``;
  * ``absent`` вычисляется коротким замыканием в прежнем порядке: история
    рынка, возраст проекта, ``volume``, ``unmet`` и только затем
    ``reachable_demand``;
  * условие отмены — ``due and (margin < -eps or absent)``: маржа проверяется
    первой, поэтому при одновременном срабатывании приоритет остаётся за ней;
  * причина у обоих условий одна — прежний литерал ``MARGIN_DEMAND_REASON``.

Цена, себестоимость, рыночный сигнал и ``reachable_demand`` остаются методами
движка: ядро зовёт их хуками в прежнем порядке, поэтому кэши движка
заполняются ровно там же, а второй project state не заводится.
"""

import builtins

# Встроенный sum: float-слагаемые CPython 3.12 складывает компенсированно,
# поэтому прогресс обязан считаться той же функцией, что и прежде.
_PY_SUM = builtins.sum

# Причина stall-отмены: тот же текст, что и в прежнем теле v3.
STALL_REASON = 'Стройка не продвигается'

# Причина отмены по марже/спросу: прежний литерал v3. У обоих условий
# (отрицательная маржа и устойчивое отсутствие спроса) причина одна.
MARGIN_DEMAND_REASON = 'Отрицательная маржа или устойчивое отсутствие спроса'

# Порог наблюдаемости отсутствия спроса: столько месяцев истории рынка и
# столько месяцев возраста проекта требует прежнее условие.
ABSENT_MIN_HISTORY = 3
ABSENT_MIN_AGE = 3


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


def check_margin_demand(object owner, object account, object project, object sells,
                        object due, object eps, object net_price, object unit_cost,
                        object market_signal, object reachable_demand):
    """Пороги маржи и спроса, условие отмены и её результат.

    Возвращает готовый результат отмены или ``None``, если проект остаётся.
    Порядок прежний и обязательный: сначала ``e`` и ``margin`` (считается
    всегда, до проверки ``due``), затем рыночный сигнал, затем ``absent``
    коротким замыканием, и только потом условие ``due and (margin < -eps or
    absent)``. Маржа в этом условии стоит первой, поэтому её приоритет
    сохранён.

    Состояние проекта здесь не меняется: ``project`` только читается, а
    ``del account['project']``, ``protected`` и отчёт остаются Python-хуку.
    ``e`` — прежний локальный словарь запроса цены: он не попадает ни в
    проект, ни в отчёт, поэтому второй project state не заводится.
    """
    cdef object good = project['good']
    cdef object loc = project['location']
    cdef object e = dict(id=account['id'], good=good,
                         price=project['expected_price'], **loc)
    # Прежний порядок арифметики: сначала разность, затем произведение
    # labor*wage*overhead. Оба операнда — object, поэтому int/float сохраняются.
    cdef object margin = (net_price(owner, e)-unit_cost(owner, e, sells)
                          -owner.goods[good]['labor']*owner.cfg['wage']
                          *owner.cfg.get('enterprise_overhead_rate', 0))
    cdef object history = owner.meta.get('market_history', [])
    cdef object signal = market_signal(owner, history, account['state_id'],
                                       loc['province_id'], good)
    # Короткое замыкание в прежнем порядке: история, возраст, volume, unmet и
    # только затем reachable_demand — его вызов виден в кэшах движка.
    cdef bint absent = (len(history) >= ABSENT_MIN_HISTORY
                        and owner.meta['month']-project['created'] >= ABSENT_MIN_AGE
                        and signal['volume'] <= eps and signal['unmet'] <= eps
                        and reachable_demand(owner, e, sells) <= eps)
    if due and (margin < -eps or absent):
        return cancellation_result(MARGIN_DEMAND_REASON)
    return None
