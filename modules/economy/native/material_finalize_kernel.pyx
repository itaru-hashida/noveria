# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Финализация material request: затронутые товары, ревизии и journal-строки.

Ядро закрывает последний участок ``project_material_requests``. К моменту
вызова смета, доставка, резерв материалов и строительные закупки уже
посчитаны существующими срезами (``delivery_kernel``/``material_kernel``), а
отмены — ``cancel_kernel``. Остаётся финализация:

  * определение товаров, затронутых material request, — в порядке сметы;
  * numeric diagnostics и готовые journal-строки: проект, товар, запрошенное,
    доставленное, зарезервированное и недостающее количество, стоимость,
    фрахт, причина отмены (если она уже рассчитана) и поля прогресса.

Второго material/project state ядро не создаёт: ``account``/``project`` — те
же живые словари движка, а смета и ``project['delivery']`` — уже посчитанные
значения. Функция ничего не мутирует и ничего не пересчитывает: она читает
готовые числа и возвращает новый dict. Delivery, reservation, cancellation и
construction-buy остаются в своих срезах, rollback не добавляется.

Про dirty goods и ревизии. В прежнем Python-пути эти обновления выполнялись не
в теле ``project_material_requests``, а хуками внутри уже нативных срезов:
``release_kernel.settle_release`` помечает котировки грязными на каждое снятие
и на отмену опустевшего оффера, а ``V3.order`` двигает ``_demand_revision`` на
каждую buy-заявку. Оба хука вызываются ровно один раз прежним путём, поэтому
ядро не помечает котировки повторно и не двигает ревизии второй раз: оно
**читает** уже выполненные обновления (``_market_quote_versions`` и
``_demand_revision``) и фиксирует их в результате. Иначе ревизии сдвинулись бы
дважды и траектория разошлась бы с эталоном.

Порядок и арифметика сохранены буквально:

  * порядок проектов задаёт вызывающий цикл по перемешанным инвесторам, ядро
    обходит записи в том же порядке;
  * товары берутся в порядке ``bill`` (порядок ``cfg['construction']``);
  * дедупликации ядро не добавляет: смета — словарь, поэтому каждый товар
    встречается ровно один раз, как и в прежнем обходе;
  * ``missing`` считается встроенным ``max(0, quantity-delivered)``: остатки
    бывают и ``int``, и ``float``, и тип виден дальше в отчёте;
  * прогресс считается встроенным ``sum``: CPython 3.12 складывает
    float-слагаемые компенсированно, и наивное накопление развело бы журнал с
    прежним путём;
  * стоимость материала в этом срезе не считается: прежний путь её не
    вычислял, а цена закупки живёт в котировке строительной закупки. Строка
    журнала честно несёт ноль, а не выдуманную цену.

RNG ядро не затрагивает вовсе.
"""

import builtins

# Встроенный max: ``max(0, x)`` сохраняет тип аргумента, поэтому целый ноль
# остаётся целым, как в прежнем Python-коде.
_PY_MAX = builtins.max
# Встроенный sum: компенсация float-слагаемых 3.12 обязана сохраниться.
_PY_SUM = builtins.sum

# Поле материалов проекта: единственный источник доставленного и
# зарезервированного количества. Второй копии этого словаря нет.
MATERIAL_FIELD = 'materials'


def affected_goods(object bill):
    """Товары, затронутые material request, в порядке сметы.

    Собственной дедупликации нет: ключи ``bill`` уникальны, поэтому прежний
    обход сметы тоже видел каждый товар ровно один раз.
    """
    cdef list goods = []
    cdef object good
    for good in bill:
        goods.append(good)
    return goods


def material_row(object account, object project, object good, object quantity,
                 object delivery, object reason, object revisions):
    """Одна готовая journal-строка по материалу проекта.

    ``quantity`` — из сметы, ``delivered``/``reserved`` — из живого
    ``project['materials']``, ``missing`` — остаток сметы, ``freight`` — уже
    посчитанная доставка. ``revision`` читается из живой ревизии спроса и не
    увеличивается здесь.
    """
    cdef object materials = project.get(MATERIAL_FIELD, {})
    cdef object delivered = materials.get(good, 0)
    cdef object revision = 0
    if revisions is not None:
        revision = revisions.get(good, 0)
    return dict(project_id=_project_id(account, project),
        good=good,
        requested=quantity,
        delivered=delivered,
        reserved=delivered,
        missing=_PY_MAX(0, quantity-delivered),
        cost=0,
        freight=delivery,
        reason=reason,
        revision=revision,
        cancelled=reason is not None,
        progress=_progress(project, materials))


def material_rows(object account, object project, object bill, object delivery,
                  object reason=None, object revisions=None):
    """Готовые journal-строки по смете в прежнем порядке товаров."""
    cdef list rows = []
    cdef object good
    cdef object quantity
    for good, quantity in bill.items():
        rows.append(material_row(account, project, good, quantity, delivery,
                                 reason, revisions))
    return rows


def finalize_material_request(object account, object project, object bill,
                              object delivery, object eps, object reason=None,
                              object revisions=None, object quote_versions=None):
    """Финализация одного проекта: товары, ревизии и journal-строки.

    ``eps`` остаётся в сигнатуре, потому что прежний участок получал его
    аргументом; ядро его не применяет, чтобы не добавить второго сравнения,
    которого не было в Python-пути.
    """
    cdef list rows = material_rows(account, project, bill, delivery, reason, revisions)
    cdef list goods = affected_goods(bill)
    cdef list dirty = _dirty_goods(goods, quote_versions)
    return dict(project_id=_project_id(account, project),
        goods=goods,
        dirty_goods=dirty,
        revisions=_revision_rows(goods, revisions),
        rows=rows,
        delivery=delivery,
        reason=reason,
        requested_total=_total(rows, 'requested'),
        delivered_total=_total(rows, 'delivered'),
        reserved_total=_total(rows, 'reserved'),
        missing_total=_total(rows, 'missing'))


def finalize_material_requests(object records, object eps, object revisions=None,
                               object quote_versions=None):
    """Финализация всех проектов месяца: те же строки в прежнем порядке.

    ``records`` — список кортежей ``(account, project, bill, delivery,
    reason)`` в порядке обхода инвесторов: порядок проектов остаётся за
    вызывающим Python-циклом, ядро его не сортирует и не переупорядочивает.
    Результат — один готовый dict: строки журнала, итоги и запись ревизий.
    """
    cdef list projects = []
    cdef list rows = []
    cdef object record
    cdef object result
    cdef object entry
    cdef Py_ssize_t index, count
    # Записи могут быть и кортежами, и списками: прежний путь их не требовал,
    # поэтому обход идёт по индексу и тип записи не навязывается.
    count = len(records)
    for index in range(count):
        record = <object>records[index]
        result = finalize_material_request(record[0], record[1], record[2],
                                           record[3], eps, record[4],
                                           revisions, quote_versions)
        projects.append(result)
        for entry in result['rows']:
            rows.append(entry)
    return dict(
        projects=projects,
        rows=rows,
        dirty_goods=_merge_goods(projects, 'dirty_goods'),
        goods=_merge_goods(projects, 'goods'),
        requested_total=_total(rows, 'requested'),
        delivered_total=_total(rows, 'delivered'),
        reserved_total=_total(rows, 'reserved'),
        missing_total=_total(rows, 'missing'))


cdef object _project_id(object account, object project):
    """Идентификатор проекта: ``project_id`` проекта, иначе счёт-владелец."""
    cdef object project_id = project.get('project_id')
    if project_id is None:
        return account.get('id')
    return project_id


cdef object _progress(object project, object materials):
    """Поля прогресса стройки — те же значения, что читает stall-срез."""
    return dict(quantity=_PY_SUM(materials.values()),
                last_progress=project.get('last_progress'),
                created=project.get('created'))


cdef list _dirty_goods(object goods, object quote_versions):
    """Товары сметы, у которых уже выросла версия котировок.

    Это запись фактически выполненных пометок: release- и cancel-срезы
    пометили котировки сами. Ядро ничего не помечает повторно, поэтому
    ревизии не сдвигаются второй раз.
    """
    cdef list dirty = []
    cdef object good
    if quote_versions is None:
        return dirty
    for good in goods:
        if quote_versions.get(good, 0) > 0:
            dirty.append(good)
    return dirty


cdef list _revision_rows(object goods, object revisions):
    """Ревизии спроса по товарам сметы в прежнем порядке."""
    cdef list rows = []
    cdef object good
    if revisions is None:
        return rows
    for good in goods:
        rows.append(dict(good=good, revision=revisions.get(good, 0)))
    return rows


cdef list _merge_goods(object projects, object field):
    """Объединить списки товаров проектов без дедупликации, в порядке обхода."""
    cdef list merged = []
    cdef object project
    cdef object good
    for project in projects:
        for good in project[field]:
            merged.append(good)
    return merged


cdef object _total(list rows, object field):
    """Сумма числового поля журнала встроенным ``sum`` в порядке строк."""
    cdef list values = []
    cdef object row
    for row in rows:
        values.append(row[field])
    return _PY_SUM(values)
